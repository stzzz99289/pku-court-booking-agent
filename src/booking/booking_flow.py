from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page

from .config import AppConfig
from .result import BookingResult

log = logging.getLogger(__name__)


_DEBUG_DUMP_DIR = Path("debugging")


# JS that lists every visible modal-/dialog-like element on the page. Used by
# `_dump_post_submit_diagnostics` to capture what was on screen at the moment
# the post-submit flow bailed out — so the *next* recurrence of an unhandled
# dialog leaves enough evidence to write a matcher without guessing.
_VISIBLE_MODALS_JS = r"""
() => {
  const sels = ['.ivu-modal-wrap', '.ivu-modal', '.el-message-box', '.verifybox',
                '[class*="modal"]', '[class*="dialog"]', '[class*="message"]', '[role="dialog"]'];
  const seen = new Set();
  const out = [];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      const visible = r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
      if (!visible) continue;
      if (el.classList.contains('ivu-modal-hidden')) continue;
      out.push({
        sel,
        cls: (el.className && el.className.toString()).slice(0, 300),
        text: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 500),
        rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      });
    }
  }
  return out;
}
"""


async def _dump_post_submit_diagnostics(page: Page, label: str) -> Path | None:
    """Capture screenshot + body HTML + visible-modal summary to ./debugging/.

    Called from the post-submit bail-out paths so the *next* unhandled
    dialog leaves enough evidence to write a matcher. Best-effort: any
    failure here is logged and swallowed (we never want to mask the
    underlying booking failure).
    """
    try:
        _DEBUG_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:60]
        base = _DEBUG_DUMP_DIR / f"{stamp}_{safe_label}"
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception as e:
            log.warning("Diagnostic screenshot failed: %s", e)
        try:
            html = await page.content()
            base.with_suffix(".html").write_text(html, encoding="utf-8")
        except Exception as e:
            log.warning("Diagnostic HTML dump failed: %s", e)
        try:
            modals = await page.evaluate(_VISIBLE_MODALS_JS)
        except Exception as e:
            modals = [{"error": f"evaluate failed: {e}"}]
        meta = {
            "timestamp": stamp,
            "label": label,
            "url": page.url,
            "visible_modals": modals,
        }
        base.with_suffix(".json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        log.warning("Post-submit diagnostic dump saved to %s.{png,html,json}", base)
        return base
    except Exception as e:
        log.warning("Failed to write post-submit diagnostic dump: %s", e)
        return None


def _sel(cfg: AppConfig, name: str) -> str:
    # Return a stripped selector string from config, or "" if unset.
    s = getattr(cfg.selectors, name, "") or ""
    return s.strip()


# `reservationStatus` values in the day/info response (verified empirically):
#   1 → free (no orderId, useNum=0)               → DOM `.reserveBlock.free`
#   4 → booked (useNum=1, alreadyNum=1)           → DOM `.reserveBlock.reserved`
# The "mid-booking" orange state (DOM `.reserveBlock.reservation`) is transient
# and not observed in JSON snapshots — we treat anything other than 1 as "not
# free".
_FREE_STATUS = 1

# Hard-coded court preference: try the 2nd and 3rd courts (0-based index 1, 2)
# first, then fall back to the 1st court (index 0), then the rest in order.
# Order of preference: 1, 2, 0, 3, 4, 5, ...
_COURT_PRIORITY_RANK = {1: 0, 2: 1, 0: 2}


def prioritize_courts(court_indices: list[int]) -> list[int]:
    """Reorder free court indices by the hard-coded preference (1, 2, 0, 3, 4, ...).

    Courts 1 and 2 (the 2nd and 3rd courts) are preferred, then court 0, then
    any remaining courts in ascending index order.
    """
    return sorted(court_indices, key=lambda c: _COURT_PRIORITY_RANK.get(c, c))


def parse_day_info(payload: dict) -> dict[int, list[int]]:
    """Parse a `reservation/day/info` JSON body into {hour: [free_court_idx, ...]}.

    `hour` is the start hour as an int (6..21). Court indices are 0-based and
    match the order courts appear in the response — which is also the order
    of `tbody tr` court rows in the DOM (1号, 2号, …). Returns {} on any
    structural mismatch so callers can cleanly fall back to the DOM walk.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    space_time_info = data.get("spaceTimeInfo") or []
    slot_id_to_hour: dict[str, int] = {}
    for slot in space_time_info:
        if not isinstance(slot, dict):
            continue
        sid = slot.get("id")
        bt = slot.get("beginTime") or ""
        if sid is None or len(bt) < 2 or not bt[:2].isdigit():
            continue
        slot_id_to_hour[str(sid)] = int(bt[:2])

    date_map = data.get("reservationDateSpaceInfo") or {}
    if not isinstance(date_map, dict) or not date_map:
        return {}
    # Each request returns exactly one date — take its courts list.
    courts = next(iter(date_map.values()))
    if not isinstance(courts, list):
        return {}

    free: dict[int, list[int]] = {}
    for idx, ct in enumerate(courts):
        if not isinstance(ct, dict):
            continue
        for sid, hour in slot_id_to_hour.items():
            v = ct.get(sid)
            if isinstance(v, dict) and v.get("reservationStatus") == _FREE_STATUS:
                free.setdefault(hour, []).append(idx)
    return free


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
        # Transient: the runner can re-navigate and try again. The
        # retry-with-reload inside `_ensure_date_buttons_visible` should
        # normally catch this earlier; reaching here means even reloads
        # didn't surface the buttons.
        return BookingResult(
            False,
            "No date buttons found on reservation page (.date_box > div).",
            {"transient": True},
        )

    available: list[str] = []
    for i in range(count):
        btn = buttons.nth(i)
        text = (await btn.inner_text()).strip()
        available.append(text)
        if target in text:
            # If the date is already selected (class "active"), the table is
            # already loaded — clicking it again won't fire an API request, so
            # skip the click to avoid a timeout on expect_response.
            btn_cls = ((await btn.get_attribute("class")) or "").split()
            if "active" in btn_cls:
                log.info("Date %s is already selected, skipping click.", target)
                # Wait for the schedule table to be populated (it may still be loading after a page refresh).
                try:
                    await page.locator(".spaceTable table thead td").nth(1).wait_for(
                        state="visible", timeout=10_000
                    )
                except Exception:
                    log.warning("Schedule table did not populate for already-selected date.")
                return None
            log.info("Selecting date: %s", target)
            # Wait for the schedule API response before returning so the table is fresh.
            # The date is already released here (post_fire_refresh loaded it), so the
            # healthy day/info response is ~1 s; a 30 s span means the *click* hung on
            # the default action timeout, not a slow response. Cap the click tight so a
            # jammed click is abandoned at 3 s and we fall through to the DOM walk
            # (empty cache below) instead of burning 30 s.
            with cfg.profiler.span("date_click_and_xhr"):
                try:
                    async with page.expect_response(
                        lambda r: "reservation/day/info" in r.url, timeout=8_000
                    ) as resp_info:
                        await btn.click(timeout=3_000)
                    resp = await resp_info.value
                except Exception as e:
                    # Either the click hung (capped at 3 s) or the response never came;
                    # both mean "no fresh cache this round" — fall through to the DOM
                    # walk in select_court_time instead of crashing the worker.
                    log.warning("Date click / day/info response failed (%s); proceeding without cache.", e)
                    cfg.cached_free_slots = {}
                    return None
            try:
                with cfg.profiler.span("parse_day_info_json"):
                    cfg.cached_free_slots = parse_day_info(await resp.json())
                log.info("JSON cache: %d hour(s) with free courts (%s).",
                         len(cfg.cached_free_slots),
                         {h: cfg.cached_free_slots[h] for h in sorted(cfg.cached_free_slots)})
            except Exception as e:
                log.warning("Could not parse reservation/day/info body (%s); proceeding without cache.", e)
                cfg.cached_free_slots = {}
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
    # Both header + body must render before we can trust a "no free court"
    # verdict — otherwise we'd silently scan an empty DOM and report the
    # priority list as exhausted when the schedule data simply hadn't loaded.
    sched_table = page.locator(".spaceTable table")
    try:
        with cfg.profiler.span("wait_schedule_header"):
            await sched_table.locator("thead td").nth(1).wait_for(state="visible", timeout=10_000)
    except Exception:
        return BookingResult(
            False,
            f"Schedule table header for {target_time} never rendered (transient — schedule data not loaded).",
            {"transient": True, "target_time": target_time},
        )

    # After a date switch, Vue tears the tbody down (rows → 1 placeholder) and
    # re-renders about 300 ms later. Headers keep stale values during the gap,
    # so wait for the tbody to repopulate before scanning for a slot.
    try:
        with cfg.profiler.span("wait_schedule_tbody"):
            await page.locator(".spaceTable table tbody tr").nth(1).wait_for(
                state="visible", timeout=5_000
            )
    except Exception:
        return BookingResult(
            False,
            f"Schedule table body for {target_time} did not repopulate after date switch (transient).",
            {"transient": True, "target_time": target_time},
        )

    header_cells = sched_table.locator("thead td")
    with cfg.profiler.span("scroll_to_target_column"):
        col_idx = await _scroll_to_target_column(
            page, header_cells, start, target_time,
            boundaries=cfg.hour_page_boundaries or None,
        )
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

    # JSON-cache fast path: the runner already picked which court_idx to click.
    # Walk only court rows (filtering out non-court rows like headers) and click
    # the row at that index directly. Skips the per-row class check.
    if cfg.target_court_index >= 0:
        with cfg.profiler.span("click_court_cell(cache)"):
            court_row_indices: list[int] = []
            for row_i in range(await court_rows.count()):
                row = court_rows.nth(row_i)
                first_cell = (await row.locator("td").nth(0).inner_text()).strip()
                if re.match(r"\d+号", first_cell):
                    court_row_indices.append(row_i)
            if cfg.target_court_index < len(court_row_indices):
                row_i = court_row_indices[cfg.target_court_index]
                row = court_rows.nth(row_i)
                target_cell = row.locator("td").nth(col_idx)
                first_cell = (await row.locator("td").nth(0).inner_text()).strip()
                log.info("Clicking court %s at %s (JSON-cache court_idx=%d).",
                         first_cell, target_time, cfg.target_court_index)
                await target_cell.click()
                return None
        log.warning("target_court_index=%d out of range (have %d court rows); falling back to row walk.",
                    cfg.target_court_index, len(court_row_indices))

    # Collect every free court (court_idx is 0-based in court-row order, matching
    # parse_day_info), then pick by the hard-coded court preference rather than
    # plain row order so courts 1 & 2 are tried before court 0.
    free_courts: list[tuple[int, object, str]] = []  # (court_idx, target_cell, label)
    court_idx = 0
    for row_i in range(await court_rows.count()):
        row = court_rows.nth(row_i)
        cells = row.locator("td")
        first_cell = (await cells.nth(0).inner_text()).strip()
        if not re.match(r"\d+号", first_cell):
            continue
        this_court_idx = court_idx
        court_idx += 1
        target_cell = cells.nth(col_idx)
        block = target_cell.locator(".reserveBlock").first
        if not await block.count():
            continue  # Unexpected cell layout — skip defensively.
        block_classes = ((await block.get_attribute("class")) or "").split()
        if "free" not in block_classes:
            continue  # Sold (reserved) or mid-booking (reservation).
        free_courts.append((this_court_idx, target_cell, first_cell))

    if free_courts:
        order = {c: i for i, c in enumerate(prioritize_courts([c for c, _, _ in free_courts]))}
        free_courts.sort(key=lambda fc: order[fc[0]])
        _, target_cell, label = free_courts[0]
        log.info("Clicking court %s at %s (court_idx=%d, priority pick).", label, target_time, free_courts[0][0])
        await target_cell.click()
        return None

    return BookingResult(
        False,
        f"No available courts at {target_time} (all sold or mid-reservation by other users).",
        {},
    )


_HEADER_HOUR_RE = re.compile(r"^(\d{2}):00-\d{2}:00$")


async def _read_header_texts(header_cells) -> list[str]:
    """Return the stripped inner text of every schedule-table header cell."""
    return [t.strip() for t in await header_cells.all_inner_texts()]


def _visible_start_hours(texts: list[str]) -> list[int]:
    """Extract the 'HH' start hours visible in the schedule header row."""
    hours = []
    for t in texts:
        m = _HEADER_HOUR_RE.match(t)
        if m:
            hours.append(int(m.group(1)))
    return hours


async def _scroll_to_target_column(
    page: Page, header_cells, target_hour: int, target_time: str,
    boundaries: list[list[int]] | None = None,
) -> int | None:
    """Scroll the schedule table directly to the page containing target_hour.

    Fast path (when `boundaries` is provided — a list of [first_hour, last_hour]
    per pagination page, pre-discovered via `discover_hour_page_boundaries`):
    compute the exact number of arrow clicks to reach the target page and
    burst-click without polling between, then wait once for the target column
    to appear.

    Slow path: pages one arrow at a time, polling for header text to change.
    Used when boundaries weren't discovered (e.g. non-scheduled CLI runs).

    Returns the matching column index, or None if the target never appeared.
    """
    if boundaries:
        idx = await _scroll_via_boundaries(page, header_cells, target_hour, target_time, boundaries)
        if idx is not None:
            return idx
        # Discovery may be stale (rare — page sizes are fixed); fall through.

    for _ in range(6):  # 16-hour booking window → ≤3 page jumps each direction.
        texts = await _read_header_texts(header_cells)
        if target_time in texts:
            return texts.index(target_time)

        visible = _visible_start_hours(texts)
        if not visible:
            # Headers not yet populated — brief wait and retry.
            await asyncio.sleep(0.05)
            continue

        if target_hour < min(visible):
            arrow = page.locator("div.arrowWrap i.ivu-icon-ios-arrow-back").first
        elif target_hour > max(visible):
            arrow = page.locator("div.arrowWrap i.ivu-icon-ios-arrow-forward").first
        else:
            return None  # in visible range but target_time not listed (hour not offered)

        if not await arrow.count():
            return None  # reached an edge without finding it

        await arrow.click()
        # Poll for headers to change (DOM typically settles in ~30 ms).
        for _ in range(20):  # up to ~200 ms
            await asyncio.sleep(0.01)
            new_texts = await _read_header_texts(header_cells)
            if new_texts != texts:
                break
    return None


def _page_index_for_hour(hour: int, boundaries: list[list[int]]) -> int | None:
    for i, rng in enumerate(boundaries):
        if rng[0] <= hour <= rng[1]:
            return i
    return None


async def _scroll_via_boundaries(
    page: Page, header_cells, target_hour: int, target_time: str,
    boundaries: list[list[int]],
) -> int | None:
    """Burst-click arrows to land on the target page using pre-discovered boundaries."""
    target_page = _page_index_for_hour(target_hour, boundaries)
    if target_page is None:
        return None  # target hour outside any known page

    texts = await _read_header_texts(header_cells)
    if target_time in texts:
        return texts.index(target_time)

    visible = _visible_start_hours(texts)
    if not visible:
        return None  # let slow path handle empty-header case
    current_page = _page_index_for_hour(min(visible), boundaries)
    if current_page is None:
        return None  # current page doesn't match any known boundary — fall back

    delta = target_page - current_page
    if delta == 0:
        return None  # in target page but target_time not listed (hour not offered)

    arrow_sel = (
        "div.arrowWrap i.ivu-icon-ios-arrow-forward" if delta > 0
        else "div.arrowWrap i.ivu-icon-ios-arrow-back"
    )
    arrow = page.locator(arrow_sel).first
    if not await arrow.count():
        return None
    for _ in range(abs(delta)):
        await arrow.click()

    # Wait for the target column header to render (DOM settles ~30 ms per page jump).
    for _ in range(40):  # up to ~400 ms
        await asyncio.sleep(0.01)
        texts = await _read_header_texts(header_cells)
        if target_time in texts:
            return texts.index(target_time)
    return None


async def discover_hour_page_boundaries(page: Page) -> list[list[int]]:
    """Walk the schedule pagination to record the hour range of each page.

    Caller must ensure the schedule table is loaded (date selected). Scrolls
    all the way left first, then forward through every page recording
    `[first_hour, last_hour]`, and stops when no further forward click changes
    the headers. Page sizes are fixed on this site, so the result stays valid
    after the post-fire refresh switches to a different bookable date.
    """
    sched_table = page.locator(".spaceTable table")
    header_cells = sched_table.locator("thead td")
    try:
        await header_cells.nth(1).wait_for(state="visible", timeout=5_000)
    except Exception:
        return []

    back = page.locator("div.arrowWrap i.ivu-icon-ios-arrow-back").first
    fwd = page.locator("div.arrowWrap i.ivu-icon-ios-arrow-forward").first

    async def _click_until_unchanged(arrow) -> None:
        for _ in range(10):
            if not await arrow.count():
                return
            prev = await _read_header_texts(header_cells)
            try:
                await arrow.click(timeout=500)
            except Exception:
                return
            for _ in range(20):
                await asyncio.sleep(0.01)
                new = await _read_header_texts(header_cells)
                if new != prev:
                    break
            else:
                return  # headers didn't change — at edge

    # Scroll to leftmost page so discovery starts from a known state.
    await _click_until_unchanged(back)

    pages: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for _ in range(10):  # safety cap; the booking window is 16 hours.
        texts = await _read_header_texts(header_cells)
        hours = _visible_start_hours(texts)
        if not hours:
            break
        key = tuple(hours)
        if key in seen:
            break
        seen.add(key)
        pages.append([min(hours), max(hours)])
        if not await fwd.count():
            break
        prev = texts
        try:
            await fwd.click(timeout=500)
        except Exception:
            break
        changed = False
        for _ in range(20):
            await asyncio.sleep(0.01)
            new = await _read_header_texts(header_cells)
            if new != prev:
                changed = True
                break
        if not changed:
            break

    return pages


async def agree_and_submit_booking(page: Page, cfg: AppConfig) -> None:
    """Check the agreement checkbox (if unchecked) and click the submit button."""
    agree = _sel(cfg, "agreement_checkbox")
    if agree:
        loc = page.locator(agree).first
        if not await loc.is_checked():
            await loc.click()

    submit = _sel(cfg, "booking_submit")
    if submit:
        with cfg.profiler.span("click_submit"):
            await page.locator(submit).first.click()
        # Poll for whichever outcome state shows up first — captcha dialog,
        # server rejection modal, or payment tab — rather than a fixed sleep.
        # Bails out as soon as any signal is seen (typically < 200 ms); caps at
        # ~2 s if nothing ever appears (caller then handles it).
        ctx = page.context
        with cfg.profiler.span("wait_post_submit_outcome"):
            for _ in range(40):
                if await page.locator(".verifybox").first.is_visible():
                    return
                if await _read_system_error_modal(page):
                    return
                if any("tradeNo=" in p.url for p in ctx.pages):
                    return
                await asyncio.sleep(0.05)


_BOOKING_CAPTCHA_MAX_RETRIES = 6

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


async def check_booking_rejection(page: Page) -> BookingResult | None:
    """Return a failure BookingResult if a '系统提示' rejection modal is visible, else None.

    Used by the runner to short-circuit the flow whenever the server rejects the
    booking — e.g. '该场地已被其他人预约，请更换场地或时间后再预约！' (another user
    grabbed the slot first) or '预约失败，您存在未支付的订单！' (unpaid order blocks
    new reservations). Both share the same iView confirm-modal structure.
    """
    error_text = await _read_system_error_modal(page)
    if not error_text:
        return None
    return BookingResult(
        False,
        f"Booking rejected by site: {error_text}",
        {"url": page.url},
    )


def _find_trade_page(context) -> Page | None:
    """Return the first page in `context` whose URL contains 'tradeNo=', or None.

    On success the site opens the payment page in a NEW TAB (the original
    reservation tab stays put), so we always scan the whole context rather
    than trusting the original `page.url`.
    """
    for p in context.pages:
        if "tradeNo=" in p.url:
            return p
    return None


async def solve_booking_captcha(page: Page, click_solver, cfg: AppConfig) -> BookingResult | None:
    """Solve the click-captcha that appears after submitting a booking.

    Screenshots the captcha image, sends it to click_solver.solve_click() to get
    coordinates, and clicks each point on the image. Retries if the captcha refreshes.
    Returns None on success, or a BookingResult error.

    Success signals (checked in order):
      1. A new tab with `tradeNo=` in its URL has opened — this is what actually
         happens in practice; the site opens the payment page in a new window
         rather than navigating in place.
      2. The '系统提示' rejection modal appeared (e.g. '预约失败，您存在未支付的订单！')
         — short-circuit with a rejection BookingResult instead of looping.
      3. The `.verifybox` captcha dialog disappeared from the original page.

    We don't rely on `verifybox.is_visible()` alone because after the captcha
    is accepted the site leaves `.verifybox` in the DOM with `display: block`
    but detaches its parent — Playwright still reports it as visible.
    """
    ctx = page.context
    consecutive_solver_errors = 0  # A2b: refresh captcha after 3 in a row
    for attempt in range(_BOOKING_CAPTCHA_MAX_RETRIES):
        # Trade page may already exist (e.g. instant acceptance before we check).
        if _find_trade_page(ctx) is not None:
            return None

        # A rejection modal may have already appeared before we get a chance to click.
        error_text = await _read_system_error_modal(page)
        if error_text:
            return BookingResult(False, f"Booking rejected by site: {error_text}", {"url": page.url})

        verifybox = page.locator(".verifybox").first
        if not await verifybox.is_visible():
            return None  # Captcha already gone — success.

        # Screenshot the captcha image. The image element appears a beat after
        # .verifybox becomes visible, so give it a short window to attach.
        # Then gate on the image actually being decoded — without this,
        # Playwright's screenshot blocks waiting for paint (0.28s fast path
        # vs ~3s slow path), so we explicitly wait for the `load` event.
        img_loc = page.locator(".verify-img-panel img").first
        try:
            with cfg.profiler.span(f"captcha_screenshot[{attempt + 1}]"):
                await img_loc.wait_for(state="visible", timeout=3_000)
                try:
                    await img_loc.evaluate(
                        "el => new Promise((resolve, reject) => {"
                        "  if (el.complete && el.naturalWidth > 0) return resolve();"
                        "  const ok = () => resolve();"
                        "  const bad = () => reject(new Error('image error'));"
                        "  el.addEventListener('load', ok, {once: true});"
                        "  el.addEventListener('error', bad, {once: true});"
                        "  setTimeout(() => reject(new Error('image load timeout')), 2500);"
                        "})"
                    )
                except Exception as e:
                    log.warning("Captcha image not fully loaded before screenshot: %s", e)
                png = await img_loc.screenshot()
        except Exception:
            # A2a: image didn't mount within 3 s. Don't give up — refresh the
            # captcha and retry on the next attempt; the post-failure screenshot
            # on 2026-05-23 proved the image does render eventually.
            log.warning("Captcha image not visible within 3 s on attempt %d/%d — refreshing and retrying.",
                        attempt + 1, _BOOKING_CAPTCHA_MAX_RETRIES)
            try:
                await page.locator(".verify-refresh").first.click(timeout=1_000)
            except Exception as refresh_err:
                log.warning("Could not click .verify-refresh: %s", refresh_err)
            await asyncio.sleep(0.5)
            continue

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
            with cfg.profiler.span(f"captcha_solve_api[{attempt + 1}]"):
                coords = await click_solver.solve_click(png, instruction)
            consecutive_solver_errors = 0
        except Exception as e:
            # Transient API errors (e.g. -3002 系统超时, network blips) — burn this
            # attempt and try again on the next iteration with a fresh screenshot.
            log.warning("Chaojiying solve_click failed on attempt %d/%d: %s",
                        attempt + 1, _BOOKING_CAPTCHA_MAX_RETRIES, e)
            consecutive_solver_errors += 1
            # A2b: after 3 consecutive solver-API errors, the current captcha image
            # may be poisoning the API; refresh it before the next attempt.
            if consecutive_solver_errors >= 3:
                log.warning("3 consecutive solver-API errors — refreshing captcha.")
                try:
                    await page.locator(".verify-refresh").first.click(timeout=1_000)
                except Exception as refresh_err:
                    log.warning("Could not click .verify-refresh: %s", refresh_err)
                consecutive_solver_errors = 0
            await asyncio.sleep(1.0)
            continue
        dpr = await page.evaluate("window.devicePixelRatio") or 1
        log.info("Click-captcha coords (attempt %d, dpr=%s): %s (chars: %s)", attempt + 1, dpr, coords, label)
        with cfg.profiler.span(f"captcha_clicks[{attempt + 1}]"):
            for x, y in coords:
                await img_loc.click(position={"x": x / dpr, "y": y / dpr})
                await asyncio.sleep(0.1)

        # Poll for success conditions: trade page opened in a (new) tab, rejection
        # modal, or captcha genuinely dismissed. Tight interval so the payment
        # tab is claimed the moment it appears — total budget stays ~3 s.
        with cfg.profiler.span(f"captcha_wait_outcome[{attempt + 1}]"):
            for _ in range(30):  # ~3 s at 0.1 s intervals.
                await asyncio.sleep(0.1)
                if _find_trade_page(ctx) is not None:
                    return None  # Payment page appeared — booking accepted.
                error_text = await _read_system_error_modal(page)
                if error_text:
                    return BookingResult(False, f"Booking rejected by site: {error_text}", {"url": page.url})
                if not await verifybox.is_visible():
                    return None  # Captcha dismissed — success.
        log.warning("Click-captcha still visible after attempt %d/%d, retrying.",
                    attempt + 1, _BOOKING_CAPTCHA_MAX_RETRIES)

    await _dump_post_submit_diagnostics(page, "captcha_exhausted")
    return BookingResult(False, f"Failed to solve click-captcha after {_BOOKING_CAPTCHA_MAX_RETRIES} attempts.", {})


async def confirm_payment(page: Page, cfg: AppConfig) -> tuple[Page, BookingResult]:
    """Handle the payment page that appears after the booking captcha is solved.

    The site opens the payment page in a NEW tab, so we poll `context.pages` for
    any page with `tradeNo=` in its URL and switch to it. Returns
    (active_page, result) — the runner uses `active_page` for the shutdown
    wait (headed mode) so the payment tab stays under the program's control.

    Reads the amount from the '请您支付' box, then clicks the pay button (which
    has a countdown like '支付 （255s）'). If the amount is 0, clicking pay
    completes the booking in place. If > 0, the site opens an external payment
    window (wechat/alipay/unionpay) which we can't automate — return success
    with a note asking the user to finish payment manually.

    If the site rejects the booking after the captcha (e.g. '预约失败，您存在
    未支付的订单！'), a '系统提示' modal appears instead of the payment page —
    in that case return a failure result with the modal's body text.
    """
    ctx = page.context
    trade_page: Page | None = None
    # After captcha the SPA either opens a new tab with `?tradeNo=...` or pops
    # a '系统提示' modal explaining the rejection. Poll tightly so the payment
    # tab is claimed the moment it appears (total budget stays ~10 s).
    with cfg.profiler.span("wait_for_trade_page"):
        for _ in range(200):  # ~10 s at 0.05 s intervals.
            trade_page = _find_trade_page(ctx)
            if trade_page is not None:
                break
            error_text = await _read_system_error_modal(page)
            if error_text:
                return page, BookingResult(
                    False,
                    f"Booking rejected by site: {error_text}",
                    {"url": page.url},
                )
            await asyncio.sleep(0.05)
        else:
            log.warning("Neither payment URL nor error modal appeared within the timeout.")
            return page, BookingResult(
                False,
                "Booking silently rejected: captcha passed but no payment tab opened "
                "and no error modal appeared (slot likely taken concurrently).",
                {"url": page.url},
            )

    # If the payment page opened in a new tab, switch to it and close the old
    # reservation tab so only one window is left when the program ends.
    if trade_page is not None and trade_page is not page:
        log.info("Payment page opened in new tab: %s", trade_page.url)
        try:
            await page.close()
        except Exception:
            pass
        page = trade_page
        await page.bring_to_front()

    # Wait for the Vue SPA to finish loading data (the <b> amount defaults to
    # "0" until the API response populates it).
    try:
        with cfg.profiler.span("trade_page_networkidle"):
            await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        log.warning("Trade page did not reach networkidle within 10 s; reading amount anyway.")

    # Scope the amount lookup to the '请您支付' key_val_box so it doesn't match other boxes.
    amount_text = await page.evaluate(
        "() => { const box = Array.from(document.querySelectorAll('.key_val_box'))"
        ".find(el => el.textContent.includes('请您支付'));"
        " if (!box) return null;"
        " const b = box.querySelector('b'); return b ? b.textContent.trim() : null; }"
    )
    if amount_text is None:
        return page, BookingResult(False, "Payment page reached but '请您支付' amount not found.", {"url": page.url})
    try:
        amount = float(amount_text)
    except ValueError:
        return page, BookingResult(False, f"Could not parse payment amount: {amount_text!r}.", {"url": page.url})
    log.info("Payment amount: ¥%s", amount)

    pay_sel = _sel(cfg, "proceed_to_pay")
    if not pay_sel:
        return page, BookingResult(False, "selectors.proceed_to_pay is not configured.", {"url": page.url})
    pay_btn = page.locator(pay_sel).first
    if not await pay_btn.count():
        return page, BookingResult(False, f"Pay button not found (selector: {pay_sel}).", {"url": page.url})
    log.info("Clicking pay button (amount ¥%s).", amount)
    with cfg.profiler.span("click_pay"):
        await pay_btn.click()

    if amount > 0:
        if cfg.headless:
            # In headless mode the wechat/alipay popup has nowhere to render —
            # the reservation is held, but the user will have to pay through the
            # site's orders page in a normal browser.
            msg = (
                f"Booking reserved (¥{amount}). Headless mode cannot complete "
                f"payment — open the site in a browser to pay from the orders page."
            )
        else:
            msg = (
                f"Booking reserved (¥{amount}). Please complete the payment manually "
                f"in the wechat/alipay/unionpay popup."
            )
        return page, BookingResult(True, msg, {"amount": amount, "url": page.url})

    # Free booking — give the SPA a moment to finalize after the click.
    await asyncio.sleep(1.5)
    return page, BookingResult(
        True,
        "Free booking confirmed (amount ¥0).",
        {"amount": amount, "url": page.url},
    )
