from __future__ import annotations

import struct
import unittest

from src.booking.booking_flow import _png_dimensions, _scale_captcha_coords
from src.booking.result import BookingResult
from src.booking.runner import _is_invalid_captcha_rejection


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


if __name__ == "__main__":
    unittest.main()
