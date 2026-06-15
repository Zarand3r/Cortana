"""Memory — tiered episodic + semantic memory over SQLite.

Episodic = per-perception `context` rows; semantic = `summaries` rows referenced
once via FK (P1). `recall()` searches via FTS5 (P5); `prune()`/`forget()` bound
growth (P4). FTS mirrors `context` exactly, including after deletes (P2).

Pure stdlib (sqlite3) — imports with no native deps (P7).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cortana.perception import Observation, Semantic

SCHEMA_VERSION = 2

# Fresh-DB schema (normalized — no `summary` text column on `context`).
_FRESH_SCHEMA = """
CREATE TABLE summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start_ts TEXT NOT NULL,
    window_end_ts   TEXT NOT NULL,
    summary         TEXT NOT NULL,
    model           TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE context (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    app_name     TEXT    NOT NULL,
    bundle_id    TEXT,
    window_title TEXT,
    ocr_text     TEXT,
    captured     INTEGER NOT NULL DEFAULT 1,
    skip_reason  TEXT,
    content_hash TEXT,
    summary_id   INTEGER REFERENCES summaries(id) ON DELETE SET NULL
);
CREATE INDEX idx_context_ts   ON context(ts);
CREATE INDEX idx_context_app  ON context(app_name);
CREATE INDEX idx_context_hash ON context(content_hash);
"""

# FTS5 external-content mirror + triggers that keep it synced (P2). Shared by the
# fresh-create and legacy-migration paths.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE context_fts USING fts5(ocr_text, content='context', content_rowid='id');
CREATE TRIGGER context_ai AFTER INSERT ON context BEGIN
    INSERT INTO context_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
END;
CREATE TRIGGER context_ad AFTER DELETE ON context BEGIN
    INSERT INTO context_fts(context_fts, rowid, ocr_text) VALUES('delete', old.id, old.ocr_text);
END;
CREATE TRIGGER context_au AFTER UPDATE ON context BEGIN
    INSERT INTO context_fts(context_fts, rowid, ocr_text) VALUES('delete', old.id, old.ocr_text);
    INSERT INTO context_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
END;
"""

GIB = 1024 ** 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Memory:
    """The agent's memory. All access is single-connection; the Phase-3 loop funnels
    writes through one thread."""

    def __init__(self, path, *, ocr_max_chars: int = 6000,
                 retention_days: int = 90, max_db_bytes: int = 2 * GIB) -> None:
        self.path = Path(path)
        self.ocr_max_chars = ocr_max_chars
        self.retention_days = retention_days
        self.max_db_bytes = max_db_bytes
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # --- schema / migration ------------------------------------------------ #
    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def migrate(self) -> None:
        """Bring the DB to SCHEMA_VERSION. Fresh DB -> create; legacy v0 -> upgrade
        additively (Step 5)."""
        if not self._table_exists("context"):
            self._conn.executescript(_FRESH_SCHEMA)
            self._conn.executescript(_FTS_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()
            return
        # Existing DB: handled in Step 5 (legacy migration).
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:  # filled in at Step 5
        pass

    # --- write path -------------------------------------------------------- #
    def _truncate(self, text: str | None) -> str:
        return (text or "")[: self.ocr_max_chars]

    def _insert_event(self, obs: Observation, *, summary_id, skip_reason=None) -> None:
        self._conn.execute(
            "INSERT INTO context "
            "(ts, app_name, bundle_id, window_title, ocr_text, captured, "
            " skip_reason, content_hash, summary_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                obs.ts, obs.app_name, obs.bundle_id, obs.window_title,
                self._truncate(obs.ocr_text), int(obs.captured),
                skip_reason if skip_reason is not None else obs.skip_reason,
                obs.content_hash or None, summary_id,
            ),
        )

    def remember(self, observations: list[Observation],
                 semantic: Semantic | None) -> int | None:
        """Persist a batch: one summary row (if any) + its events, linked by FK.
        Returns the summary_id (or None when there was no semantic record)."""
        summary_id = None
        if semantic is not None:
            cur = self._conn.execute(
                "INSERT INTO summaries "
                "(window_start_ts, window_end_ts, summary, model, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (semantic.window_start_ts, semantic.window_end_ts,
                 semantic.summary, semantic.model, _now()),
            )
            summary_id = cur.lastrowid
        for obs in observations:
            self._insert_event(obs, summary_id=summary_id)
        self._conn.commit()
        return summary_id

    def remember_dropped(self, observation: Observation) -> None:
        """Persist a backpressure-evicted perception so loss is never silent."""
        self._insert_event(observation, summary_id=None,
                           skip_reason="dropped_backpressure")
        self._conn.commit()

    # --- introspection ----------------------------------------------------- #
    def counts(self) -> dict[str, int]:
        def n(sql: str) -> int:
            return self._conn.execute(sql).fetchone()[0]
        return {
            "context": n("SELECT count(*) FROM context"),
            "context_fts": n("SELECT count(*) FROM context_fts"),
            "summaries": n("SELECT count(*) FROM summaries"),
        }

    def close(self) -> None:
        self._conn.close()
