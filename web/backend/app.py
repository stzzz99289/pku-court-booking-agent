"""FastAPI app entry point for the PKU court booking control panel.

Run with:

    python -m web.backend.app                 # local mode (127.0.0.1:8000)
    WEBAPP_MODE=remote python -m web.backend.app   # remote (behind a proxy)

Both modes require login; see web/backend/auth.py.
"""
from __future__ import annotations

import contextlib
import copy
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.booking import runner  # noqa: E402
from src.booking.config import AppConfig  # noqa: E402
from src.booking.result import BookingResult  # noqa: E402
from src.booking.site_constants import VENUES  # noqa: E402
from web.backend import auth as auth_mod  # noqa: E402
from web.backend.config_loader import load_set, per_user_config  # noqa: E402
from web.backend.jobs import (  # noqa: E402
    Job,
    configure_secret_redaction,
    get_booking_lock,
    get_job_manager,
)
from web.backend.order_cache import get_order_cache  # noqa: E402
from web.backend.scheduler import get_scheduler  # noqa: E402

# Deployment mode: "local" (default, 127.0.0.1) or "remote" (behind a TLS
# reverse proxy). Mode controls bind defaults + cookie Secure flag; the
# rest of the app is identical in both.
WEBAPP_MODE = os.environ.get("WEBAPP_MODE", "local").strip().lower()
if WEBAPP_MODE not in {"local", "remote"}:
    raise SystemExit(f"WEBAPP_MODE must be 'local' or 'remote', got {WEBAPP_MODE!r}")

# Hours selectable for booking — matches src/booking/config._parse_start_time_list bounds.
BOOKABLE_HOURS = [f"{h:02d}" for h in range(6, 22)]

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BACKEND_DIR / "templates"))


def _asset_version(name: str) -> str:
    """Cache-bust static assets by mtime so browsers pick up edits without a hard refresh."""
    p = BACKEND_DIR / "static" / name
    try:
        return str(int(p.stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_version"] = _asset_version

@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    auth = auth_mod.load_auth(secure_cookie=(WEBAPP_MODE == "remote"))
    secret_cfg = load_set("test")
    configure_secret_redaction([
        auth.password_hash,
        auth.secret.decode("utf-8", errors="ignore"),
        secret_cfg.captcha.username,
        secret_cfg.captcha.api_key,
        secret_cfg.captcha.softid,
        *(value for user in secret_cfg.users for value in (user.account, user.password)),
    ])
    scheduler = get_scheduler()
    order_cache = get_order_cache()
    await scheduler.start()
    await order_cache.start()
    try:
        yield
    finally:
        await order_cache.stop()
        await scheduler.stop()


app = FastAPI(title="PKU Court Booking Control Panel", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(BACKEND_DIR / "static")), name="static")


# Auth gate: every route except /login, /logout, /healthz, /static/*.
# auth_dependency raises _RedirectException for unauthed HTML pages; convert
# it to an actual RedirectResponse via an exception handler.
@app.exception_handler(auth_mod._RedirectException)
async def _redirect_handler(_: Request, exc: auth_mod._RedirectException):
    return exc.response


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    # Login has no authenticated state to protect and some mobile/embedded
    # browsers send a rewritten or ``null`` Origin for its form POST. Keep the
    # origin gate on every authenticated state-changing endpoint.
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path != "/login"
    ):
        origin = request.headers.get("origin")
        if origin and not _same_request_host(request, origin):
            return _security_headers(
                request,
                JSONResponse({"detail": "cross-origin request rejected"}, status_code=403),
            )
    try:
        await auth_mod.auth_dependency(request)
    except auth_mod._RedirectException as exc:
        return _security_headers(request, exc.response)
    except HTTPException as exc:
        return _security_headers(
            request, JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        )
    response = await call_next(request)
    return _security_headers(request, response)


def _same_request_host(request: Request, origin: str) -> bool:
    """Accept browser writes from this host, tolerating proxy/port rewriting."""
    origin_host = (urlsplit(origin).hostname or "").lower().rstrip(".")
    if not origin_host:
        return False

    candidate_headers = [request.headers.get("host", "")]
    # A reverse proxy may expose the public phone-facing host here while the
    # ASGI Host header contains its upstream address.
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    if forwarded_host:
        candidate_headers.append(forwarded_host)

    for value in candidate_headers:
        candidate = (urlsplit(f"//{value}").hostname or "").lower().rstrip(".")
        if candidate and auth_mod.constant_time_text_equal(origin_host, candidate):
            return True
    return False


def _security_headers(request: Request, response):
    """Apply browser hardening headers to every response."""
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    if WEBAPP_MODE == "remote":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_valid_hint(user_data_dir: Path) -> dict[str, Any]:
    """Crude "session likely valid" hint based on profile dir mtime.

    True iff the per-user profile dir exists and was modified in the last 7 days.
    Real session validity can only be confirmed by hitting the site, which we
    intentionally avoid here (no background browser launches on page load).
    """
    if not user_data_dir.is_dir():
        return {"exists": False, "valid_hint": False, "last_used": None}
    mtime = user_data_dir.stat().st_mtime
    age_days = (time.time() - mtime) / 86400
    return {
        "exists": True,
        "valid_hint": age_days < 7,
        "last_used": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
    }


