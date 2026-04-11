"""Factory: create login / booking captcha solvers from AppConfig.

Credentials are read from captcha config (user_config.yaml):

  captcha:
    username: "YOUR_USERNAME"
    api_key:  "YOUR_PASSWORD"
    softid:   "YOUR_SOFTID"

When all three are set the chaojiying API is used.  If any credential is
missing, or if the API call fails at runtime, a warning is printed and the
local ddddocr solver is used as a fallback.

Returned solver interfaces expected by the booking pipeline:
  - login solver:   solve(image_bytes: bytes) -> str
  - booking solver: solve(image_bytes: bytes, instruction: str) -> list[ClickPoint]
                    instruction: comma-separated characters to locate, e.g. "你,信,解,破"
"""

from __future__ import annotations

import logging

from .api.base import ClickPoint
from .api.chaojiying import ChaoJiYingProvider

log = logging.getLogger(__name__)


def make_login_solver(api_key: str, username: str = "", softid: str = ""):
    """Return a login captcha solver (image → digit string)."""
    cjy = _build_chaojiying(username, api_key, softid)
    if cjy is None:
        from .login_captcha import LoginCaptchaSolver
        return LoginCaptchaSolver()
    return _ApiLoginSolver(cjy)


def make_booking_solver(api_key: str, username: str = "", softid: str = ""):
    """Return a booking captcha solver (image + instruction → click coordinates)."""
    cjy = _build_chaojiying(username, api_key, softid)
    if cjy is None:
        from .booking_captcha import BookingCaptchaSolver
        return BookingCaptchaSolver()
    return _ApiBookingSolver(cjy)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _build_chaojiying(username: str, api_key: str, softid: str) -> ChaoJiYingProvider | None:
    if not all([username, api_key, softid]):
        log.info("ChaoJiYing credentials not fully configured; using local ddddocr solver.")
        return None
    return ChaoJiYingProvider(username=username, password=api_key, softid=softid)


class _ApiLoginSolver:
    def __init__(self, provider: ChaoJiYingProvider) -> None:
        self._provider = provider

    def solve(self, image_bytes: bytes) -> str:
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self.solve_async(image_bytes))

    async def solve_async(self, image_bytes: bytes) -> str:
        try:
            return await self._provider.solve_text(image_bytes)
        except Exception as e:
            from .login_captcha import LoginCaptchaSolver
            print(f"[captcha] ChaoJiYing API error: {e}  — falling back to local ddddocr")
            return LoginCaptchaSolver().solve(image_bytes)


class _ApiBookingSolver:
    def __init__(self, provider: ChaoJiYingProvider) -> None:
        self._provider = provider

    def solve(self, image_bytes: bytes, instruction: str) -> list[ClickPoint]:
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.solve_async(image_bytes, instruction)
        )

    async def solve_async(self, image_bytes: bytes, instruction: str) -> list[ClickPoint]:
        try:
            return await self._provider.solve_click(image_bytes, instruction)
        except Exception as e:
            from .booking_captcha import BookingCaptchaSolver
            print(f"[captcha] ChaoJiYing API error: {e}  — falling back to local ddddocr")
            local = BookingCaptchaSolver()
            results = local.solve(image_bytes)
            return [ClickPoint(x=r.center[0], y=r.center[1]) for r in results]
