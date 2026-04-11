"""Shared types and protocol for remote captcha-solving API providers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Default polling settings (providers may override these).
DEFAULT_POLL_INTERVAL = 5.0   # seconds between result polls
DEFAULT_TIMEOUT = 120.0       # seconds before giving up


@dataclass
class ClickPoint:
    """A pixel coordinate to click in the captcha image."""
    x: int
    y: int


@runtime_checkable
class ApiCaptchaProvider(Protocol):
    """Interface every remote captcha provider must implement."""

    async def solve_text(self, image_bytes: bytes) -> str:
        """Submit a text/digit captcha image; return the recognised string."""
        ...

    async def solve_click(self, image_bytes: bytes, instruction: str) -> list[ClickPoint]:
        """Submit a click-based captcha image and a text instruction;
        return the list of (x, y) coordinates to click, in order."""
        ...


async def poll_until_ready(
    fetch_result,           # async callable () -> dict
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_POLL_INTERVAL,
    provider_name: str = "api",
) -> dict:
    """Repeatedly call fetch_result() until status == 'ready' or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        await asyncio.sleep(interval)
        result = await fetch_result()
        if result.get("status") == "ready":
            return result
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"{provider_name}: task did not complete within {timeout:.0f}s"
            )
