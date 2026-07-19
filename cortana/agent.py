"""The agent loop — Cortana's continuous perceive → remember cycle.

A type-(a) perception loop on an ``asyncio`` event loop with dedicated single-worker
``ThreadPoolExecutor``s (capture / llm / db) so blocking native work never stalls
cadence. Design: docs/AGENT_LOOP.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from enum import Enum, auto

from cortana.backends import LLMBackend
from cortana.config import Config
from cortana.memory import Memory
from cortana.metrics import Metrics
from cortana.perception import (
    Observation,
    ScreenSensor,
    Semantic,
    changed,
    content_hash,
    extract_meaning,
)
from cortana.redaction import redact_observation
from cortana.working_memory import WorkingMemory

log = logging.getLogger("cortana.agent")

# Maintenance cadences for a long-running daemon (Phase 6 — see docs/AGENT_LOOP.md).
_METRICS_LOG_INTERVAL = 600.0       # log a metrics summary every 10 min
_PRUNE_INTERVAL = 24 * 3600.0       # re-prune memory daily


class Disposition(Enum):
    """What the producer should do with a freshly perceived observation."""

    CHANGED = auto()      # new content -> summarize + remember
    UNCHANGED = auto()    # same screen as last time -> heartbeat only, no LLM


def plan_disposition(prev_hash: str | None, obs: Observation) -> Disposition:
    """Decide whether the screen changed since the previous perception. If the sensor
    already deduped by image (skipped OCR, ``skip_reason == 'unchanged'``), honor that
    directly; otherwise dedup on the OCR text. Pure — unit-tested without the loop."""
    if obs.skip_reason == "unchanged":            # pre-OCR image dedup (sensor)
        return Disposition.UNCHANGED
    obs.content_hash = content_hash(obs.app_name, obs.window_title, obs.ocr_text)
    return Disposition.CHANGED if changed(prev_hash, obs.content_hash) else Disposition.UNCHANGED


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _AsyncMemory:
    """Funnels every Memory write through one db executor thread (single writer),
    so the event loop never blocks on SQLite and there is no write concurrency."""

    def __init__(self, memory: Memory, loop: asyncio.AbstractEventLoop,
                 executor: ThreadPoolExecutor) -> None:
        self._m = memory
        self._loop = loop
        self._ex = executor

    async def remember(self, observations, semantic, embedder=None):
        return await self._loop.run_in_executor(
            self._ex, self._m.remember, observations, semantic, embedder)

    async def remember_dropped(self, observation):
        await self._loop.run_in_executor(self._ex, self._m.remember_dropped, observation)

    async def prune(self):
        return await self._loop.run_in_executor(self._ex, self._m.prune)

    async def consolidate(self, backend):
        """Run a consolidation pass (recall recent episodes -> reflection) on the db
        executor, keeping it serial with writes (single-writer discipline)."""
        from cortana.consolidation import consolidate
        return await self._loop.run_in_executor(self._ex, consolidate, self._m, backend)


class AgentLoop:
    """Cortana's continuous perceive→remember loop on asyncio + single-worker
    executors (capture / llm / db). See docs/AGENT_LOOP.md."""

    def __init__(self, config: Config, memory: Memory, backend: LLMBackend,
                 sensor=None, working_memory: WorkingMemory | None = None,
                 embedder=None) -> None:
        self._cfg = config
        self._memory = memory
        self._backend = backend
        self._embedder = embedder     # when set, store semantic vectors on write
        # sensor: (ts) -> Observation | None. Default = the live native sensor with
        # exact-hash image dedup (safe: any change still OCRs) and the privacy
        # exclusion list enforced. Tests inject a fake so the loop runs without PyObjC.
        self._sensor = sensor or ScreenSensor(config.ocr_languages, dedup=config.image_dedup,
                                              excluded_bundles=config.excluded_bundles)
        self.metrics = Metrics()
        # Short-term memory: recent changed observations, live in RAM. Shared with
        # readers (chat/recommend) when injected; otherwise loop-local.
        self.working = working_memory or WorkingMemory(maxlen=config.working_memory_max)

    @property
    def drops_total(self) -> int:
        return self.metrics.dropped

    async def run(self, *, max_ticks: int | None = None,
                  install_signal_handlers: bool = True) -> None:
        cfg = self._cfg
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Observation] = asyncio.Queue(maxsize=cfg.queue_max)
        stop = asyncio.Event()

        capture_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
        llm_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm")
        db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        amem = _AsyncMemory(self._memory, loop, db_pool)
        tasks: list[asyncio.Task] = []
        try:
            if install_signal_handlers:  # pragma: no cover - production-only path
                for sig in (signal.SIGINT, signal.SIGTERM):
                    with contextlib.suppress(NotImplementedError, ValueError):
                        loop.add_signal_handler(sig, stop.set)

            await amem.prune()                 # bound memory before we start growing it
            prod = asyncio.create_task(self._producer(queue, stop, loop, capture_pool, amem, max_ticks))
            cons = asyncio.create_task(self._consumer(queue, stop, loop, llm_pool, amem))
            maint = asyncio.create_task(self._maintenance(stop, amem))
            tasks = [prod, cons, maint]

            await prod                          # ends on max_ticks (tests) or stop (signal)
            stop.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(queue.join(), timeout=cfg.batch_window + 30)
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            # gather retrieves results/exceptions (incl. the producer's) so none
            # are left unretrieved, and waits for cancellation to settle.
            await asyncio.gather(*tasks, return_exceptions=True)
            capture_pool.shutdown(wait=False, cancel_futures=True)
            llm_pool.shutdown(wait=True)
            db_pool.shutdown(wait=True)
            log.info("cortana metrics: %s", self.metrics.render())

    async def _sleep_or_stop(self, stop: asyncio.Event, delay: float) -> bool:
        """Sleep up to ``delay``; return True if stop fired during the wait."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def _producer(self, queue, stop, loop, capture_pool, amem, max_ticks) -> None:
        prev_hash: str | None = None
        ticks = 0
        while not stop.is_set():
            if max_ticks is not None and ticks >= max_ticks:
                break
            started = loop.time()
            obs = await loop.run_in_executor(capture_pool, self._sensor, _now_iso())
            ticks += 1
            if obs is not None:
                self.metrics.captures += 1
                if self._cfg.redact:
                    obs = redact_observation(obs)   # scrub secrets before store/summarize
                prev_hash = await self._route(obs, prev_hash, queue, amem)
            if self._cfg.interval > 0:
                elapsed = loop.time() - started
                if await self._sleep_or_stop(stop, max(0.0, self._cfg.interval - elapsed)):
                    break

    async def _route(self, obs, prev_hash, queue, amem) -> str:
        """Apply the change-detection gate and enqueue / drop. Compaction: an
        unchanged frame is NOT persisted — at 1s capture that would be O(seconds)
        dead rows. Only distinct 'episodes' (changed screens) become rows; dwell
        time is recoverable from the gap to the next episode's timestamp."""
        if plan_disposition(prev_hash, obs) is Disposition.UNCHANGED:
            self.metrics.unchanged += 1
            if obs.skip_reason == "unchanged":       # sensor skipped OCR (image dedup)
                self.metrics.ocr_skipped += 1
            return prev_hash                         # idle: count it, store nothing
        self.metrics.changed += 1
        self.working.add(obs)                        # short-term memory: recent activity
        try:
            queue.put_nowait(obs)
        except asyncio.QueueFull:
            # Backpressure: evict the oldest, persist it (visible, never silent),
            # then enqueue the newest so recent context is preserved.
            evicted = queue.get_nowait()
            queue.task_done()
            await amem.remember_dropped(evicted)
            self.metrics.dropped += 1
            queue.put_nowait(obs)
        return obs.content_hash

    async def _consumer(self, queue, stop, loop, llm_pool, amem) -> None:
        while not (stop.is_set() and queue.empty()):
            batch = await self._collect_batch(queue, stop, loop)
            if not batch:
                continue
            semantic: Semantic | None = None
            if any(o.captured and o.ocr_text for o in batch):
                started = loop.time()
                # keep the total prompt budget ~constant when a drained backlog makes
                # the batch large: shrink the per-event OCR cap proportionally.
                per_event = max(600, self._cfg.ocr_max_chars * self._cfg.batch_size
                                // max(1, len(batch)))
                try:
                    semantic = await loop.run_in_executor(
                        llm_pool, extract_meaning, batch, self._backend, per_event)
                    self.metrics.record_llm(loop.time() - started)
                    self.metrics.summarized += len(batch)
                except Exception as exc:  # noqa: BLE001 - backend down/slow/bad response
                    # Degrade visibly: keep the observations (store without a summary),
                    # count the error, and keep the loop alive rather than dying.
                    self.metrics.llm_errors += 1
                    log.warning("meaning extraction failed; storing %d events without "
                                "summary: %s", len(batch), exc)
            # task_done for every item even if remember/summarize raised, so a failure
            # can never leave queue.join() hanging at shutdown.
            try:
                await amem.remember(batch, semantic, self._embedder)
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to persist a batch of %d events: %s", len(batch), exc)
            finally:
                for _ in batch:
                    queue.task_done()

    async def _collect_batch(self, queue, stop, loop):
        """Gather up to batch_size observations, or whatever arrives within
        batch_window — draining promptly once stop is set. If a backlog has built up
        (captures outpacing the LLM), drain immediately-available items up to
        4×batch_size so one summarization call absorbs the backlog instead of the
        queue growing until backpressure drops events."""
        batch: list[Observation] = []
        deadline = loop.time() + self._cfg.batch_window
        while len(batch) < self._cfg.batch_size:
            if stop.is_set() and queue.empty():
                break
            timeout = deadline - loop.time()
            if timeout <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(queue.get(), timeout=timeout))
            except asyncio.TimeoutError:
                break
        # adaptive drain: absorb any backlog that piled up while we waited/summarized
        while len(batch) < self._cfg.batch_size * 4:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _maintenance(self, stop, amem) -> None:  # pragma: no cover - daemon-cadence (10min/24h)
        """Long-run upkeep: log a metrics summary periodically and re-prune daily.
        Cadences are far longer than any hermetic test, so this is daemon-only."""
        elapsed = 0.0
        while not await self._sleep_or_stop(stop, _METRICS_LOG_INTERVAL):
            log.info("cortana metrics: %s", self.metrics.render())
            elapsed += _METRICS_LOG_INTERVAL
            if elapsed >= _PRUNE_INTERVAL:
                elapsed = 0.0
                await amem.prune()
                # Consolidate the day's episodes into a durable reflection so the
                # semantic tier accrues automatically (no manual `cortana digest`).
                try:
                    await amem.consolidate(self._backend)
                except Exception as exc:  # noqa: BLE001 - never let upkeep kill the loop
                    log.warning("scheduled consolidation failed: %s", exc)
