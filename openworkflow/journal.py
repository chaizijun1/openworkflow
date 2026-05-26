"""Resume journal with drift detection.

The original persists each workflow invocation's script and replays cached tool results on
resume; if the replayed code makes *more* calls than were cached (or in a different order) it
flags "drift" — "likely nondeterminism (Date.now, Math.random) took a different branch." That
is why ``Date.now()/new Date()`` are banned in workflow scripts.

This is a pragmatic version of the same idea, scoped to ``agent()`` results (the expensive
calls). Each completed agent call is recorded keyed by a content hash of (phase|label|prompt)
plus an occurrence counter, so replay is robust to the scheduling nondeterminism that
``parallel()``/``pipeline()`` introduce. On ``--resume``:

  * a call whose key matches a recorded entry returns the cached value (no token spend);
  * a call with an unseen key is genuinely new work and is executed;
  * recorded entries never consumed during replay are reported as drift (the run took a
    different branch than last time).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any


def _key(phase: str | None, label: str, prompt: str) -> str:
    h = hashlib.sha256()
    h.update((phase or "").encode())
    h.update(b"\x00")
    h.update(label.encode())
    h.update(b"\x00")
    h.update(prompt.encode())
    return h.hexdigest()[:16]


@dataclass
class Journal:
    """Append-only, crash-safe record of completed agent() results."""

    path: str | None = None
    # key -> list of recorded results (one per occurrence of that key)
    _recorded: dict[str, list[Any]] = field(default_factory=dict)
    # key -> how many of the recorded results we've replayed so far this run
    _replay_cursor: dict[str, int] = field(default_factory=dict)
    # key -> how many entries existed when we loaded (for drift accounting)
    _loaded_counts: dict[str, int] = field(default_factory=dict)
    _consumed: set = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def load(cls, path: str | None, resume: bool) -> "Journal":
        j = cls(path=path)
        if resume and path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    j._recorded.setdefault(rec["key"], []).append(rec["value"])
            j._loaded_counts = {k: len(v) for k, v in j._recorded.items()}
        return j

    def try_replay(self, phase: str | None, label: str, prompt: str) -> tuple[bool, Any]:
        """Return (hit, value). A hit consumes one recorded occurrence of this key."""
        k = _key(phase, label, prompt)
        with self._lock:
            cursor = self._replay_cursor.get(k, 0)
            recorded = self._recorded.get(k, [])
            if cursor < len(recorded):
                self._replay_cursor[k] = cursor + 1
                self._consumed.add((k, cursor))
                return True, recorded[cursor]
        return False, None

    def record(self, phase: str | None, label: str, prompt: str, value: Any) -> None:
        """Persist a freshly-computed result (append-only, atomic-ish line write)."""
        k = _key(phase, label, prompt)
        with self._lock:
            self._recorded.setdefault(k, []).append(value)
            self._replay_cursor[k] = self._replay_cursor.get(k, 0) + 1
        if self.path:
            line = json.dumps({"key": k, "label": label, "value": value}, ensure_ascii=False)
            # append; flush+fsync so a crash mid-run still leaves a usable journal
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())

    def drift_report(self) -> list[str]:
        """Recorded entries that were never replayed this run = the run diverged."""
        drifts = []
        for k, total in self._loaded_counts.items():
            used = sum(1 for (ck, _i) in self._consumed if ck == k)
            if used < total:
                drifts.append(f"key {k}: {total - used}/{total} cached result(s) never reached")
        return drifts