def _users_payload() -> list[dict[str, Any]]:
    """Return an allowlisted public view; never expose booking credentials."""
    cfg = load_set("test")  # accounts.yaml is shared across both sets
    base_profile = Path(cfg.user_data_dir).resolve()
    out: list[dict[str, Any]] = []
    for u in cfg.users:
        hint = _session_valid_hint(base_profile / f"user_{u.name}")
        out.append({
            "name": u.name,
            "login_method": u.login_method,
            **hint,
        })
    return out


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = "") -> HTMLResponse:
    if auth_mod.is_authenticated(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next or "/", "error": error},
    )


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    auth = auth_mod.get_auth()
    client_ip = request.client.host if request.client else "unknown"
    if auth_mod.login_rate_limiter.is_limited(client_ip):
        return RedirectResponse(
            url="/login?" + urlencode({"error": "limited", "next": next or "/"}),
            status_code=303,
        )
    # Always run the expensive password check so an unknown username does not
    # create a cheap timing oracle.
    password_ok = auth_mod.verify_password(password, auth.password_hash)
    username_ok = (
        len(username) <= auth_mod.MAX_USERNAME_LENGTH
        and auth_mod.constant_time_text_equal(username, auth.username)
    )
    ok = username_ok and password_ok
    if not ok:
        auth_mod.login_rate_limiter.record_failure(client_ip)
        # Re-render with error; never echo the submitted password back.
        return RedirectResponse(
            url="/login?" + urlencode({"error": "invalid", "next": next or "/"}),
            status_code=303,
        )
    auth_mod.login_rate_limiter.clear_ip(client_ip)
    auth_mod.upgrade_file_password_hash(password)
    # Only allow same-origin redirects.
    target = _safe_next(next)
    response = RedirectResponse(url=target, status_code=303)
    auth_mod.issue_session(response, username)
    return response


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    auth_mod.clear_session(response)
    return response


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def page_users(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "users.html",
        {"users": _users_payload(), "active_tab": "users"},
    )


@app.get("/run", response_class=HTMLResponse)
async def page_run(request: Request) -> HTMLResponse:
    cfg = load_set("test")
    first = cfg.workers[0] if cfg.workers else None
    prefill_venue = int(cfg.venue_id) if str(cfg.venue_id).isdigit() else next(iter(VENUES))
    prefill = {
        "user": first.user if first else (cfg.users[0].name if cfg.users else ""),
        "date_iso": _yyyymmdd_to_iso(first.date) if first else date.today().isoformat(),
        "start_time_list": list(first.active_start_time_list()) if first else [],
        "venue_id": prefill_venue,
    }
    venue_options = [{"id": vid, "name": name} for vid, name in VENUES.items()]
    # If the configured venue isn't in the known mapping, surface it as an
    # extra option so the dropdown still reflects the YAML.
    if prefill_venue not in VENUES:
        venue_options.append({"id": prefill_venue, "name": f"venue {prefill_venue} (unmapped)"})
    return templates.TemplateResponse(
        request, "run.html",
        {
            "active_tab": "run",
            "users": [u.name for u in cfg.users],
            "hours": BOOKABLE_HOURS,
            "venues": venue_options,
            "prefill": prefill,
        },
    )


def _yyyymmdd_to_iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _iso_to_yyyymmdd(iso: str) -> str:
    return iso.replace("-", "")


