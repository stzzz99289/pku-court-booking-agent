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
    parser.add_argument("-c", "--user-config", type=Path, default=ROOT / "user_config.yaml",
                        help="path to user config (account, password, booking, login_method)")
    parser.add_argument("--site-config", type=Path, default=ROOT / "site_config.yaml",
                        help="path to site config (URLs, selectors, defaults)")
    parser.add_argument("--print-alignment", action="store_true",
                        help="print DevTools MCP + selector checklist and exit")
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
        from src.booking.runner import run
    except ModuleNotFoundError as e:
        if "playwright" in str(e).lower():
            print("Playwright is required: pip install -r requirements.txt && playwright install chromium",
                  file=sys.stderr)
        print(e, file=sys.stderr)
        return None
    return run


def run_booking_command(args: argparse.Namespace, run) -> int:
    # Run the async booking flow and return exit code 0 on success, 1 on failure.
    try:
        result = asyncio.run(run(args.user_config, args.site_config))
    except (FileNotFoundError, ValueError, NotImplementedError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0 if result.success else 1


def main() -> int:
    # Parse args, then dispatch to alignment checklist or booking flow.
    args = build_arg_parser().parse_args()
    if args.print_alignment:
        return run_alignment_command()
    configure_logging()
    run = import_runner()
    if run is None:
        return 1
    return run_booking_command(args, run)


if __name__ == "__main__":
    raise SystemExit(main())
