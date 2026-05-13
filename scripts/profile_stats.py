"""Aggregate stage stats across scheduled-run profile dumps.

Usage:
    python scripts/profile_stats.py [data/profiles]

Reads every `scheduled_*.json` in the given directory, collects each
stage's `duration_ms` across all workers and all runs, and prints a
table of count / mean / std / p50 / p90 / p99 / max per stage.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def collect(profile_dir: Path) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = defaultdict(list)
    for path in sorted(profile_dir.glob("scheduled_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for w in data.get("workers", []):
            for ev in w.get("events", []):
                dur = ev.get("duration_ms", 0.0)
                if dur > 0:
                    samples[ev["name"]].append(float(dur))
    return samples


def render(samples: dict[str, list[float]]) -> str:
    rows = []
    for name, vals in samples.items():
        rows.append({
            "stage": name,
            "n": len(vals),
            "mean": mean(vals),
            "std": pstdev(vals) if len(vals) > 1 else 0.0,
            "p50": percentile(vals, 0.50),
            "p90": percentile(vals, 0.90),
            "p99": percentile(vals, 0.99),
            "max": max(vals),
        })
    rows.sort(key=lambda r: r["mean"], reverse=True)
    head = f"{'stage':<32s} {'n':>4s} {'mean':>9s} {'std':>9s} {'p50':>9s} {'p90':>9s} {'p99':>9s} {'max':>9s}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['stage']:<32s} {r['n']:>4d} "
            f"{r['mean']:>9.1f} {r['std']:>9.1f} "
            f"{r['p50']:>9.1f} {r['p90']:>9.1f} "
            f"{r['p99']:>9.1f} {r['max']:>9.1f}"
        )
    return "\n".join(lines)


def render_md(samples: dict[str, list[float]]) -> str:
    rows = []
    for name, vals in samples.items():
        rows.append({
            "stage": name,
            "n": len(vals),
            "mean": mean(vals),
            "std": pstdev(vals) if len(vals) > 1 else 0.0,
            "p50": percentile(vals, 0.50),
            "p90": percentile(vals, 0.90),
            "p99": percentile(vals, 0.99),
            "max": max(vals),
        })
    rows.sort(key=lambda r: r["mean"], reverse=True)
    lines = [
        "| stage | n | mean | std | p50 | p90 | p99 | max |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['stage']} | {r['n']} | {r['mean']:.1f} | {r['std']:.1f} | "
            f"{r['p50']:.1f} | {r['p90']:.1f} | {r['p99']:.1f} | {r['max']:.1f} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/profiles")
    s = collect(d)
    print(render(s))
    if "--md" in sys.argv:
        print()
        print(render_md(s))
