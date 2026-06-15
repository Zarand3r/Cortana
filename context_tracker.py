#!/usr/bin/env python3
"""
context_tracker.py
==================

A completely local, private macOS "context tracker".

Every N seconds it:
  1. Reads the frontmost application + window title  (Quartz / AppKit)
  2. Grabs a full-screen screenshot                  (Quartz CGWindowListCreateImage)
  3. Runs on-device OCR on that image                (Apple Vision via PyObjC)
  4. Hands the text + metadata to a local LLM         (Ollama HTTP *or* native mlx-lm)
  5. Stores a highly condensed context summary        (sqlite3 -> ~/.local_mac_context.db)

Design goals
------------
* **Nothing leaves the machine.** OCR is the native Vision framework, the LLM is
  local (Ollama or MLX). The only network traffic is to 127.0.0.1:11434 (Ollama).
* **The capture loop never blocks on slow work.** Screenshot+OCR run in a worker
  thread; LLM summarisation runs in a *separate* consumer coroutine fed by an
  asyncio.Queue, so a slow model can never stutter the capture cadence.
* **It fails soft.** Secure input fields (passwords), screen-recording-permission
  gaps, DRM-protected windows and OCR errors are logged and skipped — never fatal.

Privacy / safety note
----------------------
This tool captures *your own* screen on *your own* machine and writes only to a
local SQLite file. It deliberately does **not** install a global keystroke hook.
The optional "focused text" feature (``--read-focused-text``) uses the public
Accessibility API, which macOS will not allow until you explicitly grant this
process Accessibility permission in System Settings. Treat the resulting database
as sensitive — it is a searchable log of everything you looked at.

Requires: macOS 13+, Python 3.11+, PyObjC (see README.md for the exact installs).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import json
import logging
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# PyObjC framework imports (fail loudly with install hint if missing)          #
# --------------------------------------------------------------------------- #
try:
    import Quartz                       # CoreGraphics: screen capture + window list
    import Vision                       # On-device OCR (VNRecognizeTextRequest)
    from AppKit import NSWorkspace      # Frontmost application metadata
except ImportError as exc:              # pragma: no cover - environment guard
    sys.stderr.write(
        f"\n[fatal] Missing a native dependency ({exc}).\n"
        "Install the PyObjC frameworks first:\n"
        "    pip install pyobjc-core pyobjc-framework-Quartz "
        "pyobjc-framework-Vision pyobjc-framework-Cocoa\n\n"
    )
    raise SystemExit(1)

log = logging.getLogger("context_tracker")


# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    interval: float = 30.0                       # seconds between captures
    db_path: Path = Path.home() / ".local_mac_context.db"
    backend: str = "ollama"                      # "ollama" | "mlx"
    model: str = "qwen2.5:72b-instruct-q6_K"     # ollama tag OR mlx HF repo
    ollama_host: str = "http://127.0.0.1:11434"
    ocr_languages: tuple[str, ...] = ("en-US",)
    ocr_max_chars: int = 6000                    # truncate OCR before the LLM
    batch_size: int = 4                          # events per LLM summarisation call
    batch_window: float = 60.0                   # ...or flush after this many seconds
    queue_max: int = 256                         # backpressure bound on the queue
    excluded_bundles: frozenset[str] = field(default_factory=lambda: frozenset({
        "com.apple.keychainaccess",
        "com.1password.1password",
        "com.agilebits.onepassword7",
    }))
    read_focused_text: bool = False              # opt-in Accessibility text grab


# --------------------------------------------------------------------------- #
# Captured event payload                                                        #
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CaptureEvent:
    ts: str                       # ISO-8601 UTC timestamp
    app_name: str
    bundle_id: str
    window_title: str
    ocr_text: str
    captured: bool                # did we actually get a screenshot?
    skip_reason: str = ""         # why the screenshot was skipped, if any
    focused_text: str = ""        # optional Accessibility value of focused field


# --------------------------------------------------------------------------- #
# Low-level macOS helpers (all synchronous; called from a worker thread)        #
# --------------------------------------------------------------------------- #

# IsSecureEventInputEnabled() lives in Carbon (HIToolbox) and has no PyObjC
# binding, so we reach it through ctypes. It returns true while a secure text
# field (password box, login window, sudo prompt) holds the keyboard.
_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
_carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool


def is_secure_input_enabled() -> bool:
    """True when a password/secure field is focused anywhere on the system."""
    try:
        return bool(_carbon.IsSecureEventInputEnabled())
    except Exception:  # pragma: no cover - defensive
        return False


def frontmost_app() -> tuple[str, str, Optional[int]]:
    """Return (localized_name, bundle_id, pid) of the active application."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return ("unknown", "", None)
    return (
        app.localizedName() or "unknown",
        app.bundleIdentifier() or "",
        int(app.processIdentifier()),
    )


