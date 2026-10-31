"""Match reservation date buttons independently of the site's display format."""
from __future__ import annotations

import re
from datetime import date, datetime


_LABEL_PATTERNS = (
    re.compile(r"(?<![0-9/])([0-9]{1,2})/([0-9]{1,2})(?![0-9/])"),
    re.compile(r"(?<![0-9])([0-9]{1,2})月([0-9]{1,2})日(?![0-9])"),
)


def parse_booking_date(value: str) -> date | None:
    """Accept only a real YYYYMMDD date, not a partially sliced config value."""
    value = (value or "").strip()
    if not re.fullmatch(r"[0-9]{8}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def date_button_matches(text: str, target: date) -> bool:
    """Recognize MM/DD and legacy MM月DD日, with optional weekday text.

    Compare numeric components, not substrings: 1/1 must not select 11/01.
    Apply only to the reservation date buttons, never the release-time notice.
    """
    return any(
        (int(match[1]), int(match[2])) == (target.month, target.day)
        for pattern in _LABEL_PATTERNS
        for match in pattern.finditer(text)
    )
