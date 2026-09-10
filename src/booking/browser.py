from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import AppConfig

log = logging.getLogger(__name__)

# Starting many Chromium process trees simultaneously can saturate a small
# server before any page has a chance to load. This limiter affects the
# in-process web scheduler; CLI multiprocessing still manages its own workers.
BROWSER_LAUNCH_CONCURRENCY = 2
_launch_limiter: asyncio.Semaphore | None = None


def _get_launch_limiter() -> asyncio.Semaphore:
    global _launch_limiter
    if _launch_limiter is None:
        _launch_limiter = asyncio.Semaphore(BROWSER_LAUNCH_CONCURRENCY)
    return _launch_limiter


async def launch_persistent_context(cfg: AppConfig) -> tuple[BrowserContext, Path]:
    """Start Chromium with a persistent profile (cookies survive across runs)."""
    user_data = Path(cfg.user_data_dir).expanduser().resolve()
    user_data.mkdir(parents=True, exist_ok=True)
    async with _get_launch_limiter():
        playwright = await async_playwright().start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data),
                headless=cfg.headless,
                slow_mo=cfg.browser.slow_mo_ms,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            await playwright.stop()
            raise
    setattr(context, "_playwright", playwright)  # type: ignore[attr-defined]
    return context, user_data


async def wait_until_user_closes_window(cfg: AppConfig, page: Page) -> None:
    """In headed mode, block until the user closes the browser window.

    In headless mode this is a no-op so the runner can dispose the context
    immediately after the result is printed.
    """
    if cfg.headless or page.is_closed():
        return
    log.info("Done. Close the browser window to exit the program.")
    await page.wait_for_event("close", timeout=0)


async def dispose_context(context: BrowserContext) -> None:
    # Close the browser context and stop the underlying Playwright instance.
    playwright = getattr(context, "_playwright", None)
    try:
        await context.close()
    except Exception:
        pass
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            pass
