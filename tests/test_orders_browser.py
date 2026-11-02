"""Offline regression for empty desktop cells and the default dashboard filter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from src.booking.config import AppConfig, UserConfig
from src.booking.orders import fetch_user_orders


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in offline browser test")
class BrowserOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_response_repairs_blank_cells_and_default_filter_shows_orders(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                requests = []
                records = [{"tradeNo": number, "venueName": "五四体育中心",
                            "siteName": "室外网球场", "reservationDateDetail": slot,
                            "phone": "DO-NOT-RETAIN"}
                           for number, slot in (("D10", "2号 21:00-22:00"), ("D1", "1号 20:00-21:00"))]
                html = '''<meta charset="utf-8"><table><tbody></tbody></table><script>
                  fetch('/venue-server/api/orders/mine').then(r=>r.json()).then(() => {
                    document.querySelector('tbody').innerHTML = ['D1','D10'].map(id =>
                    `<tr><td>1</td><td>[在线预约] ${id}</td><td></td><td>2026-11-03</td>
                     <td></td><td>已支付</td><td>正常</td><td>0</td><td>2026-11-02</td><td>查看</td></tr>`).join('');
                  });</script>'''

                async def fixture(route):
                    requests.append((route.request.method, route.request.url))
                    if route.request.url.endswith('/venue-server/api/orders/mine'):
                        await route.fulfill(content_type="application/json", body=json.dumps({"data": {"content": records}}))
                    elif route.request.url.endswith('/venue/orders'):
                        await route.fulfill(content_type="text/html", body=html)
                    elif route.request.url.endswith('/venue/home'):
                        await route.fulfill(content_type="text/html", body="<html></html>")
                    else:
                        await route.abort()

                await context.route("**/*", fixture)
                cfg = AppConfig(base_url="https://orders-fixture.test/venue/home", user_data_dir="unused")
                callback = AsyncMock()
                with (
                    patch("src.booking.orders.launch_persistent_context", new=AsyncMock(return_value=(context, None))),
                    patch("src.booking.orders.ensure_logged_in", new=AsyncMock()),
                    patch("src.booking.orders._orders_session_rejected", new=AsyncMock(return_value=False)),
                    patch("src.booking.orders._default_login_solver", return_value=object()),
                    patch("src.booking.orders.mark_session_verified"),
                ):
                    orders = await fetch_user_orders(cfg, UserConfig(name="fixture", login_method="alumni", account="test", password="test"), 10, after_fetch=callback)
                self.assertEqual([o.court_and_time for o in orders], ["1号 20:00-21:00", "2号 21:00-22:00"])
                self.assertTrue(all(o.venue == "五四体育中心室外网球场" for o in orders))
                callback.assert_awaited_once()
                self.assertEqual(len(requests), 3)  # home + orders + the site's own GET
                self.assertTrue(all(method == "GET" for method, _ in requests))

                page = await browser.new_page()
                await page.route("**/*", lambda route: route.abort())
                await page.set_content('<html><body></body></html>')
                await page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / "web/backend/static/app.js"))
                await page.evaluate('''orders => {
                  document.body.innerHTML = '<select id="venue-filter"><option selected>五四体育中心室外网球场</option></select><div id="orders-results"></div>';
                  renderOrderCards(orders);
                }''', [o.to_dict() for o in orders])
                self.assertEqual(await page.locator('.order-card').count(), 2)
                self.assertNotIn('no paid orders', await page.locator('#orders-results').inner_text())
            finally:
                await browser.close()
