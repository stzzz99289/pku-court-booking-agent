"""Durable record of the last time the booking site accepted a session."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

MARKER_NAME = ".last_session_verified"


def mark_session_verified(user_data_dir: str | Path) -> None:
    """Record that the site just accepted this browser profile's session."""
    try:
        profile = Path(user_data_dir).expanduser().resolve()
        profile.mkdir(parents=True, exist_ok=True)
        (profile / MARKER_NAME).touch()
    except OSError as exc:
        # Status reporting must never interrupt a booking or order query.
        log.warning("Could not record session verification time: %s", exc)


def last_session_verified(user_data_dir: str | Path) -> datetime | None:
    """Return the last explicit verification time, or None if never recorded."""
    marker = Path(user_data_dir).expanduser().resolve() / MARKER_NAME
    try:
        return datetime.fromtimestamp(marker.stat().st_mtime)
    except (OSError, ValueError):
        return None
