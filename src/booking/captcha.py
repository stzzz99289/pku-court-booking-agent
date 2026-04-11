from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

_CAPTCHA_DIR = Path("data/captcha")
_MAX_CAPTCHA_FILES = 100


@runtime_checkable
class CaptchaSolver(Protocol):
    """bytes in (PNG/JPEG crop) → text to type into the captcha field."""

    async def solve(self, image_bytes: bytes) -> str: ...


class ManualCaptchaSolver:
    """Prompt on stdin (blocking); suitable for debug / interactive runs."""

    async def solve(self, image_bytes: bytes) -> str:
        # Print image size hint so the user knows a captcha arrived.
        print(f"Captcha image received ({len(image_bytes)} bytes). Enter captcha text:", flush=True)
        return input().strip()


def save_captcha_image(image_bytes: bytes, captcha_type: str, label: str = "") -> None:
    """Save a captcha PNG to data/captcha/{captcha_type}_{label_or_timestamp}.png.

    If label is provided (e.g. the ground-truth answer), it is used in the filename
    instead of a timestamp. Keeps at most _MAX_CAPTCHA_FILES files in the folder.
    """
    # Ensure the output directory exists.
    _CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)

    # Evict oldest files if we are at the limit.
    existing = sorted(_CAPTCHA_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    while len(existing) >= _MAX_CAPTCHA_FILES:
        existing.pop(0).unlink()

    # Use label (ground truth) if provided, otherwise fall back to timestamp.
    suffix = label or str(int(time.time() * 1000))
    path = _CAPTCHA_DIR / f"{captcha_type}_{suffix}.png"
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
    text = await solver.solve(png)
    await page.locator(input_selector).fill(text)

    if save_captcha:
        save_captcha_image(png, captcha_type, label=text)
