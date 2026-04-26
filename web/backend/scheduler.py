"""Singleton background scheduler for the daily booking task.

Loop:
  1. Load `config/webapp/scheduled/` config.
  2. Compute fire_time = today's scheduled_time − scheduled_prep_seconds
     (or tomorrow's if today's already passed).
  3. Sleep until fire_time. State = "waiting".
  4. Truncate `data/scheduled_last_run.log`, attach log handlers, run all
     workers concurrently via runner.run. State = "running".
  5. Write `data/scheduled_last_run.json` sidecar with results. State = "waiting" again.

Concurrency rules (booking_lock + no-test window) land in M5; for M4 the
scheduler simply fires on its own clock.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.booking import runner
from src.booking.config import AppConfig
from src.booking.result import BookingResult
from web.backend.config_loader import load_set, per_user_config

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
LOG_FILE = DATA_DIR / "scheduled_last_run.log"
META_FILE = DATA_DIR / "scheduled_last_run.json"

LOG_RING_CAPACITY = 10_000
RETRY_AFTER_CONFIG_ERROR_S = 60


class _FileLogHandler(logging.Handler):
    """Append each formatted record to a plain-text file."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(self.format(record) + "\n")
        except Exception:
            pass


class _RingLogHandler(logging.Handler):
    """Append each formatted record into a bounded in-memory deque."""

    def __init__(self, ring: deque[str]) -> None:
        super().__init__()
        self.ring = ring
        self.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ring.append(self.format(record))
        except Exception:
            pass


def compute_next_fire(cfg: AppConfig, now: datetime | None = None) -> float:
    """Return epoch seconds for the next fire_time = scheduled_time − prep_seconds.

    If today's fire_time has already passed, returns tomorrow's.
    """
    now = now or datetime.now()
    s = cfg.scheduled_time
    submission = now.replace(hour=int(s[0:2]), minute=int(s[2:4]), second=int(s[4:6]), microsecond=0)
    fire = submission - timedelta(seconds=cfg.scheduled_prep_seconds)
    if fire <= now:
        fire = fire + timedelta(days=1)
    return fire.timestamp()


