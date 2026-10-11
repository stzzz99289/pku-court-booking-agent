from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.booking import captcha_observation as observation
from src.booking.booking_flow import _summarize_captcha_api_payload
from src.booking.result import BookingResult
from scripts.analyze_captcha_observation import analyze


def encrypted(value, key):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    padder = PKCS7(128).padder()
    padded = padder.update(value.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key.encode()), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


class Context:
    def __init__(self):
        self.listeners = {}

    def on(self, name, callback):
        self.listeners[name] = callback

    def remove_listener(self, name, callback):
        self.listeners.pop(name, None)


class Request:
    def __init__(self, path, payload=None, headers=None, resource_type="xhr"):
        self.url = "https://epe.pku.edu.cn" + path
        self.method = "POST" if payload is not None else "GET"
        self.resource_type = resource_type
        self.post_data_json = payload
        self.private_headers = headers or {"cookie": "a=secret-cookie; b=other", "authorization": "private-session", "cgauthorization": "private-cg-session"}
        self.failure = "net::ERR_CONNECTION_RESET https://example.test/?password=secret"
        self.reply = None

    async def all_headers(self):
        return self.private_headers

    async def response(self):
        return self.reply


class Response:
    def __init__(self, request, payload):
        self.request = request
        request.reply = self
        self.url = request.url
        self.status = 200
        self.headers = {"x-request-id": "opaque-secret-id"}
        self.payload = payload

    async def body(self):
        return self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()


def cfg(index=0):
    return SimpleNamespace(worker_index=index, account="private-account", login_method="alumni",
                           date="20261013", start_time_list=["20"], base_url="https://epe.pku.edu.cn/venue/home")


