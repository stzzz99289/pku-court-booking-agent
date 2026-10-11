"""Optional offline Chromium integration: no traffic to the real booking site.

RUN_BROWSER_TESTS=1 enables this fixture. Routing exists ONLY in the test to
provide synthetic responses; the production observer never routes requests.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from playwright.async_api import async_playwright

from src.booking.captcha_observation import ObservationRun, attach
from src.booking.result import BookingResult


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in offline browser test")
class BrowserObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_chromium_request_lifecycle_and_cookie_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            widget = b"window.fixtureWidget = true;"
            digest = hashlib.sha256(widget).hexdigest()
            run = ObservationRun(Path(directory), 1.0)
            run.site_study = {"algorithm_confirmed": True, "algorithm": {"source_sha256": digest},
                              "assets": [{"path": "/widget.js", "sha256": digest}]}
            activation = run.activate()
            try:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)
                    context = await browser.new_context()
                    await context.add_cookies([{"name": "session", "value": "fake-private-cookie", "url": "https://observation.test"}])

                    async def fixture(route):
                        path = route.request.url.split("observation.test", 1)[1].split("?", 1)[0]
                        if path == "/home":
                            await route.fulfill(content_type="text/html", body='<script src="/widget.js"></script>')
                        elif path == "/widget.js":
                            await route.fulfill(content_type="text/javascript", body=widget)
                        else:
                            payload = {"code": 200, "data": {"repCode": "0000", "success": True}}
                            if path.endswith("/captcha/get"):
                                payload["data"]["repData"] = {"token": "fake-private-token", "secretKey": ""}
                            await route.fulfill(content_type="application/json", body=json.dumps(payload))
                    await context.route("https://observation.test/**", fixture)
                    worker = attach(context, SimpleNamespace(worker_index=0, account="fake-private-account",
                        login_method="alumni", date="20261013", start_time_list=["20"], base_url="https://observation.test/home"))
                    page = await context.new_page()
                    await page.goto("https://observation.test/home")
                    worker.attempt = 1
                    await page.evaluate("""async () => {
                        await fetch('/venue-server/api/captcha/get?clientUid=fake-private-client&captchaType=clickWord');
                        const points = '[{"x":10,"y":20}]';
                        const headers = {'Content-Type': 'application/json', cgAuthorization: 'fake-private-auth'};
                        await fetch('/venue-server/api/captcha/check', {method:'POST', headers,
                            body:JSON.stringify({token:'fake-private-token',pointJson:points})});
                        await fetch('/venue-server/api/reservation/order/submit', {method:'POST',headers,
                            body:JSON.stringify({captchaToken:'fake-private-token',captchaVerification:'fake-private-token---'+points})});
                    }""")
                    await worker.finish(BookingResult(True, "fixture only"), "booking_attempt_1")
                    await browser.close()
                payload = json.loads(run.write().read_text(encoding="utf-8"))
            finally:
                run.deactivate(activation)
            events = payload["workers"][0]["events"]
            self.assertEqual(len(events), 3)
            self.assertIn("cookie", events[1]["session_fingerprints"])
            self.assertIn("cgauthorization", events[1]["session_fingerprints"])
            self.assertTrue(events[-1]["consistency"]["verification_generation_match"])
            self.assertTrue(payload["workers"][0]["widget_asset_match"])
            self.assertNotIn("fake-private", json.dumps(payload))
