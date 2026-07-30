"""Regression tests for the architecture/memory review (REVIEW.md): privacy
exclusions enforced, reflections read by retrieval, embedding safety, backlog
draining, live chat context, and consolidation ordering."""

import asyncio
import json

import pytest

from cortana.backends import FakeLLMBackend
from cortana.embeddings import FakeEmbedder, cosine
from cortana.memory import Memory
from cortana.perception import Observation, Semantic


def _obs(ocr, *, ts="2026-07-07T09:00:00+00:00", app="Notes"):
    return Observation(ts=ts, app_name=app, bundle_id="c", window_title="w",
                       ocr_text=ocr, captured=True)


def _sem(summary="s"):
    return Semantic(summary=summary, model="fake",
                    window_start_ts="2026-07-07T09:00:00+00:00",
                    window_end_ts="2026-07-07T09:00:00+00:00")


# --- P0: excluded_bundles enforced ------------------------------------------

def test_screen_sensor_skips_excluded_bundles(monkeypatch):
    from cortana import perception
    sensor = perception.ScreenSensor(("en-US",),
                                     excluded_bundles=frozenset({"com.1password.1password"}))
    monkeypatch.setattr(perception, "frontmost_app",
                        lambda: ("1Password", "com.1password.1password", 1))
    obs = sensor("2026-07-07T09:00:00+00:00")
    assert obs.captured is False
    assert obs.skip_reason == "excluded_app"
    assert obs.ocr_text == ""                       # nothing captured, nothing stored


# --- P1: cosine dimension guard ----------------------------------------------

def test_cosine_raises_on_dimension_mismatch():
    with pytest.raises(ValueError):
        cosine([1.0, 2.0], [1.0, 2.0, 3.0])        # zip would silently truncate


# --- P1: reflections are read by ask/reason ----------------------------------

def test_reason_includes_reflections_in_prompt(tmp_path):
    from cortana.reasoning import reason

    class Capturing(FakeLLMBackend):
        def generate(self, prompt):
            self.prompt = prompt
            return "ok"

    mem = Memory(tmp_path / "m.db")
    mem.remember([_obs("editing code")], _sem())
    mem.add_reflection("2026-07-06T00:00:00+00:00", "2026-07-06T23:00:00+00:00",
                       "Spent yesterday building the memory system.")
    be = Capturing()
    reason("what code was I editing", mem, be)
    assert "REFLECTIONS" in be.prompt
    assert "building the memory system" in be.prompt
    mem.close()


def test_prune_bounds_reflections(tmp_path):
    mem = Memory(tmp_path / "m.db", retention_days=90)
    # backdate a reflection past retention by writing created_at directly
    mem._conn.execute("INSERT INTO reflections (period_start, period_end, text, created_at) "
                      "VALUES ('a','b','old one','2020-01-01T00:00:00+00:00')")
    mem._conn.commit()
    mem.prune()
    assert mem.recent_reflections() == []           # every tier has a cap
    mem.close()


# --- P1: backlog draining (capacity math) ------------------------------------

def test_collect_batch_drains_backlog_beyond_batch_size(tmp_path):
    from cortana.agent import AgentLoop
    from cortana.config import Config
    cfg = Config(interval=0.0, batch_size=2, batch_window=0.05)
    mem = Memory(tmp_path / "m.db", check_same_thread=False)

    async def run():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        for i in range(7):                          # backlog > batch_size
            queue.put_nowait(_obs(f"s{i}"))
        al = AgentLoop(cfg, mem, FakeLLMBackend(), lambda ts: None)
        stop = asyncio.Event()
        return await al._collect_batch(queue, stop, loop)

    batch = asyncio.run(run())
    assert len(batch) == 7                          # drained up to 4x batch_size (8)
    mem.close()


# --- P1: chat sees the live working-memory view -------------------------------

def test_chat_route_includes_live_working_memory(tmp_path):
    from cortana.backends import Message, Role
    from cortana.chatapp import route
    from cortana.working_memory import WorkingMemory

    class Recorder(FakeLLMBackend):
        def chat(self, messages):
            self.msgs = list(messages)
            return ["ok"]

    wm = WorkingMemory()
    wm.add(_obs("live: editing REVIEW.md right now", app="VSCode"))
    rec = Recorder()
    body = json.dumps({"messages": [{"role": "user", "content": "what am i doing"}]}).encode()
    resp = route("POST", "/api/chat", body, rec, system_prompt="sp", index_html="",
                 memory=None, working_memory=wm)
    assert resp.status == 200
    sys_msg = [m for m in rec.msgs if m.role is Role.SYSTEM][0]
    assert "LIVE" in sys_msg.content and "REVIEW.md" in sys_msg.content


