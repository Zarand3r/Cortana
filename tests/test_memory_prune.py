"""Step 5: retention bounds — age, size, orphan summaries (P4); FTS stays synced (P2)."""

from datetime import datetime, timedelta, timezone

from cortana.memory import Memory
from cortana.perception import Observation, Semantic


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _obs(ocr, *, ts, app="Notes"):
    return Observation(ts=ts, app_name=app, bundle_id="com.app", window_title="Doc",
                       ocr_text=ocr, captured=True)


def _sem():
    return Semantic(summary="s", model="fake",
                    window_start_ts=_ts(0), window_end_ts=_ts(0))


def test_prune_by_age(tmp_path):
    mem = Memory(tmp_path / "m.db", retention_days=90)
    mem.remember([_obs("ancient", ts=_ts(200))], _sem())   # older than 90d
    mem.remember([_obs("recent", ts=_ts(1))], _sem())      # within 90d
    removed = mem.prune()
    assert removed >= 1
    remaining = [h["ocr_text"] for h in mem.recall()]
    assert "recent" in remaining
    assert "ancient" not in remaining
    mem.close()


def test_prune_by_size(tmp_path):
    # Huge retention so only the size cap can act; tiny cap forces eviction.
    mem = Memory(tmp_path / "m.db", retention_days=10_000, max_db_bytes=8192)
    for i in range(60):
        mem.remember([_obs("padding " * 200, ts=_ts(60 - i))], _sem())
    before = mem.counts()["context"]
    mem.prune()
    after = mem.counts()["context"]
    assert after < before          # oldest-first eviction kept us bounded (P4)
    mem.close()


def test_orphan_summaries_pruned(tmp_path):
    mem = Memory(tmp_path / "m.db", retention_days=90)
    mem.remember([_obs("old", ts=_ts(200))], _sem())   # its summary becomes orphan
    mem.remember([_obs("new", ts=_ts(1))], _sem())
    assert mem.counts()["summaries"] == 2
    mem.prune()
    assert mem.counts()["summaries"] == 1              # orphan removed (P4)
    mem.close()


def test_prune_keeps_fts_in_sync(tmp_path):
    mem = Memory(tmp_path / "m.db", retention_days=90)
    mem.remember([_obs("gone", ts=_ts(200))], _sem())
    mem.remember([_obs("stay", ts=_ts(1))], _sem())
    mem.prune()
    c = mem.counts()
    assert c["context_fts"] == c["context"]            # P2
    mem.close()


def test_forget_by_timestamp(tmp_path):
    mem = Memory(tmp_path / "m.db")
    mem.remember([_obs("a", ts=_ts(10))], _sem())
    mem.remember([_obs("b", ts=_ts(1))], _sem())
    removed = mem.forget(older_than=_ts(5))
    assert removed == 1
    assert [h["ocr_text"] for h in mem.recall()] == ["b"]
    mem.close()
