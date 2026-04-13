from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class BrowserConfig:
    slow_mo_ms: int = 0


@dataclass
class CaptchaConfig:
    api_key: str = ""   # chaojiying login password
    username: str = ""  # chaojiying account username
    softid: str = ""    # chaojiying software ID from dashboard


@dataclass
class SelectorConfig:
    """Playwright selector strings; fill after DevTools discovery."""

    # Login flow
    login_button: str = ""
    login_mode_iaaa: str = ""
    login_mode_alumni: str = ""
    username_input: str = ""
    password_input: str = ""
    login_captcha_image: str = ""
    login_captcha_input: str = ""
    login_captcha_refresh: str = ""
    login_submit: str = ""
    logged_in_indicator: str = ""

    # Booking submit flow
    agreement_checkbox: str = ""
    booking_submit: str = ""
    booking_captcha_image: str = ""
    booking_captcha_input: str = ""
    proceed_to_pay: str = ""

    # Verification
    user_page_url_substring: str = "user"
    booking_success_indicator: str = ""


@dataclass
class AppConfig:
    base_url: str
    user_data_dir: str
    account: str
    password: str
    date: str
    start_time: str
    end_time: str
    login_method: str = "alumni"
    venue_id: str = ""  # numeric ID from /venue/venue-reservation/<id>
    save_captcha: bool = False  # save captcha images to data/captcha/ for benchmarking
    debug: bool = False  # use manual stdin solver instead of API to save tokens
    headless: bool = False  # run Chromium headless; on finish, auto-close the browser and exit
    scheduled_mode: bool = False
    scheduled_time: str = "120000"        # HHMMSS 24-h format
    scheduled_window_minutes: int = 3     # must start within this many minutes before scheduled_time
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    selectors: SelectorConfig = field(default_factory=SelectorConfig)


REQUIRED_TOP_LEVEL = ("base_url", "user_data_dir", "account", "password", "date", "start_time", "end_time")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    # Recursively merge two dicts; override values win over base values.
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    # Load a YAML file and return it as a dict, raising on missing file or wrong type.
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a YAML mapping at the top level.")
    return raw


def load_config(user_config_path: Path, site_config_path: Path) -> AppConfig:
    """Deep-merge site_config (defaults) with user_config (secrets/booking); user wins."""
    site = _load_yaml_mapping(site_config_path)
    user = _load_yaml_mapping(user_config_path)
    raw = _deep_merge(site, user)
    missing = [k for k in REQUIRED_TOP_LEVEL if k not in raw or raw[k] is None]
    if missing:
        raise ValueError(f"Missing config keys after merge: {', '.join(missing)}")
    return AppConfig(
        base_url=str(raw["base_url"]),
        user_data_dir=str(raw["user_data_dir"]),
        account=str(raw["account"]),
        password=str(raw["password"]),
        date=str(raw["date"]),
        start_time=str(raw["start_time"]),
        end_time=str(raw["end_time"]),
        login_method=str(raw.get("login_method", "alumni")),
        venue_id=str(raw.get("venue_id", "")),
        save_captcha=bool(raw.get("save_captcha", False)),
        debug=bool(raw.get("debug", False)),
        headless=bool(raw.get("headless", False)),
        scheduled_mode=bool(raw.get("scheduled_mode", False)),
        scheduled_time=str(raw.get("scheduled_time", "120000")),
        scheduled_window_minutes=int(raw.get("scheduled_window_minutes", 3)),
        browser=_parse_browser(raw),
        captcha=_parse_captcha(raw),
        selectors=_parse_selectors(raw),
    )


def _parse_browser(data: dict[str, Any]) -> BrowserConfig:
    # Extract browser settings from merged config dict.
    b = data.get("browser") or {}
    return BrowserConfig(slow_mo_ms=int(b.get("slow_mo_ms", 0)))


def _parse_captcha(data: dict[str, Any]) -> CaptchaConfig:
    # Extract captcha settings from merged config dict.
    c = data.get("captcha") or {}
    return CaptchaConfig(
        api_key=str(c.get("api_key", "")),
        username=str(c.get("username", "")),
        softid=str(c.get("softid", "")),
    )


def _parse_selectors(data: dict[str, Any]) -> SelectorConfig:
    # Extract all selector strings from merged config dict.
    s = data.get("selectors") or {}
    return SelectorConfig(
        login_button=str(s.get("login_button", "")),
        login_mode_iaaa=str(s.get("login_mode_iaaa", "")),
        login_mode_alumni=str(s.get("login_mode_alumni", "")),
        username_input=str(s.get("username_input", "")),
        password_input=str(s.get("password_input", "")),
        login_captcha_image=str(s.get("login_captcha_image", "")),
        login_captcha_input=str(s.get("login_captcha_input", "")),
        login_captcha_refresh=str(s.get("login_captcha_refresh", "")),
        login_submit=str(s.get("login_submit", "")),
        logged_in_indicator=str(s.get("logged_in_indicator", "")),
        agreement_checkbox=str(s.get("agreement_checkbox", "")),
        booking_submit=str(s.get("booking_submit", "")),
        booking_captcha_image=str(s.get("booking_captcha_image", "")),
        booking_captcha_input=str(s.get("booking_captcha_input", "")),
        proceed_to_pay=str(s.get("proceed_to_pay", "")),
        user_page_url_substring=str(s.get("user_page_url_substring", "user")),
        booking_success_indicator=str(s.get("booking_success_indicator", "")),
    )