def active_window_title(pid: Optional[int]) -> str:
    """
    Best-effort title of the frontmost window owned by ``pid``.

    ``kCGWindowName`` is only populated when this process holds Screen Recording
    permission; without it we still know the window exists but get an empty name.
    """
    if pid is None:
        return ""
    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    window_infos = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
    for win in window_infos or []:
        if win.get("kCGWindowOwnerPID") != pid:
            continue
        if win.get("kCGWindowLayer", 0) != 0:   # layer 0 == normal app window
            continue
        name = win.get("kCGWindowName")
        if name:
            return str(name)
    return ""


def capture_main_display():
    """
    Capture the whole main display as a CGImageRef, or ``None`` if blocked.

    NOTE: ``CGWindowListCreateImage`` is the simplest capture path and works on
    macOS 13-15 with Screen Recording permission granted. It is deprecated in
    favour of ScreenCaptureKit on macOS 14+; see README for the SCK migration.
    Returns ``None`` for DRM-protected / capture-blocked content rather than
    raising, which we treat as a graceful skip upstream.
    """
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectInfinite,                  # entire screen
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    return image  # CGImageRef or None


def ocr_cgimage(cgimage, languages: tuple[str, ...]) -> str:
    """
    Run Apple's Vision OCR on a CGImage and return the recognised text.

    This is the heart of the "no Tesseract" requirement: VNRecognizeTextRequest
    is GPU/Neural-Engine accelerated and ships with the OS. We build a
    per-image request handler, configure an *accurate* recognition pass with
    language correction, run it synchronously, then read each observation's
    single best candidate string.
    """
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        cgimage, None
    )
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    if languages:
        request.setRecognitionLanguages_(list(languages))

    # performRequests:error: returns (BOOL_ok, NSError) under PyObjC.
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision request failed: {err}")

    lines: list[str] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)  # top-1 candidate per line
        if candidates and len(candidates) > 0:
            lines.append(candidates[0].string())
    return "\n".join(lines)


def focused_element_text() -> str:
    """
    Optional: read the value of the currently focused UI element via the
    Accessibility API. Requires the user to grant Accessibility permission;
    returns "" silently otherwise. This is the non-covert way to capture
    "active text" — it reads a single focused field, not a keystroke stream.
    """
    try:
        from ApplicationServices import (
            AXUIElementCreateSystemWide,
            AXUIElementCopyAttributeValue,
            kAXFocusedUIElementAttribute,
            kAXValueAttribute,
        )
    except Exception:
        return ""
    system = AXUIElementCreateSystemWide()
    err, focused = AXUIElementCopyAttributeValue(
        system, kAXFocusedUIElementAttribute, None
    )
    if err != 0 or focused is None:
        return ""
    err, value = AXUIElementCopyAttributeValue(focused, kAXValueAttribute, None)
    if err != 0 or value is None:
        return ""
    return str(value)


def capture_once(cfg: Config) -> CaptureEvent:
    """
    Perform one full capture cycle. Pure/blocking — always run in a worker
    thread via ``run_in_executor`` so it never touches the asyncio loop.

    Every failure mode degrades to a logged, screenshot-less event rather than
    an exception, so the producer loop is crash-proof.
    """
    ts = datetime.now(timezone.utc).isoformat()
    app_name, bundle_id, pid = frontmost_app()
    title = active_window_title(pid)
    focused = focused_element_text() if cfg.read_focused_text else ""

    def skipped(reason: str) -> CaptureEvent:
        log.info("skip screenshot for %s (%s)", app_name, reason)
        return CaptureEvent(ts, app_name, bundle_id, title, "", False, reason, focused)

    # 1) Never capture while a password/secure field is active.
    if is_secure_input_enabled():
        return skipped("secure_input_active")

    # 2) Respect an explicit app blocklist (password managers, etc.).
    if bundle_id in cfg.excluded_bundles:
        return skipped("excluded_app")

    # 3) Capture. None == permission missing or capture-blocked content.
    image = capture_main_display()
    if image is None:
        return skipped("capture_blocked_or_no_permission")

    # 4) OCR. A Vision failure is non-fatal — keep the metadata, drop the text.
    try:
        text = ocr_cgimage(image, cfg.ocr_languages)
    except Exception as exc:  # noqa: BLE001 - we intentionally swallow everything
        log.warning("OCR failed for %s: %s", app_name, exc)
        return skipped(f"ocr_error")

    return CaptureEvent(ts, app_name, bundle_id, title, text, True, "", focused)


