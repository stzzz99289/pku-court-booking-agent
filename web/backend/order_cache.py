"""Persistent order cache with a daily 08:00 background refresh."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.booking.orders import Order, fetch_user_orders
from web.backend.config_loader import load_set, per_user_config
from web.backend.jobs import Job, get_booking_lock, get_job_manager

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
ORDER_CACHE_FILE = DATA_DIR / "orders_cache.json"
ORDER_REFRESH_HOUR = 8
DEFAULT_ORDER_LIMIT = 10


def compute_next_order_refresh(now: datetime | None = None) -> float:
    """Return the next local 08:00 refresh as epoch seconds."""
    now = now or datetime.now()
    refresh = now.replace(hour=ORDER_REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if refresh <= now:
        refresh += timedelta(days=1)
    return refresh.timestamp()


class OrderCacheService:
    """Own the durable cache and its lightweight daily scheduler."""

    def __init__(self, cache_file: Path = ORDER_CACHE_FILE) -> None:
        self.cache_file = cache_file
        self.task: asyncio.Task | None = None
        self.next_refresh: float | None = None
        self.current_job_id: str | None = None
        self._stopped = False

    @staticmethod
    def _empty_cache() -> dict[str, Any]:
        return {
            "updated_at": None,
            "attempted_at": None,
            "orders": [],
            "errors": [],
        }

    def load_cache(self) -> dict[str, Any]:
        if not self.cache_file.is_file():
            return self._empty_cache()
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("orders"), list)
                or not all(isinstance(order, dict) for order in data["orders"])
            ):
                raise ValueError("invalid cache payload")
            return {**self._empty_cache(), **data}
        except Exception as exc:
            log.warning("order cache: could not read %s: %s", self.cache_file, exc)
            return self._empty_cache()

    def _write_cache(self, data: dict[str, Any]) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.cache_file.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_file.replace(self.cache_file)

    def status(self) -> dict[str, Any]:
        data = self.load_cache()
        active_job_id: str | None = None
        if self.current_job_id:
            job = get_job_manager().get(self.current_job_id)
            if job and job.status in {"pending", "running"}:
                active_job_id = job.id
        return {
            **data,
            "next_refresh": self.next_refresh,
            "active_job_id": active_job_id,
        }

    async def start(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._stopped = False
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="order-cache-loop")

    async def stop(self) -> None:
        self._stopped = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    def start_refresh(self, limit: int = DEFAULT_ORDER_LIMIT) -> Job:
        """Start a refresh, or return the already-running refresh job."""
        if self.current_job_id:
            current = get_job_manager().get(self.current_job_id)
            if current and current.status in {"pending", "running"}:
                return current

        async def _run(job: Job) -> dict[str, Any]:
            return await self._fetch_and_store(job, limit)

        job = get_job_manager().start(
            "orders:all", _run, capture_logger_names=(),
        )
        self.current_job_id = job.id
        return job

    async def _fetch_and_store(self, job: Job, limit: int) -> dict[str, Any]:
        base = load_set("test")
        if not base.users:
            raise RuntimeError("no users configured")

        previous = self.load_cache()
        previous_by_user: dict[str, list[dict[str, Any]]] = {}
        for order in previous["orders"]:
            previous_by_user.setdefault(str(order.get("user", "")), []).append(order)

        combined: list[dict[str, Any]] = []
        errors: list[str] = []
        successful_users = 0
        seen: set[tuple[str, str]] = set()

        # Order queries and booking runs share persistent browser profiles.
        async with get_booking_lock():
            for user in base.users:
                job.append_log(f"[orders] fetching for user={user.name}")
                cfg = per_user_config(base, user)
                try:
                    orders: list[Order] = await fetch_user_orders(cfg, user, limit)
                    user_orders = [order.to_dict() for order in orders]
                    successful_users += 1
                except Exception as exc:
                    message = f"{user.name}: {type(exc).__name__}: {exc}"
                    errors.append(message)
                    job.append_log(f"[orders] {message}; keeping cached results")
                    user_orders = previous_by_user.get(user.name, [])

                added = 0
                for order in user_orders:
                    order_no = str(order.get("order_no", ""))
                    key = (user.name, order_no)
                    if not order_no or key in seen:
                        continue
                    seen.add(key)
                    combined.append(order)
                    added += 1
                job.append_log(f"[orders] {user.name}: {added} order(s)")

        combined.sort(key=lambda order: str(order.get("use_date", "")), reverse=True)
        attempted_at = time.time()
        updated_at = attempted_at if successful_users else previous.get("updated_at")
        result = {
            "updated_at": updated_at,
            "attempted_at": attempted_at,
            "orders": combined,
            "errors": errors,
            "count": len(combined),
        }
        self._write_cache(result)
        log.info(
            "order cache: refresh finished; %d/%d user(s) succeeded, %d order(s).",
            successful_users, len(base.users), len(combined),
        )
        return result

    async def _loop(self) -> None:
        log.info("order cache: daily refresh loop started.")
        while not self._stopped:
            now = datetime.now()
            today_refresh = now.replace(
                hour=ORDER_REFRESH_HOUR, minute=0, second=0, microsecond=0,
            )
            attempted_at = self.load_cache().get("attempted_at")
            try:
                attempted_today = bool(
                    attempted_at
                    and datetime.fromtimestamp(float(attempted_at)) >= today_refresh
                )
            except (TypeError, ValueError, OSError):
                attempted_today = False

            # Catch up immediately after a restart if today's 08:00 refresh
            # was missed, but make only one automatic attempt per day.
            if now >= today_refresh and not attempted_today:
                job = self.start_refresh()
                if job.task is not None:
                    try:
                        await job.task
                    except asyncio.CancelledError:
                        raise

            self.next_refresh = compute_next_order_refresh()
            refresh_text = datetime.fromtimestamp(self.next_refresh).strftime("%Y-%m-%d %H:%M:%S")
            wait = max(0.0, self.next_refresh - time.time())
            log.info("order cache: next refresh %s (sleeping %.0fs).", refresh_text, wait)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break


_service: OrderCacheService | None = None


def get_order_cache() -> OrderCacheService:
    global _service
    if _service is None:
        _service = OrderCacheService()
    return _service
