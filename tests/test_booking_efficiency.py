from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.booking.booking_flow import _read_court_row_labels
from src.booking.config import AppConfig, SelectorConfig
from src.booking.result import BookingResult
from src.booking.runner import run


class BookingEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_labels_use_one_browser_call_and_preserve_row_positions(self):
        rows = SimpleNamespace(evaluate_all=AsyncMock(return_value=["", "场地", "1号", "2号"]))
        self.assertEqual(await _read_court_row_labels(rows), ["", "场地", "1号", "2号"])
        rows.evaluate_all.assert_awaited_once()

    async def test_transient_retry_navigates_only_once_after_optional_relogin(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                cfg = AppConfig(base_url="https://fixture.test", user_data_dir="unused",
                                start_time_list=["13"], selectors=SelectorConfig(login_button="login"))
                page = object()
                events = []

                async def navigate(*_args):
                    events.append("navigate")

                async def login(*_args, **kwargs):
                    events.append("relogin" if kwargs.get("force") else "login")

                outcomes = iter([
                    BookingResult(False, "transient", {"transient": True, "session_expired": expired}),
                    BookingResult(True, "fixture success"),
                ])

                async def attempt(*_args):
                    events.append("attempt")
                    return page, next(outcomes), None

                with (
                    patch("src.booking.runner._make_solvers", return_value=(object(), None)),
                    patch("src.booking.runner.launch_persistent_context", new=AsyncMock(return_value=(SimpleNamespace(pages=[page]), None))),
                    patch("src.booking.runner._goto_with_retry", new=AsyncMock()),
                    patch("src.booking.runner.login_automation_ready", return_value=True),
                    patch("src.booking.runner.ensure_logged_in", new=login),
                    patch("src.booking.runner._navigate_to_reservation", new=navigate),
                    patch("src.booking.runner._attempt_book_from_priority_list", new=attempt),
                    patch("src.booking.runner.wait_until_user_closes_window", new=AsyncMock()),
                    patch("src.booking.runner.dispose_context", new=AsyncMock()),
                ):
                    result = await run(Path("unused"), Path("unused"), _config_override=cfg)
                self.assertTrue(result.success)
                self.assertEqual(events, ["login", "navigate", "attempt"] +
                                 (["relogin"] if expired else []) + ["navigate", "attempt"])
