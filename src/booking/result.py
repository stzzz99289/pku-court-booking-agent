from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BookingResult:
    """Outcome of a booking run for main() to print or log."""

    success: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
