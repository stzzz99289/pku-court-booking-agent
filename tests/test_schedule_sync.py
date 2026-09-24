from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web.backend import app as webapp
from web.backend import schedule_sync
from web.backend.local_schedule import _within_prep_window
from web.backend.scheduler import Scheduler


class ScheduleModeTests(unittest.TestCase):
    def test_display_webapp_has_no_booking_route(self) -> None:
        routes = {(route.path, method) for route in webapp.app.routes for method in getattr(route, "methods", ())}
        self.assertNotIn(("/run", "GET"), routes)
        self.assertNotIn(("/api/bookings/run", "POST"), routes)
        self.assertIn(("/schedule", "GET"), routes)

    def test_one_shot_run_accepts_only_the_preparation_window(self) -> None:
        cfg = SimpleNamespace(scheduled_time="120000", scheduled_prep_seconds=180)
        self.assertFalse(_within_prep_window(cfg, datetime(2026, 9, 25, 11, 56, 40)))
        self.assertTrue(_within_prep_window(cfg, datetime(2026, 9, 25, 11, 57, 15)))
        self.assertFalse(_within_prep_window(cfg, datetime(2026, 9, 25, 12, 0)))

    def test_public_config_contains_no_account_credentials(self) -> None:
        user = SimpleNamespace(name="zy", account="15500001111", password="secret-password")
        worker = SimpleNamespace(
            user="zy", date="20260925", court_priority=[0],
            active_start_time_list=lambda: ["20"],
        )
        cfg = SimpleNamespace(
            scheduled_time="120000", scheduled_prep_seconds=180,
            venue_id="85", users=[user], workers=[worker],
        )
        with patch("web.backend.schedule_sync.load_set", return_value=cfg):
            snapshot = schedule_sync.schedule_config_snapshot(date(2026, 9, 24))
        serialized = json.dumps(snapshot)
        self.assertNotIn(user.account, serialized)
        self.assertNotIn(user.password, serialized)
        self.assertEqual(snapshot["workers"][0]["user"], "zy")


class ScheduleTransportTests(unittest.TestCase):
    def test_report_is_replaced_atomically_and_old_report_survives_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "schedule_display.json"
            report = {
                "schema": 1, "source": "laptop", "created_at": 1,
                "config": {"workers": []}, "last_run": None, "logs": [],
                "last_verified": {},
            }
            with (
                patch.object(schedule_sync, "DATA_DIR", root),
                patch.object(schedule_sync, "REPORT_FILE", report_path),
            ):
                schedule_sync.receive("report", json.dumps(report).encode())
                saved = report_path.read_bytes()
                with self.assertRaises(ValueError):
                    schedule_sync.receive("report", b'{"schema":1,"source":"laptop","logs":"bad"}')
                self.assertEqual(report_path.read_bytes(), saved)
                self.assertIsInstance(json.loads(saved)["received_at"], (int, float))

    def test_seven_day_profile_window_does_not_touch_crashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            profiles.mkdir()
            old = profiles / "scheduled_20200101.json"
            old.write_text("{}", encoding="utf-8")
            today = profiles / f"scheduled_{date.today():%Y%m%d}.json"
            today.write_text("{}", encoding="utf-8")
            crash = root / "crashes" / "20200101" / "error.png"
            crash.parent.mkdir(parents=True)
            crash.write_bytes(b"png")
            with patch("web.backend.scheduler.PROFILES_DIR", profiles):
                Scheduler._prune_profiles()
            self.assertFalse(old.exists())
            self.assertTrue(today.exists())
            self.assertTrue(crash.exists())

    def test_missing_daily_report_and_stale_heartbeat_are_visible(self) -> None:
        fixed = datetime(2026, 9, 25, 12, 20, tzinfo=schedule_sync.SHANGHAI)
        epoch = fixed.timestamp()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            heartbeat_path = root / "heartbeat.json"
            report_path.write_text(json.dumps({
                "received_at": epoch - 86400,
                "config": {"scheduled_time": "120000", "scheduled_prep_seconds": 180, "workers": []},
                "last_run": {"finished_at": epoch - 86400},
                "logs": [], "last_verified": {},
            }), encoding="utf-8")
            heartbeat_path.write_text(json.dumps({
                "received_at": epoch - 6 * 3600, "state": "idle", "host": "test-laptop",
            }), encoding="utf-8")
            with (
                patch.object(schedule_sync, "REPORT_FILE", report_path),
                patch.object(schedule_sync, "HEARTBEAT_FILE", heartbeat_path),
                patch.object(schedule_sync, "datetime", Clock),
                patch("web.backend.schedule_sync.time.time", return_value=epoch),
            ):
                status = schedule_sync.display_status()
            self.assertEqual(status["state"], "no report today")
            self.assertFalse(status["laptop_alive"])
            self.assertTrue(status["today_report_missing"])


if __name__ == "__main__":
    unittest.main()
