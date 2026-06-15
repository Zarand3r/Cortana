"""Step 5: legacy v0 DB upgrades additively and losslessly to schema v2 (P8)."""

import sqlite3

from cortana.memory import Memory

# The v0 schema shipped by context_tracker.py (denormalized summary per row).
_LEGACY_SCHEMA = """
CREATE TABLE context (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    app_name     TEXT    NOT NULL,
    bundle_id    TEXT,
    window_title TEXT,
    ocr_text     TEXT,
    summary      TEXT,
    captured     INTEGER NOT NULL DEFAULT 1,
    skip_reason  TEXT
);
CREATE INDEX idx_context_ts  ON context(ts);
CREATE INDEX idx_context_app ON context(app_name);
"""


def _seed_legacy(path):
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO context (ts, app_name, bundle_id, window_title, ocr_text, "
        "summary, captured, skip_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-01-01T09:00:00+00:00", "Numbers", "com.apple.Numbers", "Q1",
             "legacy budget figures", "Reviewing the budget.", 1, ""),
            ("2026-01-01T09:00:30+00:00", "Numbers", "com.apple.Numbers", "Q1",
             "more budget figures", "Reviewing the budget.", 1, ""),
        ],
    )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def test_legacy_migration(tmp_path):
    db = tmp_path / "legacy.db"
    _seed_legacy(db)

    mem = Memory(db)   # opening triggers migration

    # upgraded version
    assert mem._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    # new structures exist
    tables = {r[0] for r in mem._conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"summaries", "context_fts"} <= tables
    # new columns added; legacy `summary` column retained (read-only)
    cols = {r[1] for r in mem._conn.execute("PRAGMA table_info(context)")}
    assert {"summary_id", "content_hash", "summary"} <= cols
    # old rows still readable
    assert mem.counts()["context"] == 2
    # FTS rebuilt to match existing rows (P2)
    c = mem.counts()
    assert c["context_fts"] == c["context"]
    # and the rebuilt index is actually searchable
    assert len(mem.recall(query="budget")) == 2
    mem.close()


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    _seed_legacy(db)
    Memory(db).close()        # migrate once
    mem = Memory(db)          # open again — must not re-migrate or error
    assert mem._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert mem.counts()["context"] == 2
    mem.close()
