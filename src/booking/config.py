from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import copy

import yaml

from .profiler import Profiler
from .site_constants import SITE_DEFAULTS

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

    Each `start_time_list_*` is an ordered priority list of 2-digit hour
    strings (e.g. ['09', '10', '20']). Each entry represents a one-hour
    booking (start_time='09' → 09:00-10:00). The worker tries them in order
    and stops as soon as one succeeds; any that return 'slot already booked'
    rejections are skipped to try the next.

    Workday vs. weekend lists are picked at runtime based on the resolved
    target `date` (Mon–Fri → workday, Sat–Sun → weekend), so a single worker
    entry can cover both kinds of days with different slot preferences.

    `court_priority` is an optional ordered list of 0-based court indices that
    overrides the front of the default natural court order (0, 1, 2, ...) when
    several courts are free at the chosen hour. Its entries come first, then
    any unlisted court in natural order. E.g. [3] → 3, 0, 1, 2, 4, ...;
    [2, 1] → 2, 1, 0, 3, ... An empty list keeps the natural default.
    """
    user: str  # must match a UserConfig.name
    date: str
    start_time_list_workday: list[str]
    start_time_list_weekend: list[str]
    court_priority: list[int] = field(default_factory=list)

    def active_start_time_list(self) -> list[str]:
        """Return workday or weekend list based on `self.date`'s weekday."""
        d = datetime.strptime(self.date, "%Y%m%d").date()
        return self.start_time_list_weekend if d.weekday() >= 5 else self.start_time_list_workday


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
    scheduled_prep_seconds: int = 90      # webapp scheduler fires this many seconds before scheduled_time
    # Small delay past `scheduled_time` before the post-fire refresh fires.
    # The site doesn't release the new bookable date atomically with the
    # wall-clock second; a tiny offset (default 500 ms) dramatically improves
    # first-refresh hit rate while still beating other clients to the slot.
    scheduled_fire_offset_ms: int = 500
    # Per-worker post-fire stagger in milliseconds. After the fire instant, each
    # worker sleeps `worker_index * worker_stagger_ms` before hitting the
    # post-fire refresh, so workers don't all race the same throttled
    # `day_info` XHR simultaneously. 0 disables the stagger. Worker index is
    # set by the scheduler before running each worker.
    worker_stagger_ms: int = 150
    worker_index: int = 0
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
    # Discovered once during the scheduled-mode wait by walking today's already-
    # loaded schedule's pagination arrows. Each entry is [first_hour, last_hour]
    # visible on that page, in left-to-right order (e.g.
    # [[6, 10], [11, 15], [16, 20], [21, 21]]). Lets `_scroll_to_target_column`
    # jump directly to the right page instead of one-arrow-at-a-time polling.
    hour_page_boundaries: list[list[int]] = field(default_factory=list)
    # Populated by `select_booking_date` from the `reservation/day/info`
    # response: hour (int 6..21) → list of court row indices (0-based, in DOM
    # order matching `tbody tr` after filtering to court rows) that have
    # reservationStatus == 1 (free). Lets the runner pick (hour, court_idx)
    # directly from JSON instead of scanning the DOM cell-by-cell.
    cached_free_slots: dict[int, list[int]] = field(default_factory=dict)
    # Set by the runner before calling `select_court_time` when the JSON cache
    # picked a specific court row. -1 means "fall back to row-walk".
    target_court_index: int = -1
    # Per-worker court preference: 0-based court indices that override the front
    # of the default natural order (0, 1, 2, ...) when several courts are free.
    # Set by the runner from the active worker; empty keeps the natural default.
    court_priority: list[int] = field(default_factory=list)
    # When true, the booking flow records per-stage latencies via `cfg.profiler`
    # and prints a summary at the end. Off by default so production runs pay
    # zero overhead (`Profiler.span` becomes a no-op when disabled).
    profile: bool = False
    profiler: Profiler = field(default_factory=Profiler)
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


def load_split_config(
    workers_path: Path, accounts_path: Path, site_path: Path,
    today: date | None = None,
) -> AppConfig:
    """Webapp loader: merge (site defaults) ← (accounts) ← (workers) → AppConfig.

    `accounts.yaml` holds the `users:` list (credentials) and optional shared
    `captcha:` config. `workers_path` (user_config.{test,scheduled}.yaml) holds
    only `workers:` plus optional overrides like `headless`. The CLI loader
    `load_config` is left untouched so `python main.py` keeps working.
    """
    site = _load_yaml_mapping(site_path)
    accounts = _load_yaml_mapping(accounts_path)
    workers = _load_yaml_mapping(workers_path)
    if "users" in workers:
        raise ValueError(
            f"{workers_path} must not contain a 'users:' section — credentials live in {accounts_path}."
        )
    raw = _deep_merge(
        _deep_merge(_deep_merge(copy.deepcopy(SITE_DEFAULTS), site), accounts),
        workers,
    )
    missing = [k for k in REQUIRED_TOP_LEVEL if k not in raw or raw[k] is None]
    if missing:
        raise ValueError(f"Missing config keys after merge: {', '.join(missing)}")
    users_list = _parse_users(raw)
    if not users_list:
        raise ValueError(f"{accounts_path} must define at least one user.")
    workers_list = _parse_workers(raw, users_list, today=today)
    if not workers_list:
        raise ValueError(f"{workers_path} must define at least one worker.")
    profile_flag = bool(raw.get("profile", False))
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
        scheduled_prep_seconds=int(raw.get("scheduled_prep_seconds", 90)),
        scheduled_fire_offset_ms=int(raw.get("scheduled_fire_offset_ms", 500)),
        worker_stagger_ms=int(raw.get("worker_stagger_ms", 150)),
        profile=profile_flag,
        profiler=Profiler(enabled=profile_flag),
        users=users_list,
        workers=workers_list,
        browser=_parse_browser(raw),
        captcha=_parse_captcha(raw),
        selectors=_parse_selectors(raw),
    )


