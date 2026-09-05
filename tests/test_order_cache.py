from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.booking.orders import Order
from web.backend.jobs import Job
from web.backend.order_cache import OrderCacheService, compute_next_order_refresh


class OrderCacheTests(unittest.TestCase):
    def test_next_refresh_is_today_before_eight(self) -> None:
        now = datetime(2026, 9, 5, 7, 30)
        actual = datetime.fromtimestamp(compute_next_order_refresh(now))
        self.assertEqual(actual, datetime(2026, 9, 5, 8, 0))

    def test_next_refresh_is_tomorrow_at_or_after_eight(self) -> None:
        now = datetime(2026, 9, 5, 8, 0)
        actual = datetime.fromtimestamp(compute_next_order_refresh(now))
        self.assertEqual(actual, datetime(2026, 9, 6, 8, 0))

    def test_cache_round_trip_and_invalid_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders_cache.json"
            service = OrderCacheService(path)
            payload = {
                "updated_at": 123.0,
                "attempted_at": 124.0,
                "orders": [{"user": "zy", "order_no": "A123"}],
                "errors": [],
            }
            service._write_cache(payload)
            self.assertEqual(service.load_cache(), payload)

            path.write_text(json.dumps({"orders": "not-a-list"}), encoding="utf-8")
            self.assertEqual(service.load_cache()["orders"], [])


class OrderCacheRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_user_keeps_previous_cached_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = OrderCacheService(Path(directory) / "orders_cache.json")
            service._write_cache({
                "updated_at": 100.0,
                "attempted_at": 100.0,
                "orders": [
                    {"user": "stz", "order_no": "OLD-STZ", "use_date": "2026-09-01"},
                    {"user": "zy", "order_no": "OLD-ZY", "use_date": "2026-09-02"},
                ],
                "errors": [],
            })
            users = [SimpleNamespace(name="stz"), SimpleNamespace(name="zy")]
            base = SimpleNamespace(users=users)

            async def fake_fetch(_cfg, user, _limit):
                if user.name == "zy":
                    raise RuntimeError("temporary failure")
                return [Order(
                    user="stz", order_no="NEW-STZ", order_type="online",
                    venue="court", use_date="2026-09-03", court_and_time="20:00",
                    pay_status="paid", order_status="confirmed", amount="40",
                    created_at="2026-09-01",
                )]

            with (
                patch("web.backend.order_cache.load_set", return_value=base),
                patch("web.backend.order_cache.per_user_config", return_value=None),
                patch("web.backend.order_cache.fetch_user_orders", side_effect=fake_fetch),
                patch("web.backend.order_cache.get_booking_lock", return_value=asyncio.Lock()),
            ):
                result = await service._fetch_and_store(Job("test", "orders:all"), 10)

            order_numbers = {order["order_no"] for order in result["orders"]}
            self.assertEqual(order_numbers, {"NEW-STZ", "OLD-ZY"})
            self.assertEqual(len(result["errors"]), 1)
            self.assertGreater(result["updated_at"], 100.0)


if __name__ == "__main__":
    unittest.main()
