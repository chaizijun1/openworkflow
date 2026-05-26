"""Shared token budget.

Mirrors the original ``budget`` global:

    budget: {total: number|null, spent(): number, remaining(): number}

``total`` comes from the user's ``+500k``-style directive (None if unset). ``spent()`` is the
output tokens consumed this run across the main script AND every nested workflow — the pool is
shared, not per-workflow. ``remaining()`` is ``max(0, total - spent())`` or ``inf`` when no
target was set. The target is a HARD ceiling: once ``spent() >= total`` the runtime refuses
further ``agent()`` calls (it raises), it is not advisory.
"""

from __future__ import annotations

import math
import threading


class Budget:
    """A thread-safe shared output-token counter with an optional hard ceiling."""

    def __init__(self, total: int | None = None) -> None:
        self.total = total
        self._spent = 0
        self._lock = threading.Lock()

    # --- script-facing API (matches the JS shape: callables, not properties) ---
    def spent(self) -> int:
        with self._lock:
            return self._spent

    def remaining(self) -> float:
        if self.total is None:
            return math.inf
        with self._lock:
            return max(0, self.total - self._spent)

    # --- runtime-facing ---
    def add(self, tokens: int) -> None:
        with self._lock:
            self._spent += max(0, int(tokens))

    def exhausted(self) -> bool:
        """True once a ceiling exists and has been reached."""
        if self.total is None:
            return False
        with self._lock:
            return self._spent >= self.total

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        cap = "∞" if self.total is None else str(self.total)
        return f"Budget(spent={self.spent()}, total={cap})"
