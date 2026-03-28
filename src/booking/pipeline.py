from __future__ import annotations

from .config import AppConfig


def login_automation_ready(cfg: AppConfig) -> bool:
    """Return True if enough selectors are configured to attempt or detect login."""
    s = cfg.selectors
    if (s.logged_in_indicator or "").strip():
        return True
    method = (cfg.login_method or "alumni").strip().lower()
    if method == "iaaa":
        return _iaaa_login_ready(cfg)
    if method == "alumni":
        return _alumni_login_ready(cfg)
    return False


def _alumni_login_ready(cfg: AppConfig) -> bool:
    # All six alumni-login selectors must be set, plus a way to reach the login form.
    s = cfg.selectors
    required = ("login_mode_alumni", "username_input", "password_input",
                 "login_captcha_image", "login_captcha_input", "login_submit")
    if not all((getattr(s, k) or "").strip() for k in required):
        return False
    return bool((s.login_button or "").strip()) or "/venue/login" in (cfg.base_url or "")


def _iaaa_login_ready(cfg: AppConfig) -> bool:
    # Minimal check: IAAA tab selector present, plus login button or direct login URL.
    s = cfg.selectors
    has_tab = bool((s.login_mode_iaaa or "").strip())
    return has_tab and (
        bool((s.login_button or "").strip()) or "/venue/login" in (cfg.base_url or "")
    )


def submit_flow_ready(cfg: AppConfig) -> bool:
    """Return True if booking_submit selector is configured."""
    return bool((cfg.selectors.booking_submit or "").strip())


HINT_AFTER_NAVIGATE = (
    "Opened site. Next: configure login — in user_config.yaml set `login_method` (`alumni` or `iaaa`); "
    "in site_config.yaml set `selectors.logged_in_indicator` if you rely on a saved session, "
    "or the selectors for your chosen method (see user_config.example.yaml)."
)

HINT_AFTER_BOOKING_FORM = (
    "Court selected. Next: configure submit — set `selectors.booking_submit` "
    "and `selectors.agreement_checkbox` in site_config.yaml."
)
