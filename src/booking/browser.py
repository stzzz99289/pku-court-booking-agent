from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import AppConfig

log = logging.getLogger(__name__)


async def launch_persistent_context(cfg: AppConfig) -> tuple[BrowserContext, Path]:
    """Start Chromium with a persistent profile (cookies survive across runs)."""
    user_data = Path(cfg.user_data_dir).expanduser().resolve()
    user_data.mkdir(parents=True, exist_ok=True)
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
