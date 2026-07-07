# Architecture & Memory System Review

> Principal-engineer review of Cortana's architecture and memory system
> (2026-07-07, branch `feat/chat-webui`). Method: two independent fresh-context
> adversarial reviewers (memory correctness; app architecture), every finding
> **verified against source** before acting — false positives rejected and recorded.
> All P0/P1 fixes in this document are **implemented and tested** in the same
> change-set (231 tests green, coverage ≥ 95% gate).

## Verdict

**Ship with fixes (now applied).** The architecture is sound — one memory hub with
a single writer, tiered working/episodic/semantic memory, testable pure logic with
thin native shells, visible failure semantics. The review found one genuine **P0
privacy bug**, a cluster of **P1 correctness/robustness bugs** (mostly integration
seams: features built but not wired, or wired but unsafe under load), and useful P2
hygiene. Nothing required structural redesign.

---

## 1. Fixed in this pass (verified real → patched + regression-tested)

### P0 — privacy
| # | Finding | Fix |
|---|---|---|
| 1 | **`excluded_bundles` was dead config.** The TOML promised password-manager windows are excluded; nothing consumed the setting — Keychain/1Password screens were captured, OCR'd, and stored in plaintext. | `ScreenSensor` now takes `excluded_bundles` and returns a metadata-only `skip_reason="excluded_app"` observation without capturing; wired from config in `AgentLoop`. Regression test. |

### P1 — correctness / robustness
| # | Finding | Fix |
|---|---|---|
| 2 | **Reflections were write-only dead data.** `cortana digest` stored reflections nothing ever read — `recall`/`ask`/chat never touched the table ("the feature was theater"). | `reason()` now prepends `recent_reflections(3)` into the answer prompt as a labeled block. |
| 3 | **Reflections were unbounded** (violated "every tier has a cap"). | `prune()` deletes reflections older than `retention_days`. |
| 4 | **Embedding-dimension mismatch silently corrupted ranking.** `cosine` used `zip()`, which truncates: switching `embed_model` compared incompatible vector spaces and produced garbage similarities with no error. | `cosine` raises `ValueError` on dimension mismatch (message points at `embed_model`). |
| 5 | **Query embedding ran while holding the read lock** — a hung Ollama (30s timeout) would block every reader. | Embed **before** acquiring the lock; pass the vector into `_hybrid_ids`. |
| 6 | **Observation embedding ran inside the open write transaction** — a hung embedder held the write txn, stalling backpressure persistence and shutdown; no cross-process `busy_timeout` anywhere. | Embed **before** `with self._conn` (best-effort, logged); `PRAGMA busy_timeout=5000` on every connection. |
| 7 | **Capacity mismatch: 1s capture × `batch_size=4` × multi-second 7B ⇒ permanent backpressure.** Under sustained activity the queue fills in minutes and evicts forever. | `_collect_batch` now **drains the backlog adaptively** (up to 4× batch_size) so one LLM call absorbs it; per-event prompt budget scales down so total prompt stays ~constant. Cadence test updated to assert the new contract (all rows stored, *fewer* LLM calls). |
| 8 | **Chat never saw working memory.** "What am I doing *right now*" was answered from the DB, which lags by batch+LLM latency (up to minutes under load), while a fresh in-RAM answer existed in-process. | `route`/`make_handler`/`serve` accept `working_memory`; the desktop passes the shared buffer; a "LIVE: what is on the user's screen right now" block precedes the retrieved-memory context. |
| 9 | **Desktop quit lost data.** rumps' default quit killed the daemon tracking thread mid-batch: queued observations lost, connections never closed, no drain. | Custom Quit menu item: `controller.stop()` (drains + closes writer) → `read_memory.close()` → `rumps.quit_application()`. |
| 10 | **Recommendation LLM call beachballed the menu bar** (synchronous multi-second `generate` on the rumps main thread). | Runs on a worker thread; the alert shows from the follow-up. |
| 11 | **Migration crash-window left a permanently FTS-less DB.** A crash between fresh-schema scripts routed the retry down the legacy path, which skipped FTS creation, then stamped `user_version=4` — recall would raise forever. | The else-branch now guards `context_fts` (create + rebuild) exactly like embeddings/reflections. |
| 12 | **WorkingMemory survived Stop Tracking** — a dead session's data was served as "current activity" by the advisor. | `_TrackingService.stop()` clears the shared buffer. |

