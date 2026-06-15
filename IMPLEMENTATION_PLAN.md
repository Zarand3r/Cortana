# IMPLEMENTATION_PLAN.md — Cortana Phases 1 & 2 (Memory + Perception)

Execution plan for the **Memory** (Phase 1) and **Perception/meaning-extraction**
(Phase 2) subsystems of the Cortana agent, plus the **Phase 0** test spine that TDD
requires. Derived from the locked `docs/DESIGN.md`. Coding agents execute steps
top-to-bottom; tests precede implementation in every step.

**Pre-flight recommendation: BUILD (new package), not migrate-in-place.** The repo
has one working but untested ~600-line script (`context_tracker.py`) that conflates
all subsystems and is unimportable without PyObjC. The agent architecture wants
separable, testable subsystems. So we **build a new `cortana/` package** (Memory,
Perception, backends) behind lazy native imports, fully TDD'd, and **leave
`context_tracker.py` running untouched**. Phase 3 (out of scope here) rewires the
legacy entrypoint onto the package and deletes the duplicated logic — classic
delete-after-verify, gated on the package being green.

- **Already aligned (keep, do not rebuild):** the v0 pipeline shape, privacy
  reflexes, single-writer discipline. Reused conceptually; not imported yet.
- **Violates new doctrine (excised in Phase 3, not now):** `ContextDB` (denormalized
  summary), per-row summary writes, prompt/backend logic living inside the script.

---

## The steps at a glance

- [x] **Step 0 — Foundation.** `cortana/` package skeleton, `tests/` + `conftest`, `check.sh`, `requirements-dev.txt`, `pytest.ini`. Test infra only.
- [x] **Step 1 — Lock the contract.** `Observation` + `Semantic` dataclasses; `FakeLLMBackend` + `make_backend("fake")`. Schema/types compile + round-trip.
- [x] **Step 2 — Perception pure logic (Phase 2 slice).** `normalize()` → `content_hash()` → `changed()`; `build_meaning_prompt()`; `extract_meaning(batch, backend)`.
- [x] **Step 3 — Memory write path (Phase 1 slice A).** Fresh schema (WAL, FTS5, triggers); `remember()` storing summary once via FK; truncation; `remember_dropped()`.
- [x] **Step 4 — Memory recall (Phase 1 slice B).** `recall(query, since, until, app)` via FTS5 join + filters; FTS-sync-on-delete invariant.
- [x] **Step 5 — Memory bounds + migration (Phase 1 slice C).** `forget()`/`prune()` (age + size + orphan summaries); legacy-DB migration `user_version` 0→2.
- [x] **Step 6 — Golden-path integration.** perceive(fake)→change-detect→extract_meaning(fake)→remember→recall-with-citation. The spine.

> **STATUS: COMPLETE.** All 6 steps green — 35 tests, hermetic (no PyObjC/Ollama). See §"Definition of done".

```
0 ──▶ 1 ─┬─▶ 2 ─────────────┐
         └─▶ 3 ──▶ 4 ──▶ 5 ─┴─▶ 6
```
Critical path: 0 → 1 → 3 → 4 → 5 → 6. Step 2 is parallel after Step 1.

---

## Properties to preserve (gates, not aspirations)

### P1 — Summary stored once
**Invariant:** one `summaries` row per batch; every event in the batch references it by `summary_id`; no summary text is duplicated across `context` rows.
**Forbids:** a `summary` text column on the fresh `context` table; N identical summary strings.
**Allowed:** legacy migrated rows retaining their old read-only `summary` column.
**Proved by:** Step 3 — `test_remember_one_summary_per_batch`.

### P2 — FTS mirrors context exactly
**Invariant:** `count(context_fts) == count(context)` at all times, including after deletes.
**Forbids:** orphaned FTS rows after `forget`/`prune`; stale index entries.
**Proved by:** Step 4 — `test_fts_sync_on_delete`; Step 5 — `test_prune_keeps_fts_in_sync`; Step 6 migration rebuild.

### P3 — Stored OCR is bounded
**Invariant:** every stored `ocr_text` has length ≤ `ocr_max_chars`.
**Forbids:** persisting raw untruncated OCR.
**Proved by:** Step 3 — `test_ocr_truncated_at_store`.

### P4 — Memory is bounded by retention
**Invariant:** after `prune()`, no row older than `retention_days`, and DB size trends ≤ `max_db_bytes` (oldest-first eviction); no orphan summaries remain.
**Proved by:** Step 5 — `test_prune_by_age`, `test_prune_by_size`, `test_orphan_summaries_pruned`.

### P5 — Recall returns the right rows with citations
**Invariant:** `recall(query)` returns rows whose OCR matches the term, filtered by time/app, each carrying its `ts` + `app_name` (a citation).
**Proved by:** Step 4 — `test_recall_by_keyword`, `test_recall_time_and_app_filters`.

