"""FastAPI app entry point for the PKU court booking control panel.

Run locally with:

    python -m web.backend.app

Bound to 127.0.0.1 only — there is no auth in v1.
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.booking.orders import Order, fetch_user_orders  # noqa: E402
from web.backend.config_loader import load_set, per_user_config  # noqa: E402
from web.backend.jobs import Job, get_job_manager  # noqa: E402

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BACKEND_DIR / "templates"))

app = FastAPI(title="PKU Court Booking Control Panel")
app.mount("/static", StaticFiles(directory=str(BACKEND_DIR / "static")), name="static")

# In-memory cache: most recent orders fetched per (set, user). Replaced on
# each successful refresh; cleared on app restart (no DB in v1).
_orders_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}


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
    """List users from accounts.yaml plus per-user session-valid hint."""
    cfg = load_set("test")  # accounts.yaml is shared across both sets
    base_profile = Path(cfg.user_data_dir).resolve()
    out: list[dict[str, Any]] = []
    for u in cfg.users:
        hint = _session_valid_hint(base_profile / f"user_{u.name}")
        out.append({
            "name": u.name,
            "login_method": u.login_method,
            "account_masked": (u.account[:3] + "***" + u.account[-2:]) if len(u.account) > 5 else "***",
            **hint,
        })
    return out


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
    # M3 will implement this; placeholder for now so the nav doesn't 404.
    return templates.TemplateResponse(
        request, "placeholder.html",
        {"active_tab": "run", "title": "Run Booking", "milestone": "M3"},
    )


@app.get("/schedule", response_class=HTMLResponse)
async def page_schedule(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "placeholder.html",
        {"active_tab": "schedule", "title": "Scheduled Task", "milestone": "M4"},
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@app.get("/api/users")
async def api_users() -> JSONResponse:
    return JSONResponse({"users": _users_payload()})


@app.post("/api/orders/refresh")
async def api_orders_refresh(payload: dict[str, Any]) -> JSONResponse:
    """Kick off a background job that logs in as `user` and fetches `limit` orders."""
    user_name = str(payload.get("user", "")).strip()
    limit = int(payload.get("limit", 10))
    if not user_name:
        raise HTTPException(status_code=400, detail="missing 'user'")
    base = load_set("test")
    user = next((u for u in base.users if u.name == user_name), None)
    if user is None:
        raise HTTPException(status_code=404, detail=f"unknown user {user_name!r}")
    cfg = per_user_config(base, user)

    async def _fetch(job: Job) -> dict[str, Any]:
        orders: list[Order] = await fetch_user_orders(cfg, user, limit)
        rows = [o.to_dict() for o in orders]
        _orders_cache[("test", user.name)] = rows
        return {"user": user.name, "count": len(rows), "orders": rows}

    job = get_job_manager().start(f"orders:{user.name}", _fetch)
    return JSONResponse({"job_id": job.id})


@app.get("/api/orders/{user_name}")
async def api_orders_get(user_name: str) -> JSONResponse:
    rows = _orders_cache.get(("test", user_name))
    if rows is None:
        return JSONResponse({"user": user_name, "cached": False, "orders": []})
    return JSONResponse({"user": user_name, "cached": True, "orders": rows})


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
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
