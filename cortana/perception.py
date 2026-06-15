"""Perception & meaning-extraction tools — the agent's senses.

Pure logic (dataclasses, normalize/hash/changed, prompt building, extract_meaning)
is importable with no native deps. Native capture/OCR use lazy imports so this
module loads without PyObjC (P7). Filled in at Step 1 / Step 2.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Observation:
    """One perception of the screen at a moment in time (episodic memory unit)."""

    ts: str                       # ISO-8601 UTC timestamp
    app_name: str
    bundle_id: str
    window_title: str
    ocr_text: str
    captured: bool                # did we actually get a screenshot?
    skip_reason: str = ""         # '', 'unchanged', 'dropped_backpressure', 'ocr_error', …
    focused_text: str = ""        # optional Accessibility value of focused field
    content_hash: str = ""        # normalized-content hash for change detection


@dataclass(slots=True)
class Semantic:
    """The meaning extracted from a batch of observations (semantic memory unit)."""

    summary: str
    model: str
    window_start_ts: str
    window_end_ts: str
