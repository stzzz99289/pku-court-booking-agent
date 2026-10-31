from __future__ import annotations

import asyncio
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.booking.booking_flow import select_booking_date
from src.booking.config import AppConfig
from src.booking.date_labels import date_button_matches, parse_booking_date
from src.booking.runner import _refresh_until_target_date_visible
from src.booking.site_constants import BOOKING_DATE_BUTTON_SELECTOR


class DateLabelTests(unittest.TestCase):
    def test_current_and_legacy_labels(self):
        for text in ("星期日\n11/01", "星期日 11月01日", "11/1", "11月1日", "11/01\n明天"):
            with self.subTest(text=text):
                self.assertTrue(date_button_matches(text, date(2026, 11, 1)))

    def test_different_dates_and_substrings_do_not_match(self):
        for text in ("10/31", "11/02", "01/11", "111/01", "11/011", "11月11日", "", "2026/11/01"):
            with self.subTest(text=text):
                self.assertFalse(date_button_matches(text, date(2026, 11, 1)))
        self.assertFalse(date_button_matches("11/01", date(2027, 1, 1)))
        self.assertFalse(date_button_matches("11月01日", date(2027, 1, 1)))

    def test_config_dates_are_strict_and_calendar_valid(self):
        for value in ("", "2026111", "202611010", "2026-11-01", "20260229", "20261301", "abcdefgh"):
            with self.subTest(value=value):
                self.assertIsNone(parse_booking_date(value))
        self.assertEqual(parse_booking_date(" 20261101 "), date(2026, 11, 1))
        self.assertEqual(parse_booking_date("20280229"), date(2028, 2, 29))
        self.assertTrue(date_button_matches("星期五\n01/01", date(2027, 1, 1)))


class _ResponseWait:
    async def __aenter__(self):
        self.value = asyncio.get_running_loop().create_future()
        self.value.set_result(SimpleNamespace(json=AsyncMock(return_value={"data": {}})))
        return self

    async def __aexit__(self, *_args):
        return False


def _config(value="20261101"):
    return AppConfig(base_url="https://date-fixture.test", user_data_dir="unused", date=value)


def _page(labels, active_index=-1):
    buttons = [SimpleNamespace(
        inner_text=AsyncMock(return_value=text),
        get_attribute=AsyncMock(return_value="active" if i == active_index else ""),
        click=AsyncMock(),
    ) for i, text in enumerate(labels)]
    row = SimpleNamespace(count=AsyncMock(return_value=len(buttons)), nth=lambda i: buttons[i])
    table = SimpleNamespace(nth=lambda _i: SimpleNamespace(wait_for=AsyncMock()))
    page = SimpleNamespace(
        locator=Mock(side_effect=lambda selector: row if selector == BOOKING_DATE_BUTTON_SELECTOR else table),
        expect_response=Mock(side_effect=lambda *_a, **_k: _ResponseWait()),
    )
    return page, buttons


class DateSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_correct_current_and_legacy_button(self):
        for labels in (["星期六\n10/31", "星期日\n11/01"], ["10月31日", "11月01日"]):
            with self.subTest(labels=labels):
                page, buttons = _page(labels)
                self.assertIsNone(await select_booking_date(page, _config()))
                buttons[0].click.assert_not_awaited()
                buttons[1].click.assert_awaited_once_with(timeout=3_000)

    async def test_already_active_current_label_does_not_wait_for_another_response(self):
        page, buttons = _page(["10/31", "11/01"], active_index=1)
        self.assertIsNone(await select_booking_date(page, _config()))
        page.expect_response.assert_not_called()
        buttons[1].click.assert_not_awaited()

    async def test_missing_date_does_not_click_another_date(self):
        page, buttons = _page(["10/31", "11/02"])
        result = await select_booking_date(page, _config())
        self.assertFalse(result.success)
        self.assertIn("2026-11-01", result.message)
        self.assertIn("11/02", result.message)
        for button in buttons:
            button.click.assert_not_awaited()

    async def test_invalid_config_stops_before_accessing_page(self):
        page, _ = _page(["11/01"])
        result = await select_booking_date(page, _config("20260229"))
        self.assertFalse(result.success)
        page.locator.assert_not_called()


class DateReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def _refresh(self, visible_rows):
        page = SimpleNamespace(reload=AsyncMock(), expect_response=lambda *_a, **_k: _ResponseWait())
        with (
            patch("src.booking.runner._wait_for_date_buttons_after_refresh", new=AsyncMock(return_value=True)),
            patch("src.booking.runner._read_date_button_texts", new=AsyncMock(side_effect=visible_rows)),
            patch("src.booking.runner.asyncio.sleep", new=AsyncMock()),
        ):
            await _refresh_until_target_date_visible(page, _config())
        return page.reload.await_count

    async def test_current_and_legacy_date_stop_after_one_refresh(self):
        for label in ("星期日\n11/01", "11月01日"):
            with self.subTest(label=label):
                self.assertEqual(await self._refresh([["10/31", label]]), 1)

    async def test_not_released_date_retries_until_it_appears(self):
        self.assertEqual(await self._refresh([["10/31"], ["10/31", "11/01"]]), 2)

    async def test_missing_date_keeps_the_existing_three_refresh_cap(self):
        self.assertEqual(await self._refresh([["10/31"]] * 3), 3)
