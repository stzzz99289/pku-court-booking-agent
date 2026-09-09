from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from src.booking.orders import Order, _scrape_orders_table, _wait_for_order_rows


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
