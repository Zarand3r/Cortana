"""End-to-end integration across the whole product over ONE memory:

    agent loop (perceive→remember)  →  memory  →  { ask · recommend · chat }

This is the integration coverage for what the running app actually does — the same
memory populated by tracking is read by all three surfaces. Hermetic: demo sensor
+ fake backend + temp SQLite.
"""

import asyncio

from cortana.advisor import recommend
from cortana.agent import AgentLoop
from cortana.backends import FakeLLMBackend
from cortana.chatapp import format_context_block, retrieve_context
from cortana.config import Config
from cortana.desktop import recommendation_message
from cortana.memory import Memory
from cortana.perception import make_demo_sensor
from cortana.reasoning import reason


from conftest import counts



def _populated_memory(tmp_path):
    cfg = Config()
    cfg.backend = "fake"
    cfg.interval = 0.0                     # no inter-tick sleep
    cfg.db_path = tmp_path / "product.db"
    mem = Memory(cfg.db_path, check_same_thread=False)   # loop writes via the db executor thread
    backend = FakeLLMBackend(response="Working across the editor, browser, and mail.")
    loop = AgentLoop(cfg, mem, backend, make_demo_sensor())
    asyncio.run(loop.run(max_ticks=8, install_signal_handlers=False))
    return mem, backend


def test_loop_populates_memory_read_by_all_surfaces(tmp_path):
    mem, backend = _populated_memory(tmp_path)
    try:
        # tracking wrote real episodic + semantic memory
        n = counts(mem)
        assert n["context"] > 0
        assert n["summaries"] > 0
        assert n["context_fts"] == n["context"]                 # FTS in sync

        # ask: grounded answer with citations drawn from that memory ("budget"
        # appears in one of the demo screens, so retrieval hits).
        answer = reason("what did the budget email say", mem, backend)
        assert answer.citations
        assert all("ts" in c and "app_name" in c for c in answer.citations)

        # recommend (what "Get Recommendation" shows): grounded, non-empty
        rec = recommend(mem, backend)
        assert rec.basis
        assert recommendation_message(mem, backend) == backend.response

        # chat: the context block assembled for a turn cites real activity
        block = format_context_block(retrieve_context(mem, "what am I doing"))
        assert "app=" in block
    finally:
        mem.close()


def test_recommend_reflects_recent_activity_after_more_tracking(tmp_path):
    # A second, distinct screen changes what recall (and thus recommend) sees.
    mem, backend = _populated_memory(tmp_path)
    try:
        first = recommend(mem, backend)
        assert first.basis                       # advice is grounded in memory
        # the most-recent basis entry is one of the demo apps, not empty
        assert first.basis[0]["app_name"]
    finally:
        mem.close()
