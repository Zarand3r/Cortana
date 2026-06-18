"""Step 8: the AgentLoop — perceive→remember on asyncio + executors.

Driven hermetically: a scripted fake sensor (no PyObjC), the fake backend (no LLM),
a real Memory on a temp DB, and a max_ticks bound for finite, deterministic runs.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from cortana.agent import AgentLoop
from cortana.backends import FakeLLMBackend
from cortana.config import Config
from cortana.memory import Memory
from cortana.perception import Observation, Semantic


@pytest.fixture
def make_memory(tmp_path):
    """Memory factory that closes every connection it hands out (no leaks)."""
    created = []

    def _make(**kw):
        m = Memory(tmp_path / "agent.db", check_same_thread=False, **kw)
        created.append(m)
        return m

    yield _make
    for m in created:
        m.close()


def _cfg(**kw):
    base = dict(interval=0.0, batch_size=1, batch_window=0.05, queue_max=256)
    base.update(kw)
    return Config(**base)


def _obs(ocr, *, app="Notes", title="Doc", ts="2026-06-15T10:00:00+00:00"):
    return Observation(ts=ts, app_name=app, bundle_id="com." + app, window_title=title,
                       ocr_text=ocr, captured=True)


class ScriptedSensor:
    """Returns the next scripted Observation per tick (ignores the loop's ts so the
    scripted timestamps stay deterministic); None once exhausted."""

    def __init__(self, observations):
        self._obs = list(observations)
        self._i = 0

    def __call__(self, ts):
        if self._i >= len(self._obs):
            return None
        o = self._obs[self._i]
        self._i += 1
        return o


class SlowBackend(FakeLLMBackend):
    def __init__(self, delay, response="summary"):
        super().__init__(response=response)
        self._delay = delay

    def generate(self, prompt):
        time.sleep(self._delay)
        return super().generate(prompt)


def _run(loop_obj, **kw):
    asyncio.run(loop_obj.run(install_signal_handlers=False, **kw))


# --- end-to-end spine ------------------------------------------------------- #

def test_perceive_remember_recall_end_to_end(make_memory):
    sensor = ScriptedSensor([
        _obs("writing the Q2 budget", app="Numbers", ts="2026-06-15T10:00:00+00:00"),
        _obs("reading personal email", app="Mail", ts="2026-06-15T10:30:00+00:00"),
    ])
    mem, be = make_memory(), FakeLLMBackend(response="working")
    _run(AgentLoop(_cfg(batch_size=4), mem, be, sensor), max_ticks=2)

    hits = mem.recall(query="budget")
    assert len(hits) == 1
    assert hits[0]["app_name"] == "Numbers"               # citation: where
    assert hits[0]["ts"] == "2026-06-15T10:00:00+00:00"   # citation: when
    assert mem.counts()["context"] == 2
    assert mem.counts()["summaries"] == be.calls          # one summary per batch


# --- P11: idle screens never call the LLM ----------------------------------- #

def test_idle_screens_skip_llm(make_memory):
    sensor = ScriptedSensor([_obs("same screen")] * 5 + [_obs("different screen")])
    mem, be = make_memory(), FakeLLMBackend()
    _run(AgentLoop(_cfg(batch_size=1), mem, be, sensor), max_ticks=6)

    assert be.calls == 2                                   # 2 distinct screens, not 6
    rows = mem.recall(limit=100)
    heartbeats = [r for r in rows if r["skip_reason"] == "unchanged"]
    assert len(heartbeats) == 4                            # the 4 repeats
    assert mem.counts()["context"] == 6


def test_changed_but_no_ocr_text_skips_llm(make_memory):
    # A changed screen (new app) with no readable text still gets recorded, no LLM.
    sensor = ScriptedSensor([_obs("", app="Finder")])
    mem, be = make_memory(), FakeLLMBackend()
    _run(AgentLoop(_cfg(batch_size=1), mem, be, sensor), max_ticks=1)
    assert be.calls == 0
    assert mem.counts()["context"] == 1
    assert mem.counts()["summaries"] == 0


def test_sensor_returning_none_is_skipped(make_memory):
    # A sensor may report "nothing this tick" (None); the loop skips it cleanly.
    sensor = ScriptedSensor([_obs("first"), None, _obs("third", app="Mail")])
    mem, be = make_memory(), FakeLLMBackend()
    _run(AgentLoop(_cfg(batch_size=1), mem, be, sensor), max_ticks=3)
    assert mem.counts()["context"] == 2      # the None tick produced nothing
    assert be.calls == 2


# --- _collect_batch unit behavior (deterministic, no loop race) ------------- #

def test_collect_batch_flushes_partial_on_window(make_memory):
    async def scenario():
        loop = asyncio.get_running_loop()
        q = asyncio.Queue()
        await q.put(_obs("a"))
        await q.put(_obs("b"))
        al = AgentLoop(_cfg(batch_size=10, batch_window=0.02), make_memory(),
                       FakeLLMBackend(), lambda ts: None)
        return await al._collect_batch(q, asyncio.Event(), loop)

    batch = asyncio.run(scenario())
    assert len(batch) == 2                   # took what was there, flushed on window


def test_collect_batch_empty_when_stopped_and_drained(make_memory):
    async def scenario():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        stop.set()
        al = AgentLoop(_cfg(batch_size=4), make_memory(), FakeLLMBackend(), lambda ts: None)
        return await al._collect_batch(asyncio.Queue(), stop, loop)

    assert asyncio.run(scenario()) == []     # stopped + empty -> no batch


# --- P10: backpressure is visible, never silent ----------------------------- #

def test_backpressure_is_visible(make_memory):
    sensor = ScriptedSensor([_obs(f"distinct screen {i}", ts=f"2026-06-15T10:{i:02d}:00+00:00")
                             for i in range(10)])
    mem = make_memory()
    loop_obj = AgentLoop(_cfg(batch_size=1, queue_max=1), mem, SlowBackend(0.02), sensor)
    _run(loop_obj, max_ticks=10)

    total = mem.counts()["context"]
    dropped = mem._conn.execute(
        "SELECT count(*) FROM context WHERE skip_reason='dropped_backpressure'").fetchone()[0]
    summarized = mem._conn.execute(
        "SELECT count(*) FROM context WHERE summary_id IS NOT NULL").fetchone()[0]
    assert loop_obj.drops_total > 0                         # the LLM fell behind
    assert dropped == loop_obj.drops_total
    assert summarized + dropped == 10                       # every frame accounted for
    assert total == 10                                      # nothing silently lost


# --- P9: a slow LLM does not stall capture ---------------------------------- #

def test_slow_llm_does_not_stall_capture(make_memory):
    sensor = ScriptedSensor([_obs(f"screen {i}", ts=f"2026-06-15T10:{i:02d}:00+00:00")
                             for i in range(8)])
    mem, be = make_memory(), SlowBackend(0.02)
    _run(AgentLoop(_cfg(batch_size=1, queue_max=256), mem, be, sensor), max_ticks=8)

    # big queue absorbed the burst; the slow consumer caught up during drain.
    assert mem.counts()["context"] == 8                    # all captures recorded
    assert be.calls == 8


# --- P12: graceful drain on stop -------------------------------------------- #

def test_drains_on_stop(make_memory):
    sensor = ScriptedSensor([_obs(f"s{i}", ts=f"2026-06-15T10:{i:02d}:00+00:00")
                             for i in range(5)])
    mem, be = make_memory(), FakeLLMBackend()
    # a real (tiny) interval exercises the cadence-sleep path
    _run(AgentLoop(_cfg(batch_size=2, interval=0.005), mem, be, sensor), max_ticks=5)
    assert mem.counts()["context"] == 5                    # nothing left unwritten


# --- janitor prunes at startup ---------------------------------------------- #

def test_janitor_prunes_on_start(make_memory):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    mem = make_memory(retention_days=90)
    mem.remember([_obs("ancient note", ts=old_ts)],
                 Semantic("old", "fake", old_ts, old_ts))
    sensor = ScriptedSensor([_obs("fresh note", app="Notes")])
    _run(AgentLoop(_cfg(), mem, FakeLLMBackend(), sensor), max_ticks=1)

    texts = [r["ocr_text"] for r in mem.recall(limit=100)]
    assert "ancient note" not in texts                     # pruned at startup
    assert "fresh note" in texts
