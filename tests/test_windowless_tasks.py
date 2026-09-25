from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from web.backend import local_schedule


class WindowlessTaskTests(unittest.TestCase):
    def test_daily_task_uses_file_log_without_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(local_schedule, "DATA_DIR", root),
                patch.object(local_schedule.sys, "stdout", None),
                patch.object(local_schedule, "run_once", new_callable=AsyncMock, return_value=0),
                patch.object(local_schedule.logging, "basicConfig") as configure,
            ):
                try:
                    self.assertEqual(local_schedule.main(), 0)
                    handlers = configure.call_args.kwargs["handlers"]
                    self.assertEqual(len(handlers), 1)
                    self.assertEqual(Path(handlers[0].baseFilename), root / "local_schedule_task.log")
                finally:
                    for handler in configure.call_args.kwargs["handlers"]:
                        handler.close()


if __name__ == "__main__":
    unittest.main()
