# Agent Loop (Phase 3) — design & scaling path

> Status: **design, pre-implementation.** Companion to `docs/DESIGN.md` (Phase 3).
> Captures the v1 loop topology and — the focus of this doc — **how it extends to
> multiple producers and consumers** if/when perception or processing grows. Nothing
> here is built yet; decisions marked *(proposed)* are open for review.

Governed by `STYLE.md` (simple design, no speculative generality). The guiding
rule: **build the 1+1 loop now; preserve the seams that make the multi-worker
future cheap; build that future only when a concrete need arrives.**

---

## 1. What the loop is (recap)

Cortana's loop is a **continuous perception loop** (sense → dedup → summarize →
remember, forever on a cadence), *not* a goal-directed LLM/ReAct agent. The LLM is
one step *inside* a cycle, not the thing steering it. Everything below is about
running that cycle robustly and, later, at wider scope.

## 2. v1 topology — 1 producer + 1 consumer (the base)

```
 PRODUCER (1 thread)            QUEUE (bounded)        CONSUMER (1 thread)
  every ~interval:                                      collect batch (≤ size OR ≤ window)
   frontmost_app + capture  ── put ──▶ [ ][ ][ ] ── take ──▶ extract_meaning(batch, backend)
   + ocr → Observation                                   → Memory.remember(batch, semantic)
   changed()? no → idle: count only, store nothing (compaction; dwell = ts gap)
   queue full?  → Memory.remember_dropped() + drops++

 Startup: Memory.prune()   (periodic re-prune deferred to the Phase 6 daemon)
```

- **Producer** = the *perceive* thread: capture + OCR → `Observation`, change-detect, enqueue.
- **Consumer** = the *remember* thread: batch → LLM summarize → persist.
- **Prune at startup** bounds memory each run. A *periodic* re-prune only matters for
  a >24h continuous process, so it's deferred to the Phase 6 daemon — the startup
  prune already satisfies the bound, and building the periodic sweep now would be
  untested machinery.

**Why exactly one of each:** capture is serial and cheap (no producer parallelism
to gain); the local LLM runs one inference at a time and SQLite wants a single
writer (no consumer parallelism to gain). See §6 for why identical fan-out doesn't
help on a single machine.

**Concurrency substrate *(decided)*: `asyncio` event loop + dedicated
single-worker `ThreadPoolExecutor`s** (capture / llm / db). The event loop
orchestrates the producer/consumer/janitor coroutines and owns cadence, graceful
drain, and cancellation; every blocking native call (capture, OCR, LLM, SQLite)
is offloaded to its executor via `run_in_executor` so the loop never blocks. This
is the v0-proven shape, re-homed onto the new package, and it generalizes cleanly
to the multi-worker future (§4–§6). See §6 for the trade-off vs plain threads.

## 3. The one invariant that shapes all scaling: a single SQLite writer

