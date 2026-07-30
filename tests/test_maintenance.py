"""Scheduled maintenance: consolidation runs automatically with each stage on the
right executor (recall/write on the db thread, LLM generate on the llm thread) and
on a WALL-CLOCK due-check (newest reflection older than 24h), not process uptime —
so reflections accrue even for an app that is quit daily. The 10-min daemon cadence
itself is pragma; everything below is the real logic, hermetic."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from cortana.agent import AgentLoop, _AsyncMemory
from cortana.backends import FakeLLMBackend
from cortana.config import Config
from cortana.memory import Memory
from cortana.perception import Observation, Semantic


def _obs(ts="2026-07-19T09:00:00+00:00"):
    return Observation(ts=ts, app_name="Code", bundle_id="c", window_title="agent.py",
                       ocr_text="editing the agent loop", captured=True)


def _seeded(tmp_path):
    mem = Memory(tmp_path / "m.db", check_same_thread=False)
    o = _obs()
    mem.remember([o], Semantic(summary="Editing the agent loop.", model="fake",
                               window_start_ts=o.ts, window_end_ts=o.ts))
    return mem


def _run(coro):
    async def wrapper():
        loop = asyncio.get_running_loop()
        db, llm = ThreadPoolExecutor(max_workers=1), ThreadPoolExecutor(max_workers=1)
        try:
            return await coro(loop, db, llm)
        finally:
            db.shutdown(); llm.shutdown()
    return asyncio.run(wrapper())


def test_consolidate_splits_generate_onto_llm_pool(tmp_path):
    # The generate must NOT run on the db executor: a multi-second LLM call there
    # would stall every write (incl. backpressure persistence), halting capture.
    mem = _seeded(tmp_path)
    seen_threads = {}

    class ThreadRecorder(FakeLLMBackend):
        def generate(self, prompt):
            import threading
            seen_threads["generate"] = threading.current_thread().name
            return "Worked on the agent loop."

    async def go(loop, db, llm):
        amem = _AsyncMemory(mem, loop, db)
        return await amem.consolidate(ThreadRecorder(), llm)

    text = _run(go)
    assert text == "Worked on the agent loop."
    assert mem.recent_reflections()[0]["text"] == "Worked on the agent loop."
    assert seen_threads["generate"].startswith("ThreadPoolExecutor")   # ran on llm pool,
    mem.close()                                                        # not inline


def test_consolidate_no_episodes_is_a_noop(tmp_path):
    mem = Memory(tmp_path / "empty.db", check_same_thread=False)

    async def go(loop, db, llm):
        amem = _AsyncMemory(mem, loop, db)
        return await amem.consolidate(FakeLLMBackend(), llm)

    assert _run(go) is None
    assert mem.recent_reflections() == []
    mem.close()


def test_consolidation_due_when_no_reflection_exists(tmp_path):
    mem = _seeded(tmp_path)
    al = AgentLoop(Config(), mem, FakeLLMBackend(response="first reflection"),
                   lambda ts: None)

    async def go(loop, db, llm):
        await al._consolidate_if_due(_AsyncMemory(mem, loop, db), llm)

    _run(go)
    assert mem.recent_reflections()[0]["text"] == "first reflection"
    mem.close()


def test_consolidation_skipped_when_fresh_reflection(tmp_path):
    mem = _seeded(tmp_path)
    mem.add_reflection("a", "b", "fresh")            # created_at = now -> not due
    be = FakeLLMBackend(response="should not run")
    al = AgentLoop(Config(), mem, be, lambda ts: None)

    async def go(loop, db, llm):
        await al._consolidate_if_due(_AsyncMemory(mem, loop, db), llm)

    _run(go)
    assert be.calls == 0                             # skipped: newest reflection < 24h old
    mem.close()


def test_consolidation_due_and_bounded_after_24h(tmp_path):
    # A stale reflection makes consolidation due, and the new pass digests only
    # episodes AFTER the last consolidated period (no re-digesting).
    mem = _seeded(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    mem._conn.execute(
        "INSERT INTO reflections (period_start, period_end, text, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("2026-07-19T00:00:00+00:00", "2026-07-19T08:00:00+00:00", "old", old))
    mem._conn.commit()

    class Capturing(FakeLLMBackend):
        def generate(self, prompt):
            self.prompt = prompt
            return "new reflection"

    be = Capturing()
    al = AgentLoop(Config(), mem, be, lambda ts: None)

    async def go(loop, db, llm):
        await al._consolidate_if_due(_AsyncMemory(mem, loop, db), llm)

    _run(go)
    newest = mem.recent_reflections(1)[0]
    assert newest["text"] == "new reflection"
    assert newest["period_start"] >= "2026-07-19T08:00:00+00:00"   # since-bounded
    mem.close()
