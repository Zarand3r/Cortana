"""Reflection / consolidation — turn many raw episodes into a durable higher-level
insight (generative-agents style). A periodic pass summarizes recent activity into a
short 'reflection' stored as semantic memory, so recall surfaces conclusions
('worked on the agent loop all morning'), not just individual screens.
"""

from __future__ import annotations

from dataclasses import dataclass

from cortana.backends import LLMBackend
from cortana.memory import Memory

_PER_MEMORY_CHARS = 300

CONSOLIDATION_SYSTEM_PROMPT = (
    "You are Cortana, a private on-device assistant. Below is a log of the user's "
    "recent activity (each entry has a time and app). Write a brief, higher-level "
    "reflection (2-4 sentences): the main things they worked on, and any patterns or "
    "context worth remembering later. Be specific and factual; do not invent."
)


@dataclass
class Reflection:
    text: str
    period_start: str
    period_end: str
    basis_count: int


def build_digest_prompt(memories: list[dict]) -> str:
    """Assemble recent activity into a consolidation prompt."""
    parts = [CONSOLIDATION_SYSTEM_PROMPT, "\n--- RECENT ACTIVITY ---"]
    for m in memories:
        body = (m.get("summary") or m.get("ocr_text") or "").strip().replace("\n", " ")
        parts.append(f"[{m['ts']}] app={m['app_name']!r}: {body[:_PER_MEMORY_CHARS]}")
    parts.append("\n--- REFLECTION ---")
    return "\n".join(parts)


def consolidate(memory: Memory, backend: LLMBackend, *, since: str | None = None,
                until: str | None = None, limit: int = 200) -> Reflection:
    """Summarize recent episodes into a reflection and persist it. Returns the
    Reflection (``basis_count == 0`` and nothing stored when there's no activity)."""
    memories = memory.recall(since=since, until=until, limit=limit)
    if not memories:
        return Reflection(text="", period_start=since or "", period_end=until or "",
                          basis_count=0)
    # recall is newest-first, so period spans oldest..newest of the window
    period_start = memories[-1]["ts"]
    period_end = memories[0]["ts"]
    text = backend.generate(build_digest_prompt(memories)).strip()
    memory.add_reflection(period_start, period_end, text)
    return Reflection(text=text, period_start=period_start, period_end=period_end,
                      basis_count=len(memories))
