from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.booking.session_verification import (
    MARKER_NAME,
    last_session_verified,
    mark_session_verified,
)


class SessionVerificationTests(unittest.TestCase):
    def test_missing_marker_means_never_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(last_session_verified(directory))

    def test_mark_records_explicit_verification_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mark_session_verified(directory)

            marker = Path(directory) / MARKER_NAME
            self.assertTrue(marker.is_file())
            verified_at = last_session_verified(directory)
            self.assertIsInstance(verified_at, datetime)

    def test_marker_failure_does_not_interrupt_booking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("pathlib.Path.touch", side_effect=OSError("read only")):
                mark_session_verified(directory)
