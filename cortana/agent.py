"""The agent loop — Cortana's continuous perceive → remember cycle.

A type-(a) perception loop on an ``asyncio`` event loop with dedicated single-worker
``ThreadPoolExecutor``s (capture / llm / db) so blocking native work never stalls
cadence. Design: docs/AGENT_LOOP.md. ``AgentLoop`` lands in Step 8; this module
starts with the pure routing decision (Step 7).
"""

from __future__ import annotations

from enum import Enum, auto

from cortana.perception import Observation, changed, content_hash


class Disposition(Enum):
    """What the producer should do with a freshly perceived observation."""

    CHANGED = auto()      # new content -> summarize + remember
    UNCHANGED = auto()    # same screen as last time -> heartbeat only, no LLM


def plan_disposition(prev_hash: str | None, obs: Observation) -> Disposition:
    """Assign ``obs.content_hash`` and decide whether the screen changed since the
    previous perception. Pure — no I/O, so the producer's branch is unit-tested
    without the event loop."""
    obs.content_hash = content_hash(obs.app_name, obs.window_title, obs.ocr_text)
    return Disposition.CHANGED if changed(prev_hash, obs.content_hash) else Disposition.UNCHANGED
