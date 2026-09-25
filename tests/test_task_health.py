from __future__ import annotations

import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from web.backend import task_health


@unittest.skipUnless(os.name == "nt", "Windows Task Scheduler check")
class DailyTaskHealthTests(unittest.TestCase):
    def _xml(self, *, enabled: str = "true", action: str = "-X utf8 -m web.backend.local_schedule", time: str = "11:57:00") -> str:
        root = task_health.PROJECT_ROOT
        return f'''<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Settings><Enabled>{enabled}</Enabled></Settings>
          <Triggers><CalendarTrigger><StartBoundary>2026-09-24T{time}+08:00</StartBoundary>
            <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
          </CalendarTrigger></Triggers>
          <Actions><Exec><Command>{root}\\.venv\\Scripts\\pythonw.exe</Command>
            <Arguments>{action}</Arguments><WorkingDirectory>{root}</WorkingDirectory>
          </Exec></Actions>
        </Task>'''

    def _status(self, xml: str):
        fire = datetime(2026, 9, 25, 11, 57).timestamp()
        with (
            patch.object(task_health.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=xml)),
            patch.object(task_health, "load_set"),
            patch.object(task_health, "compute_next_fire", return_value=fire),
        ):
            return task_health.daily_booking_task_status()

    def test_enabled_daily_task_matches_booking_config(self) -> None:
        status = self._status(self._xml())
        self.assertEqual(status["state"], "enabled")
        self.assertEqual(status["daily_time"], "11:57:00")

    def test_disabled_and_misconfigured_tasks_are_not_marked_healthy(self) -> None:
        self.assertEqual(self._status(self._xml(enabled="false"))["state"], "disabled")
        self.assertEqual(self._status(self._xml(time="11:58:00"))["state"], "misconfigured")
        self.assertEqual(self._status(self._xml(action="-m something_else"))["state"], "misconfigured")


if __name__ == "__main__":
    unittest.main()
