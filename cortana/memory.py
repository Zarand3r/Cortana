"""Memory — tiered episodic + semantic memory over SQLite.

Episodic = per-perception `context` rows; semantic = `summaries` rows referenced
once via FK. `recall()` searches via FTS5; `prune()`/`forget()` bound growth.
Pure stdlib (sqlite3) — imports with no native deps. Filled in at Steps 3–5.
"""

from __future__ import annotations
