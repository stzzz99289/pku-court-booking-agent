from __future__ import annotations

import asyncio
import logging
import re

from playwright.async_api import Page

from .config import AppConfig
from .result import BookingResult

log = logging.getLogger(__name__)


def _sel(cfg: AppConfig, name: str) -> str:
    # Return a stripped selector string from config, or "" if unset.
    s = getattr(cfg.selectors, name, "") or ""
    return s.strip()


async def select_booking_date(page: Page, cfg: AppConfig) -> BookingResult | None:
    """Click the matching date button and wait for the schedule API to respond.

    Converts cfg.date (YYYYMMDD) → "MM月DD日" and matches against .date_box buttons.
    Returns None on success, BookingResult error otherwise.
    """
    # Parse YYYYMMDD → "MM月DD日" for matching against button text.
    date_str = cfg.date.strip()
    try:
        target = f"{date_str[4:6]}月{date_str[6:8]}日"
    except IndexError:
        return BookingResult(False, f"Invalid date format {cfg.date!r}. Expected YYYYMMDD.", {})

    # Find the date buttons and click the matching one.
    buttons = page.locator(".date_box > div")
    count = await buttons.count()
    if count == 0:
        return BookingResult(False, "No date buttons found on reservation page (.date_box > div).", {})

    available: list[str] = []
    for i in range(count):
        btn = buttons.nth(i)
        text = (await btn.inner_text()).strip()
        available.append(text)
        if target in text:
            log.info("Selecting date: %s", target)
            # Wait for the schedule API response before returning so the table is fresh.
            async with page.expect_response(
                lambda r: "reservation/day/info" in r.url, timeout=10_000
            ) as resp_info:
                await btn.click()
            try:
                await resp_info.value
            except Exception:
                log.warning("Did not capture reservation/day/info response; proceeding anyway.")
            return None

    return BookingResult(
        False,
        f"Date {target} (from config date={cfg.date!r}) is not available on this page. "
        f"Available: {', '.join(available)}",
        {},
    )


async def select_court_time(page: Page, cfg: AppConfig) -> BookingResult | None:
    """Find the target time column in the schedule table and click the first available court.

    Scrolls the table right as needed (up to 20 times). Returns None on success.
    """
    # Validate that start/end times are integers in [6, 22] differing by exactly 1.
    try:
        start = int(cfg.start_time)
        end = int(cfg.end_time)
    except ValueError:
        return BookingResult(
            False,
            f"start_time and end_time must be integers, got {cfg.start_time!r}/{cfg.end_time!r}.",
            {},
        )
    if not (6 <= start <= 22 and 6 <= end <= 22):
        return BookingResult(
            False,
            f"start_time ({start}) and end_time ({end}) must each be between 6 and 22.",
            {},
        )
    if end - start != 1:
        return BookingResult(
            False,
            f"end_time - start_time must be exactly 1 for a one-hour booking, got {end - start}.",
            {},
        )

    target_time = f"{start:02d}:00-{end:02d}:00"
    log.info("Looking for time slot: %s", target_time)

    # Wait for the schedule table (inside .spaceTable) to populate after date selection.
    sched_table = page.locator(".spaceTable table")
    try:
        await sched_table.locator("thead td").nth(1).wait_for(state="visible", timeout=10_000)
    except Exception:
        log.warning("Schedule table header cells did not appear within 10 s.")

    # Scan visible columns, scrolling right until the target time slot is found.
    header_cells = sched_table.locator("thead td")
    col_idx: int | None = None
    for scroll_attempt in range(20):  # 16 one-hour slots between 06:00-22:00
        # Batch-fetch all header cell texts in one round trip (vs. one per cell).
        texts = [t.strip() for t in await header_cells.all_inner_texts()]
        log.debug("Scroll attempt %d: header cells: %s", scroll_attempt, texts)
        if target_time in texts:
            col_idx = texts.index(target_time)
            break
        arrow = page.locator("div.arrowWrap i.ivu-icon-ios-arrow-forward").first
        if not await arrow.count():
            break
        await arrow.click()
        await asyncio.sleep(0.4)

    if col_idx is None:
        return BookingResult(
            False,
            f"Time slot {target_time} not found in the schedule table (checked all visible columns after scrolling).",
            {},
        )

    # Click the first court row (匹配 \d+号) whose target cell is available. The
    # cell's inner `.reserveBlock` div carries a state class:
    #   free        → available (click)
    #   reserved    → 已售 (sold)
    #   reservation → someone else is mid-booking (orange, empty text — skip)
    court_rows = sched_table.locator("tbody tr")
    for row_i in range(await court_rows.count()):
        row = court_rows.nth(row_i)
        cells = row.locator("td")
        first_cell = (await cells.nth(0).inner_text()).strip()
        if not re.match(r"\d+号", first_cell):
            continue
        target_cell = cells.nth(col_idx)
        block = target_cell.locator(".reserveBlock").first
        if not await block.count():
            continue  # Unexpected cell layout — skip defensively.
        block_classes = ((await block.get_attribute("class")) or "").split()
        if "free" not in block_classes:
            continue  # Sold (reserved) or mid-booking (reservation).
        cell_text = (await target_cell.inner_text()).strip()
        log.info("Clicking court %s at %s (status: %s)", first_cell, target_time, cell_text)
        await target_cell.click()
        return None

    return BookingResult(
        False,
        f"No available courts at {target_time} (all sold or mid-reservation by other users).",
        {},
    )


