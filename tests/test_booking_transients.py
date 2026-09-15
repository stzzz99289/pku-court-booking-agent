from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.booking.booking_flow import select_booking_date, select_court_time
from src.booking.config import AppConfig, SelectorConfig


class _ResponseWait:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


def _config() -> AppConfig:
    return AppConfig(
        base_url="https://epe.pku.edu.cn/venue/home",
        user_data_dir=".browser_profile/test-transient",
        account="account",
        password="password",
        date="20260916",
        start_time="20",
        end_time="21",
        selectors=SelectorConfig(),
    )


class TransientScheduleModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_date_click_format_error_is_dismissed_and_retried(self) -> None:
        button = SimpleNamespace(
            inner_text=AsyncMock(return_value="星期三\n09月16日"),
            get_attribute=AsyncMock(return_value=""),
            click=AsyncMock(side_effect=RuntimeError("click intercepted")),
        )
        buttons = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _index: button)
        page = SimpleNamespace(
            locator=lambda _selector: buttons,
            expect_response=lambda *_args, **_kwargs: _ResponseWait(),
            evaluate=AsyncMock(side_effect=["返回数据格式不正确", True]),
        )

        result = await select_booking_date(page, _config())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.success)
        self.assertTrue(result.details["transient"])
        self.assertEqual(result.details["site_error"], "返回数据格式不正确")
        self.assertEqual(page.evaluate.await_count, 2)

    async def test_late_format_error_stops_schedule_interaction(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=["返回数据格式不正确", True]),
        )

        result = await select_court_time(page, _config())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.success)
        self.assertTrue(result.details["transient"])
        self.assertEqual(result.details["target_time"], "20:00-21:00")
        self.assertEqual(page.evaluate.await_count, 2)


if __name__ == "__main__":
    unittest.main()
