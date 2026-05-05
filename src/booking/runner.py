from __future__ import annotations

import asyncio
import copy
import logging
import multiprocessing
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .booking_flow import (
    agree_and_submit_booking,
    check_booking_rejection,
    confirm_payment,
    select_booking_date,
    select_court_time,
    solve_booking_captcha,
)
from .browser import dispose_context, launch_persistent_context, wait_until_user_closes_window
from .captcha import ManualCaptchaSolver
from .config import AppConfig, UserConfig, WorkerConfig, load_config
from .login import ensure_logged_in
from .orders import Order, fetch_user_orders, format_orders_table
from .pipeline import HINT_AFTER_BOOKING_FORM, HINT_AFTER_NAVIGATE, login_automation_ready, submit_flow_ready
from .result import BookingResult

log = logging.getLogger(__name__)


def _reservation_url(cfg: AppConfig) -> str:
    # Build the direct venue-reservation URL from base_url + venue_id.
    parsed = urlparse(cfg.base_url)
    return f"{parsed.scheme}://{parsed.netloc}/venue/venue-reservation/{cfg.venue_id}"


def _make_solvers(cfg: AppConfig):
    """Create the login and booking captcha solvers based on debug mode.

    Returns (login_solver, click_solver_or_None).
    """
    if cfg.debug:
        return ManualCaptchaSolver(), None
    from .ocr import DdddocrSolver
    from .chaojiying import ChaojiyingSolver
    login_solver = DdddocrSolver()
    click_solver = ChaojiyingSolver(cfg.captcha.username, cfg.captcha.api_key, cfg.captcha.softid)
    return login_solver, click_solver


