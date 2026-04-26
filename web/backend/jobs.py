"""Minimal in-process job manager.

A `Job` wraps an `asyncio.Task` plus a per-job ring-buffer of log lines so the
frontend can poll `/api/jobs/{id}` for status + tail. M2 only needs this for
the orders-refresh flow; later milestones (M3+) reuse the same primitive for
booking runs and attach a `logging.Handler` to capture src.booking logs.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

JOB_LOG_CAPACITY = 2000


@dataclass
class Job:
    id: str
    kind: str
    status: str = "pending"            # pending | running | succeeded | failed
    started_at: float = 0.0
    finished_at: float = 0.0
    result: Any = None
    error: str = ""
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=JOB_LOG_CAPACITY))
    task: asyncio.Task | None = None

    def append_log(self, line: str) -> None:
        self.logs.append(line)

    def to_dict(self, log_offset: int = 0) -> dict[str, Any]:
        all_logs = list(self.logs)
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "logs": all_logs[log_offset:],
            "log_total": len(all_logs),
        }


class JobLogHandler(logging.Handler):
    """Tee log records emitted by `src.booking` into a Job's ring buffer."""

    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job
        self.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.append_log(self.format(record))
        except Exception:
            pass


class JobManager:
    """Tracks running jobs and exposes status/log lookups for HTTP polling."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_by_kind(self, kind: str) -> list[Job]:
        return [j for j in self._jobs.values() if j.kind == kind]

    def latest_by_kind(self, kind: str) -> Job | None:
        jobs = sorted(self.list_by_kind(kind), key=lambda j: j.started_at, reverse=True)
        return jobs[0] if jobs else None

    def start(
        self,
        kind: str,
        coro_factory: Callable[[Job], Awaitable[Any]],
        capture_logger_names: tuple[str, ...] = ("src.booking",),
    ) -> Job:
        """Launch `coro_factory(job)` as a background task; capture its logs."""
        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, kind=kind, status="pending")
        self._jobs[job_id] = job
        handler = JobLogHandler(job)
        captured_loggers = [logging.getLogger(name) for name in capture_logger_names]

        async def _runner() -> None:
            for lg in captured_loggers:
                lg.addHandler(handler)
            job.status = "running"
            job.started_at = time.time()
            job.append_log(f"[job {job_id} started: {kind}]")
            try:
                job.result = await coro_factory(job)
                job.status = "succeeded"
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.append_log(f"[job failed] {job.error}")
                log.exception("Job %s (%s) failed", job_id, kind)
            finally:
                job.finished_at = time.time()
                for lg in captured_loggers:
                    lg.removeHandler(handler)
                job.append_log(f"[job finished: {job.status}]")

        job.task = asyncio.create_task(_runner(), name=f"job-{kind}-{job_id}")
        return job


_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager


_booking_lock: asyncio.Lock | None = None


def get_booking_lock() -> asyncio.Lock:
    """Process-wide lock held for the duration of any booking flow.

    Lazy-built: asyncio.Lock binds to the running event loop on first use.
    Both the Tab 2 test run and the scheduled run acquire it, so they can
    never overlap and fight for the shared per-user browser profile.
    """
    global _booking_lock
    if _booking_lock is None:
        _booking_lock = asyncio.Lock()
    return _booking_lock
