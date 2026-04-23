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
    confirm_payment,
    select_booking_date,
    select_court_time,
    solve_booking_captcha,
)
from .browser import dispose_context, launch_persistent_context, wait_until_user_closes_window
from .captcha import ManualCaptchaSolver
from .config import AppConfig, UserConfig, WorkerConfig, load_config
from .login import ensure_logged_in
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
    """Navigate to the venue-reservation page, re-authenticating if a login modal appears."""
    url = _reservation_url(cfg)
    log.info("Navigating to venue reservation: %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    log.info("Landed on: %s", page.url)

    # Detect and dismiss the "please login" modal (appears quickly if session is invalid).
    modal = page.get_by_text("请登录后访问", exact=True)
    try:
        await modal.wait_for(state="visible", timeout=1_500)
    except Exception:
        return  # Modal did not appear — already logged in.
    log.info("'Please login' modal detected — dismissing and re-authenticating.")
    await page.get_by_role("button", name="确定").first.click()
    await ensure_logged_in(page, cfg, login_solver)
    log.info("Re-navigating to venue reservation after login.")
    await page.goto(url, wait_until="domcontentloaded")
    log.info("Landed on: %s", page.url)


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
    """Sleep until scheduled_time, then refresh the page."""
    target = _parse_scheduled_time(cfg.scheduled_time)
    remaining = (target - datetime.now()).total_seconds()
    if remaining > 0:
        log.info(
            "Scheduled mode: waiting %.1f s until %s …",
            remaining, target.strftime("%H:%M:%S"),
        )
        await asyncio.sleep(remaining)
    log.info("Scheduled time reached — refreshing page.")
    # Wait for both the page DOM and the schedule API response after reload.
    async with page.expect_response(
        lambda r: "reservation/day/info" in r.url, timeout=15_000
    ) as resp_info:
        await page.reload(wait_until="domcontentloaded")
    try:
        await resp_info.value
        log.info("Schedule data loaded after refresh.")
    except Exception:
        log.warning("Did not capture reservation/day/info response after reload; proceeding anyway.")
    # Wait for Vue app to render the date buttons after reload.
    try:
        await page.locator(".date_box > div").first.wait_for(state="visible", timeout=10_000)
    except Exception:
        log.warning("Date buttons did not appear within 10 s after reload.")


def _print_result(out: BookingResult) -> None:
    # Print the booking result message and any extra details to stdout.
    print(out.message)
    if out.details:
        print(out.details)


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

                    # Stage 5: solve click-captcha (if it appeared) then confirm payment.
                    if await page.locator(".verifybox").first.is_visible():
                        if cfg.debug or click_solver is None:
                            out = BookingResult(
                                True,
                                "Click-captcha appeared (请完成安全验证). Debug mode — solve it manually or close the browser.",
                                {"stopped_at": "captcha"},
                            )
                        else:
                            captcha_err = await solve_booking_captcha(page, click_solver, cfg)
                            if captcha_err is not None:
                                out = captcha_err
                            else:
                                # Stage 6: confirm payment (free → done, paid → manual).
                                # confirm_payment may switch `page` to a newly opened
                                # payment tab and close the old reservation tab.
                                page, out = await confirm_payment(page, cfg)
                    else:
                        page, out = await confirm_payment(page, cfg)

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
    """Return a copy of cfg populated with worker-specific date/time + referenced user's credentials.

    Multi-worker mode gets a per-user browser profile subdirectory so each user's session cookies
    persist independently across runs.
    """
    cfg = copy.deepcopy(cfg)
    user = _find_user(cfg, worker.user)
    cfg.account = user.account
    cfg.password = user.password
    cfg.login_method = user.login_method
    cfg.date = worker.date
    cfg.start_time = worker.start_time
    cfg.end_time = worker.end_time
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
    label = f"Worker {index} (user={worker.user}, date={cfg.date}, {cfg.start_time}:00-{cfg.end_time}:00)"
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
        print(f"  Worker {i} | user={w.user} date={w.date} {w.start_time}:00-{w.end_time}:00 | {status} | {result.message}")
    print("=" * 60)
    succeeded = sum(1 for r in all_results if r.success)
    print(f"  {succeeded}/{len(all_results)} workers succeeded.")
    print("=" * 60 + "\n")


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
