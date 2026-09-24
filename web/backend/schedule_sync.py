"""Private SSH transport for laptop schedule reports and scheduled configs.

The server accepts fixed payload types over an existing SSH login.
No dashboard upload endpoint or booking credentials are exposed to browsers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any

from src.booking.config import load_split_config
from src.booking.session_verification import last_session_verified
from src.booking.site_constants import VENUES
from web.backend.config_loader import ACCOUNTS_PATH, WEBAPP_CONFIG_DIR, load_set
from web.backend.jobs import configure_secret_redaction, redact_sensitive_text, redact_sensitive_value
from web.backend.scheduler import DATA_DIR, LOG_FILE, META_FILE, Scheduler, compute_next_fire

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_FILE = DATA_DIR / "schedule_display.json"
HEARTBEAT_FILE = DATA_DIR / "laptop_heartbeat.json"
CHECKIN_FILE = DATA_DIR / "laptop_checkin_request.json"
UPLOADED_FILE = DATA_DIR / "schedule_uploaded.json"
SHANGHAI = timezone(timedelta(hours=8))
SSH_TARGET = os.environ.get("SCHEDULE_SYNC_SSH", "tianze@43.173.124.100")
REMOTE_PROJECT = os.environ.get("SCHEDULE_SYNC_REMOTE_DIR", "~/pku-court-booking-agent")
MAX_PAYLOAD_BYTES = 12_000_000
HEARTBEAT_STALE_SECONDS = 5 * 3600
CHECKIN_TIMEOUT_SECONDS = 180
CONFIG_FILES = {
    "accounts": ACCOUNTS_PATH,
    "workers": WEBAPP_CONFIG_DIR / "scheduled" / "user_config.yaml",
    "site": WEBAPP_CONFIG_DIR / "scheduled" / "site_config.yaml",
}


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _next_fire_for_display(cfg) -> float:
    return compute_next_fire(cfg)


def schedule_config_snapshot(fire_date: date | None = None) -> dict[str, Any]:
    """Public, credential-free representation of the local scheduled config."""
    base = load_set("scheduled")
    if fire_date is None:
        fire_date = datetime.fromtimestamp(_next_fire_for_display(base)).date()
    cfg = load_set("scheduled", today=fire_date)
    venue_id = int(cfg.venue_id) if str(cfg.venue_id).isdigit() else None
    return {
        "scheduled_time": cfg.scheduled_time,
        "scheduled_prep_seconds": cfg.scheduled_prep_seconds,
        "venue_id": cfg.venue_id,
        "venue_name": VENUES.get(venue_id, "") if venue_id is not None else "",
        "worker_count": len(cfg.workers),
        "workers": [{
            "user": worker.user,
            "date": worker.date,
            "start_time_list": list(worker.active_start_time_list()),
            "court_priority": list(worker.court_priority),
        } for worker in cfg.workers],
    }


def verification_snapshot() -> dict[str, str | None]:
    cfg = load_set("scheduled")
    profile_root = Path(cfg.user_data_dir).resolve()
    verified: dict[str, str | None] = {}
    for user in cfg.users:
        shared = profile_root / f"user_{user.name}"
        paths = [shared, *shared.parent.glob(f"{shared.name}_worker_*")]
        times = [stamp for path in paths if (stamp := last_session_verified(path)) is not None]
        latest = max(times, default=None)
        verified[user.name] = latest.strftime("%Y-%m-%d %H:%M") if latest else None
    return verified


def build_report() -> dict[str, Any]:
    cfg = load_set("scheduled")
    configure_secret_redaction([
        cfg.captcha.username, cfg.captcha.api_key, cfg.captcha.softid,
        *(value for user in cfg.users for value in (user.account, user.password)),
    ])
    meta = _read_json(META_FILE)
    # A pre-migration local artifact may be months old. Do not present it as
    # the active laptop scheduler's most recent run after the cutover.
    if meta and float(meta.get("started_at", 0)) < time.time() - 7 * 86400:
        meta = None
    try:
        logs = LOG_FILE.read_text(encoding="utf-8").splitlines() if meta else []
    except OSError:
        logs = []
    return redact_sensitive_value({
        "schema": 1,
        "source": "laptop",
        "created_at": time.time(),
        "config": schedule_config_snapshot(),
        "last_run": meta,
        "logs": [redact_sensitive_text(line) for line in logs[-10_000:]],
        "last_verified": verification_snapshot(),
    })


def _validate_report(data: dict[str, Any]) -> None:
    if data.get("schema") != 1 or data.get("source") != "laptop":
        raise ValueError("unsupported schedule report")
    cfg = data.get("config")
    if not isinstance(cfg, dict) or not isinstance(cfg.get("workers"), list):
        raise ValueError("schedule report has no configuration")
    if not isinstance(data.get("logs"), list) or not all(isinstance(x, str) for x in data["logs"]):
        raise ValueError("schedule report logs must be strings")
    if data.get("last_run") is not None and not isinstance(data["last_run"], dict):
        raise ValueError("schedule report last_run must be an object")
    if not isinstance(data.get("last_verified"), dict):
        raise ValueError("schedule report last_verified must be an object")


def _validate_heartbeat(data: dict[str, Any]) -> None:
    if data.get("schema") != 1 or data.get("source") != "laptop":
        raise ValueError("unsupported laptop heartbeat")
    if data.get("state") not in {"idle", "running", "cooling_down", "completed", "failed"}:
        raise ValueError("invalid laptop state")
    if not isinstance(data.get("sent_at"), (int, float)):
        raise ValueError("heartbeat has no timestamp")


def _validate_checkin(data: dict[str, Any]) -> None:
    if data.get("schema") != 1 or data.get("source") != "laptop":
        raise ValueError("unsupported on-demand check-in")
    request_id = data.get("checkin_request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("invalid check-in request ID")
    if not isinstance(data.get("sent_at"), (int, float)):
        raise ValueError("check-in has no timestamp")
    if not isinstance(data.get("host"), str) or len(data["host"]) > 255:
        raise ValueError("invalid check-in host")


def checkin_status() -> dict[str, Any]:
    """Public state of the most recent on-demand request, without its token."""
    request = _read_json(CHECKIN_FILE) or {}
    if not request:
        return {"state": "none"}
    state = "responded" if request.get("responded_at") else (
        "timed_out" if time.time() >= request.get("expires_at", 0) else "pending"
    )
    return {
        "state": state,
        "requested_at": request.get("requested_at"),
        "responded_at": request.get("responded_at"),
        "expires_at": request.get("expires_at"),
    }


def request_checkin() -> dict[str, Any]:
    """Coalesce repeated clicks while a request is awaiting the laptop."""
    if checkin_status()["state"] != "pending":
        now = time.time()
        _atomic_json(CHECKIN_FILE, {
            "id": secrets.token_hex(16),
            "requested_at": now,
            "expires_at": now + CHECKIN_TIMEOUT_SECONDS,
        })
    return checkin_status()


def pending_checkin_id() -> str | None:
    request = _read_json(CHECKIN_FILE) or {}
    if checkin_status()["state"] == "pending":
        value = request.get("id")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
            return value
    return None


def _receive_config(data: dict[str, Any]) -> None:
    if data.get("schema") != 1 or not isinstance(data.get("files"), dict):
        raise ValueError("unsupported scheduled config bundle")
    files = data["files"]
    if set(files) != set(CONFIG_FILES) or not all(isinstance(x, str) for x in files.values()):
        raise ValueError("scheduled config bundle is incomplete")
    with tempfile.TemporaryDirectory(prefix="schedule-config-", dir=DATA_DIR) as tmp:
        temp = Path(tmp)
        staged = {key: temp / f"{key}.yaml" for key in files}
        for key, path in staged.items():
            path.write_text(files[key], encoding="utf-8")
        load_split_config(staged["workers"], staged["accounts"], staged["site"])
        for key, dest in CONFIG_FILES.items():
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(files[key])
                os.chmod(name, 0o600)
                os.replace(name, dest)
            finally:
                if os.path.exists(name):
                    os.unlink(name)


def receive(kind: str, raw: bytes) -> None:
    """Validate an SSH stdin payload, then atomically replace server state."""
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("sync payload is too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("sync payload must be an object")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if kind == "report":
        _validate_report(data)
        data["received_at"] = time.time()
        _atomic_json(REPORT_FILE, data)
    elif kind == "heartbeat":
        _validate_heartbeat(data)
        data["received_at"] = time.time()
        _atomic_json(HEARTBEAT_FILE, data)
        # The external server no longer fires bookings, so its old profile
        # dumps need this inexpensive check-in to age out after seven days.
        Scheduler._prune_profiles()
    elif kind == "checkin":
        _validate_checkin(data)
        request = _read_json(CHECKIN_FILE) or {}
        if data["checkin_request_id"] != request.get("id") or checkin_status()["state"] != "pending":
            raise ValueError("check-in request is unknown or expired")
        request["responded_at"] = time.time()
        request["host"] = data["host"]
        _atomic_json(CHECKIN_FILE, request)
    elif kind == "config":
        _receive_config(data)
    else:
        raise ValueError("unknown sync payload type")


def _ssh_send(kind: str, data: dict[str, Any]) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", SSH_TARGET):
        raise ValueError("invalid SCHEDULE_SYNC_SSH host")
    if not re.fullmatch(r"[A-Za-z0-9_./~-]+", REMOTE_PROJECT):
        raise ValueError("invalid SCHEDULE_SYNC_REMOTE_DIR")
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("sync payload is too large")
    remote = f"cd {REMOTE_PROJECT} && .venv/bin/python -m web.backend.schedule_sync receive {kind}"
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_TARGET, remote],
        input=payload, capture_output=True, timeout=60, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"SSH {kind} upload failed (exit {completed.returncode})")


def sync_scheduled_configs() -> None:
    # Validate locally before touching the server. The remote receiver validates
    # the exact transferred bytes again before replacing its files.
    load_set("scheduled")
    _ssh_send("config", {
        "schema": 1,
        "files": {key: path.read_text(encoding="utf-8") for key, path in CONFIG_FILES.items()},
    })


def publish_report(*, force: bool = False) -> bool:
    report = build_report()
    canonical = {key: value for key, value in report.items() if key != "created_at"}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    marker = _read_json(UPLOADED_FILE) or {}
    if not force and marker.get("digest") == digest:
        return False
    _ssh_send("report", report)
    _atomic_json(UPLOADED_FILE, {"digest": digest, "uploaded_at": time.time()})
    return True


def send_heartbeat(state: str = "idle") -> None:
    cfg = load_set("scheduled")
    _ssh_send("heartbeat", {
        "schema": 1,
        "source": "laptop",
        "state": state,
        "sent_at": time.time(),
        "host": platform.node(),
        "next_fire": _next_fire_for_display(cfg),
    })


def probe_checkin() -> bool:
    """Laptop-side, lightweight outbound poll; no browser or booking work."""
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", SSH_TARGET):
        raise ValueError("invalid SCHEDULE_SYNC_SSH host")
    if not re.fullmatch(r"[A-Za-z0-9_./~-]+", REMOTE_PROJECT):
        raise ValueError("invalid SCHEDULE_SYNC_REMOTE_DIR")
    remote = f"cd {REMOTE_PROJECT} && .venv/bin/python -m web.backend.schedule_sync pending-checkin"
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_TARGET, remote],
        capture_output=True, timeout=25, check=False, text=True,
    )
    if completed.returncode:
        raise RuntimeError(f"SSH check-in probe failed (exit {completed.returncode})")
    request_id = completed.stdout.strip()
    if not request_id:
        return False
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("server returned an invalid check-in request ID")
    _ssh_send("checkin", {
        "schema": 1,
        "source": "laptop",
        "checkin_request_id": request_id,
        "sent_at": time.time(),
        "host": platform.node(),
    })
    return True


def display_status() -> dict[str, Any]:
    """Read remote display state without loading booking credentials or browsers."""
    report = _read_json(REPORT_FILE) or {}
    heartbeat = _read_json(HEARTBEAT_FILE) or {}
    checkin = _read_json(CHECKIN_FILE) or {}
    now = time.time()
    heartbeat_seen = heartbeat.get("received_at")
    on_demand_seen = checkin.get("responded_at")
    seen = max((x for x in (heartbeat_seen, on_demand_seen) if isinstance(x, (int, float))), default=None)
    alive = isinstance(seen, (int, float)) and 0 <= now - seen < HEARTBEAT_STALE_SECONDS
    last_run = report.get("last_run") if isinstance(report.get("last_run"), dict) else None
    today = datetime.now(SHANGHAI).date()
    finished = last_run.get("finished_at") if last_run else None
    run_today = isinstance(finished, (int, float)) and datetime.fromtimestamp(finished, SHANGHAI).date() == today
    report_due = datetime.now(SHANGHAI).time() >= clock_time(12, 15)
    heartbeat_state = heartbeat.get("state") if isinstance(heartbeat_seen, (int, float)) and now - heartbeat_seen < HEARTBEAT_STALE_SECONDS else "unknown"
    if heartbeat_state in {"running", "cooling_down"} and now - float(heartbeat_seen) > 20 * 60:
        heartbeat_state = "unknown"
    missing_today = bool(report_due and not run_today and heartbeat_state not in {"running", "cooling_down"})
    if missing_today:
        state = "no report today"
    elif heartbeat_state in {"running", "cooling_down", "failed"}:
        state = heartbeat_state
    elif run_today:
        state = "completed"
    else:
        state = "waiting"
    cfg = report.get("config") if isinstance(report.get("config"), dict) else None
    next_fire = heartbeat.get("next_fire") if heartbeat_state != "unknown" else None
    if cfg and (not isinstance(next_fire, (int, float)) or next_fire < now):
        try:
            s = cfg["scheduled_time"]
            local_now = datetime.now(SHANGHAI)
            submission = local_now.replace(hour=int(s[:2]), minute=int(s[2:4]), second=int(s[4:6]), microsecond=0)
            fire = submission - timedelta(seconds=int(cfg["scheduled_prep_seconds"]))
            if fire <= local_now:
                fire += timedelta(days=1)
            next_fire = fire.timestamp()
        except (KeyError, TypeError, ValueError):
            next_fire = None
    return {
        "mode": "external",
        "state": state,
        "next_fire": next_fire,
        "now": now,
        "last_updated_at": max((x for x in (report.get("received_at"), seen) if isinstance(x, (int, float))), default=None),
        "last_report_at": report.get("received_at"),
        "laptop_last_seen_at": seen,
        "laptop_alive": alive,
        "laptop_host": checkin.get("host") if isinstance(on_demand_seen, (int, float)) and (not isinstance(heartbeat_seen, (int, float)) or on_demand_seen > heartbeat_seen) else heartbeat.get("host"),
        "checkin": checkin_status(),
        "today_report_missing": missing_today,
        "config": cfg,
        "last_verified": report.get("last_verified", {}),
        "last_run": last_run,
        "logs": report.get("logs", []) if isinstance(report.get("logs"), list) else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync local booking reports to the display server")
    parser.add_argument("command", choices=("receive", "heartbeat", "publish", "sync-config", "pending-checkin", "probe-checkin", "request-checkin"))
    parser.add_argument("kind", nargs="?", choices=("report", "heartbeat", "config", "checkin"))
    args = parser.parse_args()
    if args.command == "receive":
        if not args.kind:
            parser.error("receive requires a payload kind")
        receive(args.kind, sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1))
    elif args.command == "heartbeat":
        errors: list[str] = []
        for label, action in (("configs", sync_scheduled_configs), ("report", publish_report), ("heartbeat", send_heartbeat)):
            try:
                action()
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
    elif args.command == "publish":
        publish_report(force=True)
    elif args.command == "sync-config":
        sync_scheduled_configs()
    elif args.command == "pending-checkin":
        print(pending_checkin_id() or "")
    elif args.command == "probe-checkin":
        probe_checkin()
    elif args.command == "request-checkin":
        print(json.dumps(request_checkin()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
