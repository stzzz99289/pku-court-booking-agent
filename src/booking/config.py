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
    login_error_indicator: str = ""
    login_error_text: str = ""
    login_error_dismiss: str = ""

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
class UserConfig:
    """A single login identity: credentials + login method."""
    name: str
    login_method: str
    account: str
    password: str


@dataclass
class WorkerConfig:
    """Per-worker overrides for multi-worker booking.

    start_time_list is an ordered priority list of 2-digit hour strings
    (e.g. ['09', '10', '20']). Each entry represents a one-hour booking
    (start_time='09' → 09:00-10:00). The worker tries them in order and
    stops as soon as one succeeds; any that return 'slot already booked'
    rejections are skipped to try the next.
    """
    user: str  # must match a UserConfig.name
    date: str
    start_time_list: list[str]


@dataclass
class AppConfig:
    base_url: str
    user_data_dir: str
    venue_id: str = ""  # numeric ID from /venue/venue-reservation/<id>
    save_captcha: bool = False  # save captcha images to data/captcha/ for benchmarking
    debug: bool = False  # use manual stdin solver instead of API to save tokens
    headless: bool = False  # run Chromium headless; on finish, auto-close the browser and exit
    scheduled_mode: bool = False
    scheduled_time: str = "120000"        # HHMMSS 24-h format
    scheduled_window_minutes: int = 3     # must start within this many minutes before scheduled_time
    # Populated from the worker's referenced user; set by runner before the booking flow starts.
    account: str = ""
    password: str = ""
    login_method: str = "alumni"
    # Populated from workers list; set by runner before the booking flow starts.
    date: str = ""
    # Ordered list of 2-digit start hours to try for this worker. The runner
    # iterates through them, setting `start_time`/`end_time` per attempt, and
    # stops on the first success.
    start_time_list: list[str] = field(default_factory=list)
    # Current slot being attempted — set by the runner before each call into
    # booking_flow. `end_time` is always `start_time + 1` (one-hour bookings).
    start_time: str = ""
    end_time: str = ""
    users: list[UserConfig] = field(default_factory=list)
    workers: list[WorkerConfig] = field(default_factory=list)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    selectors: SelectorConfig = field(default_factory=SelectorConfig)


REQUIRED_TOP_LEVEL = ("base_url", "user_data_dir")


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
    users = _parse_users(raw)
    if not users:
        raise ValueError("At least one user must be configured in the 'users' list.")
    workers = _parse_workers(raw, users)
    if not workers:
        raise ValueError("At least one worker must be configured in the 'workers' list.")
    return AppConfig(
        base_url=str(raw["base_url"]),
        user_data_dir=str(raw["user_data_dir"]),
        venue_id=str(raw.get("venue_id", "")),
        save_captcha=bool(raw.get("save_captcha", False)),
        debug=bool(raw.get("debug", False)),
        headless=bool(raw.get("headless", False)),
        scheduled_mode=bool(raw.get("scheduled_mode", False)),
        scheduled_time=str(raw.get("scheduled_time", "120000")),
        scheduled_window_minutes=int(raw.get("scheduled_window_minutes", 3)),
        users=users,
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


def _parse_users(data: dict[str, Any]) -> list[UserConfig]:
    # Parse the users list from merged config dict; enforces unique non-empty names.
    raw_list = data.get("users") or []
    users: list[UserConfig] = []
    seen: set[str] = set()
    for i, u in enumerate(raw_list):
        if not isinstance(u, dict):
            raise ValueError(f"users[{i}] must be a mapping, got {type(u).__name__}.")
        for key in ("name", "login_method", "account", "password"):
            if not u.get(key):
                raise ValueError(f"users[{i}] is missing required key '{key}'.")
        name = str(u["name"]).strip()
        if name in seen:
            raise ValueError(f"users[{i}].name {name!r} is duplicated; user names must be unique.")
        seen.add(name)
        users.append(UserConfig(
            name=name,
            login_method=str(u["login_method"]).strip(),
            account=str(u["account"]),
            password=str(u["password"]),
        ))
    return users


def _parse_start_time_list(raw: Any, worker_index: int) -> list[str]:
    """Validate and normalize a worker's start_time_list.

    Each entry must be a 2-digit hour string in the booking window. The list
    must be non-empty; duplicates are allowed (first wins implicitly since
    once a slot books, the rest are skipped).
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"workers[{worker_index}].start_time_list must be a non-empty list of 2-digit hour strings."
        )
    out: list[str] = []
    for j, entry in enumerate(raw):
        h = str(entry).strip()
        if not re.fullmatch(r"\d{2}", h):
            raise ValueError(
                f"workers[{worker_index}].start_time_list[{j}] must be a 2-digit hour string "
                f"(e.g. '09'), got {entry!r}."
            )
        hv = int(h)
        if not (6 <= hv <= 21):
            raise ValueError(
                f"workers[{worker_index}].start_time_list[{j}] hour {hv:02d} is out of range; "
                f"must be 06–21 so that end_time = start_time + 1 stays ≤ 22."
            )
        out.append(h)
    return out


def _parse_workers(data: dict[str, Any], users: list[UserConfig]) -> list[WorkerConfig]:
    # Parse workers list; validates that each worker.user matches a known user name.
    known_names = {u.name for u in users}
    raw_list = data.get("workers") or []
    workers: list[WorkerConfig] = []
    for i, w in enumerate(raw_list):
        if not isinstance(w, dict):
            raise ValueError(f"workers[{i}] must be a mapping, got {type(w).__name__}.")
        for key in ("user", "date", "start_time_list"):
            if key not in w:
                raise ValueError(f"workers[{i}] is missing required key '{key}'.")
        user_name = str(w["user"]).strip()
        if user_name not in known_names:
            raise ValueError(
                f"workers[{i}].user {user_name!r} does not match any user in the 'users' list "
                f"(known: {sorted(known_names)})."
            )
        workers.append(WorkerConfig(
            user=user_name,
            date=_resolve_date(str(w["date"]), i),
            start_time_list=_parse_start_time_list(w["start_time_list"], i),
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
        login_error_indicator=str(s.get("login_error_indicator", "")),
        login_error_text=str(s.get("login_error_text", "")),
        login_error_dismiss=str(s.get("login_error_dismiss", "")),
        agreement_checkbox=str(s.get("agreement_checkbox", "")),
        booking_submit=str(s.get("booking_submit", "")),
        booking_captcha_image=str(s.get("booking_captcha_image", "")),
        booking_captcha_input=str(s.get("booking_captcha_input", "")),
        proceed_to_pay=str(s.get("proceed_to_pay", "")),
        user_page_url_substring=str(s.get("user_page_url_substring", "user")),
        booking_success_indicator=str(s.get("booking_success_indicator", "")),
    )
