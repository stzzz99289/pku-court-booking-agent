"""Benchmark login and booking CAPTCHA solvers.

Credentials are read from user_config.yaml (same file used by the booking agent).
ChaoJiYing is used when all three credentials are present; otherwise only the
local ddddocr solver runs.

Usage:
    python -m src.captcha_solver.benchmark              # all types
    python -m src.captcha_solver.benchmark --type login
    python -m src.captcha_solver.benchmark --type booking
    python -m src.captcha_solver.benchmark --config path/to/user_config.yaml
    python -m src.captcha_solver.benchmark --data-dir path/to/images
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from pathlib import Path

from .login_captcha import LoginCaptchaSolver
from .booking_captcha import BookingCaptchaSolver
from .api.chaojiying import ChaoJiYingProvider


def _load_chaojiying(config_path: Path) -> ChaoJiYingProvider | None:
    """Return a ChaoJiYingProvider if credentials are configured, else None."""
    import yaml
    if not config_path.is_file():
        return None
    with config_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    c = raw.get("captcha") or {}
    username = str(c.get("username", "")).strip()
    api_key  = str(c.get("api_key",  "")).strip()
    softid   = str(c.get("softid",   "")).strip()
    if not all([username, api_key, softid]):
        return None
    return ChaoJiYingProvider(username=username, password=api_key, softid=softid)


# ------------------------------------------------------------------
# Login benchmark (ddddocr only)
# ------------------------------------------------------------------

def bench_login(data_dir: Path) -> None:
    images = sorted(data_dir.glob("login_*.png"))
    if not images:
        print(f"No login_*.png files found in {data_dir}\n")
        return

    solver = LoginCaptchaSolver()
    durations: list[float] = []

    print(f"=== Login CAPTCHA — ddddocr  ({len(images)} image(s)) ===\n")
    print(f"{'File':<45} {'Result':<10} {'Time (ms)':>10}")
    print("-" * 68)

    for path in images:
        image_bytes = path.read_bytes()
        t0 = time.perf_counter()
        result = solver.solve(image_bytes)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        durations.append(elapsed_ms)
        print(f"{path.name:<45} {result:<10} {elapsed_ms:>9.1f}ms")

    print("-" * 68)
    _print_stats(durations)


# ------------------------------------------------------------------
# Booking benchmark (ddddocr + optional chaojiying side-by-side)
# ------------------------------------------------------------------

def bench_booking(data_dir: Path, cjy: ChaoJiYingProvider | None) -> None:
    images = sorted(data_dir.glob("booking_*.png"))
    if not images:
        print(f"No booking_*.png files found in {data_dir}\n")
        return

    _bench_booking_local(images)

    if cjy is not None:
        _bench_booking_chaojiying(images, cjy)
    else:
        print(
            "(chaojiying benchmark skipped — "
            "set captcha.username / api_key / softid in user_config.yaml to enable)\n"
        )


def _instruction_from_filename(path: Path) -> str:
    """Extract comma-separated instruction from filename, e.g. booking_唱呀流.png -> '唱,呀,流'."""
    chars = path.stem.split("_", 1)[1]   # e.g. "唱呀流"
    return ",".join(chars)


def _bench_booking_local(images: list[Path]) -> None:
    solver = BookingCaptchaSolver()
    durations: list[float] = []

    print(f"=== Booking CAPTCHA — ddddocr  ({len(images)} image(s)) ===\n")
    print(f"{'File':<38} {'Instruction':<12} {'Detected positions (by char)':<45} {'Time (ms)':>10}")
    print("-" * 108)

    for path in images:
        image_bytes = path.read_bytes()
        instruction = _instruction_from_filename(path)
        target_chars = instruction.split(",")

        t0 = time.perf_counter()
        all_results = solver.solve(image_bytes)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        durations.append(elapsed_ms)

        # Map detected chars to positions; report in instruction order.
        detected = {r.char: r for r in all_results}
        summary = "  ".join(
            f"{ch}@{detected[ch].center}" if ch in detected else f"{ch}@?"
            for ch in target_chars
        )
        print(f"{path.name:<38} {instruction:<12} {summary:<45} {elapsed_ms:>9.1f}ms")

    print("-" * 108)
    _print_stats(durations)


def _bench_booking_chaojiying(images: list[Path], cjy: ChaoJiYingProvider) -> None:
    durations: list[float] = []

    print(f"=== Booking CAPTCHA — chaojiying (9801)  ({len(images)} image(s)) ===\n")
    print(f"{'File':<38} {'Instruction':<12} {'Click coordinates (in order)':<35} {'Time (ms)':>10}")
    print("-" * 98)

    for path in images:
        image_bytes = path.read_bytes()
        instruction = _instruction_from_filename(path)

        t0 = time.perf_counter()
        try:
            points = asyncio.run(cjy.solve_click(image_bytes, instruction))
            elapsed_ms = (time.perf_counter() - t0) * 1000
            durations.append(elapsed_ms)
            coords = "  ".join(f"({p.x},{p.y})" for p in points)
            print(f"{path.name:<38} {instruction:<12} {coords:<35} {elapsed_ms:>9.1f}ms")
        except Exception as e:
            print(f"{path.name:<38} {instruction:<12} ERROR: {e}")

    if durations:
        print("-" * 98)
        _print_stats(durations)


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------

def _print_stats(durations: list[float]) -> None:
    print(f"Images solved : {len(durations)}")
    print(f"Mean time     : {statistics.mean(durations):.1f} ms")
    if len(durations) > 1:
        print(f"Median time   : {statistics.median(durations):.1f} ms")
        print(f"Stdev         : {statistics.stdev(durations):.1f} ms")
    print(f"Min / Max     : {min(durations):.1f} ms / {max(durations):.1f} ms\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark CAPTCHA solvers")
    parser.add_argument(
        "--type",
        choices=["login", "booking", "all"],
        default="all",
        help="Which captcha type to benchmark (default: all)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("user_config.yaml"),
        help="Path to user_config.yaml (default: user_config.yaml)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/captcha"),
        help="Directory containing captcha images (default: data/captcha)",
    )
    args = parser.parse_args()

    cjy = _load_chaojiying(args.config)

    if args.type in ("login", "all"):
        bench_login(args.data_dir)
    if args.type in ("booking", "all"):
        bench_booking(args.data_dir, cjy)


if __name__ == "__main__":
    main()
