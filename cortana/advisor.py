"""Guidance advisor — Cortana's proactive side.

Reads the user's recent activity from memory and asks the local LLM for one
actionable recommendation grounded in it. Read-only: it advises, it does not act
(effector actions remain out of scope — see docs/DESIGN.md). Mirrors the thin-RAG
shape of `reasoning.py`; the retrieval here is "most recent", not query-driven.
"""

from __future__ import annotations

from dataclasses import dataclass

from cortana.backends import LLMBackend
from cortana.memory import Memory

_PER_MEMORY_CHARS = 300

ADVISOR_SYSTEM_PROMPT = (
    "You are Cortana, a private on-device assistant. Given a log of what the user "
    "has recently been doing (each entry has a time and app), suggest ONE specific, "
    "helpful next action or recommendation grounded in that activity. If there is "
    "not enough signal, say so plainly. Be concise; output only the recommendation."
)


@dataclass
class Recommendation:
    """A recommendation plus the memory rows it was grounded in."""

    text: str
    basis: list[dict]


def build_recommendation_prompt(memories: list[dict]) -> str:
    """Assemble recent activity into a single recommendation prompt."""
    parts = [ADVISOR_SYSTEM_PROMPT, "\n--- RECENT ACTIVITY ---"]
    if not memories:
        parts.append("(no recent activity)")
    for m in memories:
        body = (m.get("summary") or m.get("ocr_text") or "").strip().replace("\n", " ")
        parts.append(f"[{m['ts']}] app={m['app_name']!r}: {body[:_PER_MEMORY_CHARS]}")
    parts.append("\n--- RECOMMENDATION ---")
    return "\n".join(parts)


def recommend(memory: Memory, backend: LLMBackend, *, limit: int = 12) -> Recommendation:
    """Pull the most recent memories (long-term store) and ask for one suggestion."""
    memories = memory.recall(limit=limit)          # recent activity (no query)
    text = backend.generate(build_recommendation_prompt(memories)).strip()
    return Recommendation(text=text, basis=memories)


def recommend_from_observations(observations, backend: LLMBackend) -> Recommendation:
    """Recommend from short-term (working) memory — the live recent observations —
    so the suggestion reflects what the user is doing *right now*, no DB query."""
    memories = [{"ts": o.ts, "app_name": o.app_name,
                 "ocr_text": o.ocr_text, "summary": None} for o in observations]
    text = backend.generate(build_recommendation_prompt(memories)).strip()
    return Recommendation(text=text, basis=memories)
