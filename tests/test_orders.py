from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.booking.orders import (
    Order, _OrderDetailsCapture, _apply_order_details, _order_display_details,
    _scrape_orders_table, _wait_for_order_rows,
)


def _order(order_no: str = "D1") -> Order:
    return Order(
        user="stz",
        order_no=order_no,
        order_type="online",
        venue="court",
        use_date="2026-09-11",
        court_and_time="20:00",
        pay_status="已支付",
        order_status="confirmed",
        amount="40",
        created_at="2026-09-10",
    )


class _RowsLocator:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows

    async def evaluate_all(self, _script: str) -> list[list[str]]:
        return self.rows


class _RowsPage:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows
        self.selector: str | None = None

    def locator(self, selector: str) -> _RowsLocator:
        self.selector = selector
        return _RowsLocator(self.rows)


class OrderTableTests(unittest.IsolatedAsyncioTestCase):
    async def test_passive_capture_is_exact_origin_path_and_read_only(self):
        capture = _OrderDetailsCapture("https://fixture.test/venue/home")
        payload = {"data": {"content": [{"tradeNo": "D1", "venueName": "A",
                    "siteName": "B", "reservationDateDetail": "1号 20:00-21:00",
                    "phone": "PRIVATE", "orderUid": "PRIVATE"}]}}
        response = SimpleNamespace(url="https://fixture.test/venue-server/api/orders/mine?page=0",
                                   status=200, request=SimpleNamespace(method="GET"),
                                   json=AsyncMock(return_value=payload))
        for url, method, status in (
            ("https://other.test/venue-server/api/orders/mine", "GET", 200),
            ("http://fixture.test/venue-server/api/orders/mine", "GET", 200),
            ("https://fixture.test/venue-server/api/orders/submit", "GET", 200),
            (response.url, "POST", 200), (response.url, "GET", 401),
        ):
            capture.on_response(SimpleNamespace(url=url, request=SimpleNamespace(method=method), status=status))
        self.assertEqual(capture.tasks, [])
        capture.on_response(response)
        await capture.finish()
        self.assertEqual(capture.details, {"D1": {"venue": "AB", "court_and_time": "1号 20:00-21:00"}})
        self.assertNotIn("PRIVATE", str(capture.details))

    async def test_malformed_response_is_ignored(self):
        for payload in (None, [], {}, {"data": None}, {"data": {"content": "bad"}},
                        {"data": {"content": [None, {}, {"tradeNo": 12}]}}):
            self.assertEqual(_order_display_details(payload), {})

    async def test_fill_only_blank_fields_by_exact_order_id(self):
        first, second = _order("D1"), _order("D10")
        first.venue = first.court_and_time = ""
        _apply_order_details([first, second], {
            "D10": {"venue": "WRONG", "court_and_time": "WRONG"},
            "D1": {"venue": "right", "court_and_time": "2号 20:00-21:00"},
        })
        self.assertEqual((first.venue, first.court_and_time), ("right", "2号 20:00-21:00"))
        self.assertEqual((second.venue, second.court_and_time), ("court", "20:00"))

    async def test_incomplete_active_order_fails_instead_of_poisoning_cache(self):
        order = _order()
        order.order_status, order.venue = "正常", ""
        with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
            _apply_order_details([order], {"OTHER": {"venue": "wrong"}})

    async def test_scrape_uses_all_matching_rows_in_one_snapshot(self) -> None:
        page = _RowsPage([[
            "1", "[在线预约] D123", "网球场", "2026-09-11", "1号 20:00",
            "已支付", "正常", "40", "2026-09-10", "查看",
        ]])

        rows = await _scrape_orders_table(page, "stz")

        self.assertEqual(page.selector, "table tbody tr")
        self.assertEqual([row.order_no for row in rows], ["D123"])

    async def test_wait_retries_a_transient_empty_vue_snapshot(self) -> None:
        expected = [_order()]
        with (
            patch(
                "src.booking.orders._scrape_orders_table",
                new=AsyncMock(side_effect=[[], expected]),
            ) as scrape,
            patch("src.booking.orders.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            rows = await _wait_for_order_rows(object(), "stz", timeout_s=1.0)

        self.assertEqual(rows, expected)
        self.assertEqual(scrape.await_count, 2)
        sleep.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
