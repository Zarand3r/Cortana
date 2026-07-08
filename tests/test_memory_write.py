"""Step 3: Memory schema + write path. Summary stored once (P1), OCR bounded (P3),
FTS synced (P2)."""

from cortana.memory import Memory
from cortana.perception import Observation, Semantic


def _obs(ocr, *, ts="2026-06-14T10:00:00+00:00", app="Notes", title="Doc",
         skip_reason="", captured=True):
    return Observation(ts=ts, app_name=app, bundle_id="com.app", window_title=title,
                       ocr_text=ocr, captured=captured, skip_reason=skip_reason)


def _sem(summary="Doing work.", model="fake"):
    return Semantic(summary=summary, model=model,
                    window_start_ts="2026-06-14T10:00:00+00:00",
                    window_end_ts="2026-06-14T10:01:00+00:00")


def _open(tmp_path, **kw):
    return Memory(tmp_path / "mem.db", **kw)


def test_fresh_schema(tmp_path):
    mem = _open(tmp_path)
    cur = mem._conn.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == 4
    tables = {r[0] for r in mem._conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"summaries", "context", "context_fts", "embeddings", "reflections"} <= tables
    cols = {r[1] for r in mem._conn.execute("PRAGMA table_info(context)")}
    assert "summary" not in cols           # fresh schema is normalized
    assert {"summary_id", "content_hash"} <= cols
    mem.close()


def test_remember_one_summary_per_batch(tmp_path):
    mem = _open(tmp_path)
    batch = [_obs("a"), _obs("b"), _obs("c")]
    sid = mem.remember(batch, _sem("Reviewing three things."))
    c = mem.counts()
    assert c["summaries"] == 1
    assert c["context"] == 3
    rows = list(mem._conn.execute("SELECT summary_id FROM context"))
    assert all(r[0] == sid for r in rows)   # all reference the one summary (P1)
    mem.close()


def test_no_summary_text_duplicated(tmp_path):
    mem = _open(tmp_path)
    mem.remember([_obs("a"), _obs("b")], _sem("Unique summary text."))
    # The text lives once in summaries, never copied into context rows.
    n = mem._conn.execute(
        "SELECT count(*) FROM summaries WHERE summary='Unique summary text.'"
    ).fetchone()[0]
    assert n == 1
    mem.close()


def test_ocr_truncated_at_store(tmp_path):
    mem = _open(tmp_path, ocr_max_chars=20)
    mem.remember([_obs("y" * 5000)], _sem())
    stored = mem._conn.execute("SELECT ocr_text FROM context").fetchone()[0]
    assert len(stored) <= 20                # P3
    mem.close()


def test_remember_without_summary(tmp_path):
    mem = _open(tmp_path)
    sid = mem.remember([_obs("a"), _obs("b")], None)   # no semantic record
    assert sid is None
    c = mem.counts()
    assert c["context"] == 2
    assert c["summaries"] == 0
    assert all(r[0] is None for r in mem._conn.execute("SELECT summary_id FROM context"))
    mem.close()


def test_remember_dropped(tmp_path):
    mem = _open(tmp_path)
    mem.remember_dropped(_obs("lost frame"))
    row = mem._conn.execute(
        "SELECT skip_reason, summary_id FROM context").fetchone()
    assert row[0] == "dropped_backpressure"
    assert row[1] is None
    mem.close()


def test_fts_synced_on_insert(tmp_path):
    mem = _open(tmp_path)
    mem.remember([_obs("hello"), _obs("world")], _sem())
    c = mem.counts()
    assert c["context_fts"] == c["context"]  # P2
    mem.close()
