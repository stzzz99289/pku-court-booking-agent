"""Opt-in Chromium date-selection regression; only synthetic local responses."""
from __future__ import annotations

import json
import os
import unittest

from playwright.async_api import async_playwright

from src.booking.booking_flow import select_booking_date
from src.booking.config import AppConfig
from src.booking.runner import _refresh_until_target_date_visible


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in offline browser test")
class BrowserDateSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_detection_and_selection_with_real_chromium(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                for target_label in ("11/01", "11月01日"):
                    with self.subTest(label=target_label):
                        context = await browser.new_context()
                        requests = []
                        html = """<!doctype html><html><head><meta charset="utf-8"></head><body>
                        <div class="date_box">
                          <div class="active">星期六<br>10/31</div>
                          <div id="tomorrow">星期日<br>LABEL</div>
                        </div>
                        <div class="spaceTable"><table><thead><tr><td>场地</td><td>10:00</td></tr></thead></table></div>
                        <script>
                          const tomorrow = document.querySelector('#tomorrow');
                          tomorrow.onclick = async () => {
                            await fetch('/reservation/day/info?date=20261101');
                            document.querySelector('.active').classList.remove('active');
                            tomorrow.classList.add('active');
                          };
                          fetch('/reservation/day/info?date=20261031');
                        </script></body></html>""".replace("LABEL", target_label)

                        async def fixture(route):
                            url = route.request.url
                            requests.append(url)
                            if url == "https://date-fixture.test/reservation":
                                await route.fulfill(content_type="text/html", body=html)
                            elif url.startswith("https://date-fixture.test/reservation/day/info?"):
                                await route.fulfill(content_type="application/json", body=json.dumps({
                                    "data": {
                                        "spaceTimeInfo": [{"id": 1, "beginTime": "10:00"}],
                                        "reservationDateSpaceInfo": {"20261101": [{"1": {"reservationStatus": 1}}]},
                                    },
                                }))
                            else:
                                await route.abort()

                        await context.route("**/*", fixture)
                        page = await context.new_page()
                        await page.goto("https://date-fixture.test/reservation")
                        cfg = AppConfig(base_url="https://date-fixture.test", user_data_dir="unused", date="20261101")
                        await _refresh_until_target_date_visible(page, cfg)
                        # Initial navigation plus exactly one release refresh, not three.
                        self.assertEqual(requests.count("https://date-fixture.test/reservation"), 2)
                        self.assertIsNone(await select_booking_date(page, cfg))
                        await page.locator("#tomorrow.active").wait_for()
                        self.assertEqual(cfg.cached_free_slots, {10: [0]})
                        self.assertEqual(sum("date=20261101" in url for url in requests), 1)
                        # An active date should not generate a second day-info request.
                        self.assertIsNone(await select_booking_date(page, cfg))
                        self.assertEqual(sum("date=20261101" in url for url in requests), 1)
                        await context.close()
            finally:
                await browser.close()
