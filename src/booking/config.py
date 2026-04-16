from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

# Upper bound for "N-days-later" dates. PKU's system typically opens bookings
# a few days out; anything beyond 2 weeks is almost certainly a typo.
MAX_DAYS_LATER = 14
_RELATIVE_DATE_RE = re.compile(r"^(\d+)-days?-later$")


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
class WorkerConfig:
    """Per-worker overrides for multi-worker booking."""
    date: str
    start_time: str
    end_time: str


@dataclass
class AppConfig:
    base_url: str
    user_data_dir: str
    account: str
    password: str
    login_method: str = "alumni"
    venue_id: str = ""  # numeric ID from /venue/venue-reservation/<id>
    save_captcha: bool = False  # save captcha images to data/captcha/ for benchmarking
    debug: bool = False  # use manual stdin solver instead of API to save tokens
    headless: bool = False  # run Chromium headless; on finish, auto-close the browser and exit
    scheduled_mode: bool = False
    scheduled_time: str = "120000"        # HHMMSS 24-h format
    scheduled_window_minutes: int = 3     # must start within this many minutes before scheduled_time
    # Populated from workers list; set by runner before the booking flow starts.
    date: str = ""
    start_time: str = ""
    end_time: str = ""
    workers: list[WorkerConfig] = field(default_factory=list)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    selectors: SelectorConfig = field(default_factory=SelectorConfig)


REQUIRED_TOP_LEVEL = ("base_url", "user_data_dir", "account", "password")


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
    workers = _parse_workers(raw)
    if not workers:
        raise ValueError("At least one worker must be configured in the 'workers' list.")
    return AppConfig(
        base_url=str(raw["base_url"]),
        user_data_dir=str(raw["user_data_dir"]),
        account=str(raw["account"]),
        password=str(raw["password"]),
        login_method=str(raw.get("login_method", "alumni")),
        venue_id=str(raw.get("venue_id", "")),
        save_captcha=bool(raw.get("save_captcha", False)),
        debug=bool(raw.get("debug", False)),
        headless=bool(raw.get("headless", False)),
        scheduled_mode=bool(raw.get("scheduled_mode", False)),
        scheduled_time=str(raw.get("scheduled_time", "120000")),
        scheduled_window_minutes=int(raw.get("scheduled_window_minutes", 3)),
        workers=workers,
        browser=_parse_browser(raw),
        captcha=_parse_captcha(raw),
        selectors=_parse_selectors(raw),
    )


def _resolve_date(value: str, index: int) -> str:
    """Return a YYYYMMDD date string. Accepts literal YYYYMMDD or 'N-days-later'."""
    v = value.strip()
    if v.isdigit() and len(v) == 8:
        return v
    m = _RELATIVE_DATE_RE.match(v)
    if m is None:
        raise ValueError(
            f"workers[{index}].date must be YYYYMMDD or 'N-days-later' "
            f"(got {value!r})."
        )
    n = int(m.group(1))
    if n < 0 or n > MAX_DAYS_LATER:
        raise ValueError(
            f"workers[{index}].date offset must be between 0 and {MAX_DAYS_LATER} days "
            f"(got {n})."
        )
    target = date.today() + timedelta(days=n)
    return target.strftime("%Y%m%d")


def _parse_workers(data: dict[str, Any]) -> list[WorkerConfig]:
    # Parse optional workers list from merged config dict.
    raw_list = data.get("workers") or []
    workers = []
    for i, w in enumerate(raw_list):
        if not isinstance(w, dict):
            raise ValueError(f"workers[{i}] must be a mapping, got {type(w).__name__}.")
        for key in ("date", "start_time", "end_time"):
            if key not in w:
                raise ValueError(f"workers[{i}] is missing required key '{key}'.")
        workers.append(WorkerConfig(
            date=_resolve_date(str(w["date"]), i),
            start_time=str(w["start_time"]),
            end_time=str(w["end_time"]),
        ))
    return workers


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
