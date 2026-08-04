"""Working (short-term) memory — Cortana's live 'what am I doing right now' view.

A bounded, thread-safe, in-memory rolling buffer of the most recent observations.
Distinct from long-term memory (the durable SQLite store): this is fast to read (no
DB query), holds only the recent window, and is the natural source for the advisor
and context-aware chat to ground in *current* activity.

The perception loop (one thread) writes; readers (chat/recommend, other threads)
read — so every operation is guarded by a lock and reads return a snapshot copy.
"""

from __future__ import annotations

import threading
from collections import deque

from cortana.perception import Observation


class WorkingMemory:
    """A fixed-capacity rolling buffer of recent observations. Oldest entries are
    evicted once ``maxlen`` is exceeded."""

    def __init__(self, maxlen: int = 50) -> None:
        self._buf: deque[Observation] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, observation: Observation) -> None:
        with self._lock:
            self._buf.append(observation)

    def recent(self, limit: int | None = None) -> list[Observation]:
        """A chronological (oldest→newest) snapshot; the last ``limit`` if given.
        ``limit=None`` returns all; ``limit<=0`` returns none (not everything —
        ``items[-0:]`` would be the whole list)."""
        with self._lock:
            items = list(self._buf)
        if limit is None:
            return items
        return items[-limit:] if limit > 0 else []

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)
