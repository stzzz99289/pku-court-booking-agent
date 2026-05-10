"""Lightweight stage-latency profiler for the booking flow.

When `cfg.profile` is false, `Profiler.span(...)` is a no-op (the `yield`
branch returns immediately) so production runs pay zero overhead. When
enabled, each stage records its offset from `begin()` and its duration in
milliseconds; `report()` prints a summary table at the end of the run.

Designed to be passed alongside `AppConfig` (one profiler per worker process).
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Profiler:
    enabled: bool = False
    events: list[tuple[str, float, float]] = field(default_factory=list)
    _t0: float = 0.0

    def begin(self, label: str = "profile_begin") -> None:
        if not self.enabled:
            return
        self._t0 = time.perf_counter()
        self.events.append((label, 0.0, 0.0))
        log.info("[profile] %s @ t+0.0 ms", label)

    @contextmanager
    def span(self, name: str):
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            end = time.perf_counter()
            offset = (start - self._t0) * 1000 if self._t0 else 0.0
            dur = (end - start) * 1000
            self.events.append((name, offset, dur))
            log.info("[profile] %s: %.1f ms (t+%.1f ms)", name, dur, offset)

    def mark(self, name: str) -> None:
        if not self.enabled:
            return
        t = time.perf_counter()
        offset = (t - self._t0) * 1000 if self._t0 else 0.0
        self.events.append((name, offset, 0.0))
        log.info("[profile] %s @ t+%.1f ms", name, offset)

    def report(self) -> str:
        if not self.enabled or not self.events:
            return ""
        lines = [
            "",
            "=" * 72,
            "PROFILE SUMMARY (timings in ms)",
            "=" * 72,
            f"  {'stage':<44s} {'t+offset':>12s} {'duration':>12s}",
            "  " + "-" * 70,
        ]
        for name, offset, dur in self.events:
            dur_str = f"{dur:12.1f}" if dur > 0 else f"{'-':>12s}"
            lines.append(f"  {name:<44s} {offset:>12.1f} {dur_str}")
        lines.append("=" * 72)
        return "\n".join(lines)
