"""Synthetic Chromium selection tests; no live reservation or external request."""
from __future__ import annotations

import os
import unittest

from playwright.async_api import async_playwright

from src.booking.booking_flow import _read_court_row_labels, select_court_time
from src.booking.config import AppConfig


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in offline browser test")
class BrowserCourtSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_labels_preserve_cache_indices_and_dom_priorities(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", lambda route: route.abort())
                for target, expected in ((1, "2"), (-1, "3"), (8, "3")):
                    with self.subTest(target=target):
                        await page.set_content('''<meta charset="utf-8"><div class="spaceTable"><table>
                        <thead><tr><td>场地</td><td>13:00-14:00</td></tr></thead><tbody>
                        <tr><th></th></tr><tr><td>场地</td><td>13:00-14:00</td></tr>
                        <tr><td>1号</td><td><div class="reserveBlock reserved">已售</div></td></tr>
                        <tr><td>2号</td><td onclick="document.body.dataset.selected='2'"><div class="reserveBlock free">空闲</div></td></tr>
                        <tr><td>3号</td><td onclick="document.body.dataset.selected='3'"><div class="reserveBlock free">空闲</div></td></tr>
                        </tbody></table></div>''')
                        self.assertEqual(await _read_court_row_labels(page.locator("tbody tr")),
                                         ["", "场地", "1号", "2号", "3号"])
                        cfg = AppConfig(base_url="https://fixture.test", user_data_dir="unused",
                                        start_time="13", end_time="14", target_court_index=target,
                                        court_priority=[2])
                        self.assertIsNone(await select_court_time(page, cfg))
                        self.assertEqual(await page.locator("body").get_attribute("data-selected"), expected)
            finally:
                await browser.close()
