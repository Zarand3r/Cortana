"""The agent loop — Cortana's continuous perceive → remember cycle.

A type-(a) perception loop on an ``asyncio`` event loop with dedicated single-worker
``ThreadPoolExecutor``s (capture / llm / db) so blocking native work never stalls
cadence. Design: docs/AGENT_LOOP.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from enum import Enum, auto

from cortana.backends import LLMBackend
from cortana.config import Config
from cortana.memory import Memory
from cortana.perception import (
    Observation,
    Semantic,
    changed,
    content_hash,
    extract_meaning,
    perceive,
)
from cortana.redaction import redact_observation


class Disposition(Enum):
    """What the producer should do with a freshly perceived observation."""

    CHANGED = auto()      # new content -> summarize + remember
    UNCHANGED = auto()    # same screen as last time -> heartbeat only, no LLM


def plan_disposition(prev_hash: str | None, obs: Observation) -> Disposition:
    """Assign ``obs.content_hash`` and decide whether the screen changed since the
    previous perception. Pure — no I/O, so the producer's branch is unit-tested
    without the event loop."""
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

    async def remember(self, observations, semantic):
        return await self._loop.run_in_executor(self._ex, self._m.remember, observations, semantic)

    async def remember_dropped(self, observation):
        await self._loop.run_in_executor(self._ex, self._m.remember_dropped, observation)

    async def prune(self):
        return await self._loop.run_in_executor(self._ex, self._m.prune)


class AgentLoop:
    """Cortana's continuous perceive→remember loop on asyncio + single-worker
    executors (capture / llm / db). See docs/AGENT_LOOP.md."""

    def __init__(self, config: Config, memory: Memory, backend: LLMBackend,
                 sensor=None) -> None:
        self._cfg = config
        self._memory = memory
        self._backend = backend
        # sensor: (ts) -> Observation | None. Default = the live native sensor;
        # tests inject a fake so the loop runs without PyObjC.
        self._sensor = sensor or (lambda ts: perceive(ts, config.ocr_languages))
        self.drops_total = 0

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
            tasks = [prod, cons]

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
                if self._cfg.redact:
                    obs = redact_observation(obs)   # scrub secrets before store/summarize
                prev_hash = await self._route(obs, prev_hash, queue, amem)
            if self._cfg.interval > 0:
                elapsed = loop.time() - started
                if await self._sleep_or_stop(stop, max(0.0, self._cfg.interval - elapsed)):
                    break

    async def _route(self, obs, prev_hash, queue, amem) -> str:
        """Apply the change-detection gate and enqueue / heartbeat / drop."""
        if plan_disposition(prev_hash, obs) is Disposition.UNCHANGED:
            heartbeat = replace(obs, skip_reason="unchanged", ocr_text="")
            await amem.remember([heartbeat], None)   # dwell-time signal, no LLM
            return prev_hash
        try:
            queue.put_nowait(obs)
        except asyncio.QueueFull:
            # Backpressure: evict the oldest, persist it (visible, never silent),
            # then enqueue the newest so recent context is preserved.
            evicted = queue.get_nowait()
            queue.task_done()
            await amem.remember_dropped(evicted)
            self.drops_total += 1
            queue.put_nowait(obs)
        return obs.content_hash

    async def _consumer(self, queue, stop, loop, llm_pool, amem) -> None:
        while not (stop.is_set() and queue.empty()):
            batch = await self._collect_batch(queue, stop, loop)
            if not batch:
                continue
            semantic: Semantic | None = None
            if any(o.captured and o.ocr_text for o in batch):
                semantic = await loop.run_in_executor(
                    llm_pool, extract_meaning, batch, self._backend, self._cfg.ocr_max_chars)
            await amem.remember(batch, semantic)
            for _ in batch:
                queue.task_done()

    async def _collect_batch(self, queue, stop, loop):
        """Gather up to batch_size observations, or whatever arrives within
        batch_window — draining promptly once stop is set."""
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
        return batch