### P2 — quality / hygiene
| # | Finding | Fix |
|---|---|---|
| 13 | Consolidation fed the LLM a **reverse-chronological** log and repeated each batch summary once per event (wasted prompt budget). | Chronological order + dedup by `summary_id`. Tested. |
| 14 | Hybrid-recall failures were swallowed silently (`except: pass`) — a down embedder looked identical to a working system. | Logged warning on keyword fallback; embed-skip in `remember` also logs. |
| 15 | **`read_focused_text` was dead config** (mapped, shipped in TOML, never read). | Removed from Config + TOML — a silent no-op setting is worse than none. (`Observation.focused_text` stays; redaction already covers it if a future sensor sets it.) |
| 16 | `chatapp.py` had a mid-file import (merge artifact; STYLE violation). | Moved to top-level imports. |

## 2. Claims verified and REJECTED (do not "fix")

- **"recall's quoted-phrase FTS retry mutates params unsafely."** False — `params[0]`
  is always the query when a query exists; the list is local, no aliasing.
- **"Hybrid branches apply filters inconsistently."** False — `_filter_clauses` is
  applied to both the FTS and semantic branches, parameter order verified;
  `_rows_by_ids` preserves fused order.
- **"Two-connection WAL setup risks stale reads."** Not reachable — the desktop's
  reader is a separate connection; WAL gives committed-read freshness; the reader
  lock serializes the shared chat connection. (The cross-process `SQLITE_BUSY`
  hazard was real and is fixed via `busy_timeout`, #6.)
- **`period_start`/`period_end` in consolidation** were already correct for the
  DESC recall order (only the *prompt* order was wrong — fixed as #13).

## 3. Remaining recommendations (not done — prioritized)

1. **(P1) Surface tracking-thread death in the menu.** If the loop crashes (e.g.
   prune raises on a locked DB near the 2 GiB cap), the thread dies silently while
   the menu still shows "⏸ Stop Tracking". Wrap `_TrackingService._run` and flip the
   controller state + a menu badge on failure. *(Small; needs a Mac to verify the
   rumps side, which is why it wasn't done blind.)*
2. **(P1) Consolidate the four prompt-builders.** `reasoning`/`advisor`/
   `consolidation`/`chatapp` each format `[{ts}] app=…: body` with drifted caps
   (400 vs 300 chars) and empty-case handling. One `format_memory_lines(memories,
   cap)` helper ends the drift.
3. **(P2) Auto-schedule consolidation.** `cortana digest` is manual; run it from the
   loop's `_maintenance` daily so reflections accumulate without user action.
4. **(P2) Keyword-only recall ignores BM25 rank** (`ORDER BY ts DESC` even under
   `MATCH`). Fine for recency-flavored use; hybrid path ranks properly. Consider
   `ORDER BY rank` + a recency tiebreak for the keyword path.
5. **(P2) `prune()` size estimate measures pre-VACUUM page count** (over-deletes
   somewhat after age-pruning; conservative direction). Use `page_count −
   freelist_count`, and account for embeddings JSON (~15 KB/row) in the average.
6. **(P2) Embeddings lack a model tag.** #4 makes a model switch *loud*; a `model`
   column + filter (or a re-index command) would make it *seamless*.
7. **(P3) Dead code sweep:** `Memory.forget`/`counts` (test-only), `WorkingMemory.
   since`, `AgentLoop.drops_total`, `Conversation.clear` (no route exposes it) —
   keep-or-cut decision; stale docstrings (`cli.py` header lists 3 of 7 subcommands;
   `chatapp.py` still calls chat "a separate surface").
8. **(P3) `run --demo` without `--ticks` runs unbounded at 20 Hz;** bound it or warn.
9. **(P3) Desktop `ImportError` handler** blames missing extras for *any* import
   failure inside `run_app`; check `exc.name in {"rumps", "webview"}`.

## 4. Architecture assessment (what's sound — keep)

- **One memory hub, one writer.** All writes funnel through the loop's single db
  executor; readers are isolated (separate connection + lock). Confirmed correct.
- **Tiered memory** (queue → WorkingMemory → episodic/semantic SQLite → reflections)
  matches the documented design; every tier now genuinely bounded.
- **Visible failure** discipline held everywhere it was checked (backpressure is
  persisted + counted; LLM failure degrades to store-without-summary; task_done in
  `finally`), with the silent-fallback exceptions found now fixed (#14).
- **Testable-core / native-shell split** continues to pay off: every fix in §1 except
  the two rumps ones landed with a hermetic regression test.

## 5. Verification

- `./ci/run.sh` — **231 passed**, branch coverage above the 95% gate, hermetic
  (no PyObjC/Ollama/network).
- New regression tests: `tests/test_review_fixes.py` (privacy exclusion, cosine
  guard, reflections-in-prompt, reflection pruning, backlog drain, live chat
  context, consolidation ordering) plus the updated cadence test.
- **Not verified here (needs a real Mac):** the rumps quit path, worker-thread
  alert, and live sensor exclusion — the native shell is `# pragma: no cover` by
  design; the logic behind each is tested.
