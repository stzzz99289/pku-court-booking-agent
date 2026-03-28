"""Login CAPTCHA solver using ddddocr.

The login CAPTCHA is a 4-digit numeric single-line image.
"""

from __future__ import annotations

import ddddocr


class LoginCaptchaSolver:
    """Solves the 4-digit numeric login CAPTCHA using ddddocr (offline)."""

    def __init__(self) -> None:
        # show_ad=False suppresses the ddddocr startup banner
        self._ocr = ddddocr.DdddOcr(show_ad=False)

    def solve(self, image_bytes: bytes) -> str:
        """Return the recognised digit string for the given PNG/JPEG bytes."""
        return self._ocr.classification(image_bytes)

    async def solve_async(self, image_bytes: bytes) -> str:
        """Async wrapper so this solver satisfies the CaptchaSolver protocol."""
        return self.solve(image_bytes)
