"""Local OCR captcha solver using ddddocr (带带弟弟OCR).

Used for the 4-digit login captcha. Runs entirely offline — no API tokens needed.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class DdddocrSolver:
    """Solve simple text captchas with the ddddocr library."""

    def __init__(self) -> None:
        import ddddocr
        self._ocr = ddddocr.DdddOcr(show_ad=False)

    async def solve(self, image_bytes: bytes) -> str:
        # Run OCR on the captcha image and return the recognized text.
        result = self._ocr.classification(image_bytes)
        log.info("ddddocr recognized: %s", result)
        return result
