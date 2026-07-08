"""Memory — tiered episodic + semantic memory over SQLite.

Episodic = per-perception `context` rows; semantic = `summaries` rows referenced
once via FK (P1). `recall()` searches via FTS5 (P5); `prune()`/`forget()` bound
growth (P4). FTS mirrors `context` exactly, including after deletes (P2).

Pure stdlib (sqlite3) — imports with no native deps (P7).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cortana.embeddings import cosine, reciprocal_rank_fusion
from cortana.perception import Observation, Semantic

log = logging.getLogger("cortana.memory")

SCHEMA_VERSION = 4

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

# Semantic index (v3): one embedding vector per context row with OCR text. Cascade
# so pruning a context row drops its vector (needs foreign_keys=ON).
_EMBEDDINGS_SCHEMA = """
CREATE TABLE embeddings (
    context_id INTEGER PRIMARY KEY REFERENCES context(id) ON DELETE CASCADE,
    vec        TEXT NOT NULL
);
"""

# Reflections (v4): durable higher-level insights consolidated from many episodes
# (generative-agents style) — semantic memory that outlives the raw episodes.
_REFLECTIONS_SCHEMA = """
CREATE TABLE reflections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

GIB = 1024 ** 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Memory:
    """The agent's memory. All access is single-connection; the Phase-3 loop funnels
    writes through one thread."""

    def __init__(self, path, *, ocr_max_chars: int = 6000,
                 retention_days: int = 90, max_db_bytes: int = 2 * GIB,
                 check_same_thread: bool = True) -> None:
        self.path = Path(path)
        self.ocr_max_chars = ocr_max_chars
        self.retention_days = retention_days
        self.max_db_bytes = max_db_bytes
        # The agent loop funnels every write through a single db executor thread, so
        # it opens the connection with check_same_thread=False (access stays serial).
        self._conn = sqlite3.connect(self.path, check_same_thread=check_same_thread)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")   # cross-process writers wait, not SQLITE_BUSY
        # Serializes reads issued from the chat server's per-request threads, which
        # share one connection (check_same_thread=False disables the check but does
        # NOT make concurrent use of one sqlite3 connection safe).
        self._lock = threading.Lock()
        self.migrate()

    # --- schema / migration ------------------------------------------------ #
    def _user_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def migrate(self) -> None:
        """Bring the DB to SCHEMA_VERSION. No-op when already current; creates a
        fresh schema for a new DB; upgrades a legacy v0 DB additively (P8)."""
        if self._user_version() >= SCHEMA_VERSION:
            return
        if not self._table_exists("context"):
            self._conn.executescript(_FRESH_SCHEMA)
            self._conn.executescript(_FTS_SCHEMA)
            self._conn.executescript(_EMBEDDINGS_SCHEMA)
            self._conn.executescript(_REFLECTIONS_SCHEMA)
        else:
            if not self._table_exists("summaries"):        # legacy v0 -> v2 structures
                self._upgrade_legacy()
            if not self._table_exists("context_fts"):       # crash-recovery: a partial
                self._conn.executescript(_FTS_SCHEMA)       # fresh-create must still get FTS
                self._conn.execute("INSERT INTO context_fts(context_fts) VALUES('rebuild')")
            if not self._table_exists("embeddings"):        # v2 -> v3 semantic index
                self._conn.executescript(_EMBEDDINGS_SCHEMA)
            if not self._table_exists("reflections"):       # v3 -> v4 consolidation
                self._conn.executescript(_REFLECTIONS_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    def _upgrade_legacy(self) -> None:
        """Additively upgrade a v0 DB (denormalized `summary` per row): add the new
        columns, create the semantic table + FTS, and rebuild the index over existing
        rows. Each object is guarded so a partially-applied prior run re-applies
        cleanly; the legacy `summary` column is kept, read-only."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(context)")}
        if "content_hash" not in cols:
            self._conn.execute("ALTER TABLE context ADD COLUMN content_hash TEXT")
        if "summary_id" not in cols:
            self._conn.execute("ALTER TABLE context ADD COLUMN summary_id INTEGER")
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS summaries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " window_start_ts TEXT NOT NULL, window_end_ts TEXT NOT NULL,"
            " summary TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL);"
        )
        if not self._table_exists("context_fts"):
            self._conn.executescript(_FTS_SCHEMA)
        self._conn.execute("INSERT INTO context_fts(context_fts) VALUES('rebuild')")

    # --- write path -------------------------------------------------------- #
    def _truncate(self, text: str | None) -> str:
        return (text or "")[: self.ocr_max_chars]

    def _insert_event(self, obs: Observation, *, summary_id, skip_reason=None) -> int:
        cur = self._conn.execute(
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
        return cur.lastrowid

    def remember(self, observations: list[Observation],
                 semantic: Semantic | None, embedder=None) -> int | None:
        """Persist a batch: one summary row (if any) + its events, linked by FK. When
        ``embedder`` is given, store a semantic vector for each event's OCR text.
        Returns the summary_id (or None when there was no semantic record)."""
        summary_id = None
        # Embed BEFORE opening the transaction — a slow/hung embedder (network call)
        # must not hold the write txn open (it would block the single db thread and
        # stall backpressure persistence / shutdown). Best-effort: a failed embed
        # never loses the row (keyword search still works).
        vecs: dict[int, list[float]] = {}
        if embedder is not None:
            for i, obs in enumerate(observations):
                if obs.ocr_text:
                    try:
                        vecs[i] = embedder.embed(obs.ocr_text)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("embedding failed (storing row without vector): %s", exc)
        # `with self._conn` is one atomic transaction: on any failure the summary and
        # its events roll back together — never a summary orphaned from its events.
        with self._conn:
            if semantic is not None:
                cur = self._conn.execute(
                    "INSERT INTO summaries "
                    "(window_start_ts, window_end_ts, summary, model, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (semantic.window_start_ts, semantic.window_end_ts,
                     semantic.summary, semantic.model, _now()),
                )
                summary_id = cur.lastrowid
            for i, obs in enumerate(observations):
                cid = self._insert_event(obs, summary_id=summary_id)
                if i in vecs:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO embeddings(context_id, vec) VALUES (?, ?)",
                        (cid, json.dumps(vecs[i])))
        return summary_id

    def remember_dropped(self, observation: Observation) -> None:
        """Persist a backpressure-evicted perception so loss is never silent."""
        with self._conn:
            self._insert_event(observation, summary_id=None,
                               skip_reason="dropped_backpressure")

    # --- retention (P4): bounded by age, size, and orphan cleanup ---------- #
    def _db_size(self) -> int:
        pc = self._conn.execute("PRAGMA page_count").fetchone()[0]
        ps = self._conn.execute("PRAGMA page_size").fetchone()[0]
        return pc * ps

    def _delete_older_than(self, cutoff_ts: str) -> int:
        cur = self._conn.execute("DELETE FROM context WHERE ts < ?", (cutoff_ts,))
        return cur.rowcount

    def _prune_orphan_summaries(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM summaries WHERE id NOT IN "
            "(SELECT summary_id FROM context WHERE summary_id IS NOT NULL)"
        )
        return cur.rowcount

    def _vacuum(self) -> None:
        """Reclaim freed pages so size measurements reflect deletions. Must run
        outside a transaction."""
        self._conn.commit()
        prev = self._conn.isolation_level
        self._conn.isolation_level = None
        try:
            self._conn.execute("VACUUM")
        finally:
            self._conn.isolation_level = prev   # always restore, else writes lose atomicity

    def forget(self, older_than: str) -> int:
        """Delete every memory older than ``older_than`` (ISO ts); drop orphan
        summaries. Returns rows removed."""
        removed = self._delete_older_than(older_than)
        self._prune_orphan_summaries()
        self._conn.commit()
        return removed

    def prune(self) -> int:
        """Enforce retention: drop rows older than ``retention_days``, evict
        oldest-first until under ``max_db_bytes``, then drop orphan summaries.
        Returns total rows removed."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=self.retention_days)).isoformat()
        removed = self._delete_older_than(cutoff)
        removed += self._evict_to_size_cap()
        self._prune_orphan_summaries()
        # reflections are bounded by the same age policy (every tier has a cap)
        self._conn.execute("DELETE FROM reflections WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return removed

    def _evict_to_size_cap(self) -> int:
        """Delete the oldest rows so the DB fits under ``max_db_bytes``. Freed pages
        aren't reclaimed until VACUUM, so rather than re-measure in a loop we estimate
        the rows to drop from the average row size, delete in one statement, and
        VACUUM once."""
        size = self._db_size()
        if size <= self.max_db_bytes:
            return 0
        total = self._conn.execute("SELECT count(*) FROM context").fetchone()[0]
        if total == 0:
            return 0
        avg_row = size / total
        keep = int(self.max_db_bytes * 0.9 / avg_row)      # 10% headroom
        to_delete = max(0, total - keep)                   # ≥1: size>cap implies keep<total
        cur = self._conn.execute(
            "DELETE FROM context WHERE id IN "
            "(SELECT id FROM context ORDER BY ts ASC LIMIT ?)", (to_delete,))
        self._vacuum()
        return cur.rowcount

    # --- recall ------------------------------------------------------------ #
    _COLS = ("id", "ts", "app_name", "bundle_id", "window_title", "ocr_text",
             "skip_reason", "summary_id")

    def recall(self, query: str | None = None, *, since: str | None = None,
               until: str | None = None, app: str | None = None,
               limit: int = 50, embedder=None) -> list[dict]:
        """Retrieve memories. With ``embedder`` + ``query``: **hybrid** search —
        keyword (FTS5) and semantic (embedding cosine) fused by Reciprocal Rank
        Fusion, best-match first. Otherwise: full-text (FTS5) or a recency scan,
        newest-first. Each row carries ts + app_name as a citation (P5) and its batch
        ``summary`` (NULL for dropped rows)."""
        if embedder is not None and query:
            try:
                # Embed BEFORE taking the lock — a slow/hung embedder (30s HTTP call)
                # must not block every other reader.
                qvec = embedder.embed(query)
                with self._lock:
                    ids = self._hybrid_ids(query, qvec, since, until, app, max(1, limit))
                    return self._rows_by_ids(ids)
            except Exception as exc:  # noqa: BLE001 - embedder down -> keyword fallback
                log.warning("hybrid recall failed (%s); falling back to keyword", exc)
        cols = ", ".join(f"c.{c}" for c in self._COLS) + ", s.summary"
        join = " LEFT JOIN summaries s ON s.id = c.summary_id"
        params: list = []
        # 'unchanged' heartbeats are dwell-time markers (empty OCR, no summary), not
        # content memories — excluding them keeps recall from returning stale, blank
        # rows during idle periods.
        where: list[str] = ["(c.skip_reason IS NULL OR c.skip_reason != 'unchanged')"]

        if query:
            sql = (f"SELECT {cols} FROM context_fts "
                   "JOIN context c ON c.id = context_fts.rowid" + join +
                   " WHERE context_fts MATCH ?")
            params.append(query)
        else:
            sql = f"SELECT {cols} FROM context c{join} WHERE 1=1"

        if since is not None:
            where.append("c.ts >= ?"); params.append(since)
        if until is not None:
            where.append("c.ts < ?"); params.append(until)
        if app is not None:
            where.append("c.app_name = ?"); params.append(app)
        for clause in where:
            sql += f" AND {clause}"
        sql += " ORDER BY c.ts DESC LIMIT ?"
        params.append(max(1, limit))            # LIMIT -1 would mean "unbounded" in SQLite

        out_cols = (*self._COLS, "summary")
        with self._lock:                        # serialize reads across chat threads
            try:
                rows = list(self._conn.execute(sql, params))
            except sqlite3.OperationalError:
                if not query:
                    raise
                # A raw query contained FTS5 syntax (quote/paren/operator). Retry it
                # as a quoted phrase so recall never crashes on arbitrary input.
                params[0] = '"' + query.replace('"', '""') + '"'
                rows = list(self._conn.execute(sql, params))
        return [dict(zip(out_cols, row)) for row in rows]

    # --- hybrid (keyword + semantic) retrieval ----------------------------- #
    def _filter_clauses(self, since, until, app) -> tuple[str, list]:
        where = ["(c.skip_reason IS NULL OR c.skip_reason != 'unchanged')"]
        params: list = []
        if since is not None:
            where.append("c.ts >= ?"); params.append(since)
        if until is not None:
            where.append("c.ts < ?"); params.append(until)
        if app is not None:
            where.append("c.app_name = ?"); params.append(app)
        return " AND ".join(where), params

    def _hybrid_ids(self, query, qvec, since, until, app, limit) -> list[int]:
        """Fuse FTS keyword ranking with embedding-cosine ranking (against the
        pre-computed query vector ``qvec``) via RRF. Caller holds the lock."""
        filt, fparams = self._filter_clauses(since, until, app)
        candidate_k = max(limit * 5, 50)          # deeper candidate pools -> better fusion
        # keyword ids, ranked by FTS5 relevance (rank), crash-proof on raw input
        kw_sql = (f"SELECT c.id FROM context_fts JOIN context c ON c.id=context_fts.rowid "
                  f"WHERE context_fts MATCH ? AND {filt} ORDER BY rank LIMIT ?")
        try:
            kw = [r[0] for r in self._conn.execute(kw_sql, [query, *fparams, candidate_k])]
        except sqlite3.OperationalError:
            phrase = '"' + query.replace('"', '""') + '"'
            kw = [r[0] for r in self._conn.execute(kw_sql, [phrase, *fparams, candidate_k])]
        # semantic ids, ranked by cosine to the query vector (brute-force; bounded by
        # retention — fine at this scale)
        sem_rows = self._conn.execute(
            f"SELECT e.context_id, e.vec FROM embeddings e "
            f"JOIN context c ON c.id=e.context_id WHERE {filt}", fparams)
        # Skip vectors that don't match the query's dimension (a stored vector from a
        # different embed_model, or a corrupt/empty row). cosine() raises ValueError
        # on a mismatch; a bare list-comp would let ONE bad row abort the whole scan
        # and silently demote every hybrid query to keyword-only.
        sims = []
        for cid, vec in sem_rows:
            try:
                sims.append((cid, cosine(qvec, json.loads(vec))))
            except ValueError:
                continue
        sims.sort(key=lambda t: t[1], reverse=True)
        sem = [cid for cid, _ in sims[:candidate_k]]
        return reciprocal_rank_fusion([kw, sem])[:limit]

    def _rows_by_ids(self, ids: list[int]) -> list[dict]:
        """Fetch context rows for ``ids``, preserving the given (fused) order."""
        if not ids:
            return []
        cols = ", ".join(f"c.{c}" for c in self._COLS) + ", s.summary"
        placeholders = ",".join("?" * len(ids))
        sql = (f"SELECT {cols} FROM context c LEFT JOIN summaries s ON s.id=c.summary_id "
               f"WHERE c.id IN ({placeholders})")
        by_id = {row[0]: row for row in self._conn.execute(sql, ids)}   # row[0] = c.id
        out_cols = (*self._COLS, "summary")
        return [dict(zip(out_cols, by_id[i])) for i in ids if i in by_id]

    # --- reflections (consolidated semantic memory) ------------------------ #
    def add_reflection(self, period_start: str, period_end: str, text: str) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO reflections (period_start, period_end, text, created_at) "
                "VALUES (?, ?, ?, ?)", (period_start, period_end, text, _now()))
        return cur.lastrowid

    def recent_reflections(self, limit: int = 10) -> list[dict]:
        cols = ("id", "period_start", "period_end", "text", "created_at")
        with self._lock:
            rows = list(self._conn.execute(
                f"SELECT {', '.join(cols)} FROM reflections "
                "ORDER BY created_at DESC LIMIT ?", (max(1, limit),)))
        return [dict(zip(cols, r)) for r in rows]

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
        # Take the read lock so we never close the connection out from under an
        # in-flight chat recall sharing it (the desktop closes read_memory on quit
        # while the chat server's request threads may still be querying).
        with self._lock:
            self._conn.close()
