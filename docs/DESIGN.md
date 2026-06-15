# Cortana — System Design (Principal-Engineer Deep Dive)

> Status: **roadmap, pre-implementation — refined v2.** Produced via
> `strategic-engineering-planner`, hardened by an adversarial design-review subagent
> (false positives rejected — see §Risks), then refined to resolve all open
> decisions with concrete defaults. Governed by `karpathy-guidelines` /
> `principal-production-engineer`. This file is the frozen seam for the
> `/implement` (elves) harness.

---

## Goal

Cortana is a **local, private, on-device perceptual agent for macOS**. It
continuously **perceives** the screen (capture + OCR tools), **extracts meaning**
(a local LLM turns pixels into "what is the user doing, and why"), **remembers**
(tiered episodic + semantic memory in SQLite), and **reasons over that memory** to
answer the user — *"what was I working on this morning?"* — with nothing leaving
the machine.

This is an **agent**, not a passive logger: tools (perception) + memory + an agent
loop, with a recall/reasoning capability on top. The existing
`context_tracker.py` (~600 lines) is effectively the **perceive→remember loop of
that agent already running** — a clean producer/consumer pipeline, single-writer
SQLite, fail-soft capture, real privacy reflexes (secure-input + password-manager
exclusion, no keylogging). The architecture is the right backbone; what this design
does is (a) re-group it into explicit agent subsystems — **Memory**, **Perception
tools**, **Agent loop** — fixing the data model / bounds / privacy / search gaps
along the way, and (b) add what a logger never had: an **agent loop** as a
first-class concept and a **recall-&-reasoning** capability. No rewrite; the v0
backbone is preserved and reframed.

## Success Metrics

| Dimension | Target |
|---|---|
| Capture cadence integrity | p95 inter-capture interval within ±10% of `--interval`, even while the LLM is saturated |
| Pipeline durability | Zero **silent** event loss: every captured event is stored or counted in a drop metric |
| Storage bound | DB size bounded by retention (age **and** size cap); steady-state growth known and capped |
| Wasted work | ≥70% reduction in LLM calls during idle/unchanged-screen periods vs v0 |
| Search latency | Keyword recall over 1M rows <100 ms (FTS5, not `LIKE` scan) |
| Privacy | High-confidence secrets redacted before store; at-rest posture explicit and documented |
| Recoverability | After SIGKILL or compaction, restart resumes cleanly; no corruption, no duplicate-on-restart |

## Constraints

- **Hard:** 100% on-device. Only permitted egress is `127.0.0.1` (Ollama). No telemetry.
- **Hard:** macOS 13+, Apple Silicon, Python 3.11+. PyObjC bindings only — no Tesseract/OpenCV.
- **Hard:** Capture loop must never block on the LLM or DB (the v0 invariant — preserve it).
- **Soft:** Stay few-file, stdlib-first where possible (Ollama path uses only stdlib). Resist framework creep.
- **Soft:** Local inference is RAM-bound; the default model must be feasible on a 16–32 GB Mac.

## Non-Goals

- No cloud sync, no multi-device, no server. Ever.
- No keystroke logging. The only "active text" path is the opt-in Accessibility *focused-field* read.
- No real-time/sub-second capture. This is a perceptual *journal* the agent reasons over, not a screen recorder.
- No GUI in v1. Perception runs as a daemon; recall is a thin CLI (`cortana ask …`). A UI is a later project.
- **No effector actions in v1.** The agent's "act" is read-only recall/reasoning over its own memory. Tools that *change the world* (proactive automation) are deferred to Phase 7, behind explicit confirmation.
- No general tool-using/ReAct framework. The Phase-4 reasoning path is thin RAG-over-memory, not a generic agent runtime.
- No general plugin/extensibility framework. Two LLM backends is the scope.

---

## Decisions Resolved (refinement pass)

The five open questions from v1 are now decided with defaults. All are
config-overridable; rationale in Tradeoff Analysis.