class PayloadTests(unittest.TestCase):
    def test_analyzer_separates_intermediate_invalid_from_final_rate_limit(self):
        event = {"id": "w0-r1", "path": "/venue-server/api/reservation/order/submit",
                 "start_offset_ms": 1, "request": {"root.captchaToken": {"correlation": "test-fingerprint"}},
                 "response": {"root.message": "验证码非法校验"}}
        second = {**event, "id": "w0-r2", "start_offset_ms": 2,
                  "response": {"root.message": "验证码次数超出限制"}}
        result = analyze({"workers": [{"worker_index": 0, "account_fingerprint": "test-account-fingerprint",
                         "outcome": {"reason": "captcha_rate_limit"}, "events": [event, second]}]})
        self.assertEqual(result["invalid_submit_count"], 1)
        self.assertEqual(result["rate_limit_submit_count"], 1)
        self.assertEqual(result["worker_outcomes"], {"captcha_rate_limit": 1})
        self.assertEqual(result["duplicate_token_submit_ids"], [["w0-r1", "w0-r2"]])

    def test_nested_tokens_and_actual_business_code_are_safe(self):
        payload = {"code": 200, "data": {"repCode": "0000", "repMsg": "OK", "success": True,
                   "repData": {"token": "private-token", "secretKey": "private-key", "originalImageBase64": "private-image"}}}
        result = _summarize_captcha_api_payload(payload, b"test-only-key")
        self.assertEqual(result["data.repCode"], "0000")
        self.assertIn("data.repData.token", result)
        text = json.dumps(result)
        for value in ("private-token", "private-key", "private-image"):
            self.assertNotIn(value, text)

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "optional debug dependency")
    def test_encrypted_verification_consistency_and_mismatch(self):
        key = "testkey123456789"
        points = '[{"x":10,"y":20},{"x":30,"y":40}]'
        check = {"token": "test-token", "pointJson": encrypted(points, key)}
        submit = {"captchaToken": "test-token", "captchaVerification": encrypted("test-token---" + points, key)}
        result = observation.verification_consistency(check, submit, key)
        self.assertTrue(result["verification_generation_match"])
        self.assertTrue(result["point_json_valid"])
        submit["captchaVerification"] = "incorrect"
        self.assertFalse(observation.verification_consistency(check, submit, key)["verification_generation_match"])
        submit["captchaToken"] = "other-token"
        self.assertFalse(observation.verification_consistency(check, submit, key)["check_booking_token_match"])

    def test_plaintext_generation_and_malformed_ciphertext(self):
        check = {"token": "t", "pointJson": '[{"x":1,"y":2}]'}
        submit = {"captchaToken": "t", "captchaVerification": 't---[{"x":1,"y":2}]'}
        self.assertTrue(observation.verification_consistency(check, submit, None)["verification_generation_match"])
        self.assertIn("generation_check_error", observation.verification_consistency(check, submit, "invalid-aes-key"))

    def test_marker_uses_beijing_date_and_expires(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(observation, "ROOT", Path(directory)):
            day = Path(directory) / "20261012"
            day.mkdir()
            (day / "enable.json").write_text('{"enabled":true,"run_date":"20261012"}')
            start = datetime(2026, 10, 12, 11, 57, tzinfo=timezone(timedelta(hours=8))).timestamp()
            self.assertIsNotNone(observation.ObservationRun.prepare(start))
            self.assertIsNone(observation.ObservationRun.prepare(start + 86400))
            (day / "enable.json").write_text('{"enabled":true,"run_date":"20261011"}')
            self.assertIsNone(observation.ObservationRun.prepare(start))
            (day / "enable.json").write_text('bad json')
            self.assertIsNone(observation.ObservationRun.prepare(start))


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_failure_full_chain_redacted_shared_across_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            run = observation.ObservationRun(Path(directory), 1.0)
            widget = b"public widget test fixture"
            digest = hashlib.sha256(widget).hexdigest()
            run.site_study = {"algorithm_confirmed": True, "algorithm": {"source_sha256": digest},
                              "assets": [{"path": "/venue/js/widget.js", "sha256": digest}]}
            activation = run.activate()
            try:
                for i in range(2):
                    context = Context()
                    worker = observation.attach(context, cfg(i))
                    worker.attempt = 2
                    response = Response(Request("/venue/js/widget.js", resource_type="script"), widget)
                    worker.on_response(response)
                    points = '[{"x":10,"y":20}]'
                    key = "testkey123456789" if importlib.util.find_spec("cryptography") else None
                    token = "private-challenge-token"
                    chain = [
                        (Request("/venue-server/api/captcha/get?clientUid=private-client-uid&captchaType=clickWord"), {"data": {"repCode": "0000", "repData": {"token": token, "secretKey": key}}}),
                        (Request("/venue-server/api/captcha/check", {"token": token, "pointJson": encrypted(points, key) if key else points}), {"data": {"success": True, "repCode": "0000"}}),
                        (Request("/venue-server/api/reservation/order/submit", {"captchaToken": token,
                            "captchaVerification": encrypted(token + "---" + points, key) if key else token + "---" + points, "password": "private-password"}),
                         {"code": 200 if i == 0 else 250, "message": "OK" if i == 0 else "验证码非法校验"}),
                    ]
                    for request, payload in chain:
                        worker.on_request(request)
                        worker.on_response(Response(request, payload))
                        worker.on_finished(request)
                    self.assertFalse(list(Path(directory).glob("run_*.json")))
                    await worker.finish(BookingResult(i == 0, "OK" if i == 0 else "验证码非法校验"), "booking_attempt_2")
                    self.assertFalse(context.listeners)
                    self.assertTrue(worker.asset_match)
                    self.assertFalse(worker.challenge_keys)
                path = run.write()
            finally:
                run.deactivate(activation)
            result = json.loads(path.read_text(encoding="utf-8"))
            first, second = result["workers"]
            self.assertEqual(first["outcome"]["reason"], "success")
            self.assertEqual(second["outcome"]["reason"], "invalid_verification")
            self.assertTrue(first["events"][2]["consistency"]["verification_generation_match"])
            self.assertEqual(first["account_fingerprint"], second["account_fingerprint"])
            self.assertEqual(first["events"][0]["client_uid_fingerprint"], second["events"][0]["client_uid_fingerprint"])
            self.assertIn("cgauthorization", first["events"][1]["session_fingerprints"])
            self.assertEqual(first["events"][0]["response"]["data.repData.token"], second["events"][0]["response"]["data.repData.token"])
            serialized = json.dumps(result)
            for secret in ("private-challenge-token", "testkey123456789", "private-password", "secret-cookie", "private-session", "opaque-secret-id", "private-account", "private-client-uid", "private-cg-session"):
                self.assertNotIn(secret, serialized)

    async def test_disabled_attach_and_bounds_failure_incomplete_and_unrelated(self):
        self.assertIsNone(observation.attach(Context(), cfg()))
        run = observation.ObservationRun(Path("unused"), 1.0)
        worker = observation.WorkerObservation(run, Context(), cfg())
        worker.on_request(Request("/unrelated", {"password": "secret"}))
        self.assertFalse(worker.records)
        with patch.object(observation, "MAX_REQUESTS", 2):
            failed = Request("/venue-server/api/captcha/check", {"token": "t"})
            worker.on_request(failed)
            worker.on_failed(failed)
            incomplete = Request("/venue-server/api/captcha/get")
            worker.on_request(incomplete)
            worker.on_request(Request("/venue-server/api/captcha/get"))
        await worker.finish(None, "browser_crashed")
        events = worker.export()["events"]
        self.assertEqual(worker.dropped, 1)
        self.assertEqual(events[0]["network_error_code"], "net::ERR_CONNECTION_RESET")
        self.assertTrue(events[1]["incomplete_at_worker_end"])
        self.assertNotIn("password=secret", json.dumps(events))

    async def test_header_read_timeout_does_not_fail_booking(self):
        run = observation.ObservationRun(Path("unused"), 1.0)
        worker = observation.WorkerObservation(run, Context(), cfg())
        request = Request("/venue-server/api/captcha/get")
        async def stalled():
            await asyncio.sleep(10)
        request.all_headers = stalled
        worker.on_request(request)
        worker.on_response(Response(request, {"code": 200}))
        with patch.object(observation, "READ_TIMEOUT", 0.01):
            worker.on_finished(request)
            await worker.finish(BookingResult(True, "OK"), "finished")
        self.assertEqual(worker.outcome["reason"], "success")
        self.assertEqual(worker.export()["events"][0]["headers_error"], "TimeoutError")


if __name__ == "__main__":
    unittest.main()
