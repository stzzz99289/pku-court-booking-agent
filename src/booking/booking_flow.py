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
    col_idx: int | None = None
    for scroll_attempt in range(20):  # 16 one-hour slots between 06:00-22:00
        header_cells = sched_table.locator("thead td")
        texts = [(await header_cells.nth(i).inner_text()).strip() for i in range(await header_cells.count())]
        log.debug("Scroll attempt %d: header cells: %s", scroll_attempt, texts)
        for i, text in enumerate(texts):
            if text == target_time:
                col_idx = i
                break
        if col_idx is not None:
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

    # Click the first court row (匹配 \d+号) that is not 已售 at the target column.
    court_rows = sched_table.locator("tbody tr")
    for row_i in range(await court_rows.count()):
        row = court_rows.nth(row_i)
        cells = row.locator("td")
        first_cell = (await cells.nth(0).inner_text()).strip()
        if not re.match(r"\d+号", first_cell):
            continue
        target_cell = cells.nth(col_idx)
        cell_text = (await target_cell.inner_text()).strip()
        if "已售" not in cell_text:
            log.info("Clicking court %s at %s (status: %s)", first_cell, target_time, cell_text)
            await target_cell.click()
            return None

    return BookingResult(False, f"All courts are sold out (已售) at {target_time}.", {})


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


async def verify_booking(page: Page, cfg: AppConfig) -> BookingResult:
    """Check the current page for booking success indicators."""
    # Navigate to the user page if we're not already there.
    sub = _sel(cfg, "user_page_url_substring")
    if sub and sub not in page.url:
        await page.goto(cfg.base_url, wait_until="domcontentloaded")

    # Try the configured success indicator first, then fall back to text heuristics.
    ok_sel = _sel(cfg, "booking_success_indicator")
    if ok_sel:
        loc = page.locator(ok_sel).first
        if await loc.count() and await loc.is_visible():
            text = (await loc.inner_text()).strip()
            return BookingResult(True, "Booking verification succeeded.", {"indicator_text": text})

    body = await page.content()
    if re.search(r"(成功|已预约|预约成功)", body):
        return BookingResult(True, "Booking likely succeeded (heuristic text match).", {})

    return BookingResult(
        False,
        "Could not confirm booking; check selectors.booking_success_indicator or the site UI.",
        {"url": page.url},
    )