| # | Decision | Resolution (default) | Why |
|---|---|---|---|
| 1 | **Retention** | Age **and** size cap, prune whichever trips first: `retention_days=90`, `max_db_bytes=2 GB`. Prune at startup + every 24 h. | Age bounds staleness; size bounds worst case (heavy days). Both, with "oldest-first" eviction, is simple and safe. |
| 2 | **Redaction** | **On by default**, high-confidence deny-list only (private keys, AWS/`sk-`/`ghp_`/Bearer tokens, Luhn-valid card numbers, US SSN). Applied to `ocr_text` + `focused_text` before store. | Optimize for **few false positives** — an over-redacted log is useless. Catch the unambiguous secrets; accept some false negatives. |
| 3 | **At-rest encryption** | **v1:** documented FileVault reliance + redaction. **v2:** opt-in SQLCipher (`--encrypt`, key in Keychain). | FileVault already encrypts the disk for most users; SQLCipher adds a native dep + key management. **Confirmed by owner** (see §Decisions). |
| 4 | **Change-detection** | **Fuzzy:** normalize OCR (collapse whitespace, lowercase, strip standalone clock/date tokens) → SHA-256; skip LLM if unchanged. v2 upgrade: Jaccard line-set similarity >0.95. | Exact hashing churns on the menu-bar clock; light normalization kills 95% of false "changes" with ~5 lines of code. |
| 5 | **Default model** | `qwen2.5:7b-instruct` (Ollama `q4_K_M`). 72B stays opt-in. | 7B is ample for one factual sentence, runs in ~16 GB, multi-x faster. 3–4B loses coherence; 72B needs ~70 GB. |

## Requirement Audit

| Requirement | Keep? | Rationale |
|---|---|---|
| Per-capture full-screen OCR every N s | **Keep, gate it** | Core value, but skip unchanged screens. |
| Per-batch LLM summary | **Keep, remodel** | Right idea; storage model is wrong (see Data Model). |
| 72B default model | **Replace default** | Over-specced for one sentence; default 7B, keep 72B opt-in. |
| `--read-focused-text` (Accessibility) | **Keep, fence** | Highest-sensitivity input; off by default, behind consent + redaction. |
| Store raw full `ocr_text` forever | **Bound it** | Unbounded growth; truncate at store + retention. |
| "Searchable log" (README claim) | **Make true** | No FTS today; add FTS5 or the claim is false. |
| Multi-backend (Ollama + MLX) | **Keep** | Ollama = zero-dep; MLX = fastest resident. |

## Existing System Understanding

**Pipeline (verified against source):**
```
producer (1 thread)              asyncio.Queue(256)        consumer (batches)
 capture_once() every N s   ──▶  put_nowait               ──▶  collect_batch (size=4 OR window=60s)
   frontmost app/title            (drop OLDEST on full)         build prompt → backend.summarize()
   secure-input/blocklist guard                                 → insert_batch() (1 writer thread)
   CGWindowListCreateImage
   Vision OCR
```
Three single-worker `ThreadPoolExecutor`s (capture / llm / db) keep blocking native
calls off the event loop. Shutdown: SIGINT/SIGTERM → `stop` → `queue.join()` (30s)
→ cancel → close pools/DB.

**Keep (genuinely good):** fail-soft capture (every error → logged skip-event,
producer never dies); single SQLite writer; backpressure exists; privacy reflexes
(`IsSecureEventInputEnabled` via ctypes, bundle blocklist, no keylogger); MLX pinned
to a 1-worker pool.

**Schema today:** `context(id, ts, app_name, bundle_id, window_title, ocr_text,
summary, captured, skip_reason)` + indexes on `ts`, `app_name`.

## Architecture Decomposition

Cortana-as-agent has four core subsystems + two cross-cutting concerns. Each maps
to a build phase (see Milestone Roadmap).

