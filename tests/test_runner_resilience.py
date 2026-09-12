from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.booking.config import AppConfig, SelectorConfig
from src.booking.diagnostics import dump_crash_diagnostics
from src.booking.login import ensure_logged_in
from src.booking.runner import _read_date_button_texts, run


class LoginRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_session_discovered_while_opening_form_stops_login(self) -> None:
        cfg = AppConfig(
            base_url="https://epe.pku.edu.cn/venue/home",
            user_data_dir=".browser_profile/test-login-race",
            account="account",
            password="password",
            login_method="alumni",
            selectors=SelectorConfig(),
        )
        page = object()
        with (
            patch("src.booking.login._session_looks_logged_in", new=AsyncMock(return_value=False)),
            patch("src.booking.login._open_login_form", new=AsyncMock(return_value=False)),
            patch("src.booking.login._alumni_login", new=AsyncMock()) as alumni_login,
        ):
            await ensure_logged_in(page, cfg, object())

        alumni_login.assert_not_awaited()


class DateRowRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_date_row_is_read_with_one_atomic_locator_operation(self) -> None:
        locator = SimpleNamespace(all_inner_texts=AsyncMock(return_value=["09月12日\n今天", "09月13日"]))
        page = SimpleNamespace(locator=lambda _selector: locator)

        self.assertEqual(
            await _read_date_button_texts(page),
            ["09月12日\n今天", "09月13日"],
        )
        locator.all_inner_texts.assert_awaited_once_with()


class CrashDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_crash_without_a_page_still_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.booking.diagnostics._CRASH_DUMP_DIR", Path(directory)):
                base = await dump_crash_diagnostics(
                    None, label="browser launch", metadata={"exception": "launch failed"},
                )

            self.assertIsNotNone(base)
            assert base is not None
            payload = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(payload["metadata"]["exception"], "launch failed")
            self.assertEqual(payload["screenshot_error"], "page unavailable")

    async def test_crash_bundle_contains_page_and_error_evidence(self) -> None:
        class Page:
            url = "https://epe.pku.edu.cn/venue/home"

            async def screenshot(self, *, path: str, **_kwargs) -> None:
                Path(path).write_bytes(b"png")

            async def content(self) -> str:
                return "<html>failure page</html>"

            async def title(self) -> str:
                return "Venue"

            async def evaluate(self, _script: str) -> list[dict[str, str]]:
                return [{"text": "failure dialog"}]

        with tempfile.TemporaryDirectory() as directory:
            with patch("src.booking.diagnostics._CRASH_DUMP_DIR", Path(directory)):
                base = await dump_crash_diagnostics(
                    Page(), label="worker 0", metadata={"stage": "login", "exception": "boom"},
                )

            self.assertIsNotNone(base)
            assert base is not None
            self.assertEqual(base.with_suffix(".png").read_bytes(), b"png")
            self.assertEqual(base.with_suffix(".html").read_text(encoding="utf-8"), "<html>failure page</html>")
            payload = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(payload["metadata"]["stage"], "login")
            self.assertEqual(payload["visible_dialogs"], [{"text": "failure dialog"}])

    async def test_unhandled_runner_error_is_captured_before_context_disposal(self) -> None:
        cfg = AppConfig(
            base_url="https://epe.pku.edu.cn/venue/home",
            user_data_dir=".browser_profile/scheduled_worker_0_stz",
            account="account",
            password="password",
            selectors=SelectorConfig(login_button="login"),
        )
        page = object()
        context = SimpleNamespace(pages=[page])
        events: list[str] = []

        async def capture(*_args, **_kwargs) -> None:
            events.append("capture")

        async def dispose(*_args, **_kwargs) -> None:
            events.append("dispose")

        with (
            patch("src.booking.runner._make_solvers", return_value=(object(), None)),
            patch("src.booking.runner.launch_persistent_context", new=AsyncMock(return_value=(context, None))),
            patch("src.booking.runner._goto_with_retry", new=AsyncMock()),
            patch("src.booking.runner.login_automation_ready", return_value=True),
            patch("src.booking.runner.ensure_logged_in", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch("src.booking.runner.dump_crash_diagnostics", side_effect=capture) as diagnostics,
            patch("src.booking.runner.wait_until_user_closes_window", new=AsyncMock()),
            patch("src.booking.runner.dispose_context", side_effect=dispose),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await run(Path("unused-user.yaml"), Path("unused-site.yaml"), _config_override=cfg)

        diagnostics.assert_awaited_once()
        self.assertEqual(events, ["capture", "dispose"])


if __name__ == "__main__":
    unittest.main()
