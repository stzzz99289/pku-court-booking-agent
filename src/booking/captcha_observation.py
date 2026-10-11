"""Date-scoped, passive CAPTCHA investigation. No raw credentials on disk.

Enabled only by a dated private marker, only inside the shared scheduler.
All workers share an in-memory HMAC key. Nothing here intercepts requests or
changes booking behavior. Remove once docs/captcha_verification_investigation.md
has an evidence-supported conclusion.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .booking_flow import _CAPTCHA_DIAGNOSTIC_PATHS, _summarize_captcha_api_payload

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2] / "debugging/investigations/captcha-verification"
BEIJING = timezone(timedelta(hours=8))
MAX_REQUESTS = 256  # per worker; explicit truncation marker
MAX_BODY_BYTES = 512_000
READ_TIMEOUT = 1.0
_active: contextvars.ContextVar[ObservationRun | None] = contextvars.ContextVar(
    "captcha_observation_run", default=None,
)


def fingerprint(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def verification_consistency(check: dict, submit: dict, secret_key: str | None) -> dict:
    """Compare only; never return decrypted coordinates, tokens or AES keys.

    Public VerifyPoints code (dated asset manifest) uses AES-ECB/PKCS7 for
    pointJson and for token + '---' + the identical JSON string. No bypass or
    replacement CAPTCHA is sent; this runs after the worker outcome.
    """
    result = {"check_booking_token_match": check.get("token") == submit.get("captchaToken")}
    try:
        point_json = check["pointJson"]
        token = check["token"]
        if secret_key:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives.padding import PKCS7

            cipher = Cipher(algorithms.AES(secret_key.encode("utf-8")), modes.ECB())
            decryptor = cipher.decryptor()
            padded = decryptor.update(base64.b64decode(point_json, validate=True)) + decryptor.finalize()
            unpadder = PKCS7(128).unpadder()
            point_text = (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
            padder = PKCS7(128).padder()
            value = padder.update((token + "---" + point_text).encode("utf-8")) + padder.finalize()
            encryptor = cipher.encryptor()
            expected = base64.b64encode(encryptor.update(value) + encryptor.finalize()).decode("ascii")
        else:
            point_text = point_json
            expected = token + "---" + point_text
        points = json.loads(point_text)
        result["point_json_valid"] = isinstance(points, list) and all(
            isinstance(p, dict) and set(p) == {"x", "y"}
            and all(isinstance(v, (int, float)) for v in p.values()) for p in points
        )
        if result["point_json_valid"]:
            result["point_count"] = len(points)
            result["points_in_widget_coordinate_bounds"] = all(
                0 <= p["x"] <= 310 and 0 <= p["y"] <= 155 for p in points
            )
        result["verification_generation_match"] = hmac.compare_digest(
            expected, submit["captchaVerification"],
        )
    except Exception as exc:
        result["generation_check_error"] = type(exc).__name__
    return result


class ObservationRun:
    def __init__(self, day_dir: Path, started_at: float) -> None:
        self.day_dir = day_dir
        self.started_at = started_at
        self.run_id = datetime.fromtimestamp(started_at, BEIJING).strftime("%H%M%S_%f")
        self.key = secrets.token_bytes(32)  # never serialized or exported
        self.workers: list[WorkerObservation] = []
        self.expected_worker_count: int | None = None
        try:
            value = json.loads((day_dir / "site-study/manifest.json").read_text(encoding="utf-8"))
            self.site_study = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.site_study = {}

    @classmethod
    def prepare(cls, started_at: float) -> ObservationRun | None:
        day = datetime.fromtimestamp(started_at, BEIJING).strftime("%Y%m%d")
        directory = ROOT / day
        try:
            marker = json.loads((directory / "enable.json").read_text(encoding="utf-8"))
            if marker.get("enabled") is True and marker.get("run_date") == day:
                return cls(directory, started_at)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("CAPTCHA observation marker ignored: %s", type(exc).__name__)
        return None

    def activate(self) -> contextvars.Token:
        return _active.set(self)

    @staticmethod
    def deactivate(token: contextvars.Token) -> None:
        _active.reset(token)

    def write(self) -> Path | None:
        """Called only after scheduled workers have finished; fail open."""
        try:
            path = self.day_dir / f"run_{self.run_id}.json"
            payload = {
                "schema": 1, "run_id": self.run_id,
                "started_at": self.started_at, "written_at": time.time(),
                "timezone": "Asia/Shanghai", "max_requests_per_worker": MAX_REQUESTS,
                "studied_asset_hash": self.site_study.get("algorithm", {}).get("source_sha256"),
                "generation_algorithm_confirmed": self.site_study.get("algorithm_confirmed", False),
                "expected_worker_count": self.expected_worker_count,
                "workers": [w.export() for w in self.workers],
            }
            self.day_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("CAPTCHA observation saved: %s (%d workers)", path, len(self.workers))
            return path
        except Exception as exc:
            log.warning("CAPTCHA observation write failed: %s", type(exc).__name__)
            return None


class WorkerObservation:
    def __init__(self, run: ObservationRun, context: Any, cfg: Any) -> None:
        self.run = run
        self.context = context
        self.worker_index = cfg.worker_index
        self.account_id = fingerprint(run.key, cfg.account)
        self.login_method = cfg.login_method
        self.target_date = cfg.date
        self.hours = list(cfg.start_time_list)
        self.host = urlsplit(cfg.base_url).netloc
        self.attempt = 0
        self.records: dict[Any, dict[str, Any]] = {}
        self.pending: set[asyncio.Task] = set()
        self.completed: set[Any] = set()
        self.dropped = 0
        self.script_paths: set[str] = set()
        self.outcome: dict[str, Any] = {}
        self.drain_ms = 0.0
        self.asset_match: bool | None = None
        self.observed_widget_hashes: list[str] = []
        self.widget_responses: list[Any] = []
        self.semaphore = asyncio.Semaphore(2)
        self.challenge_keys: dict[str, str | None] = {}  # transient, never exported
        self.check_payloads: dict[Any, dict] = {}
        self.submit_payloads: dict[Any, dict] = {}
        self.listeners = {
            "request": self.on_request, "response": self.on_response,
            "requestfinished": self.on_finished, "requestfailed": self.on_failed,
        }
        self.capture_errors: list[str] = []
        for name, callback in self.listeners.items():
            context.on(name, callback)

    def on_request(self, request: Any) -> None:
        try:
            url = urlsplit(request.url)
            if url.netloc != self.host:
                return
            if request.resource_type == "script":
                if len(self.script_paths) < 100:
                    self.script_paths.add(url.path)  # no query strings
                return
            if url.path not in _CAPTCHA_DIAGNOSTIC_PATHS:
                return
            if len(self.records) >= MAX_REQUESTS:
                self.dropped += 1
                return
            record = {
                "id": f"w{self.worker_index}-r{len(self.records) + 1}",
                "attempt": self.attempt, "path": url.path, "method": request.method,
                "started_at": time.time(), "start_offset_ms": self.offset(),
            }
            self.records[request] = record
            if request.method == "GET" and url.path.endswith("/captcha/get"):
                query = parse_qs(url.query)
                if query.get("clientUid"):
                    record["client_uid_fingerprint"] = fingerprint(self.run.key, query["clientUid"][0])
                kind = query.get("captchaType", [None])[0]
                if kind in {"clickWord", "blockPuzzle"}:
                    record["captcha_type"] = kind
            if request.method == "POST":
                try:
                    payload = request.post_data_json
                    record["request"] = _summarize_captcha_api_payload(
                        payload, self.run.key,
                    )
                    if isinstance(payload, dict):
                        if url.path.endswith("/captcha/check"):
                            self.check_payloads[request] = {
                                name: payload.get(name) for name in ("token", "pointJson")
                            }
                        elif url.path.endswith("/reservation/order/submit"):
                            self.submit_payloads[request] = {
                                name: payload.get(name) for name in ("captchaToken", "captchaVerification")
                            }
                except Exception as exc:
                    record["request_error"] = type(exc).__name__
        except Exception as exc:
            self.dropped += 1
            if len(self.capture_errors) < 20:
                self.capture_errors.append(f"request:{type(exc).__name__}")

    def offset(self) -> float:
        return round((time.time() - self.run.started_at) * 1000, 3)

    def on_response(self, response: Any) -> None:
        try:
            expected_path = next((a["path"] for a in self.run.site_study.get("assets", [])
                                  if a.get("sha256") == self.run.site_study.get("algorithm", {}).get("source_sha256")), None)
            if (expected_path and urlsplit(response.url).path == expected_path
                    and urlsplit(response.url).netloc == self.host
                    and len(self.widget_responses) < 4):
                self.widget_responses.append(response)
            record = self.records.get(response.request)
            if record is not None:
                record["http_status"] = response.status
                record["response_offset_ms"] = self.offset()
        except Exception as exc:
            if len(self.capture_errors) < 20:
                self.capture_errors.append(f"response:{type(exc).__name__}")

    def on_finished(self, request: Any) -> None:
        self.enqueue(request, False)

    def on_failed(self, request: Any) -> None:
        self.enqueue(request, True)

    def enqueue(self, request: Any, failed: bool) -> None:
        if request not in self.records or request in self.completed:
            return
        self.completed.add(request)
        record = self.records[request]
        record["finish_offset_ms"] = self.offset()
        record["network_failed"] = failed
        if failed:
            import re
            match = re.search(r"net::ERR_[A-Z_]+", request.failure or "")
            if match:
                record["network_error_code"] = match.group(0)
        # Do not log arbitrary failure strings (they can contain URLs/secrets).
        task = asyncio.create_task(self.summarize(request, failed))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def summarize(self, request: Any, failed: bool) -> None:
        record = self.records[request]
        async with self.semaphore:
            try:
                headers = await asyncio.wait_for(request.all_headers(), READ_TIMEOUT)
                sessions = {}
                for name in ("authorization", "cookie", "token", "x-token", "x-access-token",
                             "cgauthorization", "cgappauthorization"):
                    value = headers.get(name)
                    if value:
                        # Normalize cookie ordering; never save names or values.
                        if name == "cookie":
                            value = "; ".join(sorted(s.strip() for s in value.split(";")))
                        sessions[name] = fingerprint(self.run.key, value)
                record["session_fingerprints"] = sessions
            except Exception as exc:
                record["headers_error"] = type(exc).__name__
            if not failed:
                try:
                    response = await asyncio.wait_for(request.response(), READ_TIMEOUT)
                    body = await asyncio.wait_for(response.body(), READ_TIMEOUT)
                    if len(body) > MAX_BODY_BYTES:
                        record["response_error"] = "BodySizeLimit"
                    else:
                        payload = json.loads(body)
                        record["response"] = _summarize_captcha_api_payload(
                            payload, self.run.key,
                        )
                        if record["path"].endswith("/captcha/get") and isinstance(payload, dict):
                            data = payload.get("data")
                            rep = data.get("repData") if isinstance(data, dict) else None
                            if isinstance(rep, dict) and isinstance(rep.get("token"), str):
                                self.challenge_keys[rep["token"]] = rep.get("secretKey")
                    # Opaque backend request IDs are fingerprints, not raw strings.
                    for name in ("x-request-id", "x-correlation-id", "traceparent"):
                        value = response.headers.get(name)
                        if value:
                            record.setdefault("server_id_fingerprints", {})[name] = fingerprint(
                                self.run.key, value,
                            )
                except Exception as exc:
                    record["response_error"] = type(exc).__name__

    async def finish(self, result: Any, stage: str) -> None:
        """Drain bounded summary work before closing Chromium; no disk I/O."""
        start = time.perf_counter()
        for name, callback in self.listeners.items():
            self.context.remove_listener(name, callback)
        # Include pending requests which never reached finished/failed.
        for request in self.records:
            if request not in self.completed:
                if "http_status" in self.records[request]:
                    # A fetch can resolve before requestfinished is delivered.
                    # Read its known response with the same bounded timeout.
                    self.enqueue(request, False)
                else:
                    self.records[request]["incomplete_at_worker_end"] = True
        if self.pending:
            _, unfinished = await asyncio.wait(tuple(self.pending), timeout=3.0)
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        expected_hash = self.run.site_study.get("algorithm", {}).get("source_sha256")
        for response in self.widget_responses:
            try:
                body = await asyncio.wait_for(response.body(), READ_TIMEOUT)
                if len(body) <= MAX_BODY_BYTES:
                    self.observed_widget_hashes.append(hashlib.sha256(body).hexdigest())
            except Exception:
                pass
        if self.observed_widget_hashes:
            self.asset_match = all(value == expected_hash for value in self.observed_widget_hashes)
        self.widget_responses.clear()
        self.check_generation()
        # Keep only safe result classification, not exception/message/details.
        message = result.message if result is not None else ""
        reason = "success" if result is not None and result.success else "other_failure"
        for text, label in (
            ("验证码非法校验", "invalid_verification"),
            ("验证码次数超出限制", "captcha_rate_limit"),
            ("No available courts", "no_available_courts"),
            ("Daily booking limit", "daily_booking_limit"),
        ):
            if text in message:
                reason = label
        self.outcome = {"reason": reason, "stage": stage, "worker_completed": result is not None}
        self.drain_ms = round((time.perf_counter() - start) * 1000, 3)

    def check_generation(self) -> None:
        """Post-outcome comparison, matched by token and request chronology."""
        try:
            for request, submit in self.submit_payloads.items():
                record = self.records[request]
                if not self.run.site_study.get("algorithm_confirmed"):
                    record["generation_check_error"] = "UnconfirmedPublicAlgorithm"
                    continue
                if self.asset_match is not True:
                    record["generation_check_error"] = "WidgetAssetChangedOrNotCaptured"
                    continue
                previous_checks = [
                    (r, check) for r, check in self.check_payloads.items()
                    if self.records[r]["start_offset_ms"] <= record["start_offset_ms"]
                ]
                if previous_checks:
                    latest, check = max(previous_checks, key=lambda item: self.records[item[0]]["start_offset_ms"])
                    record["latest_prior_check_request_id"] = self.records[latest]["id"]
                    record["latest_check_booking_token_match"] = check.get("token") == submit.get("captchaToken")
                candidates = [
                    (r, check) for r, check in self.check_payloads.items()
                    if self.records[r]["start_offset_ms"] <= record["start_offset_ms"]
                    and check.get("token") == submit.get("captchaToken")
                ]
                if not candidates:
                    record["generation_check_error"] = "NoMatchingPriorCheck"
                    continue
                previous, check = max(candidates, key=lambda item: self.records[item[0]]["start_offset_ms"])
                record["matched_check_request_id"] = self.records[previous]["id"]
                token = check.get("token")
                if token not in self.challenge_keys:
                    record["generation_check_error"] = "ChallengeKeyNotCaptured"
                    continue
                record["consistency"] = verification_consistency(check, submit, self.challenge_keys[token])
        finally:
            self.challenge_keys.clear()
            self.check_payloads.clear()
            self.submit_payloads.clear()

    def export(self) -> dict[str, Any]:
        return {
            "worker_index": self.worker_index, "account_fingerprint": self.account_id,
            "login_method": self.login_method, "target_date": self.target_date,
            "hours": self.hours, "outcome": self.outcome,
            "dropped_requests": self.dropped, "summary_drain_ms": self.drain_ms,
            "capture_errors": self.capture_errors,
            "widget_asset_match": self.asset_match,
            "observed_widget_sha256": self.observed_widget_hashes,
            "script_paths": sorted(self.script_paths), "events": list(self.records.values()),
        }


def attach(context: Any, cfg: Any) -> WorkerObservation | None:
    run = _active.get()
    if run is None:
        return None
    try:
        worker = WorkerObservation(run, context, cfg)
        run.workers.append(worker)
        return worker
    except Exception as exc:
        log.warning("CAPTCHA observation attach failed: %s", type(exc).__name__)
        return None
