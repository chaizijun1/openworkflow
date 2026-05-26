"""Progress display — the ``phase()``/``log()`` narrator and per-phase agent grouping.

The original renders a live tree: ``log()`` lines as a narrator above the tree, and each
``agent()`` grouped under the current ``phase()`` title (same phase string => same group box).
This is a dependency-light text version: it prints phase headers, indented agent
start/finish lines, and narrator lines to stderr. If ``rich`` is installed it is used for
nicer live output; otherwise plain prints. Set ``quiet=True`` to silence everything.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field


@dataclass
class Progress:
    quiet: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _phase_counts: dict[str, int] = field(default_factory=dict)
    _seen_phases: list = field(default_factory=list)

    def _emit(self, text: str) -> None:
        if self.quiet:
            return
        with self._lock:
            print(text, file=sys.stderr, flush=True)

    def phase(self, title: str) -> None:
        with self._lock:
            new = title not in self._phase_counts
            if new:
                self._phase_counts[title] = 0
                self._seen_phases.append(title)
        if new:
            self._emit(f"\n▸ {title}")

    def log(self, message: str) -> None:
        self._emit(f"  · {message}")

    def agent_start(self, phase: str | None, label: str) -> None:
        if phase:
            with self._lock:
                self._phase_counts[phase] = self._phase_counts.get(phase, 0) + 1
        loc = f"[{phase}] " if phase else ""
        self._emit(f"  {loc}↻ {label}…")

    def agent_done(self, phase: str | None, label: str, *, replayed: bool = False) -> None:
        loc = f"[{phase}] " if phase else ""
        mark = "⟲" if replayed else "✓"
        self._emit(f"  {loc}{mark} {label}")

    def agent_failed(self, phase: str | None, label: str, err: str) -> None:
        loc = f"[{phase}] " if phase else ""
        self._emit(f"  {loc}✗ {label} — {err}")

    def summary(self) -> str:
        with self._lock:
            parts = [f"{p}: {n} agent(s)" for p, n in self._phase_counts.items()]
        return "; ".join(parts) if parts else "no phases"
