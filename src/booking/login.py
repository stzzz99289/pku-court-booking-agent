from __future__ import annotations

import logging
import re

from playwright.async_api import Page

from .captcha import CaptchaSolver, solve_and_fill
from .config import AppConfig

log = logging.getLogger(__name__)

_LOGIN_CAPTCHA_MAX_RETRIES = 3
_LOGIN_SUBMIT_MAX_ATTEMPTS = 4
_LOGIN_REDIRECT_TIMEOUT_MS = 6_000


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


async def _dismiss_please_login_modal(page: Page) -> bool:
    """If the home page is fronted by a '请登录后访问' modal, click 确定. Returns True if dismissed."""
    modal = page.get_by_text("请登录后访问", exact=True)
    try:
        await modal.wait_for(state="visible", timeout=500)
    except Exception:
        return False
    log.info("'请登录后访问' modal detected on home page; dismissing before login.")
    try:
        await page.get_by_role("button", name="确定").first.click()
        await page.wait_for_timeout(300)
        return True
    except Exception as e:
        log.warning("Failed to dismiss '请登录后访问' modal: %s", e)
        return False


async def _open_login_form(page: Page, cfg: AppConfig) -> None:
    """Click the login button to reach the login form, or verify we're already there."""
    if "/venue/login" in page.url:
        return
    if await _alumni_login_form_visible(page):
        return

    # Some sessions land on the home page with a '请登录后访问' modal covering
    # the login button — dismiss it before probing for the control.
    await _dismiss_please_login_modal(page)

    login_btn = _sel(cfg, "login_button")
    if not login_btn:
        raise ValueError(
            "selectors.login_button is required to open the login form from the home page, "
            "or set base_url to the login URL (…/venue/login)."
        )
    login_loc = page.locator(login_btn)
    # SPA race: domcontentloaded can fire before Vue paints the header. Wait
    # briefly for either the login button OR a logged-in indicator to show up.
    try:
        await login_loc.first.wait_for(state="visible", timeout=5_000)
    except Exception:
        # Re-check session heuristics — maybe '退出' rendered late and we're
        # actually already logged in.
        if await _session_looks_logged_in(page):
            log.info("Login button never appeared but session indicators visible; treating as logged in.")
            return
        # One more chance: dismiss a late-arriving 请登录 modal and retry.
        if await _dismiss_please_login_modal(page):
            try:
                await login_loc.first.wait_for(state="visible", timeout=3_000)
            except Exception:
                pass
    if not await login_loc.count():
        # Dump some context to help next-time debugging.
        try:
            url = page.url
            title = await page.title()
            body_snip = (await page.locator("body").first.inner_text())[:300]
        except Exception:
            url, title, body_snip = "?", "?", "?"
        log.error(
            "Login control %r not found. url=%s title=%r body[:300]=%r",
            login_btn, url, title, body_snip,
        )
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
        await _alumni_login(page, cfg, solver)
        return

    raise ValueError(f"Unknown login_method: {cfg.login_method!r}; use 'alumni' or 'iaaa'.")


