"""Retrieval evaluation harness (LongMemEval-style, local + hermetic).

Measures how often the right memory is retrieved for a question — the number to
watch when tuning retrieval (keyword vs. hybrid, RRF weights, a future reranker).
"Measure before optimizing" (docs/MEMORY.md roadmap).

Each case seeds a memory with a target row + distractors and asks a question; we
score **hit@k** (did the target's app appear in the top-k results). Run hermetically
with the FakeEmbedder (mechanics/regression) or point it at OllamaEmbedder for the
true semantic number:

    python -m benchmarks.retrieval_eval            # keyword-only
    python -m benchmarks.retrieval_eval --hybrid   # + FakeEmbedder
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from cortana.memory import Memory
from cortana.perception import Observation, Semantic
from cortana.reasoning import question_to_fts

_TS = "2026-07-05T09:{:02d}:00+00:00"


@dataclass
class Case:
    seed: list[tuple[str, str]]   # (app_name, ocr_text) — order = chronological
    question: str
    expect_app: str               # the app whose row should be retrieved


DATASET: list[Case] = [
    Case(seed=[("Numbers", "quarterly budget spreadsheet Q3 revenue"),
               ("Terminal", "python asyncio event loop traceback"),
               ("Mail", "lunch plans with the team on friday"),
               ("Safari", "morning news headlines")],
         question="what were the budget numbers", expect_app="Numbers"),
    Case(seed=[("Numbers", "quarterly budget spreadsheet"),
               ("Terminal", "python asyncio event loop traceback"),
               ("Mail", "lunch plans"), ("Safari", "news")],
         question="the asyncio traceback i was debugging", expect_app="Terminal"),
    Case(seed=[("Numbers", "budget"), ("Terminal", "python code"),
               ("Mail", "lunch plans with the team friday"), ("Safari", "news")],
         question="lunch plans", expect_app="Mail"),
    Case(seed=[("Xcode", "swift compiler error segmentation fault"),
               ("Numbers", "budget"), ("Mail", "email"), ("Notes", "todo list")],
         question="the swift compiler error", expect_app="Xcode"),
]


def _seed_memory(case: Case, path: Path, embedder) -> Memory:
    mem = Memory(path)
    for i, (app, ocr) in enumerate(case.seed):
        ts = _TS.format(i)
        mem.remember([Observation(ts, app, "c", "w", ocr, True)],
                     Semantic("s", "fake", ts, ts), embedder=embedder)
    return mem


def _retrieve(mem: Memory, question: str, embedder, k: int) -> list[dict]:
    fts = question_to_fts(question)
    if embedder is not None and fts:
        return mem.recall(query=question, embedder=embedder, limit=k)
    return mem.recall(query=fts, limit=k)


def hit_at_k(results: list[dict], expect_app: str, k: int) -> bool:
    return any(r["app_name"] == expect_app for r in results[:k])


def evaluate(cases: list[Case] = DATASET, *, embedder=None, k: int = 3) -> dict:
    """Return {cases, hits, hit_at_k, k}. Each case runs in its own temp DB."""
    hits = 0
    with tempfile.TemporaryDirectory() as d:
        for i, case in enumerate(cases):
            mem = _seed_memory(case, Path(d) / f"c{i}.db", embedder)
            try:
                results = _retrieve(mem, case.question, embedder, k)
                if hit_at_k(results, case.expect_app, k):
                    hits += 1
            finally:
                mem.close()
    n = len(cases)
    return {"cases": n, "hits": hits, "hit_at_k": hits / n if n else 0.0, "k": k}


def main() -> None:  # pragma: no cover - CLI convenience
    import sys
    from cortana.embeddings import FakeEmbedder
    embedder = FakeEmbedder() if "--hybrid" in sys.argv else None
    result = evaluate(embedder=embedder)
    mode = "hybrid" if embedder else "keyword"
    print(f"retrieval eval ({mode}): hit@{result['k']} = "
          f"{result['hit_at_k']:.0%} ({result['hits']}/{result['cases']})")


if __name__ == "__main__":  # pragma: no cover
    main()