No matter how many workers exist upstream, **all writes funnel through one writer**
(`Memory`'s single connection/thread). This avoids SQLite lock contention and keeps
the FTS-sync triggers and batch atomicity correct. Every topology below **fans out
on capture/compute but fans back in to one writer.**

```
   ...many producers... ─▶ queue(s) ─▶ ...many compute stages... ─▶ [ONE writer] ─▶ DB
```

## 4. Scaling shape A — multiple *producers* = heterogeneous sensors

"More producers" is never *copies* of the screen capturer (that's redundant — one
screen, captured serially). It's **different kinds of sensor**, each its own thread,
all emitting `Observation`s into the **same queue**:

```
 [screen sensor]     ─┐
 [per-display × N]   ─┼─ put ─▶ queue ─▶ consumer ─▶ memory
 [clipboard sensor]  ─┤
 [audio sensor]      ─┘   (each its own cadence; queue is source-agnostic)
```

**What changes** when the second sensor arrives:
- Introduce the `Sensor` ABC we deferred: `class Sensor(ABC): def perceive(self) -> Observation | None`.
- Add `Observation.source: str` (e.g. `"screen"`, `"clipboard"`) so memory/recall can filter by modality.
- The loop owns a *list* of sensors, each driven on its own timer thread.

**What does NOT change:** the queue, the consumer, the batch/summarize logic, and
the single writer. The queue being source-agnostic is the seam that makes this
additive rather than a rewrite.

**Seams to preserve now (cost: ~0):** keep the queue typed on `Observation` (not a
screen-specific struct), and keep the producer's capture logic in one function
that's trivially liftable behind `Sensor.perceive()` later. Do **not** build the
`Sensor` ABC or `source` field until a real second sensor exists.

## 5. Scaling shape B — multiple *consumers* = a pipeline of stages

"More consumers" is **not** a pool of identical summarizers (a single local model
won't run them in parallel — §6). It's a **pipeline**: split processing into stages,
each its own worker, items flowing through bounded queues between them:

```
 q0 ─▶ [summarize] ─▶ q1 ─▶ [embed] ─▶ q2 ─▶ [classify] ─▶ q3 ─▶ [ONE writer] ─▶ DB
```

Different stages run concurrently on *different* items (item A embedding while item
B summarizes) — that's real, useful parallelism, unlike duplicate summarizers.
**When justified:** Phase 7's embedding/semantic memory or classification — i.e.
when there's a *second distinct processing step*, not before. Each inter-stage
queue is bounded (independent backpressure per stage); the tail always converges to
the single writer.

## 6. Substrate: plain threads vs `asyncio` + thread executors

Your question — *would the multi-worker future use asyncio + thread executors?*
The honest breakdown:

**The work is blocking and native.** Screenshot (Quartz), OCR (Vision), LLM
inference (MLX/Ollama), and SQLite are all blocking calls implemented in C. You
**cannot** make them truly async; the only way to overlap them is to run them on
**threads**. Crucially, blocking native/I/O calls *release the GIL* while they run,
so threads give real wall-clock overlap here (while the LLM thread is deep in native
inference, the capture thread runs).

So threads are the execution mechanism **either way**. The real question is only
whether you put an **event-loop coordination layer on top of them**:

| | Plain threads + `queue.Queue` | `asyncio` + `run_in_executor` (thread pools) |
|---|---|---|
| What it is | Long-lived worker threads, thread-safe queues, `Event` for shutdown | An event loop *orchestrates*; blocking work is offloaded to thread executors (what v0 did) |
| Best when | A **small, fixed** set of long-lived workers (our case) | **Many / dynamic** concurrent operations; you want structured cancellation, per-op timeouts, a task graph |
| Cost | Trivial mental model; manual timeout/cancel via queue timeouts | Event-loop ceremony; every blocking call must be wrapped in an executor anyway |
| Parallelism source | the threads | still the threads (asyncio adds coordination, not parallelism) |

**Decision (v1): `asyncio` + executors.** Chosen over plain threads because the
event loop gives us, for free, the primitives this loop genuinely uses — a clean
cadence timer (`wait_for(stop.wait(), timeout=...)`), structured **graceful drain**
(`queue.join()` with a timeout), and **cooperative cancellation** of the
producer/consumer/janitor on shutdown — and because it is the natural substrate for
the multi-worker future (N sensor coroutines, a stage pipeline, per-op timeouts on a
slow LLM) without a later rewrite. The cost is a modest amount of event-loop
ceremony; the blocking work still runs on the executors either way.

Either way the rule holds: **blocking work runs on threads; writes fan in to one
writer.** Plain threads remain a perfectly valid alternative for a strictly-fixed
small topology; we choose `asyncio` so the orchestration primitives and the growth
path are built in from the start.

> Forward note: free-threaded CPython (PEP 703, building momentum in 3.13+) removes
> the GIL and would make *CPU-bound* Python parallel too. It doesn't change our
> calculus (our heavy work is native/I/O that already releases the GIL), but it's
> the thing that would most change this table in the future.

## 7. Migration path (how we get from 1+1 to N without a rewrite)

1. **Now:** 1 producer + 1 consumer + janitor on an `asyncio` loop with three
   single-worker executors, a source-agnostic `Observation` queue, single writer.
   Capture logic isolated in one `perceive()` function (injectable for tests).
2. **Add a sensor:** introduce `Sensor` ABC + `Observation.source`; spawn N sensor
   coroutines (each its own cadence) onto the existing queue. Consumer/writer untouched.
3. **Add a processing stage:** split the consumer into a stage pipeline with bounded
   inter-stage `asyncio.Queue`s; tail still writes through the one writer.
4. **Per-op robustness:** wrap slow LLM calls in `wait_for` deadlines, add retries —
   the event-loop primitives are already in place.

Each step is additive and gated by a *real* need — none is built ahead of time.

## 8. Out of scope / explicitly NOT built now (YAGNI)

- The `Sensor` ABC, `Observation.source`, and any non-screen sensor.
- A consumer pipeline / stages (no embeddings yet — that's Phase 7).
- An `asyncio` orchestrator (plain threads until proven insufficient).
- Identical worker pools of either role (no win on one machine — §6).
- Distributed / multi-machine anything (Cortana is single-user, on-device by design).
