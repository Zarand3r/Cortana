"""Recall & reasoning — Cortana's "act" side.

A thin RAG over memory: retrieve the relevant entries, hand them to the local LLM,
return a grounded answer plus the source rows as citations. Deliberately *not* a
multi-step tool-using agent (see docs/DESIGN.md Phase 4 scope guard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cortana.backends import LLMBackend
from cortana.memory import Memory

# Dropped from full-text queries: function words that carry no retrieval signal.
_STOPWORDS = frozenset({
    "what", "was", "were", "the", "a", "an", "and", "or", "did", "do", "does",
    "i", "me", "my", "is", "are", "am", "on", "in", "at", "to", "of", "for",
    "this", "that", "when", "how", "why", "with", "you", "it", "be", "been",
    "doing", "going", "get", "got", "work", "working",   # generic verbs -> fall back to recent
    # temporal / present-tense fillers: these describe *when* (now), not *what*, so
    # they must not become search terms — else "what am I doing right now" matches
    # old screens that merely contain the words "now"/"right".
    "now", "right", "currently", "current", "today", "recently", "lately", "just",
    "moment", "presently", "here", "up",
})

_PER_MEMORY_CHARS = 400   # cap each memory's text in the prompt to bound size

ANSWER_SYSTEM_PROMPT = (
    "You are Cortana, a private on-device assistant. Answer the user's question "
    "using ONLY the retrieved memory entries below, each of which has a timestamp "
    "and app. Cite the time and app you relied on. If the memories do not contain "
    "the answer, say you have no record of it. Be concise and factual; do not invent."
)


@dataclass
class Answer:
    """A grounded answer plus the memory rows it was drawn from (citations)."""

    text: str
    citations: list[dict]


def question_to_fts(question: str) -> str | None:
    """Turn a natural-language question into an FTS5 OR-query of its content words,
    or None when nothing useful remains (caller then falls back to filters/recent)."""
    terms = [w for w in re.findall(r"[a-z0-9]+", question.lower())
             if len(w) > 2 and w not in _STOPWORDS]
    return " OR ".join(terms) or None


def build_answer_prompt(question: str, memories: list[dict],
                        reflections: list[dict] | None = None) -> str:
    """Assemble retrieved memories (+ any consolidated reflections) and the question
    into a single answering prompt."""
    parts = [ANSWER_SYSTEM_PROMPT]
    if reflections:
        parts.append("\n--- REFLECTIONS (consolidated summaries of past periods) ---")
        for r in reflections:
            parts.append(f"[{r['period_start']} … {r['period_end']}] "
                         f"{r['text'][:_PER_MEMORY_CHARS]}")
    parts.append("\n--- RETRIEVED MEMORIES ---")
    if not memories:
        parts.append("(none)")
    for m in memories:
        body = (m.get("summary") or m.get("ocr_text") or "").strip().replace("\n", " ")
        parts.append(f"[{m['ts']}] app={m['app_name']!r}: {body[:_PER_MEMORY_CHARS]}")
    parts.append(f"\n--- QUESTION ---\n{question}\n\nAnswer:")
    return "\n".join(parts)


def reason(question: str, memory: Memory, backend: LLMBackend, *,
           since: str | None = None, until: str | None = None,
           app: str | None = None, limit: int = 20, embedder=None) -> Answer:
    """Retrieve relevant memories, reason over them with the LLM, return a grounded
    Answer. Retrieval is hybrid (keyword + semantic) when an ``embedder`` is given,
    else full-text on the question's content words; narrowed by any time/app filters."""
    fts = question_to_fts(question)
    if embedder is not None and fts:
        # has content terms -> semantic hybrid over the raw question
        memories = memory.recall(query=question, since=since, until=until,
                                 app=app, limit=limit, embedder=embedder)
    else:
        # vague / present-tense ("what am I doing") -> most recent activity, not a
        # relevance ranking that could surface stale-but-similar memories. `fts` is
        # None for all-stopword questions, so recall falls back to recency.
        memories = memory.recall(query=fts, since=since, until=until,
                                 app=app, limit=limit)
    reflections = memory.recent_reflections(limit=3)   # consolidated knowledge, if any
    text = backend.generate(
        build_answer_prompt(question, memories, reflections)).strip()
    return Answer(text=text, citations=memories)