@app.get("/schedule", response_class=HTMLResponse)
async def page_schedule(request: Request) -> HTMLResponse:
    # Resolve worker dates relative to the next fire date so the user sees
    # the dates that the actual scheduled run will book — not whatever date
    # the webapp happened to boot on.
    next_fire = get_scheduler().next_fire
    fire_date = (
        datetime.fromtimestamp(next_fire).date() if next_fire else date.today()
    )
    cfg = load_set("scheduled", today=fire_date)
    workers_summary = [
        {
            "user": w.user,
            "date": w.date,
            "start_time_list": list(w.active_start_time_list()),
            "court_priority": list(w.court_priority),
        }
        for w in cfg.workers
    ]
    venue_id = int(cfg.venue_id) if str(cfg.venue_id).isdigit() else None
    venue_name = VENUES.get(venue_id) if venue_id is not None else None
    return templates.TemplateResponse(
        request, "schedule.html",
        {
            "active_tab": "schedule",
            "scheduled_time": cfg.scheduled_time,
            "scheduled_prep_seconds": cfg.scheduled_prep_seconds,
            "venue_id": venue_id,
            "venue_name": venue_name,
            "workers": workers_summary,
        },
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@app.get("/api/users")
async def api_users() -> JSONResponse:
    return JSONResponse({"users": _users_payload()})


@app.post("/api/orders/refresh_all")
async def api_orders_refresh_all(payload: dict[str, Any] | None = None) -> JSONResponse:
    """Start a manual refresh of the persistent all-user order cache."""
    payload = payload or {}
    try:
        limit = int(payload.get("limit", 10))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be an integer")
    if not 1 <= limit <= 50:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 50")
    base = load_set("test")
    if not base.users:
        raise HTTPException(status_code=400, detail="no users configured")
    job = get_order_cache().start_refresh(limit)
    return JSONResponse({"job_id": job.id})


@app.get("/api/orders/cache")
async def api_orders_cache() -> JSONResponse:
    """Return cached orders immediately without launching a browser."""
    return JSONResponse(get_order_cache().status())


@app.post("/api/bookings/run")
async def api_bookings_run(payload: dict[str, Any]) -> JSONResponse:
    """Launch the test booking flow as a background job.

    Body: {"user": "alice", "date": "2026-05-03", "start_time_list": ["09","10"]}
    Concurrency rules (booking_lock + no-test window) land in M5.
    """
    user_name = str(payload.get("user", "")).strip()
    date_iso = str(payload.get("date", "")).strip()
    raw_hours = payload.get("start_time_list") or []
    raw_venue = payload.get("venue_id")
    if not user_name or not date_iso or not raw_hours or raw_venue in (None, ""):
        raise HTTPException(
            status_code=400,
            detail="user, venue_id, date, and start_time_list are required",
        )
    try:
        venue_id = int(raw_venue)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"invalid venue_id {raw_venue!r}")

    base = load_set("test")
    user = next((u for u in base.users if u.name == user_name), None)
    if user is None:
        raise HTTPException(status_code=404, detail=f"unknown user {user_name!r}")

    # Validate hours: must be 2-digit strings in 06–21.
    hours: list[str] = []
    for h in raw_hours:
        s = str(h).strip()
        if s not in BOOKABLE_HOURS:
            raise HTTPException(status_code=400, detail=f"invalid hour {h!r} (allowed: 06–21)")
        hours.append(s)

    # Validate date: HTML date input emits YYYY-MM-DD.
    try:
        date.fromisoformat(date_iso)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid date {date_iso!r} (expected YYYY-MM-DD)")
    yyyymmdd = _iso_to_yyyymmdd(date_iso)

    # Concurrency gate: refuse if the scheduled task is running or its
    # no-test window is open. Frontend greys the button using the same
    # status, but we re-check here so curl/stale pages can't slip through.
    scheduler = get_scheduler()
    lock = get_booking_lock()
    if scheduler.in_no_test_window() or lock.locked():
        reason = (
            "scheduled task is running"
            if scheduler.state == "running"
            else "scheduled task is about to fire — test runs are blocked in the prep window"
        )
        raise HTTPException(status_code=409, detail=reason)

    cfg = per_user_config(base, user)
    cfg.date = yyyymmdd
    cfg.start_time_list = hours
    cfg.venue_id = str(venue_id)
    cfg.headless = True  # webapp is headless-only
    cfg.scheduled_mode = False

    async def _run(job: Job) -> dict[str, Any]:
        slot_str = ",".join(f"{h}:00" for h in hours)
        venue_name = VENUES.get(venue_id, f"venue {venue_id}")
        job.append_log(
            f"[booking] user={user.name} venue={venue_name} ({venue_id}) "
            f"date={yyyymmdd} slots=[{slot_str}]"
        )
        # Hold booking_lock for the whole flow so the scheduler can't fire on
        # top of us. If the scheduler is already holding it, await blocks.
        async with get_booking_lock():
            # Re-check the no-test window after acquiring the lock — between
            # the pre-flight check and now, the scheduler may have advanced.
            if get_scheduler().in_no_test_window():
                raise RuntimeError("scheduled task entered its prep window before this test run could start")
            # Paths are unused when _config_override is provided.
            result: BookingResult = await runner.run(
                user_config_path=Path("/dev/null"),
                site_config_path=Path("/dev/null"),
                _config_override=cfg,
            )
        return asdict(result)

    job = get_job_manager().start(f"booking:{user.name}", _run)
    return JSONResponse({"job_id": job.id})


@app.get("/api/schedule/status")
async def api_schedule_status() -> JSONResponse:
    return JSONResponse(get_scheduler().status())


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str, log_offset: int = 0) -> JSONResponse:
    job = get_job_manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return JSONResponse(job.to_dict(log_offset=log_offset))


def main() -> None:
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # In remote mode the app still binds to 127.0.0.1 by default; a TLS
    # reverse proxy (e.g. Caddy) is expected in front of it. Override with
    # WEBAPP_HOST / WEBAPP_PORT if you really need to bind elsewhere (e.g.
    # 0.0.0.0 inside a container that is itself the public surface).
    host = os.environ.get("WEBAPP_HOST", "127.0.0.1")
    port = int(os.environ.get("WEBAPP_PORT", "8000"))
    forwarded_allow_ips = os.environ.get(
        "WEBAPP_FORWARDED_ALLOW_IPS",
        "127.0.0.1" if WEBAPP_MODE == "remote" else "",
    )
    log.info("starting webapp in %s mode on %s:%d", WEBAPP_MODE, host, port)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=(WEBAPP_MODE == "remote"),
        forwarded_allow_ips=forwarded_allow_ips or None,
    )


if __name__ == "__main__":
    main()
