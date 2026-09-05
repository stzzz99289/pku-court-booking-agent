from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from web.backend import auth
from web.backend import app as webapp
from web.backend.jobs import Job, configure_secret_redaction


class PasswordHashTests(unittest.TestCase):
    def test_argon2id_hash_round_trip(self) -> None:
        encoded = auth.hash_password("a unique test passphrase")
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertTrue(auth.verify_password("a unique test passphrase", encoded))
        self.assertFalse(auth.verify_password("wrong", encoded))
        self.assertFalse(auth.password_hash_needs_upgrade(encoded))

    def test_legacy_pbkdf2_hash_remains_accepted(self) -> None:
        encoded = auth._hash_password_pbkdf2("legacy", iterations=1_000)
        self.assertTrue(auth.verify_password("legacy", encoded))
        self.assertFalse(auth.verify_password("wrong", encoded))
        self.assertTrue(auth.password_hash_needs_upgrade(encoded))

    def test_malformed_hash_is_rejected(self) -> None:
        self.assertFalse(auth.verify_password("anything", "not-a-hash"))

    def test_unicode_username_comparison_is_safe(self) -> None:
        self.assertFalse(auth.constant_time_text_equal("用户", "admin"))

    def test_legacy_file_hash_is_upgraded_after_valid_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.yaml"
            legacy = auth._hash_password_pbkdf2("current-password", iterations=1_000)
            path.write_text(yaml.safe_dump({
                "username": "admin",
                "password_hash": legacy,
                "secret": "test-secret",
            }), encoding="utf-8")
            current = auth.AuthConfig("admin", legacy, b"test-secret", False)
            with (
                patch.object(auth, "AUTH_YAML", path),
                patch.object(auth, "_auth", current),
                patch.dict("os.environ", {}, clear=True),
            ):
                self.assertTrue(auth.upgrade_file_password_hash("current-password"))

            stored = yaml.safe_load(path.read_text(encoding="utf-8"))["password_hash"]
            self.assertTrue(stored.startswith("$argon2id$"))
            self.assertTrue(auth.verify_password("current-password", stored))


class LoginRateLimiterTests(unittest.TestCase):
    def test_limits_and_then_expires_source_ip(self) -> None:
        limiter = auth.LoginRateLimiter()
        for index in range(auth.LOGIN_MAX_FAILURES_PER_IP):
            limiter.record_failure("203.0.113.1", now=float(index))
        self.assertTrue(limiter.is_limited("203.0.113.1", now=10.0))
        self.assertFalse(
            limiter.is_limited("203.0.113.1", now=auth.LOGIN_WINDOW_S + 20.0)
        )


class PublicUserPayloadTests(unittest.TestCase):
    def test_booking_credentials_are_not_returned(self) -> None:
        user = SimpleNamespace(
            name="zy",
            login_method="password",
            account="15500001111",
            password="court-secret",
        )
        cfg = SimpleNamespace(users=[user], user_data_dir=".browser_profile")
        with (
            patch("web.backend.app.load_set", return_value=cfg),
            patch("web.backend.app._session_valid_hint", return_value={
                "exists": False,
                "valid_hint": False,
                "last_used": None,
            }),
        ):
            payload = webapp._users_payload()

        rendered = repr(payload)
        self.assertNotIn(user.account, rendered)
        self.assertNotIn(user.password, rendered)
        self.assertEqual(
            set(payload[0]),
            {"name", "login_method", "exists", "valid_hint", "last_used"},
        )

    def test_external_next_url_is_rejected(self) -> None:
        self.assertEqual(webapp._safe_next("//example.com"), "/")
        self.assertEqual(webapp._safe_next("https://example.com"), "/")
        self.assertEqual(webapp._safe_next("/schedule"), "/schedule")

    def test_job_api_payload_redacts_registered_booking_secrets(self) -> None:
        configure_secret_redaction(["15500001111", "court-secret"])
        job = Job(id="test", kind="booking")
        job.result = {"message": "account 15500001111", "nested": ["court-secret"]}
        job.error = "court-secret"
        job.append_log("login 15500001111 with court-secret")

        rendered = repr(job.to_dict())
        self.assertNotIn("15500001111", rendered)
        self.assertNotIn("court-secret", rendered)
        self.assertIn("[REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()
