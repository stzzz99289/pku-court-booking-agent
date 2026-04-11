from __future__ import annotations

import logging
import re

from playwright.async_api import Page

from .captcha import CaptchaSolver, solve_and_fill
from .config import AppConfig

log = logging.getLogger(__name__)

_LOGIN_CAPTCHA_MAX_RETRIES = 3


def _sel(cfg: AppConfig, name: str) -> str:
    # Return a stripped selector string from config, or "" if unset.
    s = getattr(cfg.selectors, name, "") or ""
    return s.strip()


async def _alumni_login_form_visible(page: Page) -> bool:
    # Check if the alumni phone-number textbox is present on the current page.
    return await page.get_by_role("textbox", name="手机号").count() > 0


async def _session_looks_logged_in(page: Page) -> bool:
    # Heuristic: authenticated pages show "退出" or "欢迎您"; unauthenticated ones show "登录".
    logout = page.get_by_text("退出", exact=True)
    if await logout.count() > 0 and await logout.first.is_visible():
        return True
    welcome = page.get_by_text("欢迎您", exact=False)
    if await welcome.count() > 0 and await welcome.first.is_visible():
        return True
    return False


async def _open_login_form(page: Page, cfg: AppConfig) -> None:
    """Click the login button to reach the login form, or verify we're already there."""
    if "/venue/login" in page.url:
        return
    if await _alumni_login_form_visible(page):
        return
    login_btn = _sel(cfg, "login_button")
    if not login_btn:
        raise ValueError(
            "selectors.login_button is required to open the login form from the home page, "
            "or set base_url to the login URL (…/venue/login)."
        )
    login_loc = page.locator(login_btn)
    if not await login_loc.count():
        raise RuntimeError(f"Login control not found: {login_btn!r}")
    await login_loc.first.click()
    await page.wait_for_load_state("domcontentloaded")


async def ensure_logged_in(page: Page, cfg: AppConfig, solver: CaptchaSolver) -> None:
    """Skip if already logged in; otherwise complete the login flow for the configured method."""
    # Return early if a valid session is detected.
    ind = _sel(cfg, "logged_in_indicator")
    if ind and await page.locator(ind).count() and await page.locator(ind).first.is_visible():
        log.info("Session appears logged in (logged_in_indicator visible).")
        return
    if await _session_looks_logged_in(page):
        log.info("Session appears logged in (saved session / heuristic).")
        return

    method = (cfg.login_method or "alumni").strip().lower()
    await _open_login_form(page, cfg)

    if method == "iaaa":
        tab = _sel(cfg, "login_mode_iaaa")
        if tab:
            await page.locator(tab).first.click()
        raise NotImplementedError(
            "IAAA (校内师生) login is not implemented yet; set login_method: alumni "
            "or configure logged_in_indicator after a manual IAAA login."
        )

    if method == "alumni":
        # Switch to the alumni tab, fill credentials, solve login captcha if present.
        tab = _sel(cfg, "login_mode_alumni")
        if tab:
            await page.locator(tab).first.click()
        user = _sel(cfg, "username_input")
        pwd = _sel(cfg, "password_input")
        if not user or not pwd:
            raise ValueError("selectors.username_input (phone) and password_input are required for alumni login.")
        await page.locator(user).first.fill(cfg.account)
        await page.locator(pwd).first.fill(cfg.password)
        cap_img = _sel(cfg, "login_captcha_image")
        cap_in = _sel(cfg, "login_captcha_input")
        if cap_img and cap_in:
            refresh = _sel(cfg, "login_captcha_refresh")
            if refresh:
                await page.locator(refresh).first.click()
            # Retry loop: login captcha must be exactly 4 digits; refresh and re-solve otherwise.
            for attempt in range(_LOGIN_CAPTCHA_MAX_RETRIES):
                await solve_and_fill(page, cap_img, cap_in, solver,
                                     save_captcha=cfg.save_captcha, captcha_type="login")
                answer = await page.locator(cap_in).first.input_value()
                if re.fullmatch(r"\d{4}", answer):
                    break
                log.warning("Login captcha answer %r is not 4 digits (attempt %d/%d), retrying.",
                            answer, attempt + 1, _LOGIN_CAPTCHA_MAX_RETRIES)
                if refresh:
                    await page.locator(refresh).first.click()
            else:
                log.warning("Failed to get a valid 4-digit captcha answer after %d attempts; proceeding anyway.",
                            _LOGIN_CAPTCHA_MAX_RETRIES)

        # Submit and wait for the SPA to redirect away from the login page (JWT is stored on redirect).
        submit = _sel(cfg, "login_submit")
        if not submit:
            raise ValueError("selectors.login_submit is required to finish login.")
        await page.locator(submit).first.click()
        try:
            await page.wait_for_url(lambda url: "/venue/login" not in url, timeout=15_000)
        except Exception:
            log.warning("Login redirect did not occur within 15s; proceeding anyway.")
        await page.wait_for_load_state("domcontentloaded")
        return

    raise ValueError(f"Unknown login_method: {cfg.login_method!r}; use 'alumni' or 'iaaa'.")