async def _navigate_to_reservation(page, cfg: AppConfig, login_solver) -> None:
    """Navigate to the venue-reservation page, re-authenticating if a login modal appears.

    On return, the date buttons (`.date_box > div`) are visible — both the
    initial nav and the retry-after-rejection path rely on this so that
    `select_booking_date` doesn't see an empty `.date_box` and bail out.
    """
    url = _reservation_url(cfg)
    log.info("Navigating to venue reservation: %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    log.info("Landed on: %s", page.url)

    # Detect and dismiss the "please login" modal (appears quickly if session is invalid).
    modal = page.get_by_text("请登录后访问", exact=True)
    try:
        await modal.wait_for(state="visible", timeout=1_500)
    except Exception:
        await _ensure_date_buttons_visible(page)
        return  # Modal did not appear — already logged in.
    log.info("'Please login' modal detected — dismissing and re-authenticating.")
    await page.get_by_role("button", name="确定").first.click()
    await ensure_logged_in(page, cfg, login_solver)
    log.info("Re-navigating to venue reservation after login.")
    await page.goto(url, wait_until="domcontentloaded")
    log.info("Landed on: %s", page.url)
    await _ensure_date_buttons_visible(page)


# Date-buttons retry budget — kept tight so the happy path (Vue renders in
# well under a second) is unaffected. Reloads between attempts so a stuck
# Vue/XHR is given a fresh shot.
_DATE_BUTTONS_ATTEMPTS = 3
_DATE_BUTTONS_TIMEOUT_MS = 4_000


async def _ensure_date_buttons_visible(page) -> bool:
    """Wait for `.date_box > div` to render, reloading the page on each miss.

    Returns True if the buttons appear within `_DATE_BUTTONS_ATTEMPTS` tries,
    False otherwise. Callers that depend on a populated date row should treat
    False as a transient failure worth re-navigating for at the runner level.
    """
    for i in range(1, _DATE_BUTTONS_ATTEMPTS + 1):
        try:
            await page.locator(".date_box > div").first.wait_for(
                state="visible", timeout=_DATE_BUTTONS_TIMEOUT_MS,
            )
            if i > 1:
                log.info("Date buttons appeared on attempt %d/%d.", i, _DATE_BUTTONS_ATTEMPTS)
            return True
        except Exception:
            if i < _DATE_BUTTONS_ATTEMPTS:
                log.warning(
                    "Date buttons not visible after %.1f s (attempt %d/%d). Reloading and retrying.",
                    _DATE_BUTTONS_TIMEOUT_MS / 1000, i, _DATE_BUTTONS_ATTEMPTS,
                )
                try:
                    await page.reload(wait_until="domcontentloaded")
                except Exception as e:
                    log.warning("Reload during date-button retry raised: %s", e)
            else:
                log.error(
                    "Date buttons never appeared after %d attempts (each %.1f s + reload).",
                    _DATE_BUTTONS_ATTEMPTS, _DATE_BUTTONS_TIMEOUT_MS / 1000,
                )
    return False


def _parse_scheduled_time(hhmmss: str) -> datetime:
    """Parse HHMMSS string into a datetime for today."""
    h, m, s = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
    return datetime.now().replace(hour=h, minute=m, second=s, microsecond=0)


def _check_schedule_window(cfg: AppConfig) -> BookingResult | None:
    """Return an error BookingResult if we're outside the allowed window, else None."""
    target = _parse_scheduled_time(cfg.scheduled_time)
    window_start = target - timedelta(minutes=cfg.scheduled_window_minutes)
    now = datetime.now()
    if now < window_start or now >= target:
        return BookingResult(
            False,
            f"Outside scheduled window. Current time: {now.strftime('%H:%M:%S')}. "
            f"Allowed window: {window_start.strftime('%H:%M:%S')} – {target.strftime('%H:%M:%S')}. Exiting.",
        )
    return None


async def _wait_for_scheduled_time(page, cfg: AppConfig) -> None:
    """Sleep until scheduled_time (+offset), then refresh until the target date is released.

    The site doesn't release the new bookable date atomically with the
    wall-clock second tick — refreshing exactly at `scheduled_time` can
    return the pre-rollover 3-day window. We mitigate this two ways:
      1. Wait `scheduled_fire_offset_ms` past the boundary before the first
         refresh (boosts first-attempt hit rate at minimal latency cost).
      2. If the target date is still missing after refresh, reload a few
         more times with a short delay (cheaper than re-navigating, and
         catches the rare boundary miss). After 12:00:00 the site is under
         load, so we keep this retry budget tight.
    """
    target = _parse_scheduled_time(cfg.scheduled_time)
    remaining = (target - datetime.now()).total_seconds()
    if remaining > 0:
        log.info(
            "Scheduled mode: waiting %.1f s until %s …",
            remaining, target.strftime("%H:%M:%S"),
        )
        await asyncio.sleep(remaining)
    if cfg.scheduled_fire_offset_ms > 0:
        await asyncio.sleep(cfg.scheduled_fire_offset_ms / 1000)
    log.info("Scheduled time reached — refreshing page.")
    await _refresh_until_target_date_visible(page, cfg)


# Refresh budget after `scheduled_time` for the new bookable date to appear.
# Total worst-case wait at the boundary ≈ N * (reload + delay), kept tight
# so a real "date never available" config error still surfaces quickly.
_MAX_DATE_REFRESH_ATTEMPTS = 3
_DATE_REFRESH_RETRY_DELAY_MS = 500


def _format_target_date_label(date_str: str) -> str | None:
    """YYYYMMDD → 'MM月DD日' as displayed on the .date_box buttons, or None if unparseable."""
    s = (date_str or "").strip()
    if len(s) < 8 or not s[:8].isdigit():
        return None
    return f"{s[4:6]}月{s[6:8]}日"


async def _read_date_button_texts(page) -> list[str]:
    """Return the inner text of every .date_box button currently rendered."""
    buttons = page.locator(".date_box > div")
    count = await buttons.count()
    return [(await buttons.nth(i).inner_text()).strip() for i in range(count)]


async def _refresh_until_target_date_visible(page, cfg: AppConfig) -> None:
    """Reload until cfg.date appears in the date row, capped by `_MAX_DATE_REFRESH_ATTEMPTS`.

    On the final attempt we still proceed even if the date is missing — the
    subsequent `select_booking_date` will then return a clear error listing
    what's actually available, instead of looping silently.
    """
    target = _format_target_date_label(cfg.date)
    for attempt in range(1, _MAX_DATE_REFRESH_ATTEMPTS + 1):
        async with page.expect_response(
            lambda r: "reservation/day/info" in r.url, timeout=15_000
        ) as resp_info:
            await page.reload(wait_until="domcontentloaded")
        try:
            await resp_info.value
        except Exception:
            log.warning("Did not capture reservation/day/info response after reload (attempt %d/%d).",
                        attempt, _MAX_DATE_REFRESH_ATTEMPTS)
        await _ensure_date_buttons_visible(page)

        if target is None:
            log.info("Schedule data loaded after refresh.")
            return

        visible = await _read_date_button_texts(page)
        if any(target in t for t in visible):
            log.info("Schedule data loaded after refresh — target date %s visible (attempt %d/%d).",
                     target, attempt, _MAX_DATE_REFRESH_ATTEMPTS)
            return

        if attempt < _MAX_DATE_REFRESH_ATTEMPTS:
            log.warning(
                "Target date %s not yet released (visible: %s); retrying in %d ms (attempt %d/%d).",
                target, visible, _DATE_REFRESH_RETRY_DELAY_MS, attempt, _MAX_DATE_REFRESH_ATTEMPTS,
            )
            await asyncio.sleep(_DATE_REFRESH_RETRY_DELAY_MS / 1000)
        else:
            log.warning(
                "Target date %s still not visible after %d refreshes (visible: %s); proceeding.",
                target, _MAX_DATE_REFRESH_ATTEMPTS, visible,
            )


def _print_result(out: BookingResult) -> None:
    # Print the booking result message and any extra details to stdout.
    print(out.message)
    if out.details:
        print(out.details)


# Rejection messages from `check_booking_rejection`, `solve_booking_captcha`,
# and `confirm_payment` all share this prefix; use it to decide whether a
# failed BookingResult should trigger a next-slot retry or abort immediately.
_REJECTION_PREFIX = "Booking rejected by site:"


def _is_site_rejection(result: BookingResult) -> bool:
    """True if `result` is a server-side rejection (slot taken, unpaid order, etc.)."""
    return not result.success and result.message.startswith(_REJECTION_PREFIX)


# Safety cap on how many times we refresh and re-walk the priority list. The
# natural exit is "every hour in the list shows no free courts"; this cap only
# kicks in if other users keep releasing slots faster than we can claim them.
_MAX_REFRESH_ATTEMPTS = 20

# Cap on transient re-navigations (date buttons / schedule table never
# rendered). Counted independently from `_MAX_REFRESH_ATTEMPTS` so a
# pathologically slow page still bails out instead of looping forever.
_MAX_TRANSIENT_RETRIES = 3


async def _attempt_book_from_priority_list(
    page, cfg: AppConfig, click_solver,
) -> tuple[object, BookingResult, str | None]:
    """Walk start_time_list on the current schedule page and book the first available slot.

    Steps:
      1. Select the date once.
      2. Walk start_time_list in order; for each hour, call select_court_time
         which scrolls + scans `.reserveBlock` classes (DOM only, no network).
         Stop at the first hour that has a free court — select_court_time
         clicks it as a side effect.
      3. Run submit → captcha → payment for the chosen hour.

    Returns (active_page, result, chosen_hour). When `chosen_hour` is None,
    no hour in the list had a free court — the caller should NOT refresh
    again because every priority slot is sold out.
    """
    # Stage 2: select date (re-select after a refresh; no-op if already active).
    date_err = await select_booking_date(page, cfg)
    if date_err is not None:
        return page, date_err, None

    # Stage 3: walk the priority list and click the first hour with a free court.
    chosen_hour: str | None = None
    tried: list[str] = []
    transient_hours = 0
    for hour in cfg.start_time_list:
        cfg.start_time = hour
        cfg.end_time = f"{int(hour) + 1:02d}"
        tried.append(hour)
        time_err = await select_court_time(page, cfg)
        if time_err is None:
            chosen_hour = hour
            break
        # Per-hour outcome — kept at info so a 0/N walk is never silent.
        is_transient = bool(time_err.details.get("transient"))
        if is_transient:
            transient_hours += 1
        log.info("Hour %s:00 not booked (%s%s).",
                 hour, "transient: " if is_transient else "", time_err.message)

    if chosen_hour is None:
        slots_str = ", ".join(f"{h}:00" for h in tried)
        # If every hour in the priority list failed transiently (e.g.
        # schedule table never populated), tell the runner to re-navigate
        # rather than declaring the list exhausted. Otherwise this is a
        # real "every priority sold out" exit.
        all_transient = transient_hours == len(tried) and tried
        if all_transient:
            return page, BookingResult(
                False,
                f"Schedule data did not load for any priority slot ({slots_str}); transient.",
                {"url": page.url, "tried_hours": tried, "transient": True},
            ), None
        return page, BookingResult(
            False,
            f"No available courts at any priority slot ({slots_str}); every hour in start_time_list is sold out.",
            {"url": page.url, "tried_hours": tried},
        ), None

    # Stage 4: agree + submit.
    if not submit_flow_ready(cfg):
        return page, BookingResult(
            True, HINT_AFTER_BOOKING_FORM,
            {"stopped_at": "after_court_selection", "final_url": page.url},
        ), chosen_hour
    await agree_and_submit_booking(page, cfg)

    rejection = await check_booking_rejection(page)
    if rejection is not None:
        return page, rejection, chosen_hour

    # Stage 5: click-captcha (if it appeared).
    if await page.locator(".verifybox").first.is_visible():
        if cfg.debug or click_solver is None:
            return page, BookingResult(
                True,
                "Click-captcha appeared (请完成安全验证). Debug mode — solve it manually or close the browser.",
                {"stopped_at": "captcha"},
            ), chosen_hour
        captcha_err = await solve_booking_captcha(page, click_solver, cfg)
        if captcha_err is not None:
            return page, captcha_err, chosen_hour
        rejection = await check_booking_rejection(page)
        if rejection is not None:
            return page, rejection, chosen_hour

    # Stage 6: confirm payment (may switch page to a new trade tab).
    page, result = await confirm_payment(page, cfg)
    return page, result, chosen_hour


async def run(
    user_config_path: Path,
    site_config_path: Path,
    _config_override: AppConfig | None = None,
) -> BookingResult:
    """Orchestrate the full booking flow: login → navigate → select → submit → captcha → verify."""
    cfg = _config_override or load_config(user_config_path, site_config_path)

    # Schedule gate: exit immediately if outside the allowed window.
    if cfg.scheduled_mode:
        window_err = _check_schedule_window(cfg)
        if window_err is not None:
            _print_result(window_err)
            return window_err

    login_solver, click_solver = _make_solvers(cfg)
    context, _ = await launch_persistent_context(cfg)
    page = context.pages[0] if context.pages else await context.new_page()
    out: BookingResult | None = None
    try:
        await page.goto(cfg.base_url, wait_until="domcontentloaded")

        # Stage 1: login (stop with hint if selectors not configured).
        if not login_automation_ready(cfg):
            out = BookingResult(True, HINT_AFTER_NAVIGATE, {"stopped_at": "after_navigate", "final_url": page.url})
        else:
            await ensure_logged_in(page, cfg, login_solver)
            await _navigate_to_reservation(page, cfg, login_solver)

            # Scheduled mode: wait at the reservation page, then refresh.
            if cfg.scheduled_mode:
                await _wait_for_scheduled_time(page, cfg)

            # Refresh-and-rewalk loop: each iteration walks start_time_list
            # from the top. If the chosen slot is rejected (someone else grabbed
            # it), refresh the page and re-walk — the FIRST hour may still have
            # other free courts. Exit when no priority hour has any free court,
            # on success, on a non-retryable error, or on the safety cap.
            if not cfg.start_time_list:
                out = BookingResult(False, "No start hours configured (start_time_list is empty).", {})
            else:
                last_failure: BookingResult | None = None
                transient_attempts = 0
                for refresh_attempt in range(1, _MAX_REFRESH_ATTEMPTS + 1):
                    if refresh_attempt > 1:
                        log.info("Refresh #%d: re-checking start_time_list from the top.",
                                 refresh_attempt - 1)
                        await _navigate_to_reservation(page, cfg, login_solver)

                    page, result, chosen_hour = await _attempt_book_from_priority_list(
                        page, cfg, click_solver,
                    )

                    if result.success:
                        out = result
                        break

                    last_failure = result

                    # Transient page/data-load failure (no date buttons, schedule
                    # table never populated, etc.). Re-navigate and try again so
                    # we don't silently report "no slots" when the data wasn't
                    # actually loaded.
                    if result.details.get("transient") and transient_attempts < _MAX_TRANSIENT_RETRIES:
                        transient_attempts += 1
                        log.warning(
                            "Transient failure (%s). Re-navigating (transient retry %d/%d).",
                            result.message, transient_attempts, _MAX_TRANSIENT_RETRIES,
                        )
                        await _navigate_to_reservation(page, cfg, login_solver)
                        continue

                    # No hour in the list has any free court — every priority slot is
                    # sold out. Refreshing won't help (the data was just fetched).
                    if chosen_hour is None:
                        out = result
                        break

                    if _is_site_rejection(result):
                        log.info("Slot %s:00 rejected by site (%s). Refreshing to re-walk priority list.",
                                 chosen_hour, result.message)
                        continue

                    # Hard error inside submit/captcha/payment that's not a site
                    # rejection (selector misconfig, OCR failure, etc.) — won't be
                    # fixed by another walk.
                    out = result
                    break
                else:
                    # Hit the safety cap.
                    out = last_failure or BookingResult(
                        False,
                        f"Hit refresh cap ({_MAX_REFRESH_ATTEMPTS}) without booking; site keeps releasing slots faster than we can claim them.",
                        {},
                    )

        assert out is not None
        return out
    finally:
        if out is not None:
            _print_result(out)
        await wait_until_user_closes_window(cfg, page)
        await dispose_context(context)


# ---------------------------------------------------------------------------
# Multi-worker support
# ---------------------------------------------------------------------------


def _find_user(cfg: AppConfig, name: str) -> UserConfig:
    # Look up a user by name in cfg.users; raises if not found (config.py validates this upfront).
    for u in cfg.users:
        if u.name == name:
            return u
    raise ValueError(f"Worker references unknown user {name!r}.")


def _apply_worker_config(cfg: AppConfig, worker: WorkerConfig, index: int, multi: bool) -> AppConfig:
    """Return a copy of cfg populated with worker-specific date/slots + referenced user's credentials.

    Multi-worker mode gets a per-user browser profile subdirectory so each user's session cookies
    persist independently across runs.
    """
    cfg = copy.deepcopy(cfg)
    user = _find_user(cfg, worker.user)
    cfg.account = user.account
    cfg.password = user.password
    cfg.login_method = user.login_method
    cfg.date = worker.date
    cfg.start_time_list = list(worker.active_start_time_list())
    # cfg.start_time / cfg.end_time are set by the runner before each attempt.
    if multi:
        cfg.user_data_dir = str(Path(cfg.user_data_dir).resolve() / f"user_{user.name}")
    return cfg


def _worker_process(
    index: int,
    worker: WorkerConfig,
    user_config_path: Path,
    site_config_path: Path,
    result_queue: multiprocessing.Queue,
) -> None:
    """Entry point for a worker process — runs the full booking flow."""
    # Child processes don't inherit the parent's logging config.
    logging.basicConfig(
        level=logging.INFO,
        format="[PID=%(process)d] [%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(user_config_path, site_config_path)
    cfg = _apply_worker_config(cfg, worker, index, multi=True)
    slots_str = ",".join(f"{h}:00" for h in cfg.start_time_list)
    label = f"Worker {index} (user={worker.user}, date={cfg.date}, slots=[{slots_str}])"
    log.info("%s starting.", label)
    try:
        result = asyncio.run(run(
            user_config_path, site_config_path,
            _config_override=cfg,
        ))
    except Exception as e:
        result = BookingResult(False, f"{label} crashed: {e}")
    result_queue.put((index, worker, result))


def _print_summary(workers: list[WorkerConfig], all_results: list[BookingResult]) -> None:
    """Print a summary table of all worker results."""
    print("\n" + "=" * 60)
    print("BOOKING SUMMARY")
    print("=" * 60)
    for i, (w, result) in enumerate(zip(workers, all_results)):
        status = "SUCCESS" if result.success else "FAILED"
        slots_str = ",".join(f"{h}:00" for h in w.active_start_time_list())
        print(f"  Worker {i} | user={w.user} date={w.date} slots=[{slots_str}] | {status} | {result.message}")
    print("=" * 60)
    succeeded = sum(1 for r in all_results if r.success)
    print(f"  {succeeded}/{len(all_results)} workers succeeded.")
    print("=" * 60 + "\n")


async def query_all_orders(
    user_config_path: Path, site_config_path: Path, limit: int,
) -> dict[str, list[Order]]:
    """Login as each configured user sequentially and fetch their N most recent paid orders.

    Sequential (not parallel) because each run reuses the persistent browser
    profile keyed by user.name. Returns a {user_name: [Order, ...]} dict and
    also prints a formatted table per user for terminal use.
    """
    cfg = load_config(user_config_path, site_config_path)
    out: dict[str, list[Order]] = {}
    for user in cfg.users:
        user_cfg = copy.deepcopy(cfg)
        user_cfg.account = user.account
        user_cfg.password = user.password
        user_cfg.login_method = user.login_method
        # Per-user browser profile so saved sessions don't collide.
        user_cfg.user_data_dir = str(Path(cfg.user_data_dir).resolve() / f"user_{user.name}")
        log.info("Querying orders for user %s …", user.name)
        try:
            orders = await fetch_user_orders(user_cfg, user, limit)
        except Exception as e:
            log.error("Failed to fetch orders for %s: %s", user.name, e)
            orders = []
        out[user.name] = orders
        print(f"\n=== {user.name}: {len(orders)} paid order(s) ===")
        print(format_orders_table(orders))
    return out


async def run_all(user_config_path: Path, site_config_path: Path) -> list[BookingResult]:
    """Run all configured workers. 1 worker = direct run; N workers = separate processes."""
    cfg = load_config(user_config_path, site_config_path)
    workers = cfg.workers

    # Single worker: run directly in the current process (no multiprocessing overhead).
    if len(workers) == 1:
        single_cfg = _apply_worker_config(cfg, workers[0], 0, multi=False)
        result = await run(user_config_path, site_config_path, _config_override=single_cfg)
        return [result]

    # Multiple workers: spawn one process per worker.
    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    processes: list[multiprocessing.Process] = []
    for i, w in enumerate(workers):
        p = multiprocessing.Process(
            target=_worker_process,
            args=(i, w, user_config_path, site_config_path, result_queue),
            name=f"booking-worker-{i}",
        )
        processes.append(p)

    log.info("Launching %d booking workers.", len(processes))
    for p in processes:
        p.start()
    for p in processes:
        p.join()

    # Collect results (order may differ from launch order).
    results_by_index: dict[int, tuple[WorkerConfig, BookingResult]] = {}
    while not result_queue.empty():
        idx, worker, result = result_queue.get_nowait()
        results_by_index[idx] = (worker, result)

    all_results: list[BookingResult] = []
    for i, w in enumerate(workers):
        if i in results_by_index:
            _, result = results_by_index[i]
        else:
            result = BookingResult(False, f"Worker {i} did not return a result.")
        all_results.append(result)

    _print_summary(workers, all_results)
    return all_results
