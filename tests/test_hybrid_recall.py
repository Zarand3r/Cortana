"""Hybrid retrieval: keyword (FTS) + semantic (embeddings) fused by RRF, and vectors
stored on write. Uses FakeEmbedder (bag-of-words) — mechanics, not model quality."""

from cortana.embeddings import FakeEmbedder
from cortana.memory import Memory
from cortana.perception import Observation, Semantic


def _obs(ocr, *, ts, app):
    return Observation(ts=ts, app_name=app, bundle_id="c", window_title="w",
                       ocr_text=ocr, captured=True)


def _sem():
    return Semantic(summary="s", model="fake",
                    window_start_ts="2026-07-05T00:00:00+00:00",
                    window_end_ts="2026-07-05T00:00:00+00:00")


def _seed(tmp_path, emb):
    mem = Memory(tmp_path / "m.db")
    mem.remember([_obs("quarterly budget spreadsheet",
                       ts="2026-07-05T09:00:00+00:00", app="Numbers")], _sem(), embedder=emb)
    mem.remember([_obs("python asyncio event loop",
                       ts="2026-07-05T15:00:00+00:00", app="Terminal")], _sem(), embedder=emb)
    return mem


def test_remember_with_embedder_stores_vectors(tmp_path):
    mem = _seed(tmp_path, FakeEmbedder())
    n = mem._conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    assert n == 2
    mem.close()


def test_hybrid_recall_returns_relevant_first(tmp_path):
    emb = FakeEmbedder()
    mem = _seed(tmp_path, emb)
    hits = mem.recall(query="budget", embedder=emb)
    assert hits and hits[0]["app_name"] == "Numbers"
    mem.close()


def test_hybrid_beats_keyword_only(tmp_path):
    # "budget report": FTS needs BOTH terms (implicit AND); no row has "report", so
    # keyword-only finds nothing — but semantic still surfaces the budget row.
    emb = FakeEmbedder()
    mem = _seed(tmp_path, emb)
    assert mem.recall(query="budget report") == []                 # keyword-only: miss
    hits = mem.recall(query="budget report", embedder=emb)         # hybrid: hit
    assert hits and hits[0]["app_name"] == "Numbers"
    mem.close()


def test_reason_uses_hybrid_when_embedder_given(tmp_path):
    from cortana.backends import FakeLLMBackend
    from cortana.reasoning import reason
    emb = FakeEmbedder()
    mem = _seed(tmp_path, emb)
    # "budget report" keyword-only misses (no row has 'report'); hybrid finds it.
    ans = reason("budget report", mem, FakeLLMBackend(response="ok"), embedder=emb)
    assert ans.citations and ans.citations[0]["app_name"] == "Numbers"
    mem.close()


def test_pruned_rows_drop_their_embeddings(tmp_path):
    # ON DELETE CASCADE: removing a context row removes its vector too.
    emb = FakeEmbedder()
    mem = _seed(tmp_path, emb)
    mem.forget(older_than="2026-07-05T12:00:00+00:00")             # drops the 09:00 row
    n = mem._conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    assert n == 1
    mem.close()
