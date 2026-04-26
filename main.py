#!/usr/bin/env python3
"""Entry point for the court booking agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_arg_parser() -> argparse.ArgumentParser:
    # Define CLI flags: --user-config, --site-config, --print-alignment.
    parser = argparse.ArgumentParser(description="PKU court booking agent")
    parser.add_argument("-c", "--user-config", type=Path, default=ROOT / "config/cli/user_config.yaml",
                        help="path to user config (account, password, booking, login_method)")
    parser.add_argument("--site-config", type=Path, default=ROOT / "config/cli/site_config.yaml",
                        help="path to site config (URLs, selectors, defaults)")
    parser.add_argument("--print-alignment", action="store_true",
                        help="print DevTools MCP + selector checklist and exit")
    parser.add_argument("--query-orders", type=int, nargs="?", const=10, default=None, metavar="N",
                        help="for each configured user, log in and print their N most recent paid bookings (default 10), then exit")
    parser.add_argument("--print-split-config", choices=("test", "scheduled"), default=None,
                        help="load the webapp split-config pair (accounts.yaml + user_config.<set>.yaml + "
                             "site_config.<set>.yaml), print the resolved AppConfig, and exit")
    return parser


def configure_logging() -> None:
    # Set up INFO-level logging with a compact format.
    logging.basicConfig(level=logging.INFO, format="[PID=%(process)d] [%(asctime)s] [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")


def run_alignment_command() -> int:
    # Print the DevTools MCP selector discovery checklist and exit.
    from src.booking.session import alignment_steps
    for i, step in enumerate(alignment_steps(), 1):
        print(f"{i}. {step}")
    return 0


def import_runner():
    # Import the async runner, printing a helpful message if Playwright is missing.
    try:
        from src.booking import runner
    except ModuleNotFoundError as e:
        if "playwright" in str(e).lower():
            print("Playwright is required: pip install -r requirements.txt && playwright install chromium",
                  file=sys.stderr)
        print(e, file=sys.stderr)
        return None
    return runner


def run_query_orders_command(args: argparse.Namespace, runner) -> int:
    # Fetch and print each user's most recent paid bookings, then exit.
    try:
        asyncio.run(runner.query_all_orders(args.user_config, args.site_config, args.query_orders))
    except (FileNotFoundError, ValueError, NotImplementedError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def run_booking_command(args: argparse.Namespace, runner) -> int:
    # Run all configured workers (1 = direct, N = multiprocessing). Return 0 if any succeeded.
    try:
        results = asyncio.run(runner.run_all(args.user_config, args.site_config))
    except (FileNotFoundError, ValueError, NotImplementedError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0 if any(r.success for r in results) else 1


def run_print_split_config_command(set_name: str) -> int:
    # Load the webapp split-config pair and print a sanity summary.
    from src.booking.config import load_split_config
    workers = ROOT / f"config/webapp/{set_name}/user_config.yaml"
    accounts = ROOT / "config/webapp/accounts.yaml"
    site = ROOT / f"config/webapp/{set_name}/site_config.yaml"
    try:
        cfg = load_split_config(workers, accounts, site)
    except (FileNotFoundError, ValueError) as e:
        print(f"Failed to load split config '{set_name}': {e}", file=sys.stderr)
        return 1
    print(f"Loaded split-config set '{set_name}':")
    print(f"  workers file:  {workers}")
    print(f"  accounts file: {accounts}")
    print(f"  site file:     {site}")
    print(f"  base_url={cfg.base_url}  venue_id={cfg.venue_id}  headless={cfg.headless}")
    print(f"  scheduled_mode={cfg.scheduled_mode}  scheduled_time={cfg.scheduled_time}  "
          f"prep_seconds={cfg.scheduled_prep_seconds}")
    print(f"  users ({len(cfg.users)}):")
    for u in cfg.users:
        print(f"    - {u.name}  login_method={u.login_method}  account={u.account[:3]}***")
    print(f"  workers ({len(cfg.workers)}):")
    for w in cfg.workers:
        print(f"    - user={w.user}  date={w.date}  start_time_list={w.start_time_list}")
    return 0


def main() -> int:
    # Parse args, then dispatch to alignment checklist or booking flow.
    args = build_arg_parser().parse_args()
    if args.print_alignment:
        return run_alignment_command()
    configure_logging()
    if args.print_split_config is not None:
        return run_print_split_config_command(args.print_split_config)
    runner = import_runner()
    if runner is None:
        return 1
    if args.query_orders is not None:
        return run_query_orders_command(args, runner)
    return run_booking_command(args, runner)


if __name__ == "__main__":
    raise SystemExit(main())