### P6 — Change-detection ignores volatile churn
**Invariant:** two frames differing only in clock/date tokens hash equal ⇒ `changed()` is False ⇒ no `extract_meaning` call.
**Forbids:** re-summarizing an idle screen because the menu-bar clock ticked.
**Proved by:** Step 2 — `test_normalize_strips_clock`, `test_idle_loop_skips_extraction` (≥70% fewer calls).

### P7 — Pure logic imports without native deps
**Invariant:** `import cortana.memory`, `cortana.perception`, `cortana.backends` succeed with no PyObjC/Ollama installed.
**Forbids:** module-level `import Quartz/Vision/AppKit`.
**Proved by:** Step 0 — `test_imports_are_hermetic`; enforced by the whole suite running without PyObjC.

### P8 — Migration is additive and lossless
**Invariant:** opening a legacy v0 DB upgrades it to `user_version=2` with old rows still readable and FTS rebuilt to match.
**Proved by:** Step 5 — `test_legacy_migration`.

---

## How to execute

1. **Tests first, always.** Write the step's test file and watch it fail before implementing.
2. **Vertical slices.** Each step leaves the package importable and green. No "build all of Memory then test."
3. **Binary acceptance.** Every step ends with `./check.sh` green + the named tests passing. Greps that must return empty are acceptance gates.
4. **Rewrite-when-easier.** If a module fights the test, rewrite that file from scratch against the test — not scope creep.
5. **Commit per step** with the step number in the message.
6. **Iteration loop:** see §B. **Stuck >30 min:** stop, print expected vs actual, re-read Acceptance, consider a fresh rewrite of the one file.

---

## Step 0 — Foundation

**Goal:** a hermetic test harness and an importable package skeleton.
**Why now:** TDD is impossible without a runner that works with zero native deps.

### Tests first
- [ ] `tests/test_imports_are_hermetic.py` — `import cortana.memory`, `cortana.perception`, `cortana.backends`, `cortana.config` all succeed (proves P7).

### Implementation
- [ ] `cortana/__init__.py`, `cortana/config.py` (`Config` dataclass with `ocr_max_chars`, `retention_days`, `max_db_bytes`, model defaults), empty `cortana/memory.py`, `cortana/perception.py`, `cortana/backends.py`.
- [ ] `requirements-dev.txt` (`pytest`), `pytest.ini` (`testpaths = tests`), `check.sh` (`#!/usr/bin/env bash; set -euo pipefail; .venv/bin/python -m pytest -q`).

### Integration check
- [ ] `./check.sh` runs and collects the one test green.

### Acceptance
- [ ] `./check.sh` exits 0 with no PyObjC installed.
- [ ] `grep -rnE "^import (Quartz|Vision)|^from (AppKit|Quartz|Vision)" cortana/` returns empty.

**Depends on:** none.

## Step 1 — Lock the contract

**Goal:** freeze the data shapes and the backend seam.
**Why now:** every later step references `Observation`/`Semantic` and a backend; lock them before logic.

### Tests first
- [ ] `tests/test_backends.py` — `FakeLLMBackend.generate(prompt)` returns its canned string and increments `.calls`; `make_backend("fake")` returns a `FakeLLMBackend`.
- [ ] `tests/test_contract.py` — `Observation` and `Semantic` construct with expected fields; defaults (`skip_reason=""`, `content_hash=""`).

### Implementation
- [ ] `cortana/perception.py`: `@dataclass(slots=True) Observation(ts, app_name, bundle_id, window_title, ocr_text, captured, skip_reason="", focused_text="", content_hash="")`; `Semantic(summary, model, window_start_ts, window_end_ts)`.
- [ ] `cortana/backends.py`: `FakeLLMBackend(response, model="fake")` with sync `generate()` + `.calls`; `OllamaBackend`/`MLXBackend` with sync `generate()` (native/network, untested); `make_backend(name, cfg=None)` incl. `"fake"`.

### Integration check
- [ ] `./check.sh` green.

### Acceptance
- [ ] `test_backends.py` + `test_contract.py` pass.

**Depends on:** 0.

## Step 2 — Perception pure logic *(Phase 2)*

**Goal:** the senses' pure, testable core: normalize → hash → changed; prompt; extract_meaning.
**Why now:** independent of Memory; unblocks the idle-skip property and the integration spine.

### Tests first
- [ ] `tests/test_perception.py`:
  - `test_normalize_strips_clock` — same screen with `12:34` vs `12:35` → equal normalized string (P6).
  - `test_content_hash_stable_and_sensitive` — identical content → equal hash; different content → different hash.
  - `test_changed` — `changed(None, h)` True; `changed(h, h)` False.
  - `test_build_meaning_prompt` — contains app name + window title; truncates each OCR to `max_chars` with an ellipsis marker.
  - `test_extract_meaning_uses_backend` — with `FakeLLMBackend`, returns `Semantic(summary=canned, model="fake", window_start_ts=batch[0].ts, window_end_ts=batch[-1].ts)`; `backend.calls == 1`.
  - `test_idle_loop_skips_extraction` — simulate 10 identical frames through `changed()`-gated extraction; assert `extract_meaning` called once (≥70% reduction) (P6).

