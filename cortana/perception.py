"""Perception & meaning-extraction tools — the agent's senses.

Pure logic (dataclasses, normalize/hash/changed, prompt building, extract_meaning)
is importable with no native deps. Native capture/OCR use lazy imports so this
module loads without PyObjC (P7). Filled in at Step 1 / Step 2.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from cortana.backends import LLMBackend


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


# --------------------------------------------------------------------------- #
# Change detection (P6): normalize away volatile churn, then hash.            #
# --------------------------------------------------------------------------- #
_WHITESPACE_RE = re.compile(r"\s+")
# Standalone clock/date tokens that change every tick on an otherwise idle
# screen (menu-bar clock, calendar headers). Stripping them stops idle screens
# from looking "changed" and needlessly driving the LLM.
_VOLATILE_RE = re.compile(
    r"\b("
    r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?"   # 12:34, 14:05:30, 1:05 pm
    r"|\d{4}-\d{2}-\d{2}"                       # 2026-06-14
    r"|\d{1,2}/\d{1,2}/\d{2,4}"                 # 06/14/2026
    r")\b",
    re.IGNORECASE,
)


def normalize(app_name: str, window_title: str, ocr_text: str) -> str:
    """Canonicalize a perception for change detection: lowercase, strip volatile
    clock/date tokens, collapse whitespace."""
    raw = f"{app_name}\n{window_title}\n{ocr_text}".lower()
    raw = _VOLATILE_RE.sub(" ", raw)
    return _WHITESPACE_RE.sub(" ", raw).strip()


def content_hash(app_name: str, window_title: str, ocr_text: str) -> str:
    """Stable SHA-256 of the normalized perception."""
    return hashlib.sha256(
        normalize(app_name, window_title, ocr_text).encode("utf-8")
    ).hexdigest()


def changed(prev_hash: str | None, new_hash: str) -> bool:
    """True when the screen content differs from the previous perception."""
    return prev_hash != new_hash


# --------------------------------------------------------------------------- #
# Meaning extraction (the LLM tool). Sync; the Phase-3 loop wraps in executor. #
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are a private, on-device context-tracking assistant. Given a short log "
    "of what the user was looking at (app, window title, and OCR'd on-screen "
    "text), write ONE highly condensed sentence describing what the user is "
    "actually doing and why. Be specific and factual. Do not invent details. "
    "Output only the sentence."
)


def build_meaning_prompt(batch: list[Observation], max_chars: int) -> str:
    """Render a batch of observations into one meaning-extraction prompt."""
    parts: list[str] = [SYSTEM_PROMPT, "\n--- ACTIVITY LOG ---"]
    for ev in batch:
        ocr = (ev.ocr_text or "").strip().replace("\r", " ")
        if len(ocr) > max_chars:
            ocr = ocr[:max_chars] + " …[truncated]"
        parts.append(
            f"\n[{ev.ts}] app={ev.app_name!r} window={ev.window_title!r}\n"
            f"on-screen text:\n{ocr or '(none)'}"
        )
        if ev.focused_text:
            parts.append(f"focused field: {ev.focused_text[:500]!r}")
    parts.append("\n--- END LOG ---\nOne-sentence summary:")
    return "\n".join(parts)


def extract_meaning(batch: list[Observation], backend: LLMBackend,
                    max_chars: int) -> Semantic:
    """Run the LLM over a batch and return its semantic record. ``backend`` is any
    `cortana.backends.LLMBackend` (sync ``generate(prompt) -> str`` + ``model``)."""
    prompt = build_meaning_prompt(batch, max_chars)
    summary = backend.generate(prompt).strip()
    return Semantic(
        summary=summary,
        model=backend.model,
        window_start_ts=batch[0].ts,
        window_end_ts=batch[-1].ts,
    )


# --------------------------------------------------------------------------- #
# Native sensors (macOS). Lazy imports keep this module importable without     #
# PyObjC (P7). Not unit-tested — exercised on a real Mac via the agent loop.   #
# --------------------------------------------------------------------------- #
def frontmost_app() -> tuple[str, str, int | None]:
    """(localized_name, bundle_id, pid) of the active application."""
    from AppKit import NSWorkspace  # lazy

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return ("unknown", "", None)
    return (
        app.localizedName() or "unknown",
        app.bundleIdentifier() or "",
        int(app.processIdentifier()),
    )


def capture_screen():
    """Capture the whole desktop as a CGImageRef, or None if blocked."""
    import Quartz  # lazy

    return Quartz.CGWindowListCreateImage(
        Quartz.CGRectInfinite,
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )


def ocr_image(cgimage, languages: tuple[str, ...] = ("en-US",)) -> str:
    """Run Apple Vision OCR on a CGImage; return recognized text."""
    import Vision  # lazy

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        cgimage, None
    )
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    if languages:
        request.setRecognitionLanguages_(list(languages))
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision request failed: {err}")
    lines: list[str] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if candidates and len(candidates) > 0:
            lines.append(candidates[0].string())
    return "\n".join(lines)
