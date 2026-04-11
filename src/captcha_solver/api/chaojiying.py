"""ChaoJiYing (超级鹰) provider.

API docs:
  https://www.chaojiying.com/api-5.html   (general API)
  https://www.chaojiying.com/api-98.html  (coordinate recognition types)

Authentication uses username + MD5(password) + softid.
No polling needed — the endpoint returns the result synchronously.

Codetypes used:
  1902 — login captcha: short alphanumeric text (4–5 chars)
  9801 — booking captcha: locate specific chars in order via {8a:字1,字2,字3/8a} instruction;
         response pic_str: "x1,y1|x2,y2|x3,y3" — coordinates in instruction order
"""

from __future__ import annotations

import base64
import hashlib
import os

import httpx

from .base import ClickPoint

_ENDPOINT = "https://upload.chaojiying.net/Upload/Processing.php"
_CODETYPE_LOGIN   = 1902   # short alphanumeric text
_CODETYPE_BOOKING = 9801   # locate specific chars by instruction; returns coords in order


class ChaoJiYingProvider:
    """Captcha solver backed by the ChaoJiYing (超级鹰) HTTP API."""

    name = "chaojiying"

    def __init__(self, username: str, password: str, softid: str) -> None:
        self._user   = username
        self._pass2  = hashlib.md5(password.encode()).hexdigest()
        self._softid = softid

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def solve_text(self, image_bytes: bytes) -> str:
        """Solve a short text/digit captcha; returns the recognised string."""
        result = await self._post(self._base_form(image_bytes, _CODETYPE_LOGIN))
        return result["pic_str"]

    async def solve_click(self, image_bytes: bytes, instruction: str) -> list[ClickPoint]:
        """Locate specific Chinese characters and return click coordinates in order.

        Args:
            image_bytes: PNG/JPEG bytes of the captcha image.
            instruction:  Comma-separated characters to locate in click order,
                          e.g. "唱,呀,流".
        Response pic_str format: "x1,y1|x2,y2|x3,y3" — one pair per instruction char.
        """
        data = self._base_form(image_bytes, _CODETYPE_BOOKING)
        data["str_debug"] = f"{{8a:{instruction}/8a}}"
        result = await self._post(data)
        return _parse_coordinates(result["pic_str"])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _base_form(self, image_bytes: bytes, codetype: int) -> dict:
        return {
            "user":        self._user,
            "pass2":       self._pass2,
            "softid":      self._softid,
            "codetype":    str(codetype),
            "file_base64": base64.b64encode(image_bytes).decode(),
        }

    async def _post(self, data: dict) -> dict:
        async with httpx.AsyncClient(timeout=60, **_proxy_kwargs()) as client:
            resp = await client.post(_ENDPOINT, data=data)
            resp.raise_for_status()
            result = resp.json()
        if result.get("err_no", -1) != 0:
            raise RuntimeError(
                f"ChaoJiYing error {result.get('err_no')}: {result.get('err_str')} "
                f"(pic_id={result.get('pic_id')})"
            )
        return result


def _proxy_kwargs() -> dict:
    """Build httpx proxy kwargs from environment variables, skipping SOCKS entries.

    httpx supports HTTP/HTTPS proxies but not SOCKS (without extra deps).
    Rather than letting httpx read all proxy env vars via trust_env=True (which
    would fail if ALL_PROXY is a socks:// URL), we always disable trust_env and
    manually forward only the HTTP/HTTPS proxy when one is present.
    """
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        url = os.environ.get(var, "").strip()
        if url and not url.lower().startswith("socks"):
            return {"trust_env": False, "proxy": url}

    # No usable proxy found — connect directly.
    return {"trust_env": False}


def _parse_coordinates(pic_str: str) -> list[ClickPoint]:
    """Parse 'x1,y1|x2,y2|x3,y3' into a list of ClickPoint."""
    points = []
    for pair in pic_str.strip().split("|"):
        x_str, y_str = pair.split(",")
        points.append(ClickPoint(x=int(x_str), y=int(y_str)))
    return points