async def agree_and_submit_booking(page: Page, cfg: AppConfig) -> None:
    """Check the agreement checkbox (if unchecked) and click the submit button."""
    agree = _sel(cfg, "agreement_checkbox")
    if agree:
        loc = page.locator(agree).first
        if not await loc.is_checked():
            await loc.click()

    submit = _sel(cfg, "booking_submit")
    if submit:
        await page.locator(submit).first.click()
        # Give the SPA time to render the click-captcha dialog.
        await asyncio.sleep(1.5)


_BOOKING_CAPTCHA_MAX_RETRIES = 3

# JS helper: walks every .ivu-modal-wrap, skips hidden ones, and returns the
# rejection reason from the first visible '系统提示' modal. On this site the
# rejection modal has no .ivu-modal-header — the body text itself looks like:
#     "系统提示\n预约失败，您存在未支付的订单！\n确定"
# so we match on the body text and strip the header/button lines.
_SYSTEM_ERROR_MODAL_JS = """
() => {
    const wraps = Array.from(document.querySelectorAll('.ivu-modal-wrap'));
    for (const w of wraps) {
        if (w.classList.contains('ivu-modal-hidden')) continue;
        const body = w.querySelector('.ivu-modal-body');
        if (!body) continue;
        const raw = body.innerText.trim();
        if (!raw || !raw.includes('系统提示')) continue;
        // Split into non-empty lines; drop the leading '系统提示' header and the
        // trailing '确定' button so only the actual reason remains.
        const lines = raw.split('\\n').map(s => s.trim()).filter(Boolean);
        const start = lines[0] === '系统提示' ? 1 : 0;
        const end = lines[lines.length - 1] === '确定' ? lines.length - 1 : lines.length;
        const reason = lines.slice(start, end).join(' ').trim();
        return reason || raw;
    }
    return '';
}
"""


async def _read_system_error_modal(page: Page) -> str:
    """Return the body text of a visible '系统提示' rejection modal, or '' if none."""
    return await page.evaluate(_SYSTEM_ERROR_MODAL_JS)


async def solve_booking_captcha(page: Page, click_solver, cfg: AppConfig) -> BookingResult | None:
    """Solve the click-captcha that appears after submitting a booking.

    Screenshots the captcha image, sends it to click_solver.solve_click() to get
    coordinates, and clicks each point on the image. Retries if the captcha refreshes.
    Returns None on success (captcha dialog disappears), or a BookingResult error.

    Also detects the '系统提示' rejection modal (e.g. '预约失败，您存在未支付的订单！')
    which the site can pop up even after a correct captcha answer — in that case
    we short-circuit with a rejection BookingResult instead of looping.
    """
    for attempt in range(_BOOKING_CAPTCHA_MAX_RETRIES):
        # A rejection modal may have already appeared before we get a chance to click.
        error_text = await _read_system_error_modal(page)
        if error_text:
            return BookingResult(False, f"Booking rejected by site: {error_text}", {"url": page.url})

        verifybox = page.locator(".verifybox").first
        if not await verifybox.is_visible():
            return None  # Captcha already gone — success.

        # Screenshot the captcha image.
        img_loc = page.locator(".verify-img-panel img").first
        if not await img_loc.count():
            return BookingResult(False, "Booking captcha dialog visible but image element not found.", {})
        png = await img_loc.screenshot()

        # Extract instruction chars from "请依次点击【转,线,导】" (or 【转线导】).
        label = ""
        msg_loc = page.locator(".verify-msg").first
        if await msg_loc.count():
            msg = (await msg_loc.inner_text()).strip()
            m = re.search(r"【(.+?)】", msg)
            if m:
                label = m.group(1).replace(",", "")
        if not label:
            # The captcha may be mid-dismiss (verifybox visible but .verify-msg empty)
            # because a rejection modal is opening on top — give it a moment and re-check.
            await asyncio.sleep(0.8)
            error_text = await _read_system_error_modal(page)
            if error_text:
                return BookingResult(False, f"Booking rejected by site: {error_text}", {"url": page.url})
            return BookingResult(False, "Booking captcha instruction text not found (.verify-msg).", {})
        if cfg.save_captcha:
            from .captcha import save_captcha_image
            save_captcha_image(png, "booking", label=label)

        # Solve via codetype 9801: comma-separated instruction → one (x,y) per char.
        instruction = ",".join(label)
        try:
            coords = await click_solver.solve_click(png, instruction)
        except Exception as e:
            # Transient API errors (e.g. -3002 系统超时, network blips) — burn this
            # attempt and try again on the next iteration with a fresh screenshot.
            log.warning("Chaojiying solve_click failed on attempt %d/%d: %s",
                        attempt + 1, _BOOKING_CAPTCHA_MAX_RETRIES, e)
            await asyncio.sleep(1.0)
            continue
        dpr = await page.evaluate("window.devicePixelRatio") or 1
        log.info("Click-captcha coords (attempt %d, dpr=%s): %s (chars: %s)", attempt + 1, dpr, coords, label)
        for x, y in coords:
            await img_loc.click(position={"x": x / dpr, "y": y / dpr})
            await asyncio.sleep(0.3)

        # Wait for the captcha to either disappear (success), refresh (wrong answer),
        # or be overtaken by a rejection modal (captcha accepted but booking denied).
        await asyncio.sleep(1.5)
        error_text = await _read_system_error_modal(page)
        if error_text:
            return BookingResult(False, f"Booking rejected by site: {error_text}", {"url": page.url})
        if not await verifybox.is_visible():
            return None  # Captcha dismissed — success.
        log.warning("Click-captcha still visible after attempt %d/%d, retrying.",
                    attempt + 1, _BOOKING_CAPTCHA_MAX_RETRIES)

    return BookingResult(False, f"Failed to solve click-captcha after {_BOOKING_CAPTCHA_MAX_RETRIES} attempts.", {})


