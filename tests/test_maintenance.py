"""Scheduled maintenance: consolidation now runs automatically (via the db executor,
serial with writes) so reflections accrue without a manual `cortana digest`. The
daemon cadence in `_maintenance` is pragma; here we cover the executor-backed step."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from cortana.agent import _AsyncMemory
from cortana.backends import FakeLLMBackend
from cortana.memory import Memory
from cortana.perception import Observation, Semantic


def test_async_memory_consolidate_creates_reflection(tmp_path):
    mem = Memory(tmp_path / "m.db", check_same_thread=False)
    o = Observation(ts="2026-07-19T09:00:00+00:00", app_name="Code", bundle_id="c",
                    window_title="agent.py", ocr_text="editing the agent loop", captured=True)
    mem.remember([o], Semantic(summary="Editing the agent loop.", model="fake",
                               window_start_ts=o.ts, window_end_ts=o.ts))

    async def run():
        loop = asyncio.get_running_loop()
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            amem = _AsyncMemory(mem, loop, ex)
            return await amem.consolidate(FakeLLMBackend(response="Worked on the agent loop."))
        finally:
            ex.shutdown()

    ref = asyncio.run(run())
    assert ref.basis_count == 1
    assert mem.recent_reflections()[0]["text"] == "Worked on the agent loop."
    mem.close()
