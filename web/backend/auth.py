"""Single-user cookie-session auth for the webapp.

There is exactly one account. Username + a password hash come
from env vars (`WEBAPP_USER`, `WEBAPP_PASSWORD_HASH`) or, as a fallback, from
`config/webapp/auth.yaml`. The session cookie is an HMAC-signed token; the
HMAC key comes from `WEBAPP_SECRET` (env or auth.yaml).

Run `python -m web.backend.auth hash` to generate a password hash to paste
into env / auth.yaml.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import yaml
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUTH_YAML = PROJECT_ROOT / "config" / "webapp" / "auth.yaml"
log = logging.getLogger(__name__)

COOKIE_NAME = "pku_session"
SESSION_TTL_S = 7 * 24 * 3600  # 7 days
PBKDF2_ITERATIONS = 600_000
MAX_USERNAME_LENGTH = 128
MAX_PASSWORD_LENGTH = 1024

# OWASP's minimum Argon2id work factors: 19 MiB memory, 2 iterations, 1 lane.
_ARGON2 = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# Layered throttles make online guessing expensive while avoiding a permanent
# lockout. State is intentionally process-local; this app runs as one worker.
LOGIN_WINDOW_S = 15 * 60
LOGIN_MAX_FAILURES_PER_IP = 10
LOGIN_MAX_FAILURES_GLOBAL = 50


@dataclass
class AuthConfig:
    username: str
    password_hash: str
    secret: bytes
    secure_cookie: bool


_auth: AuthConfig | None = None


def load_auth(secure_cookie: bool) -> AuthConfig:
    """Load auth config from env, falling back to config/webapp/auth.yaml.

    `secure_cookie` is wired from the deployment mode — True in remote mode
    (HTTPS terminated by Caddy), False in local mode (plain http on
    127.0.0.1).
    """
    global _auth
    user = os.environ.get("WEBAPP_USER")
    pw_hash = os.environ.get("WEBAPP_PASSWORD_HASH")
    secret = os.environ.get("WEBAPP_SECRET")

    if not (user and pw_hash and secret) and AUTH_YAML.is_file():
        with AUTH_YAML.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        user = user or data.get("username")
        pw_hash = pw_hash or data.get("password_hash")
        secret = secret or data.get("secret")

    if not (user and pw_hash and secret):
        raise RuntimeError(
            "webapp auth not configured: set WEBAPP_USER / WEBAPP_PASSWORD_HASH / "
            "WEBAPP_SECRET, or fill in config/webapp/auth.yaml "
            "(see auth.example.yaml)"
        )

    _auth = AuthConfig(
        username=str(user),
        password_hash=str(pw_hash),
        secret=str(secret).encode("utf-8"),
        secure_cookie=secure_cookie,
    )
    if password_hash_needs_upgrade(_auth.password_hash):
        log.warning(
            "dashboard password uses a legacy hash; generate an Argon2id hash "
            "with `python -m web.backend.auth hash`"
        )
    return _auth


def get_auth() -> AuthConfig:
    if _auth is None:
        raise RuntimeError("auth not loaded — call load_auth() at app startup")
    return _auth


# ---------------------------------------------------------------------------
# Password hashing (Argon2id; legacy PBKDF2 hashes remain verifiable)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _ARGON2.hash(password)


def _hash_password_pbkdf2(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Generate a legacy hash for compatibility tests and migrations."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    if encoded.startswith("$argon2id$"):
        try:
            return _ARGON2.verify(encoded, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False
    return _verify_password_pbkdf2(password, encoded)


def _verify_password_pbkdf2(password: str, encoded: str) -> bool:
    try:
        scheme, iters_s, salt_b64, hash_b64 = encoded.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iters_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, binascii.Error):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def password_hash_needs_upgrade(encoded: str) -> bool:
    """Return True for legacy or weaker-than-current password hashes."""
    if not encoded.startswith("$argon2id$"):
        return True
    try:
        return _ARGON2.check_needs_rehash(encoded)
    except InvalidHashError:
        return True


class LoginRateLimiter:
    """Bound failed login attempts by source IP and across the single account."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, events: deque[float], now: float) -> None:
        cutoff = now - LOGIN_WINDOW_S
        while events and events[0] <= cutoff:
            events.popleft()

    def is_limited(self, ip: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            ip_events = self._events[f"ip:{ip}"]
            global_events = self._events["global"]
            self._prune(ip_events, current)
            self._prune(global_events, current)
            return (
                len(ip_events) >= LOGIN_MAX_FAILURES_PER_IP
                or len(global_events) >= LOGIN_MAX_FAILURES_GLOBAL
            )

    def record_failure(self, ip: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            for key in (f"ip:{ip}", "global"):
                events = self._events[key]
                self._prune(events, current)
                events.append(current)

    def clear_ip(self, ip: str) -> None:
        with self._lock:
            self._events.pop(f"ip:{ip}", None)


login_rate_limiter = LoginRateLimiter()


# ---------------------------------------------------------------------------
# Cookie session: token = "<expiry>.<username>.<sig>"
# ---------------------------------------------------------------------------


def issue_session(response: Response, username: str) -> None:
    auth = get_auth()
    expiry = int(time.time()) + SESSION_TTL_S
    payload = f"{expiry}.{username}"
    sig = _sign(payload, auth.secret)
    token = f"{payload}.{sig}"
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        secure=auth.secure_cookie,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _verify_cookie(token: str) -> str | None:
    auth = get_auth()
    try:
        expiry_s, username, sig = token.rsplit(".", 2)
    except ValueError:
        return None
    payload = f"{expiry_s}.{username}"
    expected = _sign(payload, auth.secret)
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        if int(expiry_s) < int(time.time()):
            return None
    except ValueError:
        return None
    if not constant_time_text_equal(username, auth.username):
        return None
    return username


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    return bool(token and _verify_cookie(token))


# Routes that bypass auth: login form, healthcheck, static assets.
_PUBLIC_PREFIXES = ("/login", "/logout", "/healthz", "/static/")


async def auth_dependency(request: Request) -> None:
    """App-level dependency. HTML routes redirect, JSON routes 401."""
    path = request.url.path
    if any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES):
        return
    if is_authenticated(request):
        return
    if path.startswith("/api/"):
        raise HTTPException(status_code=401, detail="not authenticated")
    # Redirect HTML pages to /login with a `next` hint.
    from fastapi.responses import RedirectResponse
    nxt = request.url.path
    if request.url.query:
        nxt += "?" + request.url.query
    raise _RedirectException(RedirectResponse(url=f"/login?next={nxt}", status_code=303))


class _RedirectException(Exception):
    def __init__(self, response):
        self.response = response


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sign(payload: str, secret: bytes) -> str:
    mac = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64e(mac)


def constant_time_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------------------
# CLI: generate a password hash + a fresh secret
# ---------------------------------------------------------------------------


def _cli() -> None:
    import argparse
    import getpass

    p = argparse.ArgumentParser(description="webapp auth helpers")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash", help="prompt for a password and print its Argon2id hash")
    sub.add_parser("secret", help="print a fresh random session secret")
    args = p.parse_args()

    if args.cmd == "hash":
        pw = getpass.getpass("password: ")
        pw2 = getpass.getpass("confirm:  ")
        if pw != pw2:
            raise SystemExit("passwords do not match")
        if not pw:
            raise SystemExit("empty password")
        if len(pw) < 15:
            raise SystemExit("password must be at least 15 characters")
        print(hash_password(pw))
    elif args.cmd == "secret":
        print(secrets.token_urlsafe(48))


if __name__ == "__main__":
    _cli()
