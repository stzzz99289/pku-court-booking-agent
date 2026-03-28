from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from .booking_flow import agree_and_submit_booking, select_booking_date, select_court_time, verify_booking
from .browser import dispose_context, launch_persistent_context, wait_until_user_closes_window
from .captcha import make_solver
from .config import AppConfig, load_config
from .login import ensure_logged_in
from .pipeline import HINT_AFTER_BOOKING_FORM, HINT_AFTER_NAVIGATE, login_automation_ready, submit_flow_ready
from .result import BookingResult

log = logging.getLogger(__name__)


def _reservation_url(cfg: AppConfig) -> str:
    # Build the direct venue-reservation URL from base_url + venue_id.
    parsed = urlparse(cfg.base_url)
    return f"{parsed.scheme}://{parsed.netloc}/venue/venue-reservation/{cfg.venue_id}"


async def _navigate_to_reservation(page, cfg: AppConfig, solver) -> None:
    """Navigate to the venue-reservation page, re-authenticating if a login modal appears."""
    url = _reservation_url(cfg)
    log.info("Navigating to venue reservation: %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    log.info("Landed on: %s", page.url)

    # Detect and dismiss the "please login" modal that can appear on direct navigation.
    modal = page.get_by_text("请登录后访问", exact=True)
    try:
        await modal.wait_for(state="visible", timeout=5_000)
        log.info("'Please login' modal detected — dismissing and re-authenticating.")
        await page.get_by_role("button", name="确定").first.click()
        await ensure_logged_in(page, cfg, solver)
        log.info("Re-navigating to venue reservation after login.")
        await page.goto(url, wait_until="domcontentloaded")
        log.info("Landed on: %s", page.url)
    except Exception:
        pass  # Modal did not appear — already logged in.


def _print_result(out: BookingResult) -> None:
    # Print the booking result message and any extra details to stdout.
    print(out.message)
    if out.details:
        print(out.details)


async def run(user_config_path: Path, site_config_path: Path) -> BookingResult:
    """Orchestrate the full booking flow: login → navigate → select date/time → submit."""
    cfg = load_config(user_config_path, site_config_path)
    solver = make_solver(cfg.captcha.provider, cfg.captcha.api_key)
    context, _ = await launch_persistent_context(cfg)
    page = context.pages[0] if context.pages else await context.new_page()
    out: BookingResult | None = None
    try:
        await page.goto(cfg.base_url, wait_until="domcontentloaded")

        # Stage 1: login (stop with hint if selectors not configured).
        if not login_automation_ready(cfg):
            out = BookingResult(True, HINT_AFTER_NAVIGATE, {"stopped_at": "after_navigate", "final_url": page.url})
        else:
            await ensure_logged_in(page, cfg, solver)
            await _navigate_to_reservation(page, cfg, solver)

            # Stage 2: select date.
            date_err = await select_booking_date(page, cfg)
            if date_err is not None:
                out = date_err

            # Stage 3: select court and time slot.
            else:
                time_err = await select_court_time(page, cfg)
                if time_err is not None:
                    out = time_err

                # Stage 4: agree and submit (stop with hint if submit selector not configured).
                elif not submit_flow_ready(cfg):
                    out = BookingResult(True, HINT_AFTER_BOOKING_FORM, {"stopped_at": "after_court_selection", "final_url": page.url})
                else:
                    await agree_and_submit_booking(page, cfg)

                    # Stage 5: detect click-captcha or verify booking success.
                    if await page.locator(".verifybox").first.is_visible():
                        out = BookingResult(
                            True,
                            "Click-captcha appeared (请完成安全验证). Not yet automated — solve it manually or close the browser.",
                            {"stopped_at": "captcha"},
                        )
                    else:
                        out = await verify_booking(page, cfg)

        assert out is not None
        return out
    finally:
        if out is not None:
            _print_result(out)
        await wait_until_user_closes_window(cfg, page)
        await dispose_context(context)
