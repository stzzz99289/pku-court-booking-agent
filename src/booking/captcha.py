from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

_CAPTCHA_DIR = Path("data/captcha")
_MAX_CAPTCHA_FILES = 10


@runtime_checkable
class CaptchaSolver(Protocol):
    """bytes in (PNG/JPEG crop) → text to type into the captcha field."""

    async def solve(self, image_bytes: bytes) -> str: ...


class ManualCaptchaSolver:
    """Prompt on stdin (blocking); suitable for interactive runs."""

    async def solve(self, image_bytes: bytes) -> str:
        # Print image size hint so the user knows a captcha arrived.
        print(f"Captcha image received ({len(image_bytes)} bytes). Enter captcha text:", flush=True)
        return input().strip()


class StubCaptchaSolver:
    """Fixed answer for dry-run / tests only."""

    def __init__(self, answer: str = "0000") -> None:
        self._answer = answer

    async def solve(self, image_bytes: bytes) -> str:
        # Return the hardcoded stub answer and warn that this is not for production.
        log.warning("StubCaptchaSolver returning fixed answer (not for production).")
        return self._answer


class EnvCaptchaSolver:
    """Read answer from CAPTCHA_ANSWER env (useful for scripted local tests)."""

    async def solve(self, image_bytes: bytes) -> str:
        # Read the answer from the environment variable, raising if it is missing.
        import os
        v = os.environ.get("CAPTCHA_ANSWER", "").strip()
        if not v:
            raise RuntimeError("EnvCaptchaSolver requires CAPTCHA_ANSWER in the environment.")
        return v


class TwoCaptchaPlaceholderSolver:
    """Reserved for a future 2Captcha API integration."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def solve(self, image_bytes: bytes) -> str:
        # Raise until the real 2Captcha integration is implemented.
        raise NotImplementedError(
            "2Captcha integration is not implemented yet; set captcha.provider to "
            "manual, stub, or env, or extend TwoCaptchaPlaceholderSolver."
        )


def make_solver(provider: str, api_key: str) -> CaptchaSolver:
    # Instantiate the correct solver based on the provider name.
    p = (provider or "manual").strip().lower()
    if p in ("manual", ""):
        return ManualCaptchaSolver()
    if p == "stub":
        return StubCaptchaSolver()
    if p == "env":
        return EnvCaptchaSolver()
    if p in ("2captcha", "twocaptcha"):
        return TwoCaptchaPlaceholderSolver(api_key=api_key)
    raise ValueError(f"Unknown captcha provider: {provider!r}. Use manual, stub, env, or 2captcha (placeholder).")


def save_captcha_image(image_bytes: bytes, captcha_type: str) -> None:
    """Save a captcha PNG to data/captcha/{captcha_type}_{timestamp}.png.

    Keeps at most _MAX_CAPTCHA_FILES files in the folder by deleting the oldest first.
    """
    # Ensure the output directory exists.
    _CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)

    # Evict oldest files if we are at the limit.
    existing = sorted(_CAPTCHA_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    while len(existing) >= _MAX_CAPTCHA_FILES:
        existing.pop(0).unlink()

    # Write the new file with a millisecond timestamp to keep names unique across runs.
    timestamp = int(time.time() * 1000)
    path = _CAPTCHA_DIR / f"{captcha_type}_{timestamp}.png"
    path.write_bytes(image_bytes)
    log.info("Saved captcha image: %s", path)


async def solve_and_fill(
    page: object,
    image_selector: str,
    input_selector: str,
    solver: CaptchaSolver,
    *,
    save_captcha: bool = False,
    captcha_type: str = "captcha",
) -> None:
    """Screenshot the captcha element, optionally save it, solve it, and fill the answer."""
    from playwright.async_api import Page

    if not isinstance(page, Page):
        raise TypeError("page must be a Playwright Page")
    if not image_selector.strip() or not input_selector.strip():
        raise ValueError("captcha image and input selectors are required")

    loc = page.locator(image_selector)
    await loc.wait_for(state="visible", timeout=60_000)
    png = await loc.screenshot()

    if save_captcha:
        save_captcha_image(png, captcha_type)

    text = await solver.solve(png)
    await page.locator(input_selector).fill(text)
