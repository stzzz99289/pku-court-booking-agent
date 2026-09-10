from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.booking.browser import launch_persistent_context


class _FakeContext:
    pass


class _LaunchTracker:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0

    async def launch_persistent_context(self, **_kwargs) -> _FakeContext:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return _FakeContext()


class _FakePlaywright:
    def __init__(self, tracker: _LaunchTracker) -> None:
        self.chromium = tracker

    async def stop(self) -> None:
        pass


class _FakeStarter:
    def __init__(self, tracker: _LaunchTracker) -> None:
        self.tracker = tracker

    async def start(self) -> _FakePlaywright:
        return _FakePlaywright(self.tracker)


class BrowserLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_process_launches_are_bounded(self) -> None:
        tracker = _LaunchTracker()
        with tempfile.TemporaryDirectory() as directory:
            configs = [
                SimpleNamespace(
                    user_data_dir=str(Path(directory) / str(i)),
                    headless=True,
                    browser=SimpleNamespace(slow_mo_ms=0),
                )
                for i in range(5)
            ]
            with (
                patch("src.booking.browser._launch_limiter", None),
                patch(
                    "src.booking.browser.async_playwright",
                    side_effect=lambda: _FakeStarter(tracker),
                ),
            ):
                await asyncio.gather(*(launch_persistent_context(cfg) for cfg in configs))

        self.assertEqual(tracker.maximum, 2)


if __name__ == "__main__":
    unittest.main()
