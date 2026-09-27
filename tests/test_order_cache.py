from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.booking.orders import Order
from web.backend.jobs import Job
from web.backend.order_cache import (
    PKU_TIMEZONE, OrderCacheService, compute_next_order_refresh,
    order_is_cancellable,
)


class OrderCacheTests(unittest.TestCase):
    def test_next_refresh_is_today_before_thirteen(self) -> None:
        now = datetime(2026, 9, 5, 12, 30)
        actual = datetime.fromtimestamp(compute_next_order_refresh(now))
        self.assertEqual(actual, datetime(2026, 9, 5, 13, 0))

    def test_next_refresh_is_tomorrow_at_or_after_thirteen(self) -> None:
        now = datetime(2026, 9, 5, 13, 0)
        actual = datetime.fromtimestamp(compute_next_order_refresh(now))
        self.assertEqual(actual, datetime(2026, 9, 6, 13, 0))

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

    def test_only_active_paid_future_order_is_cancellable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = OrderCacheService(Path(directory) / "orders_cache.json")
            tomorrow = (datetime.now(PKU_TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d")
            service._write_cache({"orders": [
                {"user": "stz", "order_no": "TARGET", "use_date": tomorrow,
                 "court_and_time": "1号 20:00-21:00",
                 "pay_status": "已支付", "order_status": "正常"},
                {"user": "zy", "order_no": "OTHER", "use_date": tomorrow,
                 "court_and_time": "3号 21:00-22:00",
                 "pay_status": "已支付", "order_status": "已取消"},
            ], "errors": []})
            self.assertIsNotNone(service.cancellable_order("stz", "TARGET"))
            self.assertIsNone(service.cancellable_order("zy", "OTHER"))
            self.assertIsNone(service.cancellable_order("stz", "OTHER"))
            service.mark_canceled("stz", "TARGET")
            self.assertIsNone(service.cancellable_order("stz", "TARGET"))
            marked = service.load_cache()["orders"][0]
            self.assertEqual(marked["order_status"], "已取消")
            self.assertEqual(marked["pay_status"], "已支付")
            self.assertEqual(marked["cancel_state"], "canceled")
            self.assertFalse(marked["can_cancel"])
            self.assertIsNone(marked["proof_url"])

    def test_today_later_slot_is_cancellable_but_started_slot_is_not(self) -> None:
        now = datetime(2026, 9, 27, 10, 30, tzinfo=PKU_TIMEZONE)
        order = {
            "use_date": "2026-09-27", "court_and_time": "1号 20:00-21:00",
            "pay_status": "已支付", "order_status": "正常",
        }
        self.assertTrue(order_is_cancellable(order, now))
        self.assertFalse(order_is_cancellable({**order, "court_and_time": "1号 09:00-10:00"}, now))
        self.assertFalse(order_is_cancellable({**order, "court_and_time": "unknown"}, now))
        self.assertFalse(order_is_cancellable({**order, "order_status": "已取消"}, now))

    def test_failed_cancel_job_remains_visible_after_page_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = OrderCacheService(Path(directory) / "orders_cache.json")
            service._write_cache({"orders": [{"user": "stz", "order_no": "TARGET"}], "errors": []})
            service.cancel_jobs[("stz", "TARGET")] = "failed-job"
            failed = Job("failed-job", "orders:cancel", status="failed")
            with patch("web.backend.order_cache.get_job_manager") as manager:
                manager.return_value.get.return_value = failed
                status = service.status()
            self.assertEqual(status["orders"][0]["cancel_job_id"], "failed-job")


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

            async def fake_fetch(_cfg, user, _limit, **_kwargs):
                if user.name == "zy":
                    raise RuntimeError("temporary failure")
                return [Order(
                    user="stz", order_no="NEW-STZ", order_type="online",
                    venue="court", use_date="2026-09-03", court_and_time="20:00",
                    pay_status="已支付", order_status="正常", amount="40",
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

    async def test_suspicious_empty_result_keeps_previous_cached_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = OrderCacheService(Path(directory) / "orders_cache.json")
            service._write_cache({
                "updated_at": 100.0,
                "attempted_at": 100.0,
                "orders": [
                    {"user": "stz", "order_no": "KNOWN", "use_date": "2026-09-11"},
                ],
                "errors": [],
            })
            user = SimpleNamespace(name="stz")
            base = SimpleNamespace(users=[user])

            with (
                patch("web.backend.order_cache.load_set", return_value=base),
                patch("web.backend.order_cache.per_user_config", return_value=None),
                patch("web.backend.order_cache.fetch_user_orders", return_value=[]),
                patch("web.backend.order_cache.get_booking_lock", return_value=asyncio.Lock()),
            ):
                result = await service._fetch_and_store(Job("test", "orders:all"), 10)

            self.assertEqual(
                [order["order_no"] for order in result["orders"]], ["KNOWN"],
            )
            self.assertEqual(result["updated_at"], 100.0)
            self.assertIn("keeping cached results", result["errors"][0])

    async def test_canceled_order_disappears_even_if_live_query_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = OrderCacheService(Path(directory) / "orders_cache.json")
            service._write_cache({
                "updated_at": 100.0, "attempted_at": 100.0,
                "orders": [{"user": "stz", "order_no": "CANCELED",
                            "use_date": "2026-09-28", "cancel_state": "canceled"}],
                "errors": [],
            })
            base = SimpleNamespace(users=[SimpleNamespace(name="stz")])
            with (
                patch("web.backend.order_cache.load_set", return_value=base),
                patch("web.backend.order_cache.per_user_config", return_value=None),
                patch("web.backend.order_cache.fetch_user_orders", return_value=[]),
                patch("web.backend.order_cache.get_booking_lock", return_value=asyncio.Lock()),
            ):
                result = await service._fetch_and_store(Job("test", "orders:all"), 10)
            self.assertEqual(result["orders"], [])

    async def test_cancel_job_is_deduplicated_and_marks_only_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = OrderCacheService(Path(directory) / "orders_cache.json")
            tomorrow = (datetime.now(PKU_TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d")
            service._write_cache({"orders": [
                {"user": "stz", "order_no": "TARGET", "use_date": tomorrow,
                 "court_and_time": "1号 20:00-21:00",
                 "pay_status": "已支付", "order_status": "正常"},
                {"user": "zy", "order_no": "OTHER", "use_date": tomorrow,
                 "court_and_time": "3号 21:00-22:00",
                 "pay_status": "已支付", "order_status": "正常"},
            ], "errors": []})
            base = SimpleNamespace(users=[SimpleNamespace(name="stz")])

            async def fake_cancel(_cfg, _order_no, *, on_stage):
                on_stage("completed", "site confirmed")
                await asyncio.sleep(0)
                return "confirmed"

            with (
                patch("web.backend.order_cache.load_set", return_value=base),
                patch("web.backend.order_cache.per_user_config", return_value=None),
                patch("web.backend.order_cache.cancel_user_order", side_effect=fake_cancel) as cancel,
                patch("web.backend.order_cache.get_booking_lock", return_value=asyncio.Lock()),
            ):
                first = service.start_cancel("stz", "TARGET")
                second = service.start_cancel("stz", "TARGET")
                self.assertIs(first, second)
                await first.task
            cancel.assert_awaited_once()
            self.assertEqual(cancel.await_args.args, (None, "TARGET"))
            self.assertEqual(first.status, "succeeded")
            self.assertEqual(first.stage, "completed")
            orders = service.load_cache()["orders"]
            self.assertEqual(orders[0]["cancel_state"], "canceled")
            self.assertEqual(orders[1]["order_status"], "正常")


if __name__ == "__main__":
    unittest.main()
