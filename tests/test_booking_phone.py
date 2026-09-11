from __future__ import annotations

import unittest

from src.booking.booking_flow import _prepare_booking_phone
from src.booking.config import AppConfig, _parse_users


class _PhoneInput:
    def __init__(self, value: str = "") -> None:
        self.value = value

    @property
    def first(self) -> "_PhoneInput":
        return self

    def locator(self, selector: str) -> "_PhoneInput":
        return self

    async def is_visible(self) -> bool:
        return True

    async def input_value(self) -> str:
        return self.value

    async def fill(self, value: str) -> None:
        self.value = value


class _Page:
    def __init__(self) -> None:
        self.phone = _PhoneInput()

    def get_by_text(self, text: str, exact: bool = False) -> _PhoneInput:
        return self.phone


class BookingPhoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_student_phone_is_filled_from_private_account_config(self) -> None:
        page = _Page()
        cfg = AppConfig(
            base_url="https://example.test/venue/home",
            user_data_dir=".browser_profile",
            login_method="student",
            account="student-id",
            booking_phone="13800000000",
        )

        error = await _prepare_booking_phone(page, cfg)

        self.assertIsNone(error)
        self.assertEqual(page.phone.value, "13800000000")

    async def test_missing_student_phone_returns_configuration_error(self) -> None:
        page = _Page()
        cfg = AppConfig(
            base_url="https://example.test/venue/home",
            user_data_dir=".browser_profile",
            login_method="student",
            account="student-id",
        )

        error = await _prepare_booking_phone(page, cfg)

        self.assertIsNotNone(error)
        self.assertFalse(error.success)
        self.assertEqual(error.details["configuration"], "booking_phone")

    async def test_alumni_login_phone_is_used_as_booking_phone(self) -> None:
        page = _Page()
        cfg = AppConfig(
            base_url="https://example.test/venue/home",
            user_data_dir=".browser_profile",
            login_method="alumni",
            account="13900000000",
        )

        error = await _prepare_booking_phone(page, cfg)

        self.assertIsNone(error)
        self.assertEqual(page.phone.value, "13900000000")


class BookingPhoneConfigTests(unittest.TestCase):
    def test_invalid_configured_phone_is_rejected_at_load_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "11-digit"):
            _parse_users({
                "users": [{
                    "name": "student",
                    "login_method": "student",
                    "account": "student-id",
                    "password": "secret",
                    "booking_phone": "123",
                }]
            })


if __name__ == "__main__":
    unittest.main()
