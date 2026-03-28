"""Benchmark login and booking CAPTCHA solvers.

Usage:
    python -m src.captcha_solver.benchmark              # both solvers
    python -m src.captcha_solver.benchmark --type login
    python -m src.captcha_solver.benchmark --type booking
    python -m src.captcha_solver.benchmark --data-dir path/to/images
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

from .login_captcha import LoginCaptchaSolver
from .booking_captcha import BookingCaptchaSolver


def bench_login(data_dir: Path) -> None:
    images = sorted(data_dir.glob("login_*.png"))
    if not images:
        print(f"No login_*.png files found in {data_dir}\n")
        return

    solver = LoginCaptchaSolver()
    durations: list[float] = []

    print(f"=== Login CAPTCHA  ({len(images)} image(s)) ===\n")
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


def bench_booking(data_dir: Path) -> None:
    images = sorted(data_dir.glob("booking_*.png"))
    if not images:
        print(f"No booking_*.png files found in {data_dir}\n")
        return

    solver = BookingCaptchaSolver()
    durations: list[float] = []

    print(f"=== Booking CAPTCHA  ({len(images)} image(s)) ===\n")
    print(f"{'File':<40} {'Detected chars (left→right)':<35} {'Time (ms)':>10}")
    print("-" * 88)

    for path in images:
        image_bytes = path.read_bytes()
        t0 = time.perf_counter()
        results = solver.solve(image_bytes)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        durations.append(elapsed_ms)

        char_summary = "  ".join(
            f"{r.char}@({r.x1},{r.y1})-({r.x2},{r.y2})" for r in results
        )
        print(f"{path.name:<40} {char_summary:<35} {elapsed_ms:>9.1f}ms")

    print("-" * 88)
    _print_stats(durations)


def _print_stats(durations: list[float]) -> None:
    print(f"\nImages solved : {len(durations)}")
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
        help="Which solver to benchmark (default: all)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/captcha"),
        help="Directory containing captcha images (default: data/captcha)",
    )
    args = parser.parse_args()

    if args.type in ("login", "all"):
        bench_login(args.data_dir)
    if args.type in ("booking", "all"):
        bench_booking(args.data_dir)


if __name__ == "__main__":
    main()
