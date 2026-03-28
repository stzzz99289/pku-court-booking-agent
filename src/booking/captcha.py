from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class CaptchaSolver(Protocol):
    """bytes in (PNG/JPEG crop) → text to type into the captcha field."""

    async def solve(self, image_bytes: bytes) -> str: ...


class ManualCaptchaSolver:
    """Prompt on stdin (blocking); suitable for interactive runs."""

    async def solve(self, image_bytes: bytes) -> str:
        path_hint = f"(image size {len(image_bytes)} bytes)"
        print(f"Captcha image received {path_hint}. Enter captcha text:", flush=True)
        return input().strip()


class StubCaptchaSolver:
    """Fixed answer for dry-run / tests only."""

    def __init__(self, answer: str = "0000") -> None:
        self._answer = answer

    async def solve(self, image_bytes: bytes) -> str:
        log.warning("StubCaptchaSolver returning fixed answer (not for production).")
        return self._answer


class EnvCaptchaSolver:
    """Read answer from CAPTCHA_ANSWER env (useful for scripted local tests)."""

    async def solve(self, image_bytes: bytes) -> str:
        import os

        v = os.environ.get("CAPTCHA_ANSWER", "").strip()
        if not v:
            raise RuntimeError(
                "EnvCaptchaSolver requires CAPTCHA_ANSWER in the environment."
            )
        return v


def make_solver(provider: str, api_key: str) -> CaptchaSolver:
    p = (provider or "manual").strip().lower()
    if p in ("manual", ""):
        return ManualCaptchaSolver()
    if p == "stub":
        return StubCaptchaSolver()
    if p == "env":
        return EnvCaptchaSolver()
    if p in ("2captcha", "twocaptcha"):
        return TwoCaptchaPlaceholderSolver(api_key=api_key)
    raise ValueError(
        f"Unknown captcha provider: {provider!r}. "
        "Use manual, stub, env, or 2captcha (placeholder)."
    )


class TwoCaptchaPlaceholderSolver:
    """Reserved for a future 2Captcha API integration."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def solve(self, image_bytes: bytes) -> str:
        raise NotImplementedError(
            "2Captcha integration is not implemented yet; set captcha.provider to "
            "manual, stub, or env, or extend TwoCaptchaPlaceholderSolver."
        )


async def solve_and_fill(
    page: object,
    image_selector: str,
    input_selector: str,
    solver: CaptchaSolver,
) -> None:
    """Crop captcha via ``locator(image_selector).screenshot()`` (matches MCP uid crops)."""
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
