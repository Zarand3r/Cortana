"""Shared test fixtures."""

import sys
from pathlib import Path

# Make the repo root importable so `import cortana...` works when pytest is run
# from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def counts(mem) -> dict[str, int]:
    """Row counts of the memory tables — the suite's cheap invariant probe.
    (Lived on Memory as product code, but was only ever a testing seam — moved
    here in the dead-code prune, REVIEW.md §3.)"""
    def n(sql: str) -> int:
        return mem._conn.execute(sql).fetchone()[0]
    return {
        "context": n("SELECT count(*) FROM context"),
        "context_fts": n("SELECT count(*) FROM context_fts"),
        "summaries": n("SELECT count(*) FROM summaries"),
    }
