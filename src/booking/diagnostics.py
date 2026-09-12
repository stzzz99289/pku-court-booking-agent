from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page


log = logging.getLogger(__name__)

_CRASH_DUMP_DIR = Path("debugging") / "crashes"
_CAPTURE_TIMEOUT_S = 4.0

_VISIBLE_DIALOGS_JS = r"""
() => {
  const sels = ['.ivu-modal-wrap', '.ivu-modal', '.el-message-box', '.verifybox',
                '[class*="modal"]', '[class*="dialog"]', '[class*="message"]', '[role="dialog"]'];
  const seen = new Set();
  const out = [];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const cs = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0 || cs.display === 'none' || cs.visibility === 'hidden') continue;
      if (el.classList.contains('ivu-modal-hidden')) continue;
      out.push({
        selector: sel,
        class_name: (el.className && el.className.toString()).slice(0, 300),
        text: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 500),
        rect: {x: Math.round(rect.x), y: Math.round(rect.y),
               width: Math.round(rect.width), height: Math.round(rect.height)},
      });
    }
  }
  return out;
}
"""


async def _bounded(awaitable, *, fallback: Any) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=_CAPTURE_TIMEOUT_S)
    except Exception as exc:
        return {"capture_error": f"{type(exc).__name__}: {exc}"} if isinstance(fallback, dict) else fallback


async def dump_crash_diagnostics(
    page: Page | None,
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
) -> Path | None:
    """Best-effort screenshot, HTML, and state capture before a crashed browser closes."""
    try:
        now = datetime.now()
        day_dir = _CRASH_DUMP_DIR / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_")[:80] or "worker"
        base = day_dir / f"{now.strftime('%H%M%S_%f')}_{safe_label}"

        screenshot_error: str | None = "page unavailable"
        html_error: str | None = "page unavailable"
        title: Any = ""
        dialogs: Any = []
        url = ""
        if page is not None:
            url = page.url
            screenshot_error = None
            try:
                await asyncio.wait_for(
                    page.screenshot(
                        path=str(base.with_suffix(".png")),
                        full_page=True,
                        animations="disabled",
                        timeout=int(_CAPTURE_TIMEOUT_S * 1000),
                    ),
                    timeout=_CAPTURE_TIMEOUT_S + 0.5,
                )
            except Exception as exc:
                screenshot_error = f"{type(exc).__name__}: {exc}"

            html_error = None
            try:
                html = await asyncio.wait_for(page.content(), timeout=_CAPTURE_TIMEOUT_S)
                base.with_suffix(".html").write_text(html, encoding="utf-8")
            except Exception as exc:
                html_error = f"{type(exc).__name__}: {exc}"

            title = await _bounded(page.title(), fallback="")
            dialogs = await _bounded(page.evaluate(_VISIBLE_DIALOGS_JS), fallback=[])
        payload = {
            "timestamp": now.isoformat(),
            "label": label,
            "url": url,
            "title": title,
            "visible_dialogs": dialogs,
            "screenshot_error": screenshot_error,
            "html_error": html_error,
            "metadata": metadata or {},
        }
        base.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        log.error(
            "Worker crash diagnostics saved to %s.json (screenshot and HTML are best-effort).",
            base,
        )
        return base
    except Exception as exc:
        log.warning("Failed to save worker crash diagnostics: %s", exc)
        return None
