"""Perception & meaning-extraction tools — the agent's senses.

Pure logic (dataclasses, normalize/hash/changed, prompt building, extract_meaning)
is importable with no native deps. Native capture/OCR use lazy imports so this
module loads without PyObjC (P7).
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
# Pre-OCR image dedup (P: cheap 1s capture). A perceptual image hash (dHash)   #
# lets us SKIP the expensive OCR pass when the screen is visually ~unchanged.  #
# The hashing is native (below); the similarity decision is pure + tested.     #
# --------------------------------------------------------------------------- #
def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two integer hashes."""
    return (a ^ b).bit_count()


def images_similar(a: int, b: int, threshold: int = 4) -> bool:
    """True if two dHashes are within ``threshold`` bits — i.e. the screen is
    visually ~unchanged (tolerates the cursor blink / menu-bar clock ticking).
    A larger real change flips many bits and reads as changed, so OCR still runs."""
    return hamming_distance(a, b) <= threshold


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
# Synthetic sensor — for deps-free local runs / demos (no PyObjC, no model).   #
# --------------------------------------------------------------------------- #
_DEMO_SCREENS: list[tuple[str, str, str]] = [
    ("Visual Studio Code", "agent.py — Cortana",
     "class AgentLoop: async def run(self): perceive -> remember loop"),
    ("Google Chrome", "SQLite FTS5 — full-text search",
     "external content tables keep the index in sync via triggers; MATCH queries"),
    ("Mail", "Inbox — 2 unread",
     "Subject: Q3 budget review  From: finance@company.com  Please review by Friday"),
    ("Terminal", "cortana — zsh",
     "$ python -m cortana ask 'what was I doing'  145 passed"),
]


def make_demo_sensor(scripts: list[tuple[str, str, str]] | None = None):
    """Return a sensor ``(ts) -> Observation`` that cycles through scripted
    (app, window_title, ocr_text) screens. Lets the full loop run with no PyObjC
    and no model (``cortana run --demo --backend fake``) for local testing."""
    screens = scripts or _DEMO_SCREENS
    state = {"i": 0}

    def sensor(ts: str) -> Observation:
        app, title, ocr = screens[state["i"] % len(screens)]
        state["i"] += 1
        return Observation(ts=ts, app_name=app,
                           bundle_id=f"com.demo.{app.split()[0].lower()}",
                           window_title=title, ocr_text=ocr, captured=True)

    return sensor


# --------------------------------------------------------------------------- #
# Native sensors (macOS). Lazy imports keep this module importable without     #
# PyObjC (P7). Not unit-tested — exercised on a real Mac via the agent loop.   #
# --------------------------------------------------------------------------- #
def frontmost_app() -> tuple[str, str, int | None]:  # pragma: no cover - native macOS
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


def capture_screen():  # pragma: no cover - native macOS (Quartz)
    """Capture the whole desktop as a CGImageRef, or None if blocked."""
    import Quartz  # lazy

    return Quartz.CGWindowListCreateImage(
        Quartz.CGRectInfinite,
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )


def perceive(ts: str, languages: tuple[str, ...] = ("en-US",)) -> Observation:  # pragma: no cover - native macOS
    """Compose the native sensors into one Observation: frontmost app/title +
    screenshot + OCR. The agent loop injects a fake of this in tests, so the live
    path stays here behind the native-only marker. Fails soft: a blocked capture
    yields a metadata-only, uncaptured Observation."""
    app_name, bundle_id, _pid = frontmost_app()
    image = capture_screen()
    if image is None:
        return Observation(ts, app_name, bundle_id, "", "", captured=False,
                           skip_reason="capture_blocked_or_no_permission")
    try:
        text = ocr_image(image, languages)
    except Exception as exc:  # noqa: BLE001 - OCR failure is non-fatal
        return Observation(ts, app_name, bundle_id, "", "", captured=False,
                           skip_reason=f"ocr_error: {exc}")
    return Observation(ts, app_name, bundle_id, "", text, captured=True)


def image_dhash(cgimage, size: int = 8) -> int:  # pragma: no cover - native macOS (Quartz)
    """Perceptual difference-hash of a CGImage: downscale to (size+1)×size grayscale
    and emit one bit per adjacent-pixel comparison. Robust to tiny changes (cursor,
    clock); distinct screens differ in many bits. NOTE: native bitmap read — verify
    on a real Mac; the sensor falls back to OCR if this raises."""
    import Quartz  # lazy

    w, h = size + 1, size
    gray = Quartz.CGColorSpaceCreateDeviceGray()
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, w, gray, Quartz.kCGImageAlphaNone)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cgimage)
    px = memoryview(Quartz.CGBitmapContextGetData(ctx).as_buffer(w * h))
    bits = 0
    for row in range(h):
        base = row * w
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


class ScreenSensor:  # pragma: no cover - native macOS (capture/OCR)
    """The live sensor with pre-OCR image dedup. Each call captures the screen and
    computes a cheap perceptual hash; if it's ~unchanged from the last frame it
    returns an 'unchanged' Observation WITHOUT running OCR — the expensive step —
    making 1s capture cheap on battery during idle/static periods. Any hashing error
    falls back to OCR (fail-safe: never skip a real change)."""

    def __init__(self, languages: tuple[str, ...] = ("en-US",),
                 similarity_threshold: int = 4) -> None:
        self._languages = languages
        self._threshold = similarity_threshold
        self._last_dhash: int | None = None

    def __call__(self, ts: str) -> Observation:
        app_name, bundle_id, _pid = frontmost_app()
        image = capture_screen()
        if image is None:
            return Observation(ts, app_name, bundle_id, "", "", captured=False,
                               skip_reason="capture_blocked_or_no_permission")
        try:
            dh = image_dhash(image)
        except Exception:  # noqa: BLE001 - hashing must never break capture
            dh = None
        if (dh is not None and self._last_dhash is not None
                and images_similar(dh, self._last_dhash, self._threshold)):
            return Observation(ts, app_name, bundle_id, "", "", captured=True,
                               skip_reason="unchanged", content_hash=hex(dh))
        try:
            text = ocr_image(image, self._languages)
        except Exception as exc:  # noqa: BLE001 - OCR failure is non-fatal
            return Observation(ts, app_name, bundle_id, "", "", captured=False,
                               skip_reason=f"ocr_error: {exc}")
        if dh is not None:
            self._last_dhash = dh
        return Observation(ts, app_name, bundle_id, "", text, captured=True)


def ocr_image(cgimage, languages: tuple[str, ...] = ("en-US",)) -> str:  # pragma: no cover - native macOS (Vision)
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