# --------------------------------------------------------------------------- #
# LLM backends                                                                  #
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = (
    "You are a private, on-device context-tracking assistant. Given a short log "
    "of what the user was looking at (app, window title, and OCR'd on-screen "
    "text), write ONE highly condensed sentence describing what the user is "
    "actually doing and why. Be specific and factual. Do not invent details. "
    "Output only the sentence."
)


def _build_summary_prompt(batch: list[CaptureEvent], max_chars: int) -> str:
    """Render a batch of capture events into a single summarisation prompt."""
    parts: list[str] = [_SYSTEM_PROMPT, "\n--- ACTIVITY LOG ---"]
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


class OllamaBackend:
    """Talks to a local Ollama server over HTTP. Uses only the stdlib."""

    def __init__(self, cfg: Config) -> None:
        self.url = cfg.ollama_host.rstrip("/") + "/api/generate"
        self.model = cfg.model

    def _generate_sync(self, prompt: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 128},
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("response") or "").strip()

    async def summarize(self, loop, executor, prompt: str) -> str:
        return await loop.run_in_executor(executor, self._generate_sync, prompt)


class MLXBackend:
    """Native Apple-Silicon inference via mlx-lm. Model stays resident in RAM."""

    def __init__(self, cfg: Config) -> None:
        try:
            from mlx_lm import load, generate
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "mlx-lm not installed. Run: pip install mlx-lm  (or: uv tool install mlx-lm)"
            ) from exc
        self._generate = generate
        log.info("loading MLX model %s (this may take a moment)…", cfg.model)
        self.model, self.tokenizer = load(cfg.model)

    def _generate_sync(self, prompt: str) -> str:
        messages = [{"role": "user", "content": prompt}]
        text = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False
        )
        return self._generate(
            self.model, self.tokenizer, prompt=text, max_tokens=128, verbose=False
        ).strip()

    async def summarize(self, loop, executor, prompt: str) -> str:
        # MLX is not thread-safe across workers; the caller passes a 1-worker pool.
        return await loop.run_in_executor(executor, self._generate_sync, prompt)


def make_backend(cfg: Config):
    if cfg.backend == "ollama":
        return OllamaBackend(cfg)
    if cfg.backend == "mlx":
        return MLXBackend(cfg)
    raise ValueError(f"unknown backend: {cfg.backend!r}")