1. **Perception tools** (Phase 2) — the agent's senses, as discrete testable tools:
   *capture* (frontmost app/title + screenshot), *OCR* (Vision → text),
   *change-detection* (don't re-perceive an unchanged screen), *meaning-extraction*
   (LLM: raw OCR + metadata → structured "what/why" semantic record).
2. **Memory** (Phase 1) — *episodic* (per-perception events) + *semantic* (LLM
   summaries, stored once via FK) tiers in SQLite; `recall()` via FTS5; bounded by
   retention. The `Memory` interface: `remember()`, `recall()`, `forget()`.
3. **Agent loop** (Phase 3) — composes perception → memory on a fixed cadence,
   non-blocking, with visible backpressure. The existing producer/consumer reframed.
4. **Recall & reasoning** (Phase 4) — the "act" side: retrieve memory → LLM reasons
   → grounded, cited answer to a user question (`cortana ask`).
5. **Privacy/safety** (Phase 5, cross-cutting) — redaction before memory write,
   secure-input/exclusions, documented at-rest. Gates running on real data.
6. **Lifecycle/ops** (Phase 6, cross-cutting) — 7B default, launchd daemon, metrics.

## Core Data Model (the central fix)

Today (lines 503–509) the single per-batch `summary` is **copied into every event
row**: N identical summaries, ambiguous ownership, temporal smear, duplicate query
results. Target — normalize, with FTS5 kept in sync by triggers:

```sql
PRAGMA journal_mode=WAL;            -- concurrent reads during writes; crash-safe
PRAGMA user_version=2;             -- schema version for migrations

CREATE TABLE summaries (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  window_start_ts TEXT NOT NULL,
  window_end_ts   TEXT NOT NULL,
  summary         TEXT NOT NULL,
  model           TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE context (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT    NOT NULL,
  app_name     TEXT    NOT NULL,
  bundle_id    TEXT,
  window_title TEXT,
  ocr_text     TEXT,                          -- truncated at store (≤ ocr_max_chars)
  captured     INTEGER NOT NULL DEFAULT 1,
  skip_reason  TEXT,                           -- '', 'unchanged', 'dropped_backpressure', …
  content_hash TEXT,                           -- normalized-OCR hash for dedup
  summary_id   INTEGER REFERENCES summaries(id) ON DELETE SET NULL
);
CREATE INDEX idx_context_ts   ON context(ts);
CREATE INDEX idx_context_app  ON context(app_name);
CREATE INDEX idx_context_hash ON context(content_hash);

-- Full-text search over OCR, external-content mirror of context (no data duplication):
CREATE VIRTUAL TABLE context_fts USING fts5(ocr_text, content='context', content_rowid='id');
CREATE TRIGGER context_ai AFTER INSERT ON context BEGIN
  INSERT INTO context_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
END;
CREATE TRIGGER context_ad AFTER DELETE ON context BEGIN
  INSERT INTO context_fts(context_fts, rowid, ocr_text) VALUES('delete', old.id, old.ocr_text);
END;
CREATE TRIGGER context_au AFTER UPDATE ON context BEGIN
  INSERT INTO context_fts(context_fts, rowid, ocr_text) VALUES('delete', old.id, old.ocr_text);
  INSERT INTO context_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
END;
```
One summary row per batch; events reference it by FK. Retention `DELETE FROM
context …` fires `context_ad`, keeping FTS in sync automatically; orphan summaries
(no referencing events) are pruned in the same pass. Summaries are small and few
(one per batch) — searched by `LIKE`/join, no separate FTS needed in v1.

**Migration (existing `~/.local_mac_context.db`, `user_version` 0→2):** additive
and lossless. Create the new tables/triggers; keep the legacy `context.summary`
column read-only; backfill `content_hash=NULL`, `summary_id=NULL` on old rows.
Old rows remain queryable; only new writes use the normalized path. No attempt to
reconstruct historical batch boundaries (lossy) — documented.

## Data Flow / Control Flow

**Change-detection gate (new — biggest efficiency + quality win):** in
`capture_once`, compute `content_hash = sha256(normalize(app, title, ocr_text))`
where `normalize` lowercases, collapses whitespace, and strips standalone
time/date tokens (regex). If equal to the last stored event's hash, write a
lightweight `skip_reason="unchanged"` heartbeat and **do not enqueue for
summarization** — idle screens stop driving the model.

**Visible backpressure:** on `QueueFull`, the evicted oldest event is **persisted**
with `skip_reason="dropped_backpressure"` (fast path: direct synchronous insert,
no LLM) and a `drops_total` counter increments. Loss becomes a metric, never a
silent gap.

## State Machines / Lifecycles

**Per-capture decision (fail-soft, ordered):**
```
secure_input?  ──yes──▶ skip("secure_input_active")
excluded app?  ──yes──▶ skip("excluded_app")
capture==None? ──yes──▶ skip("capture_blocked_or_no_permission")
ocr error?     ──yes──▶ skip("ocr_error")          [keep metadata]
unchanged?     ──yes──▶ skip("unchanged")          [NEW: store heartbeat, no LLM]
otherwise      ──────▶ redact → enqueue full event
```

**Process lifecycle (target):** `launchd` (RunAtLoad + KeepAlive) → run loop →
SIGTERM → bounded drain → flush metrics → exit 0. Abnormal kill → next start is a
clean session (in-memory queue is lossy by design — documented; WAL guarantees
already-written rows survive uncorrupted).

## Architecture Options

**A. Storage model** — (1) status quo: summary per row ✗ ambiguous/bloated; (2)
**normalized `summaries` + FK + FTS5 ✓ ← chosen**; (3) per-event *and* per-batch
LLM ✗ doubles cost.

**B. Change detection** — (1) none ✗; (2) **normalized text-hash ✓ ← v1**; (3)
perceptual image hash (skips OCR too) — more code, **defer v2**.

**C. Default model** — (1) 72B ✗ ~70 GB/slow; (2) **7–8B instruct ✓ ← chosen**.

**D. At-rest** — (1) plaintext+FileVault, undocumented △; (2) **redaction + documented
FileVault ✓ ← v1**; (3) SQLCipher ✓ strongest — native dep + keys, **defer v2 opt-in**.

## Tradeoff Analysis

Throughline: **fix correctness and bounds before adding features.** The normalized
model (A2) and change-detection (B2) are cheap, high-leverage, and unblock the rest
(FTS, retention, smaller model all assume a sane schema). Perceptual hashing (B3)
and SQLCipher (D3) are real wins but cost code/deps and are deferrable without
blocking anything. Dropping the 72B default (C2) is the single biggest feasibility
fix and costs nothing but a config change + doc. On redaction (D-2): deliberately
tuned for **precision over recall** — a journal redacted into Swiss cheese defeats
its purpose, so v1 only matches unambiguous secret shapes.

## Risks and Bottlenecks

| Risk | Sev | Mitigation / experiment |
|---|---|---|
| **Summary duplication** (lines 503–509) | High | Normalize: `summaries` + FK. *Test:* one summary row per batch; events join. |
| **Silent event loss on backpressure** (lines 438–445) | High | Persist evicted event (`dropped_backpressure`) + `drops_total`. Loss visible. |
| **Unbounded DB growth**: `ocr_max_chars` truncates only the *prompt* (line 306), not stored text (line 505) | High | Truncate `ocr_text` at store; retention (age+size). ~6 MB/day → bounded. |
| **Wasted LLM/OCR on idle screens** | High | Change-detection gate (B2). |
| **72B default infeasible** (line 84) | High | Default 7B; 72B opt-in. |
| **"Searchable" claim unbacked** | Med | FTS5 external-content + triggers. |
| **On-screen + focused-field secrets in plaintext** | Med | Precision redaction before store; documented FileVault; SQLCipher v2. |
| **Deprecated `CGWindowListCreateImage`** macOS 14+ (line 180) | Med | ScreenCaptureKit path + fallback; startup deprecation log (M8). |
| **FTS/retention drift** (FTS not kept in sync on delete) | Med | External-content triggers (incl. delete) — verified against the SQLite FTS5 external-content contract. *Test:* delete a row, assert it leaves FTS. |
| Ollama no retry; 180s timeout stalls consumer on hung server (line 335) | Low | Shorter timeout + bounded retries; failure already degrades to store-without-summary. |
| No tests; no metrics; no daemon | Med | Unit tests for pure logic; counters + periodic stats; launchd plist + installer. |
| Merged multi-display image → large OCR cost (CGRectInfinite) | Low | Per-display capture or document; not a correctness bug. |

**Rejected by verification (do NOT action):** the adversarial pass flagged
`queue.task_done()`/`join()` accounting as broken — traced and **false**: in the
drop path `get_nowait()` leaves `_unfinished_tasks` unchanged, `task_done()` −1,
`put_nowait()` +1; net zero, `join()` converges. Recorded so no one "fixes" a
non-bug.

## Invariants

1. The producer never blocks on the consumer (preserve v0's design).
2. Every captured event is **either stored or counted** — no silent loss.
3. Exactly one SQLite writer; writes batched and committed atomically.
4. A batch's summary is stored **once** and referenced, never duplicated per row.
5. DB size bounded by retention; growth rate known and logged.
6. Nothing captured while secure input is active or an excluded app is frontmost.
7. No network egress except to the configured local Ollama host.
8. Stored `ocr_text` length ≤ `ocr_max_chars`; high-confidence secrets redacted pre-store.
9. Every failure mode is observable (log + metric); none is fatal to the loop.
10. `context_fts` row count == `context` row count at all times (sync invariant).

## Vertical Slice Strategy

**Thinnest end-to-end *agentic* slice — proves perceive→remember→recall in one
thread:** perceive one screen (capture + OCR) → **change-detect** → **redact** →
**extract meaning** (7B) → **`remember()`** (normalized episodic+semantic write, FTS
synced) → **`cortana ask` recalls it with a citation** (timestamp + app). This
single path touches every core subsystem (Phases 1–4) and the privacy gate; green
here ⇒ every later phase is an extension, not a redesign. Build this before
breadth.

## Milestone Roadmap (agent subsystems)

Phases are organized around the **agent architecture**, not bug-fix order. Each is
a subsystem with a capability · code surface · tests. Build order: foundation (P0)
→ memory (P1) → senses (P2) → loop that integrates them (P3) → reasoning that uses
them (P4); privacy (P5) gates real-data operation; ops (P6) makes it always-on.
The **agentic vertical slice** (perceive→remember→recall) is built first across
P1–P4 before breadth.

### Phase 0 — Testing spine *(foundation; no product features)*
- **Capability:** test the agent hermetically — no Ollama, Vision, or network.
- **Code:** build the `cortana/` package skeleton (`config`, `backends`,
  `perception`, `memory`) with **lazy** native imports so pure logic imports without
  PyObjC; `FakeLLMBackend` (canned record) in `make_backend`. `tests/` + `conftest`,
  `check.sh` (`pytest -q`), `requirements-dev.txt`, `pytest.ini`. Legacy
  `context_tracker.py` left running; Phase 3 rewires it onto the package.
- **DoD:** `./check.sh` green on a clean checkout, zero native deps.

### Phase 1 — Memory *(what Cortana remembers + how it recalls)*
- **Capability:** a `cortana/memory.py::Memory` interface — `remember()`,
  `recall(query, since, until, app)`, `forget(older_than)`, `prune()` — over episodic
  + semantic tiers; bounded; searchable.
- **Code/Schema:** `summaries` (semantic) + normalized `context` (episodic) with
  `summary_id` FK — summary stored **once**, not per row; WAL; `user_version=2` +
  additive legacy migration. `context_fts` FTS5 external-content + insert/delete/update
  sync triggers for `recall()`. `prune()` (age 90 d **or** 2 GB, oldest-first, +
  orphan summaries). Truncate stored `ocr_text` at write.
- **Tests:** one summary row per batch; recall join + FTS keyword hit; migration on
  a seeded legacy DB; inv. #10 (`count(context_fts)==count(context)` after delete);
  bounded-growth + orphan cleanup. *(Absorbs old M1+M2+M4.)*

### Phase 2 — Perception & meaning-extraction tools *(the senses)*
- **Capability:** discrete, testable tools in `cortana/perception.py`: `capture`,
  `ocr`, `change-detect`, `extract_meaning`.
- **Code:** `Observation`/`Semantic` dataclasses; `normalize()` (strip clock/
  whitespace) → `content_hash()` → `changed()`; `build_meaning_prompt()` +
  `extract_meaning(batch, backend)` (sync, backend-agnostic); native
  `capture_screen()`/`ocr_image()` behind lazy imports. The agent loop (Phase 3)
  uses `changed()` to skip unchanged frames (`skip_reason="unchanged"`, no LLM).
- **Tests:** clock-only diff → same hash (no re-perceive); fake-OCR → fake-meaning
  contract; idle loop ≥70% fewer `extract_meaning` calls. *(Absorbs old M3 +
  capture/OCR/LLM surface reframed as tools.)*

### Phase 3 — The agent loop *(perceive → meaning → memory)*
- **Capability:** Cortana's continuous, non-blocking background loop; no silent
  memory loss under load.
- **Code:** the producer/consumer reframed as `agent_loop()` composing Phase-2 tools
  + Phase-1 `Memory`. Visible backpressure: on `QueueFull`, persist the evicted
  perception (`skip_reason="dropped_backpressure"`, no LLM) + `drops_total` counter.
- **Tests:** forced overload → `perceived == remembered + dropped_counted`; cadence
  holds p95 ±10% with a slow fake LLM. *(Absorbs old M5.)*

### Phase 4 — Recall & reasoning *(Cortana the assistant)*
- **Capability:** `cortana ask "<question>"` → retrieve relevant memory (FTS + time
  filters) → LLM reasons → **grounded answer with citations** (ts + app). Read-only.
- **Code:** a `reason(question) -> Answer` path: `Memory.recall()` → context-assembly
  → LLM → answer + source rows. CLI subcommand `ask`.
- **Tests:** eval set of Q→expected-recall (deterministic via fake LLM asserting the
  *retrieved set*, not the prose); citation points to a real row.
- **Scope guard:** thin RAG-over-memory only — NOT a generic ReAct/tool-runtime.

### Phase 5 — Privacy & safety guardrails *(cross-cutting; gates real-data runs)*
- **Capability:** nothing sensitive enters memory; at-rest posture explicit.
- **Code:** `_REDACTION_PATTERNS` + `redact()` (keys, `AKIA…`/`sk-…`/`ghp_…`/Bearer,
  Luhn cards, SSN) applied to `ocr_text`+`focused_text` **before** `remember()` and
  before the prompt; `--no-redact`. Existing secure-input + bundle exclusions kept.
  README at-rest FileVault note (Decision 3).
- **Tests:** each secret redacted; **benign text untouched** (false-positive guard).
- **Gate:** must land before the loop runs on real screen data. *(Old M6 privacy half.)*

### Phase 6 — Defaults & ops *(always-on)*
- **Capability:** runs as a feasible, observable daemon.
- **Code:** default model `qwen2.5:7b-instruct` (72B opt-in);
  `dist/com.cortana.tracker.plist` (RunAtLoad+KeepAlive) + `install.sh`; `Metrics`
  (queue depth, LLM p95, OCR fail rate, drops) logged every 10 min.
- **Tests:** percentile math. *(Old M6 model default + M7.)*

### Phase 7 — Advanced agency *(deferred — only when a concrete need appears)*
Effector tools that *act* (proactive suggestions/automation, with confirmation) ·
embedding/semantic memory + **consolidation** (daily digests, summary-of-summaries)
· ScreenCaptureKit (with `CGWindowListCreateImage` fallback) · SQLCipher
`--encrypt` + Keychain key · perceptual image hash (skip OCR too) · multi-display
attribution · Ollama retry/backoff.

## Verification Strategy

- **Unit (pure logic):** normalize+hash, redaction regexes (positive *and*
  false-positive cases), prompt building, batch collect/timeout, backpressure
  eviction accounting, retention pruning. Mock the Quartz/Vision/AppKit seam.
- **Property:** `stored + dropped_counted == captured` (no silent loss); `summary
  rows == batches`; `len(stored ocr_text) ≤ cap`; `count(context_fts) ==
  count(context)`.
- **Integration (golden path):** the vertical slice against a fake backend + temp
  DB; assert FTS recall and FK join.
- **Migration:** seed a legacy v0 DB, upgrade, assert old rows readable + new schema live.
- **Load:** slow fake backend saturates the LLM; assert cadence holds and drops counted.
- **Manual/observed:** run under `launchd` a day; inspect metrics + DB growth (`/verify`).

## Deferred Complexity

ScreenCaptureKit, perceptual image hashing, SQLCipher, multi-display attribution,
adaptive batch sizing, query UI, semantic/embedding search. None blocks v1; each is
added only when a concrete need appears.

## Recommended Next Step

Approve this roadmap, then build the **agentic vertical slice** — `Phase 0`
(testing spine) → the perceive→remember→recall thread through `Phase 1` (Memory) →
`Phase 2` (tools) → `Phase 3` (loop) → `Phase 4` (recall), with `Phase 5`
redaction landing before any real-data run. Hand to `principal-production-engineer`
(or run elves with this file as the frozen `DESIGN.md`). Start with Phase 0 so
every subsystem lands behind tests. Do **not** start `Phase 7` or any optimization
before the vertical slice is green.

## Decisions (all confirmed)

All five decisions are locked; this seam is ready to freeze.

- Decisions 1, 2, 4, 5 — resolved with the defaults in §Decisions Resolved.
- **At-rest encryption (Decision 3) — CONFIRMED by owner:** v1 = precision
  redaction + documented FileVault reliance; SQLCipher deferred to **Phase 7** as an
  opt-in `--encrypt` flag. Redaction is the load-bearing v1 privacy control, so
  **Phase 5** must hold the false-positive bar (benign text untouched).