### Implementation
- [ ] `normalize(app, title, ocr)` (lowercase, strip clock/date tokens via regex, collapse whitespace); `content_hash(app, title, ocr)` (sha256 of normalized); `changed(prev_hash, new_hash)`.
- [ ] `SYSTEM_PROMPT`, `build_meaning_prompt(batch, max_chars)`, `extract_meaning(batch, backend, max_chars)`.
- [ ] Native `capture_screen()`, `ocr_image()`, `frontmost_app()`, `active_window_title()` with **lazy** `import` inside the function bodies (not unit-tested).

### Integration check
- [ ] `./check.sh` green.

### Acceptance
- [ ] All `test_perception.py` pass; P6 tests green.
- [ ] `grep -nE "^import (Quartz|Vision|AppKit)" cortana/perception.py` empty (lazy only).

**Depends on:** 1.

## Step 3 — Memory write path *(Phase 1, slice A)*

**Goal:** create the normalized schema and `remember()` that stores a summary once.
**Why now:** the central data-model fix; everything downstream reads what this writes.

### Tests first
- [ ] `tests/test_memory_write.py`:
  - `test_fresh_schema` — new DB → `PRAGMA user_version == 2`; tables `summaries`, `context`, `context_fts` exist; fresh `context` has **no** `summary` text column.
  - `test_remember_one_summary_per_batch` — `remember([3 obs], semantic)` → 1 summaries row, 3 context rows, all sharing one `summary_id` (P1).
  - `test_ocr_truncated_at_store` — `ocr_max_chars=20`, long OCR → stored length ≤ 20 (P3).
  - `test_remember_dropped` — `remember_dropped(obs)` → one context row, `skip_reason="dropped_backpressure"`, `summary_id IS NULL`.
  - `test_fts_populated_on_insert` — after `remember`, `count(context_fts) == count(context)` (P2).

### Implementation
- [ ] `cortana/memory.py::Memory(path, *, ocr_max_chars, retention_days, max_db_bytes)`: connect, `PRAGMA journal_mode=WAL`, `foreign_keys=ON`; `migrate()` creates fresh schema + triggers when absent, sets `user_version=2`.
- [ ] `remember(observations, semantic) -> summary_id|None`; `remember_dropped(observation)`; `counts()`; `close()`.

### Integration check
- [ ] `./check.sh` green.

### Acceptance
- [ ] All `test_memory_write.py` pass; P1, P2 (insert), P3 green.

**Depends on:** 1.

## Step 4 — Memory recall *(Phase 1, slice B)*

**Goal:** `recall()` over FTS + filters, and prove FTS stays synced on delete.
**Why now:** "searchable memory" is the capability; the delete-sync invariant must hold before pruning exists.

### Tests first
- [ ] `tests/test_memory_recall.py`:
  - `test_recall_by_keyword` — remember rows incl. OCR "quarterly budget"; `recall(query="budget")` returns it; non-matching term returns none (P5).
  - `test_recall_time_and_app_filters` — `since/until/app` narrow results correctly (P5).
  - `test_recall_returns_citation` — each result carries `ts` + `app_name`.
  - `test_fts_sync_on_delete` — delete a context row directly; `count(context_fts) == count(context)` (P2).

### Implementation
- [ ] `recall(query=None, since=None, until=None, app=None, limit=50) -> list[dict]`: FTS5 `MATCH` join when `query`, else filtered select; ordered `ts DESC`.

### Integration check
- [ ] `./check.sh` green.

### Acceptance
- [ ] All `test_memory_recall.py` pass; P2 (delete), P5 green.

**Depends on:** 3.

## Step 5 — Memory bounds + migration *(Phase 1, slice C)*

**Goal:** retention (`forget`/`prune`) and legacy-DB upgrade.
**Why now:** unbounded growth is a P0 risk; migration protects existing users' data.

### Tests first
- [ ] `tests/test_memory_prune.py`:
  - `test_prune_by_age` — rows older than `retention_days` removed; recent kept (P4).
  - `test_prune_by_size` — tiny `max_db_bytes` → oldest rows evicted until under cap (P4).
  - `test_orphan_summaries_pruned` — deleting all events of a summary removes the summary (P4).
  - `test_prune_keeps_fts_in_sync` — after prune, `count(context_fts) == count(context)` (P2).
