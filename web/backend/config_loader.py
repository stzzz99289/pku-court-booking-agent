"""Resolve config paths and load AppConfigs for the webapp's two flow sets.

The webapp uses three files per set: shared `accounts.yaml`, `user_config.<set>.yaml`,
`site_config.<set>.yaml`. Paths are resolved relative to the project root.
"""
from __future__ import annotations

import copy
from pathlib import Path

from src.booking.config import AppConfig, UserConfig, load_split_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEBAPP_CONFIG_DIR = PROJECT_ROOT / "config" / "webapp"
ACCOUNTS_PATH = WEBAPP_CONFIG_DIR / "accounts.yaml"


def paths_for(set_name: str) -> tuple[Path, Path, Path]:
    """Return (workers_path, accounts_path, site_path) for a given set name."""
    if set_name not in ("test", "scheduled"):
        raise ValueError(f"Unknown config set {set_name!r} (expected 'test' or 'scheduled').")
    workers = WEBAPP_CONFIG_DIR / set_name / "user_config.yaml"
    site = WEBAPP_CONFIG_DIR / set_name / "site_config.yaml"
    return workers, ACCOUNTS_PATH, site


def load_set(set_name: str) -> AppConfig:
    """Load and return the AppConfig for a config set."""
    workers, accounts, site = paths_for(set_name)
    return load_split_config(workers, accounts, site)


def per_user_config(base: AppConfig, user: UserConfig) -> AppConfig:
    """Clone `base` and inject `user`'s credentials + per-user browser profile."""
    cfg = copy.deepcopy(base)
    cfg.account = user.account
    cfg.password = user.password
    cfg.login_method = user.login_method
    cfg.user_data_dir = str(Path(cfg.user_data_dir).resolve() / f"user_{user.name}")
    return cfg
