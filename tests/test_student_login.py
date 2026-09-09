from __future__ import annotations

import unittest
from unittest.mock import patch

from src.booking.config import AppConfig, SelectorConfig
from src.booking.login import _iaaa_login
from src.booking.pipeline import login_automation_ready


class _FakeLocator:
    def __init__(self, selector: str, events: list[tuple[str, str, str]]) -> None:
        self.selector = selector
        self.events = events

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def click(self) -> None:
        self.events.append(("click", self.selector, ""))

    async def fill(self, value: str) -> None:
        self.events.append(("fill", self.selector, value))


class _FakePage:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.waited_urls = [
            "https://epe.pku.edu.cn/ggtypt/login?service=callback",
            "https://epe.pku.edu.cn/venue/home",
        ]

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector, self.events)

    async def wait_for_url(self, predicate, timeout: int) -> None:
        url = self.waited_urls.pop(0)
        if not predicate(url):
            raise TimeoutError(url)

    async def wait_for_load_state(self, state: str) -> None:
        self.events.append(("wait", state, ""))


def _student_config(method: str = "student") -> AppConfig:
    return AppConfig(
        base_url="https://epe.pku.edu.cn/venue/home",
        user_data_dir=".browser_profile",
        account="student-id",
        password="secret",
        login_method=method,
        selectors=SelectorConfig(
            login_button="login-button",
            login_mode_iaaa="student-tab",
            iaaa_login_link="iaaa-link",
            iaaa_username_input="iaaa-user",
            iaaa_password_input="iaaa-password",
            iaaa_submit="iaaa-submit",
        ),
    )


class StudentLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_iaaa_login_uses_dedicated_form_selectors(self) -> None:
        page = _FakePage()

        with patch("src.booking.login.mark_session_verified") as mark_verified:
            await _iaaa_login(page, _student_config())

        mark_verified.assert_called_once_with(".browser_profile")

        self.assertEqual(
            page.events,
            [
                ("click", "student-tab", ""),
                ("click", "iaaa-link", ""),
                ("fill", "iaaa-user", "student-id"),
                ("fill", "iaaa-password", "secret"),
                ("click", "iaaa-submit", ""),
                ("wait", "domcontentloaded", ""),
            ],
        )

    async def test_missing_student_selector_fails_before_typing_credentials(self) -> None:
        cfg = _student_config()
        cfg.selectors.iaaa_submit = ""
        page = _FakePage()

        with self.assertRaisesRegex(ValueError, "IAAA submit"):
            await _iaaa_login(page, cfg)
        self.assertEqual(page.events, [])


class StudentPipelineTests(unittest.TestCase):
    def test_student_and_iaaa_alias_are_ready(self) -> None:
        self.assertTrue(login_automation_ready(_student_config("student")))
        self.assertTrue(login_automation_ready(_student_config("iaaa")))

    def test_student_requires_all_dedicated_selectors(self) -> None:
        cfg = _student_config()
        cfg.selectors.iaaa_password_input = ""
        self.assertFalse(login_automation_ready(cfg))


if __name__ == "__main__":
    unittest.main()