- [ ] `tests/test_migration.py`:
  - `test_legacy_migration` — seed a v0 DB (old `context` w/ `summary`, `user_version=0`, rows) → open with `Memory` → `user_version==2`, `summaries`/`context_fts` exist, old rows readable, `content_hash`/`summary_id` columns present, `count(context_fts)==count(context)` (P8).

### Implementation
- [ ] `forget(older_than) -> int`, `prune() -> int` (age, then size loop oldest-first, then orphan summaries).
- [ ] `migrate()` legacy branch: detect old `context` (no `summary_id`) → `ALTER TABLE` add `content_hash`,`summary_id`; create `summaries`,`context_fts`,triggers; `INSERT INTO context_fts(context_fts) VALUES('rebuild')`; set `user_version=2`.

### Integration check
- [ ] `./check.sh` green.

### Acceptance
- [ ] All `test_memory_prune.py` + `test_migration.py` pass; P4, P8 green.

**Depends on:** 4.

## Step 6 — Golden-path integration

**Goal:** prove the Phase 1+2 spine end-to-end in one test.
**Why now:** integration is the regression spine; it must pass after every later change.

### Tests first
- [ ] `tests/test_integration.py` (§A): fixed fixture of fake `Observation`s (one changed screen + duplicates) → gate with `changed()` → `extract_meaning(FakeLLMBackend)` → `remember()` → `recall(query)` returns the row with a citation, and `extract_meaning` was called once for the duplicate run.

### Implementation
- [ ] None beyond wiring the existing functions in the test (compose-only).

### Integration check
- [ ] `./check.sh` green; full suite green.

### Acceptance
- [ ] `test_integration.py` passes; P1, P2, P5, P6 all exercised in one path.

**Depends on:** 2, 5.

---

## Definition of done (Phases 1 & 2)

- [ ] `./check.sh` green; all property tests P1–P8 pass with no PyObjC/Ollama installed.
- [ ] `cortana/memory.py` + `cortana/perception.py` + `cortana/backends.py` deliver the Memory and Perception interfaces from `docs/DESIGN.md`.
- [ ] Golden-path integration test (§A) green.
- [ ] `context_tracker.py` still present and unmodified (Phase 3 rewires it).
- [ ] Committed per step with passing tests.

## §A — Golden-path integration test (the spine)

```
GIVEN  a fixed list of fake Observations: one "writing the Q2 budget" screen,
       then 4 byte-identical duplicates, then one "reading email" screen
WHEN   the pipeline runs: changed()-gate → extract_meaning(FakeLLMBackend) → remember() → recall()
THEN   recall("budget") returns the budget row, carrying ts + app_name (citation)
AND    extract_meaning was called exactly twice (once per distinct screen, not 6x)
AND    count(context_fts) == count(context)
AND    summaries row count == number of remembered batches
```

## §B — Iteration loop

```
Read failing assertion verbatim
  → Is the test's invariant correct?
      No  → fix the test, note why
      Yes → fix impl (minimum change OR rewrite the one file fresh)
  → re-run the failing test → run ./check.sh → green ⇒ done
Stuck >30 min: stop, print expected vs observed, re-read Acceptance,
consider rewriting the single file from scratch against the test.
```

## §C — Out of scope (Phases 1 & 2)

- The agent loop / async producer-consumer rewiring (Phase 3).
- `cortana ask` recall-&-reasoning CLI (Phase 4).
- Redaction patterns + `--no-redact` (Phase 5) — Memory truncates but does not yet redact.
- Default-model swap, launchd, metrics (Phase 6); SQLCipher, ScreenCaptureKit (Phase 7).
- Deleting `ContextDB` / rewiring `context_tracker.py` (Phase 3, delete-after-verify).
- Real screenshot/OCR/LLM execution (native paths exist but are not unit-tested).

## §D — Design tensions surfaced for review

**D1. Package vs. single file.** The plan introduces a `cortana/` package while the
design's "stay few-file" soft constraint and the running `context_tracker.py` remain.
*Options:* (a) package now, rewire legacy in Phase 3 *(recommended — matches the agent
subsystem decomposition and is far more testable)*; (b) keep everything in one file.
**Recommendation: (a).** Temporary duplication of backend/prompt logic is resolved by
delete-after-verify in Phase 3.

**D2. `extract_meaning` sync vs async.** v0's backend is `async summarize(loop,
executor, prompt)`. This plan makes the **tool** layer sync (`generate`/`extract_meaning`)
and defers async/executor wrapping to the Phase-3 loop. *Recommendation:* sync tools,
async loop — cleaner tests, single place that owns concurrency.

**D3. Change-detection scope of `normalize`.** Stripping clock/date tokens kills the
common idle-churn but won't catch a progress bar or blinking cursor that changes a few
chars. *Recommendation:* ship token-strip now (P6); the Jaccard-similarity upgrade is a
documented v2 item, not v1.
