"""FastAPI app entry point for the PKU court booking control panel.

Run with:

    python -m web.backend.app                 # local mode (127.0.0.1:8000)
    WEBAPP_MODE=remote python -m web.backend.app   # remote (behind a proxy)

Both modes require login; see web/backend/auth.py.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.booking.session_verification import last_session_verified  # noqa: E402
from web.backend import auth as auth_mod  # noqa: E402
from web.backend.config_loader import load_set  # noqa: E402
from web.backend.jobs import (  # noqa: E402
    configure_secret_redaction,
    get_job_manager,
)
from web.backend.order_cache import get_order_cache  # noqa: E402
from web.backend.schedule_sync import display_status, request_checkin  # noqa: E402
from web.backend.scheduler import get_scheduler  # noqa: E402

# Deployment mode: "local" (default, 127.0.0.1) or "remote" (behind a TLS
# reverse proxy). Mode controls bind defaults + cookie Secure flag; the
# rest of the app is identical in both.
WEBAPP_MODE = os.environ.get("WEBAPP_MODE", "local").strip().lower()
if WEBAPP_MODE not in {"local", "remote"}:
    raise SystemExit(f"WEBAPP_MODE must be 'local' or 'remote', got {WEBAPP_MODE!r}")

SCHEDULE_EXECUTION_MODE = os.environ.get("SCHEDULE_EXECUTION_MODE", "external").strip().lower()
if SCHEDULE_EXECUTION_MODE not in {"external", "embedded"}:
    raise SystemExit("SCHEDULE_EXECUTION_MODE must be 'external' or 'embedded'")

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
    secret_cfg = load_set("scheduled")
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
    if SCHEDULE_EXECUTION_MODE == "embedded":
        await scheduler.start()
    await order_cache.start()
    try:
        yield
    finally:
        await order_cache.stop()
        if SCHEDULE_EXECUTION_MODE == "embedded":
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


def _users_payload() -> list[dict[str, Any]]:
    """Return an allowlisted public view; never expose booking credentials."""
    cfg = load_set("scheduled")
    base_profile = Path(cfg.user_data_dir).resolve()
    uploaded_verified = (
        display_status().get("last_verified", {})
        if SCHEDULE_EXECUTION_MODE == "external" else {}
    )
    out: list[dict[str, Any]] = []
    for u in cfg.users:
        shared_profile = base_profile / f"user_{u.name}"
        profile_paths = [shared_profile, *shared_profile.parent.glob(
            f"{shared_profile.name}_worker_*"
        )]
        verified_times = (
            [value for value in (last_session_verified(path) for path in profile_paths)
             if value is not None]
            if SCHEDULE_EXECUTION_MODE == "embedded" else []
        )
        verified_at = max(verified_times, default=None)
        out.append({
            "name": u.name,
            "login_method": u.login_method,
            "last_verified": (
                verified_at.strftime("%Y-%m-%d %H:%M")
                if verified_at else uploaded_verified.get(u.name)
            ),
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
        {"users": _users_payload(), "active_tab": "orders"},
    )


@app.get("/schedule", response_class=HTMLResponse)
async def page_schedule(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "schedule.html",
        {"active_tab": "schedule"},
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
    base = load_set("scheduled")
    if not base.users:
        raise HTTPException(status_code=400, detail="no users configured")
    job = get_order_cache().start_refresh(limit)
    return JSONResponse({"job_id": job.id})


@app.get("/api/orders/cache")
async def api_orders_cache() -> JSONResponse:
    """Return cached orders immediately without launching a browser."""
    return JSONResponse(get_order_cache().status())


@app.get("/api/orders/proof")
async def api_order_proof(user: str, order_no: str) -> FileResponse:
    """Serve a cached proof only while its order remains in the visible cache."""
    service = get_order_cache()
    visible = any(
        str(order.get("user", "")) == user
        and str(order.get("order_no", "")) == order_no
        for order in service.load_cache().get("orders", [])
    )
    if not visible:
        raise HTTPException(status_code=404, detail="proof screenshot not found")
    path = service.proofs.cached_path(user, order_no)
    if path is None:
        raise HTTPException(status_code=404, detail="proof screenshot not found")
    return FileResponse(
        path,
        media_type="image/png",
        filename="order-proof.png",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/schedule/status")
async def api_schedule_status() -> JSONResponse:
    if SCHEDULE_EXECUTION_MODE == "external":
        return JSONResponse(display_status())
    scheduler = get_scheduler()
    result = scheduler.status()
    result["mode"] = "embedded"
    from web.backend.schedule_sync import schedule_config_snapshot
    result["config"] = schedule_config_snapshot()
    result["last_updated_at"] = result["last_run"].get("finished_at") if result["last_run"] else None
    result["laptop_alive"] = None
    result["laptop_last_seen_at"] = None
    result["today_report_missing"] = False
    return JSONResponse(result)


@app.post("/api/schedule/check-in")
async def api_schedule_checkin() -> JSONResponse:
    if SCHEDULE_EXECUTION_MODE != "external":
        raise HTTPException(status_code=409, detail="on-demand laptop check-in requires external mode")
    return JSONResponse(request_checkin())


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
