from __future__ import annotations

import asyncio
import json
import struct
import unittest

from src.booking.booking_flow import (
    _captcha_network_diagnostics,
    _png_dimensions,
    _scale_captcha_coords,
    start_captcha_network_capture,
)
from src.booking.result import BookingResult
from src.booking.runner import _daily_booking_limit_result, _is_invalid_captcha_rejection


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)


class CaptchaCoordinateTests(unittest.TestCase):
    def test_png_dimensions_reads_ihdr(self) -> None:
        self.assertEqual(_png_dimensions(_png_header(600, 300)), (600, 300))

    def test_solver_pixels_scale_to_target_coordinate_space(self) -> None:
        self.assertEqual(
            _scale_captcha_coords([(300, 150), (50, 75)], (600, 300), (300.0, 150.0)),
            [(150.0, 75.0), (25.0, 37.5)],
        )

    def test_out_of_bounds_solver_coordinate_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            _scale_captcha_coords([(600, 10)], (600, 300), (300.0, 150.0))

    def test_invalid_captcha_rejection_is_classified(self) -> None:
        self.assertTrue(_is_invalid_captcha_rejection(BookingResult(
            False, "Booking rejected by site: 验证码非法校验", {},
        )))
        self.assertFalse(_is_invalid_captcha_rejection(BookingResult(
            False, "Booking rejected by site: 验证码次数超出限制", {},
        )))

    def test_daily_booking_limit_stops_with_clear_reason(self) -> None:
        result = _daily_booking_limit_result(BookingResult(
            False, "Booking rejected by site: 每天只能预约2次", {"url": "test"},
        ))
        self.assertIsNotNone(result)
        self.assertEqual(result.details["reason"], "daily_booking_limit")
        self.assertIn("2 courts per account", result.message)
        self.assertIsNone(_daily_booking_limit_result(BookingResult(
            False, "Booking rejected by site: 该场地已被其他人预约", {},
        )))

    def test_captcha_api_diagnostics_are_correlatable_and_redacted(self) -> None:
        class Request:
            resource_type = "xhr"
            method = "POST"
            headers = {"authorization": "Bearer private-session-token-123456789"}

            def __init__(self, payload):
                self.post_data_json = payload

        class Response:
            status = 200

            def __init__(self, path, request_payload, response_payload):
                self.url = f"https://example.test{path}"
                self.request = Request(request_payload)
                self.response_payload = response_payload

            async def json(self):
                return self.response_payload

        token = "secret-verification-token-1234567890"
        responses = [
            Response(
                "/venue-server/api/captcha/check",
                {"captchaToken": "secret-challenge-token-123456789", "pointJson": "private"},
                {"code": 0, "msg": "验证成功", "data": {"captchaVerification": token}},
            ),
            Response(
                "/venue-server/api/reservation/order/submit",
                {"captchaVerification": token, "phone": "13800138000", "password": "private"},
                {"code": 400, "message": "验证码非法校验 13800138000"},
            ),
        ]
        captured = [(f"2026-09-26T12:00:0{i}.000", response)
                    for i, response in enumerate(responses)]
        events = asyncio.run(_captcha_network_diagnostics(captured, b"ephemeral-test-key"))
        self.assertEqual(events[0]["response"]["root.code"], 0)
        self.assertEqual(events[1]["response"]["root.message"], "验证码非法校验 [phone]")
        self.assertEqual(events[0]["authorizationCorrelation"],
                         events[1]["authorizationCorrelation"])
        self.assertEqual(
            events[0]["response"]["data.captchaVerification"]["correlation"],
            events[1]["request"]["root.captchaVerification"]["correlation"],
        )
        serialized = json.dumps(events, ensure_ascii=False)
        for secret in (token, "secret-challenge-token-123456789", "13800138000", "private"):
            self.assertNotIn(secret, serialized)

    def test_capture_includes_initial_challenge_but_excludes_other_endpoints(self) -> None:
        class Page:
            def on(self, name, callback):
                self.callback = callback

        class Request:
            resource_type = "xhr"

        class Response:
            request = Request()

            def __init__(self, path):
                self.url = f"https://example.test{path}"

        page = Page()
        captured, _ = start_captcha_network_capture(page)
        page.callback(Response("/venue-server/api/captcha/get"))
        page.callback(Response("/venue-server/api/some-unrelated-endpoint"))
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0][1].url.endswith("/captcha/get"))


if __name__ == "__main__":
    unittest.main()
