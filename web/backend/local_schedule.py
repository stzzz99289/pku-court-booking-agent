"""One-shot scheduled booking on a laptop, launched by the OS task scheduler."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

from web.backend.config_loader import load_set
from web.backend.jobs import configure_secret_redaction
from web.backend.schedule_sync import publish_report, send_heartbeat
from web.backend.scheduler import DATA_DIR, Scheduler

log = logging.getLogger(__name__)
COOLDOWN_SECONDS = 300
PUBLISH_RETRIES = 3


class _RunLock:
    """Refuse a second local invocation while one task already owns the run."""

    def __enter__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.file = (DATA_DIR / "local_schedule.lock").open("a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.seek(0)
            self.file.write(b"1")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("a local scheduled run is already active") from None
        return self

    def __exit__(self, *_):
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()


def _within_prep_window(cfg, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    text = cfg.scheduled_time
    submit = now.replace(hour=int(text[:2]), minute=int(text[2:4]), second=int(text[4:6]), microsecond=0)
    prepare = submit - timedelta(seconds=cfg.scheduled_prep_seconds)
    # A one-minute grace period tolerates Task Scheduler launch jitter. It
    # never starts late at noon or catches up after a sleeping laptop wakes.
    return prepare - timedelta(seconds=10) <= now < submit


async def _send_state(state: str) -> None:
    try:
        await asyncio.to_thread(send_heartbeat, state)
    except Exception as exc:
        log.warning("Could not upload laptop state %s: %s", state, exc)


async def run_once(*, cooldown_seconds: int = COOLDOWN_SECONDS) -> int:
    cfg = load_set("scheduled")
    if not _within_prep_window(cfg):
        log.error("Outside the scheduled preparation window; no booking was attempted.")
        return 2
    configure_secret_redaction([
        cfg.captcha.username, cfg.captcha.api_key, cfg.captcha.softid,
        *(value for user in cfg.users for value in (user.account, user.password)),
    ])
    with _RunLock():
        scheduler = Scheduler()
        # Upload without holding up Chromium preparation. The 4-hour routine
        # heartbeat is deliberately absent from the noon booking window.
        start_upload = asyncio.create_task(_send_state("running"))
        try:
            await scheduler._fire(cfg)
        except Exception:
            log.exception("Local scheduler failed outside worker handling")
            await _send_state("failed")
            return 1
        await start_upload
        await _send_state("cooling_down")
        await asyncio.sleep(cooldown_seconds)
        published = False
        for attempt in range(PUBLISH_RETRIES):
            try:
                await asyncio.to_thread(publish_report, force=True)
                published = True
                break
            except Exception as exc:
                log.warning("Schedule report upload attempt %d failed: %s", attempt + 1, exc)
                if attempt + 1 < PUBLISH_RETRIES:
                    await asyncio.sleep(30)
        await _send_state("completed" if published else "failed")
        if not published:
            log.error("The booking run finished locally but its report is pending upload.")
            return 3
        return 0


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(DATA_DIR / "local_schedule_task.log", mode="w", encoding="utf-8"),
    ]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    try:
        return asyncio.run(run_once())
    except Exception:
        log.exception("Local scheduled task crashed before completing the run.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
