"""Site-invariant defaults: URL, browser tuning, and Playwright selectors.

These describe how the PKU venue site is laid out, not user preferences, so
they live in code (not YAML) and are shared across the CLI and webapp flows.
Update this file when the booking website UI changes.

The values are deep-merged into the loaded config as the lowest priority
layer, so a YAML site_config can still override any of them if needed
(useful for one-off staging tests).
"""
from __future__ import annotations

BASE_URL = "https://epe.pku.edu.cn/venue/home"

# Known venues, keyed by the numeric ID in
# https://epe.pku.edu.cn/venue/venue-reservation/<id>. The webapp uses these
# names in its UI; YAML configs keep the integer ID as the source of truth.
VENUES: dict[int, str] = {
    64: "邱德拔体育馆-B1台球厅",
    85: "五四体育中心-室外网球场",
}


def venue_label(venue_id: int | str) -> str:
    """Return "<id> — <name>" for known venues, or just the ID for unknown ones."""
    try:
        vid = int(venue_id)
    except (TypeError, ValueError):
        return str(venue_id)
    name = VENUES.get(vid)
    return f"{vid} — {name}" if name else str(vid)

BROWSER: dict[str, object] = {
    # Playwright slow-motion: pause this many ms between automated actions.
    # 0 in production; bump to 100–300 when debugging selectors visually.
    "slow_mo_ms": 0,
}

SELECTORS: dict[str, str] = {
    # Login flow.
    # Top-right "登录" is a clickable generic/div, not an <a>, so role=link doesn't match.
    "login_button": "text=/^登录$/",
    "login_mode_iaaa": "text=校内师生登录",
    "login_mode_alumni": "text=校友登录",
    "username_input": 'role=textbox[name="手机号"]',
    "password_input": 'role=textbox[name="密码"]',
    "login_captcha_image": 'img[src^="blob"]',
    "login_captcha_input": 'input[placeholder="验证码"]',
    "login_captcha_refresh": "",
    "login_submit": 'role=button[name="登录"]',
    # Optional marker; if empty, code still treats visible 退出 / 欢迎您 as logged-in.
    "logged_in_indicator": "",
    # iView confirm-dialog error variant — its presence signals a login failure
    # (info/warn modals reuse the same container class).
    "login_error_indicator": ".ivu-modal-confirm-head-icon-error",
    "login_error_text": ".ivu-modal-confirm-body",
    "login_error_dismiss": ".ivu-modal-confirm-footer button",

    # Booking submit flow.
    "agreement_checkbox": "input.ivu-checkbox-input",
    "booking_submit": ".submit_order_box .btn",
    "booking_captcha_image": "",
    "booking_captcha_input": "",
    "proceed_to_pay": ".btn-group .btn:not(.cancel)",

    # Verification.
    "user_page_url_substring": "user",
    "booking_success_indicator": "",
}

SITE_DEFAULTS: dict[str, object] = {
    "base_url": BASE_URL,
    "browser": dict(BROWSER),
    "selectors": dict(SELECTORS),
}
