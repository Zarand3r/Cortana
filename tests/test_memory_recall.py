"""Step 4: Memory recall via FTS + filters (P5), and FTS-sync-on-delete (P2)."""

from cortana.memory import Memory
from cortana.perception import Observation, Semantic


from conftest import counts



def _obs(ocr, *, ts, app="Notes", title="Doc"):
    return Observation(ts=ts, app_name=app, bundle_id="com.app", window_title=title,
                       ocr_text=ocr, captured=True)


def _sem():
    return Semantic(summary="s", model="fake",
                    window_start_ts="2026-06-14T00:00:00+00:00",
                    window_end_ts="2026-06-14T00:00:00+00:00")


def _seed(tmp_path):
    mem = Memory(tmp_path / "m.db")
    mem.remember([_obs("quarterly budget spreadsheet",
                       ts="2026-06-14T09:00:00+00:00", app="Numbers")], _sem())
    mem.remember([_obs("reading personal email",
                       ts="2026-06-14T12:00:00+00:00", app="Mail")], _sem())
    mem.remember([_obs("python traceback debugging",
                       ts="2026-06-14T15:00:00+00:00", app="Terminal")], _sem())
    return mem


def test_recall_by_keyword(tmp_path):
    mem = _seed(tmp_path)
    hits = mem.recall(query="budget")
    assert len(hits) == 1
    assert hits[0]["app_name"] == "Numbers"
    assert mem.recall(query="nonexistentterm") == []
    mem.close()


def test_recall_returns_citation(tmp_path):
    mem = _seed(tmp_path)
    hit = mem.recall(query="traceback")[0]
    assert hit["ts"] == "2026-06-14T15:00:00+00:00"   # when
    assert hit["app_name"] == "Terminal"               # where  (citation)
    assert "ocr_text" in hit
    mem.close()


def test_recall_time_filter(tmp_path):
    mem = _seed(tmp_path)
    hits = mem.recall(since="2026-06-14T11:00:00+00:00",
                      until="2026-06-14T13:00:00+00:00")
    assert {h["app_name"] for h in hits} == {"Mail"}
    mem.close()


def test_recall_app_filter(tmp_path):
    mem = _seed(tmp_path)
    hits = mem.recall(app="Terminal")
    assert len(hits) == 1 and hits[0]["app_name"] == "Terminal"
    mem.close()


def test_recall_includes_summary_text(tmp_path):
    mem = _seed(tmp_path)
    hit = mem.recall(query="budget")[0]
    assert hit["summary"] == "s"           # joined semantic text, not just summary_id
    mem.close()


def test_recall_orders_newest_first(tmp_path):
    mem = _seed(tmp_path)
    hits = mem.recall()
    ts = [h["ts"] for h in hits]
    assert ts == sorted(ts, reverse=True)
    mem.close()


def test_recall_excludes_unchanged_heartbeats(tmp_path):
    # Regression: dwelling on a screen writes 'unchanged' heartbeats (empty OCR).
    # They must NOT dominate recall, or repeated questions get the same blank/stale
    # rows instead of the real content.
    from cortana.perception import Observation
    mem = Memory(tmp_path / "m.db")
    mem.remember([_obs("real content here", ts="2026-07-04T09:00:00+00:00", app="Notes")],
                 _sem())
    # later heartbeats are newest but carry no content
    for i in range(3):
        hb = Observation(ts=f"2026-07-04T10:0{i}:00+00:00", app_name="Notes",
                         bundle_id="c", window_title="w", ocr_text="",
                         captured=True, skip_reason="unchanged")
        mem.remember([hb], None)               # a dwell heartbeat, as the loop writes it
    recent = mem.recall()                      # no query -> the vague-question path
    assert recent, "recall should surface real content, not be empty"
    assert all(r["skip_reason"] != "unchanged" for r in recent)
    assert recent[0]["ocr_text"] == "real content here"
    mem.close()


def test_fts_sync_on_delete(tmp_path):
    mem = _seed(tmp_path)
    mem._conn.execute("DELETE FROM context WHERE app_name='Mail'")
    mem._conn.commit()
    c = counts(mem)
    assert c["context_fts"] == c["context"]   # P2: index stays in lockstep
    assert mem.recall(query="email") == []     # deleted row no longer searchable
    mem.close()