async def _alumni_login(page: Page, cfg: AppConfig, solver: CaptchaSolver) -> None:
    """Alumni (phone + password + captcha) login with retry on captcha-rejection."""
    tab = _sel(cfg, "login_mode_alumni")
    if tab:
        await page.locator(tab).first.click()
    user_sel = _sel(cfg, "username_input")
    pwd_sel = _sel(cfg, "password_input")
    if not user_sel or not pwd_sel:
        raise ValueError("selectors.username_input (phone) and password_input are required for alumni login.")
    submit_sel = _sel(cfg, "login_submit")
    if not submit_sel:
        raise ValueError("selectors.login_submit is required to finish login.")
    cap_img = _sel(cfg, "login_captcha_image")
    cap_in = _sel(cfg, "login_captcha_input")
    refresh_sel = _sel(cfg, "login_captcha_refresh")

    # Outer retry loop: if the server rejects the submit (we stay on /venue/login),
    # inspect the error modal. Captcha misses get a fresh captcha + retry; other errors
    # (e.g. wrong password) raise immediately since retry won't help.
    last_error: str | None = None
    for attempt in range(1, _LOGIN_SUBMIT_MAX_ATTEMPTS + 1):
        await page.locator(user_sel).first.fill(cfg.account)
        await page.locator(pwd_sel).first.fill(cfg.password)
        if cap_img and cap_in:
            await _fill_login_captcha(page, cfg, solver, cap_img, cap_in, refresh_sel)
        await page.locator(submit_sel).first.click()
        if await _login_redirected(page):
            await page.wait_for_load_state("domcontentloaded")
            return

        error_text = await _read_login_error(page, cfg)
        last_error = error_text
        if error_text and "验证码" not in error_text:
            # Unrecoverable (wrong phone/password/etc). Dismiss and bail.
            await _dismiss_login_error(page, cfg)
            raise RuntimeError(f"Login rejected: {error_text}")

        log.warning(
            "Login submit %d/%d rejected (%s). Refreshing captcha and retrying.",
            attempt, _LOGIN_SUBMIT_MAX_ATTEMPTS,
            error_text or "no error modal detected",
        )
        await _dismiss_login_error(page, cfg)
        if attempt < _LOGIN_SUBMIT_MAX_ATTEMPTS:
            await _refresh_login_captcha(page, cap_img, refresh_sel)

    raise RuntimeError(
        f"Login did not succeed after {_LOGIN_SUBMIT_MAX_ATTEMPTS} attempts"
        + (f" (last error: {last_error})" if last_error else "")
        + "."
    )


async def _fill_login_captcha(
    page: Page, cfg: AppConfig, solver: CaptchaSolver,
    cap_img: str, cap_in: str, refresh_sel: str,
) -> None:
    """Solve and fill the login captcha. Re-tries if OCR output isn't exactly 4 digits."""
    for attempt in range(_LOGIN_CAPTCHA_MAX_RETRIES):
        await solve_and_fill(page, cap_img, cap_in, solver,
                             save_captcha=cfg.save_captcha, captcha_type="login")
        answer = await page.locator(cap_in).first.input_value()
        if re.fullmatch(r"\d{4}", answer):
            return
        log.warning("Login captcha answer %r is not 4 digits (attempt %d/%d), retrying.",
                    answer, attempt + 1, _LOGIN_CAPTCHA_MAX_RETRIES)
        await _refresh_login_captcha(page, cap_img, refresh_sel)
    log.warning("Failed to get a valid 4-digit captcha answer after %d attempts; submitting anyway.",
                _LOGIN_CAPTCHA_MAX_RETRIES)


async def _refresh_login_captcha(page: Page, cap_img: str, refresh_sel: str) -> None:
    """Trigger a new captcha image. Prefers a dedicated refresh button; falls back to clicking the image."""
    if refresh_sel:
        await page.locator(refresh_sel).first.click()
        return
    if cap_img:
        try:
            await page.locator(cap_img).first.click()
        except Exception:
            pass


async def _login_redirected(page: Page) -> bool:
    """Return True if the SPA navigated away from /venue/login within the redirect window."""
    try:
        await page.wait_for_url(
            lambda url: "/venue/login" not in url,
            timeout=_LOGIN_REDIRECT_TIMEOUT_MS,
        )
        return True
    except Exception:
        return "/venue/login" not in page.url


async def _read_login_error(page: Page, cfg: AppConfig) -> str | None:
    """Return the text of the login error modal if one is visible, else None."""
    indicator = _sel(cfg, "login_error_indicator")
    body = _sel(cfg, "login_error_text")
    if not indicator or not body:
        return None
    try:
        ind_loc = page.locator(indicator).first
        if not await ind_loc.count() or not await ind_loc.is_visible():
            return None
        text = (await page.locator(body).first.inner_text()).strip()
        return text or None
    except Exception:
        return None


async def _dismiss_login_error(page: Page, cfg: AppConfig) -> None:
    """Click the confirm button on the login error modal if it's open."""
    dismiss = _sel(cfg, "login_error_dismiss")
    if not dismiss:
        return
    try:
        btn = page.locator(dismiss).first
        if await btn.count() and await btn.is_visible():
            await btn.click()
            # Give the modal time to unmount so subsequent form interactions aren't intercepted.
            await page.locator(dismiss).first.wait_for(state="hidden", timeout=2_000)
    except Exception:
        pass