# --------------------------------------------------------------------------- #
# Storage                                                                       #
# --------------------------------------------------------------------------- #
class ContextDB:
    """Thin SQLite wrapper. All access is funnelled through one writer thread."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS context (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT    NOT NULL,
        app_name     TEXT    NOT NULL,
        bundle_id    TEXT,
        window_title TEXT,
        ocr_text     TEXT,
        summary      TEXT,
        captured     INTEGER NOT NULL DEFAULT 1,
        skip_reason  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_context_ts  ON context(ts);
    CREATE INDEX IF NOT EXISTS idx_context_app ON context(app_name);
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def insert_batch(self, rows: list[tuple]) -> None:
        self._conn.executemany(
            "INSERT INTO context "
            "(ts, app_name, bundle_id, window_title, ocr_text, summary, captured, skip_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def close(self) -> None:
        with self._conn:
            self._conn.close()


# --------------------------------------------------------------------------- #
# Async producer / consumer pipeline                                            #
# --------------------------------------------------------------------------- #
async def producer(
    cfg: Config,
    queue: "asyncio.Queue[CaptureEvent]",
    stop: asyncio.Event,
    capture_pool: ThreadPoolExecutor,
) -> None:
    """Capture on a fixed cadence; enqueue events; never block on consumers."""
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        tick = loop.time()
        try:
            event = await loop.run_in_executor(capture_pool, capture_once, cfg)
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Backpressure: the LLM is behind. Drop the *oldest* item so the
                # most recent context is always preserved, and warn once.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                    queue.task_done()
                queue.put_nowait(event)
                log.warning("queue full — dropped oldest event (LLM is behind)")
        except Exception as exc:  # noqa: BLE001 - producer must never die
            log.exception("capture cycle errored (continuing): %s", exc)

        # Sleep the remainder of the interval, accounting for capture time/drift.
        elapsed = loop.time() - tick
        delay = max(0.0, cfg.interval - elapsed)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass  # normal: interval elapsed, loop again


async def consumer(
    cfg: Config,
    queue: "asyncio.Queue[CaptureEvent]",
    stop: asyncio.Event,
    backend,
    db: ContextDB,
    llm_pool: ThreadPoolExecutor,
    db_pool: ThreadPoolExecutor,
) -> None:
    """
    Drain the queue in batches, summarise each batch with one LLM call, and
    persist. Batching amortises model latency and keeps writes coarse-grained.
    """
    loop = asyncio.get_running_loop()

    async def collect_batch() -> list[CaptureEvent]:
        batch: list[CaptureEvent] = []
        deadline = loop.time() + cfg.batch_window
        while len(batch) < cfg.batch_size:
            timeout = deadline - loop.time()
            if timeout <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(queue.get(), timeout=timeout))
            except asyncio.TimeoutError:
                break
        return batch

    while not (stop.is_set() and queue.empty()):
        batch = await collect_batch()
        if not batch:
            continue

        # One condensed summary per batch (covers the whole time window). Events
        # with no screenshot still get logged with their skip_reason.
        summary = ""
        if any(ev.captured and ev.ocr_text for ev in batch):
            prompt = _build_summary_prompt(batch, cfg.ocr_max_chars)
            try:
                summary = await backend.summarize(loop, llm_pool, prompt)
            except (urllib.error.URLError, TimeoutError) as exc:
                log.error("LLM summarisation failed (storing without summary): %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("unexpected LLM error: %s", exc)

        rows = [
            (
                ev.ts, ev.app_name, ev.bundle_id, ev.window_title,
                ev.ocr_text, summary, int(ev.captured), ev.skip_reason,
            )
            for ev in batch
        ]
        try:
            await loop.run_in_executor(db_pool, db.insert_batch, rows)
            log.info("stored %d events | summary: %s", len(rows), summary or "(none)")
        except Exception as exc:  # noqa: BLE001
            log.exception("DB write failed: %s", exc)
        finally:
            for _ in batch:
                queue.task_done()


# --------------------------------------------------------------------------- #
# Orchestration                                                                 #
# --------------------------------------------------------------------------- #
async def run(cfg: Config) -> None:
    queue: asyncio.Queue[CaptureEvent] = asyncio.Queue(maxsize=cfg.queue_max)
    stop = asyncio.Event()

    # Dedicated single-worker pools: capture is serialised, the LLM (especially
    # MLX) must be single-threaded, and SQLite gets exactly one writer.
    capture_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
    llm_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm")
    db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")

    backend = make_backend(cfg)
    db = ContextDB(cfg.db_path)
    log.info("tracking every %.0fs -> %s (backend=%s, model=%s)",
             cfg.interval, cfg.db_path, cfg.backend, cfg.model)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    prod = asyncio.create_task(producer(cfg, queue, stop, capture_pool))
    cons = asyncio.create_task(
        consumer(cfg, queue, stop, backend, db, llm_pool, db_pool)
    )
    try:
        await stop.wait()                 # block until SIGINT/SIGTERM
        log.info("shutting down — draining queue…")
        await asyncio.wait_for(queue.join(), timeout=30)
    finally:
        prod.cancel()
        cons.cancel()
        await asyncio.gather(prod, cons, return_exceptions=True)
        capture_pool.shutdown(wait=False, cancel_futures=True)
        llm_pool.shutdown(wait=True)
        db_pool.shutdown(wait=True)
        db.close()
        log.info("stopped cleanly.")


def parse_args(argv: Optional[list[str]] = None) -> Config:
    p = argparse.ArgumentParser(description="Local, private macOS context tracker.")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between captures")
    p.add_argument("--db", type=Path, default=Path.home() / ".local_mac_context.db")
    p.add_argument("--backend", choices=("ollama", "mlx"), default="ollama")
    p.add_argument("--model", default="qwen2.5:72b-instruct-q6_K",
                   help="ollama tag, or HF repo for mlx (e.g. mlx-community/Qwen2.5-72B-Instruct-8bit)")
    p.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--batch-window", type=float, default=60.0)
    p.add_argument("--read-focused-text", action="store_true",
                   help="also log the focused field's text (needs Accessibility permission)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return Config(
        interval=a.interval,
        db_path=a.db,
        backend=a.backend,
        model=a.model,
        ollama_host=a.ollama_host,
        batch_size=a.batch_size,
        batch_window=a.batch_window,
        read_focused_text=a.read_focused_text,
    )


def main() -> None:
    cfg = parse_args()
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