async def confirm_payment(page: Page, cfg: AppConfig) -> BookingResult:
    """Handle the payment page that appears after the booking captcha is solved.

    Reads the amount from the '请您支付' box, then clicks the pay button (which
    has a countdown like '支付 （255s）'). If the amount is 0, clicking pay
    completes the booking in place — return success. If > 0, the site opens an
    external payment window (wechat/alipay/unionpay) which we can't automate —
    return success with a note asking the user to finish payment manually.

    If the site rejects the booking after the captcha (e.g. '预约失败，您存在
    未支付的订单！'), a '系统提示' modal appears instead of the payment page —
    in that case return a failure result with the modal's body text.
    """
    # After captcha the SPA either navigates to `?tradeNo=...` (success) or pops
    # a '系统提示' modal explaining why the booking was rejected. Poll for both.
    for _ in range(50):  # ~15 s at 0.3 s intervals.
        if "tradeNo=" in page.url:
            break
        error_text = await _read_system_error_modal(page)
        if error_text:
            return BookingResult(
                False,
                f"Booking rejected by site: {error_text}",
                {"url": page.url},
            )
        await asyncio.sleep(0.3)
    else:
        log.warning("Neither payment URL nor error modal appeared within the timeout.")

    # Scope the amount lookup to the '请您支付' key_val_box so it doesn't match other boxes.
    amount_text = await page.evaluate(
        "() => { const box = Array.from(document.querySelectorAll('.key_val_box'))"
        ".find(el => el.textContent.includes('请您支付'));"
        " if (!box) return null;"
        " const b = box.querySelector('b'); return b ? b.textContent.trim() : null; }"
    )
    if amount_text is None:
        return BookingResult(False, "Payment page reached but '请您支付' amount not found.", {"url": page.url})
    try:
        amount = float(amount_text)
    except ValueError:
        return BookingResult(False, f"Could not parse payment amount: {amount_text!r}.", {"url": page.url})
    log.info("Payment amount: ¥%s", amount)

    pay_sel = _sel(cfg, "proceed_to_pay")
    if not pay_sel:
        return BookingResult(False, "selectors.proceed_to_pay is not configured.", {"url": page.url})
    pay_btn = page.locator(pay_sel).first
    if not await pay_btn.count():
        return BookingResult(False, f"Pay button not found (selector: {pay_sel}).", {"url": page.url})
    log.info("Clicking pay button (amount ¥%s).", amount)
    await pay_btn.click()

    if amount > 0:
        return BookingResult(
            True,
            f"Booking reserved (¥{amount}). Please complete the payment manually "
            f"in the wechat/alipay/unionpay popup.",
            {"amount": amount, "url": page.url},
        )

    # Free booking — give the SPA a moment to finalize after the click.
    await asyncio.sleep(1.5)
    return BookingResult(
        True,
        "Free booking confirmed (amount ¥0).",
        {"amount": amount, "url": page.url},
    )
