"""Read-only verification of the laptop's Windows daily booking task."""
from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from web.backend.config_loader import load_set
from web.backend.scheduler import compute_next_fire

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_NAME = "PKU Court Booking - Daily Run"
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _result(state: str, detail: str, daily_time: str | None = None) -> dict[str, str | None]:
    return {"state": state, "detail": detail, "daily_time": daily_time}


def daily_booking_task_status() -> dict[str, str | None]:
    """Verify enabled state, daily trigger, configured time, and action."""
    if os.name != "nt":
        return _result("unavailable", "Windows Task Scheduler is not available on this host.")
    try:
        completed = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", TASK_NAME, "/XML"],
            capture_output=True, text=True, timeout=10, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _result("unavailable", "Could not query Windows Task Scheduler.")
    if completed.returncode:
        return _result("unavailable", "Daily booking task was not found or could not be read.")
    try:
        root = ET.fromstring(completed.stdout.lstrip("\ufeff"))
        if root.findtext("t:Settings/t:Enabled", default="true", namespaces=TASK_NS).lower() == "false":
            return _result("disabled", "Daily booking task is disabled.")
        triggers = root.findall("t:Triggers/*", TASK_NS)
        daily = [trigger for trigger in triggers if trigger.findtext("t:ScheduleByDay/t:DaysInterval", namespaces=TASK_NS) == "1"
                 and trigger.findtext("t:Enabled", default="true", namespaces=TASK_NS).lower() != "false"]
        if len(triggers) != 1 or len(daily) != 1:
            return _result("misconfigured", "Expected one enabled daily trigger.")
        start = datetime.fromisoformat(daily[0].findtext("t:StartBoundary", namespaces=TASK_NS))
        actual_time = start.strftime("%H:%M:%S")
        expected_time = datetime.fromtimestamp(compute_next_fire(load_set("scheduled"))).strftime("%H:%M:%S")
        if actual_time != expected_time:
            return _result("misconfigured", f"Task is set for {actual_time}; config expects {expected_time}.", actual_time)
        actions = root.findall("t:Actions/t:Exec", TASK_NS)
        if len(actions) != 1:
            return _result("misconfigured", "Expected one booking action.", actual_time)
        action = actions[0]
        command = action.findtext("t:Command", default="", namespaces=TASK_NS)
        arguments = action.findtext("t:Arguments", default="", namespaces=TASK_NS)
        working_directory = action.findtext("t:WorkingDirectory", default="", namespaces=TASK_NS)
        expected_command = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
        if (os.path.normcase(os.path.normpath(command)) != os.path.normcase(os.path.normpath(str(expected_command)))
                or arguments.strip() != "-X utf8 -m web.backend.local_schedule"
                or os.path.normcase(os.path.normpath(working_directory)) != os.path.normcase(os.path.normpath(str(PROJECT_ROOT)))):
            return _result("misconfigured", "Daily task action does not match this installation.", actual_time)
        return _result("enabled", f"Daily booking task is enabled for {actual_time}.", actual_time)
    except (ET.ParseError, OSError, TypeError, ValueError):
        return _result("unavailable", "Could not read the daily task configuration.")