def load_config(user_config_path: Path, site_config_path: Path) -> AppConfig:
    """Deep-merge site_config (defaults) with user_config (secrets/booking); user wins."""
    site = _load_yaml_mapping(site_config_path)
    user = _load_yaml_mapping(user_config_path)
    raw = _deep_merge(_deep_merge(copy.deepcopy(SITE_DEFAULTS), site), user)
    missing = [k for k in REQUIRED_TOP_LEVEL if k not in raw or raw[k] is None]
    if missing:
        raise ValueError(f"Missing config keys after merge: {', '.join(missing)}")
    users = _parse_users(raw)
    if not users:
        raise ValueError("At least one user must be configured in the 'users' list.")
    workers = _parse_workers(raw, users)
    if not workers:
        raise ValueError("At least one worker must be configured in the 'workers' list.")
    profile_flag = bool(raw.get("profile", False))
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
        scheduled_prep_seconds=int(raw.get("scheduled_prep_seconds", 90)),
        scheduled_fire_offset_ms=int(raw.get("scheduled_fire_offset_ms", 500)),
        worker_stagger_ms=int(raw.get("worker_stagger_ms", 150)),
        profile=profile_flag,
        profiler=Profiler(enabled=profile_flag),
        users=users,
        workers=workers,
        browser=_parse_browser(raw),
        captcha=_parse_captcha(raw),
        selectors=_parse_selectors(raw),
    )


def _resolve_date(value: str, index: int, today: date | None = None) -> str:
    """Return a YYYYMMDD date string. Accepts literal YYYYMMDD or 'N-days-later'.

    `today` controls what 'N-days-later' is relative to. Defaults to the
    real-world today; the webapp's scheduler passes the fire date instead so
    relative dates reflect when the booking flow will actually run, not when
    the YAML was first loaded.
    """
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
    target = (today or date.today()) + timedelta(days=n)
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


def _parse_start_time_list(raw: Any, worker_index: int, field_name: str) -> list[str]:
    """Validate and normalize a worker's start_time_list_{workday,weekend}.

    Each entry must be a 2-digit hour string in the booking window. The list
    must be non-empty; duplicates are allowed (first wins implicitly since
    once a slot books, the rest are skipped).
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"workers[{worker_index}].{field_name} must be a non-empty list of 2-digit hour strings."
        )
    out: list[str] = []
    for j, entry in enumerate(raw):
        h = str(entry).strip()
        if not re.fullmatch(r"\d{2}", h):
            raise ValueError(
                f"workers[{worker_index}].{field_name}[{j}] must be a 2-digit hour string "
                f"(e.g. '09'), got {entry!r}."
            )
        hv = int(h)
        if not (6 <= hv <= 21):
            raise ValueError(
                f"workers[{worker_index}].{field_name}[{j}] hour {hv:02d} is out of range; "
                f"must be 06–21 so that end_time = start_time + 1 stays ≤ 22."
            )
        out.append(h)
    return out


def _parse_court_priority(raw: Any, worker_index: int) -> list[int]:
    """Validate and normalize a worker's optional `court_priority`.

    Each entry is a 0-based court index (non-negative int; numeric strings are
    coerced). Duplicates are rejected. Out-of-range indices are allowed — they
    simply never match a free court. Missing/empty → [] (natural default).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"workers[{worker_index}].court_priority must be a list of 0-based court indices."
        )
    out: list[int] = []
    seen: set[int] = set()
    for j, entry in enumerate(raw):
        try:
            c = int(str(entry).strip())
        except ValueError:
            raise ValueError(
                f"workers[{worker_index}].court_priority[{j}] must be an integer, got {entry!r}."
            )
        if c < 0:
            raise ValueError(
                f"workers[{worker_index}].court_priority[{j}] must be non-negative, got {c}."
            )
        if c in seen:
            raise ValueError(
                f"workers[{worker_index}].court_priority[{j}] duplicates index {c}."
            )
        seen.add(c)
        out.append(c)
    return out


def _parse_workers(
    data: dict[str, Any], users: list[UserConfig], today: date | None = None,
) -> list[WorkerConfig]:
    # Parse workers list; validates that each worker.user matches a known user name.
    known_names = {u.name for u in users}
    raw_list = data.get("workers") or []
    workers: list[WorkerConfig] = []
    for i, w in enumerate(raw_list):
        if not isinstance(w, dict):
            raise ValueError(f"workers[{i}] must be a mapping, got {type(w).__name__}.")
        for key in ("user", "date", "start_time_list_workday", "start_time_list_weekend"):
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
            date=_resolve_date(str(w["date"]), i, today=today),
            start_time_list_workday=_parse_start_time_list(
                w["start_time_list_workday"], i, "start_time_list_workday"),
            start_time_list_weekend=_parse_start_time_list(
                w["start_time_list_weekend"], i, "start_time_list_weekend"),
            court_priority=_parse_court_priority(w.get("court_priority"), i),
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