class Scheduler:
    """Owns the background loop, current run state, and last-run persistence."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.state: str = "waiting"            # "waiting" | "running"
        self.next_fire: float | None = None    # epoch seconds; None until first compute
        self.current_logs: deque[str] | None = None  # populated while running
        self.last_run: dict[str, Any] | None = self._load_meta_from_disk()
        self._stopped = False

    # ---- persistence ----

    def _load_meta_from_disk(self) -> dict[str, Any] | None:
        if not META_FILE.is_file():
            return None
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("scheduler: could not parse %s: %s", META_FILE, e)
            return None

    def _read_log_file(self) -> list[str]:
        if not LOG_FILE.is_file():
            return []
        try:
            return LOG_FILE.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []

    # ---- public API ----

    def status(self) -> dict[str, Any]:
        if self.state == "running" and self.current_logs is not None:
            logs = list(self.current_logs)
        else:
            logs = self._read_log_file()
        return {
            "state": self.state,
            "next_fire": self.next_fire,
            "now": time.time(),
            "last_run": self.last_run,
            "logs": logs,
            "log_total": len(logs),
        }

    async def start(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.task = asyncio.create_task(self._loop(), name="scheduler-loop")

    async def stop(self) -> None:
        self._stopped = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    # ---- loop ----

    async def _loop(self) -> None:
        log.info("scheduler: loop started.")
        while not self._stopped:
            try:
                cfg = load_set("scheduled")
            except Exception as e:
                log.exception("scheduler: failed to load scheduled config; retry in %ds: %s",
                              RETRY_AFTER_CONFIG_ERROR_S, e)
                await asyncio.sleep(RETRY_AFTER_CONFIG_ERROR_S)
                continue

            self.next_fire = compute_next_fire(cfg)
            self.state = "waiting"
            wait = max(0.0, self.next_fire - time.time())
            fire_str = datetime.fromtimestamp(self.next_fire).strftime("%Y-%m-%d %H:%M:%S")
            log.info("scheduler: next fire %s (sleeping %.0fs).", fire_str, wait)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break

            # Reload cfg right before firing so 'N-days-later' dates resolve
            # against the *fire date*, not the date the webapp booted on.
            try:
                fresh_cfg = load_set("scheduled")
            except Exception:
                log.exception("scheduler: failed to reload config at fire time; using stale cfg.")
                fresh_cfg = cfg

            try:
                await self._fire(fresh_cfg)
            except Exception:
                log.exception("scheduler: unexpected error during fire.")

    async def _fire(self, cfg: AppConfig) -> None:
        """One scheduled run: capture logs, run all workers concurrently, persist meta."""
        self.state = "running"
        self.current_logs = deque(maxlen=LOG_RING_CAPACITY)
        LOG_FILE.write_text("", encoding="utf-8")  # truncate
        booking_log = logging.getLogger("src.booking")
        my_log = logging.getLogger(__name__)
        file_h = _FileLogHandler(LOG_FILE)
        ring_h = _RingLogHandler(self.current_logs)
        for lg in (booking_log, my_log):
            lg.addHandler(file_h)
            lg.addHandler(ring_h)

        started_at = time.time()
        log.info("scheduler: firing scheduled run; %d worker(s) will start in parallel.",
                 len(cfg.workers))
        results: list[BookingResult] = []
        errors: list[str] = []
        try:
            results = await self._run_workers(cfg)
            log.info("scheduler: run finished. %d/%d worker(s) succeeded.",
                     sum(1 for r in results if r.success), len(results))
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            log.exception("scheduler: run aborted with exception.")
        finally:
            for lg in (booking_log, my_log):
                lg.removeHandler(file_h)
                lg.removeHandler(ring_h)
            finished_at = time.time()
            self.last_run = {
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_s": round(finished_at - started_at, 1),
                "any_success": any(r.success for r in results),
                "worker_count": len(results),
                "results": [self._serialize_result(r) for r in results],
                "errors": errors,
            }
            try:
                META_FILE.write_text(
                    json.dumps(self.last_run, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                log.warning("scheduler: could not write %s: %s", META_FILE, e)
            self.state = "waiting"
            self.current_logs = None  # waiting view will re-read from disk

    @staticmethod
    def _serialize_result(r: BookingResult) -> dict[str, Any]:
        d = asdict(r)
        # Ensure JSON-safe (nested details may contain non-serializable types).
        try:
            json.dumps(d)
            return d
        except TypeError:
            return {"success": r.success, "message": r.message,
                    "details": {"_repr": repr(r.details)}}

    async def _run_workers(self, cfg: AppConfig) -> list[BookingResult]:
        """Run each worker's booking flow concurrently in the same process.

        Each worker gets its own per-user browser profile dir, so persistent
        contexts don't lock each other. We can't reuse runner.run_all here:
        it spawns child processes for N>1, which would break log capture.
        """
        if not cfg.workers:
            log.info("scheduler: no workers configured; skipping.")
            return []
        coros = []
        for w in cfg.workers:
            user = next((u for u in cfg.users if u.name == w.user), None)
            if user is None:
                # _parse_workers should have caught this at load_set time, but be defensive.
                log.warning("scheduler: worker references unknown user %r; skipping.", w.user)
                continue
            wcfg = per_user_config(cfg, user)
            wcfg.date = w.date
            wcfg.start_time_list = list(w.start_time_list)
            wcfg.headless = True
            # Force scheduled_mode on regardless of YAML — without it the
            # booking flow would submit immediately on the (still-stale) page
            # instead of waiting at the reservation page for scheduled_time.
            wcfg.scheduled_mode = True
            coros.append(self._run_one(wcfg, w.user))
        return await asyncio.gather(*coros)

    async def _run_one(self, cfg: AppConfig, user_name: str) -> BookingResult:
        try:
            return await runner.run(
                user_config_path=Path("/dev/null"),
                site_config_path=Path("/dev/null"),
                _config_override=cfg,
            )
        except Exception as e:
            log.exception("scheduler: worker %s crashed.", user_name)
            return BookingResult(False, f"worker {user_name} crashed: {e}")


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
