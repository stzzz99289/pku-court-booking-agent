"""Booking CAPTCHA solver using ddddocr.

The booking CAPTCHA contains 4 Chinese characters scattered across a photo
background at varying positions and rotations. The solver:
  1. Detects bounding boxes of all characters via ddddocr detection mode.
  2. Crops each character and recognises it with ddddocr OCR mode.
  3. Returns a list of CharResult (box + character) sorted left-to-right.

Note: ddddocr accuracy degrades on heavily rotated characters. For the
booking flow the caller typically needs to click the characters in a
prompted order, so position accuracy matters most.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import ddddocr
from PIL import Image


@dataclass
class CharResult:
    """A recognised character and its bounding box in the original image."""
    char: str
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    @property
    def area(self) -> int:
        return (self.x2 - self.x1) * (self.y2 - self.y1)


class BookingCaptchaSolver:
    """Detects and recognises the 4 Chinese characters in the booking CAPTCHA."""

    # Number of characters expected in every booking CAPTCHA.
    CHAR_COUNT = 4

    def __init__(self) -> None:
        # show_ad=False suppresses the ddddocr startup banner.
        self._det = ddddocr.DdddOcr(det=True, show_ad=False, beta=True)
        self._ocr = ddddocr.DdddOcr(show_ad=False, beta=True)

    def solve(self, image_bytes: bytes) -> list[CharResult]:
        """Return CHAR_COUNT CharResults sorted left-to-right by x position.

        If detection finds more boxes than expected (e.g. UI chrome like the
        refresh icon), the largest boxes by area are kept.
        """
        boxes = self._det.detection(image_bytes)

        # Keep only the CHAR_COUNT largest boxes to filter out UI noise.
        boxes = sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        boxes = boxes[: self.CHAR_COUNT]

        # Sort remaining boxes left-to-right for consistent ordering.
        boxes.sort(key=lambda b: b[0])

        img = Image.open(io.BytesIO(image_bytes))
        results: list[CharResult] = []
        for x1, y1, x2, y2 in boxes:
            crop = img.crop((x1, y1, x2, y2))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            char = self._ocr.classification(buf.getvalue())
            results.append(CharResult(char=char, x1=x1, y1=y1, x2=x2, y2=y2))

        return results

    async def solve_async(self, image_bytes: bytes) -> list[CharResult]:
        """Async wrapper for use in the booking pipeline."""
        return self.solve(image_bytes)
