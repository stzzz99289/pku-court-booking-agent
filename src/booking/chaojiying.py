"""Chaojiying (超级鹰) captcha solving API client.

API docs:
  https://www.chaojiying.com/api-5.html   (general API)
  https://www.chaojiying.com/api-98.html  (coordinate recognition types)

Used only for the booking click-captcha — login captcha is solved locally by
DdddocrSolver. Codetype 9801 locates a specific list of Chinese characters in
the image and returns one (x,y) per requested char, in instruction order.
"""

from __future__ import annotations

import base64
import hashlib
import logging

import httpx

log = logging.getLogger(__name__)

_API_URL = "http://upload.chaojiying.net/Upload/Processing.php"
_CODETYPE_BOOKING = "9801"  # locate specific chars by instruction; returns coords in order

# Per-call deadline. The SDK occasionally hangs to its default 30 s timeout;
# bound it tightly and retry once so the outer booking loop sees a fast
# signal (worst case 2*8 s instead of 30 s).
_PER_CALL_TIMEOUT_S = 8.0
_MAX_CALL_ATTEMPTS = 2


class ChaojiyingSolver:
    """Solve PKU booking click-captchas via the Chaojiying HTTP API (codetype 9801)."""

    def __init__(self, username: str, password: str, softid: str) -> None:
        self._username = username
        self._pass2 = hashlib.md5(password.encode()).hexdigest()
        self._softid = softid

    async def solve_click(self, image_bytes: bytes, instruction: str) -> list[tuple[int, int]]:
        """Locate the chars named in `instruction` and return click coords in order.

        Args:
            image_bytes: PNG bytes of the captcha image.
            instruction: Comma-separated chars to locate, e.g. "转,线,导".

        Returns: list of (x, y) tuples, one per char in `instruction`.
        """
        data = {
            "user": self._username,
            "pass2": self._pass2,
            "softid": self._softid,
            "codetype": _CODETYPE_BOOKING,
            "file_base64": base64.b64encode(image_bytes).decode(),
            "str_debug": f"{{8a:{instruction}/8a}}",
        }
        async with httpx.AsyncClient(trust_env=False, timeout=_PER_CALL_TIMEOUT_S) as client:
            resp = None
            last_exc: Exception | None = None
            for attempt in range(1, _MAX_CALL_ATTEMPTS + 1):
                try:
                    resp = await client.post(_API_URL, data=data)
                    last_exc = None
                    break
                except httpx.TimeoutException as e:
                    last_exc = e
                    log.warning(
                        "Chaojiying API timed out after %.1fs (attempt %d/%d).",
                        _PER_CALL_TIMEOUT_S, attempt, _MAX_CALL_ATTEMPTS,
                    )
            if resp is None:
                raise RuntimeError(
                    f"Chaojiying API exhausted {_MAX_CALL_ATTEMPTS} timeout attempts."
                ) from last_exc
            result = resp.json()
        err_no = result.get("err_no", -1)
        if err_no != 0:
            log.error("Chaojiying API error %s: %s", err_no, result.get("err_str", "unknown"))
            raise RuntimeError(f"Chaojiying API error {err_no}: {result}")

        pic_str = result.get("pic_str", "")
        log.info("Chaojiying solved click captcha (instruction=%s): %s", instruction, pic_str)
        coords: list[tuple[int, int]] = []
        for pair in pic_str.split("|"):
            parts = pair.strip().split(",")
            if len(parts) == 2:
                coords.append((int(parts[0]), int(parts[1])))
        return coords