# --- P2/P3: consolidation feeds a chronological, deduped log -------------------

def test_consolidation_prompt_chronological_and_deduped(tmp_path):
    from cortana.consolidation import consolidate

    class Capturing(FakeLLMBackend):
        def generate(self, prompt):
            self.prompt = prompt
            return "reflection"

    mem = Memory(tmp_path / "m.db")
    # one batch of two events sharing a summary, then a later distinct one
    mem.remember([_obs("a", ts="2026-07-07T09:00:00+00:00"),
                  _obs("b", ts="2026-07-07T09:01:00+00:00")], _sem("batch summary"))
    mem.remember([_obs("c", ts="2026-07-07T10:00:00+00:00")], _sem("later summary"))
    be = Capturing()
    consolidate(mem, be)
    assert be.prompt.count("batch summary") == 1                 # deduped
    assert be.prompt.index("batch summary") < be.prompt.index("later summary")  # chronological
    mem.close()


# --- Review pass 3 -----------------------------------------------------------

# P0: one bad-dimension embedding must not kill the whole hybrid path.
def test_hybrid_recall_survives_bad_dimension_vector(tmp_path):
    emb = FakeEmbedder()                                     # dim 128
    mem = Memory(tmp_path / "m.db")
    mem.remember([_obs("quarterly budget spreadsheet", app="Numbers")], _sem(), embedder=emb)
    mem.remember([_obs("python asyncio event loop", app="Terminal")], _sem(), embedder=emb)
    # Corrupt the Terminal row's vector to a different dimension — as if embed_model
    # changed, or Ollama returned a short/empty vector. cosine() will raise on it.
    mem._conn.execute("UPDATE embeddings SET vec = ? WHERE context_id = "
                      "(SELECT id FROM context WHERE app_name='Terminal')",
                      (json.dumps([1.0, 2.0, 3.0]),))
    mem._conn.commit()
    # "budget report": keyword-only misses (implicit AND, no row has 'report'); only
    # the semantic branch can surface the budget row. If the bad vector aborted the
    # scan, recall would fall back to keyword-only and return nothing.
    hits = mem.recall(query="budget report", embedder=emb)
    assert hits and hits[0]["app_name"] == "Numbers"        # hybrid still works
    mem.close()


# P1: a long reflection must be capped in the answer prompt, like memories are.
def test_reflection_text_capped_in_prompt():
    from cortana.reasoning import build_answer_prompt, _PER_MEMORY_CHARS
    long_text = "x" * (_PER_MEMORY_CHARS + 500)
    prompt = build_answer_prompt(
        "q", memories=[],
        reflections=[{"period_start": "a", "period_end": "b", "text": long_text}])
    assert ("x" * _PER_MEMORY_CHARS) in prompt
    assert ("x" * (_PER_MEMORY_CHARS + 1)) not in prompt     # not the full untruncated blob


# Review pass 4: make_loop shares an injected backend (one resident MLX model, not
# two) and embed=true without Ollama is loudly disabled, not silently broken.
def test_make_loop_uses_injected_backend(tmp_path):
    from cortana.cli import make_loop
    from cortana.config import Config
    cfg = Config(); cfg.backend = "fake"; cfg.db_path = tmp_path / "m.db"
    shared = FakeLLMBackend()
    loop_obj, mem = make_loop(cfg, backend=shared)
    try:
        assert loop_obj._backend is shared           # no second backend constructed
    finally:
        mem.close()


def test_make_loop_disables_embeddings_without_ollama(tmp_path, caplog):
    from cortana.cli import make_loop
    from cortana.config import Config
    cfg = Config(); cfg.backend = "fake"; cfg.embed = True; cfg.db_path = tmp_path / "m.db"
    with caplog.at_level("WARNING"):
        loop_obj, mem = make_loop(cfg)
    try:
        assert loop_obj._embedder is None            # not a broken Ollama embedder
        assert any("DISABLED" in r.message for r in caplog.records)   # and it says so
    finally:
        mem.close()


# P1: close() serializes against in-flight recall on the shared read connection.
def test_close_waits_for_in_flight_recall(tmp_path):
    import threading
    mem = Memory(tmp_path / "m.db", check_same_thread=False)
    mem.remember([_obs("some text")], _sem())
    mem._lock.acquire()                                      # simulate a recall holding the lock
    closed = threading.Event()

    def do_close():
        mem.close()
        closed.set()

    t = threading.Thread(target=do_close, daemon=True)
    t.start()
    assert not closed.wait(0.2)                              # close blocks while lock is held
    mem._lock.release()
    assert closed.wait(1.0)                                  # proceeds once the lock frees
    t.join()
