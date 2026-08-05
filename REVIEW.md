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

## 1b. Second review pass (2026-07-08) — fixed

Two fresh-context adversarial reviewers (desktop lifecycle/concurrency; memory/
retrieval correctness), same verify-against-source discipline. Three confirmed
real, patched + regression-tested; the rest rejected (see §2b).

| # | Sev | Finding | Fix |
|---|---|---|---|
| 17 | **P0** | **One bad-dimension embedding silently killed ALL hybrid recall.** `_hybrid_ids` scored vectors in a list-comp `cosine(qvec, json.loads(vec))`; `cosine` raises `ValueError` on a dimension mismatch (a vector from a changed `embed_model`, or a short/empty vector from a bad Ollama response that `remember` still stored). The raise propagated to `recall`'s broad `except`, which demoted **every** hybrid query to keyword-only for the life of that bad row. | Score in a loop; `except ValueError: continue` skips the incompatible row and keeps the rest. Semantic ranking degrades gracefully instead of vanishing. Regression test seeds a wrong-dim vector and asserts a keyword-only-miss query still hits via semantic. |
| 18 | **P0** (UX) | **Stopping beachballed the menu bar.** `_TrackingService.stop()` joined the tracking thread (`timeout = batch_window + 30`) on the caller's thread — and every stop path (toggle-off, window-close `_sync`, Quit) ran on the rumps **main thread**. A mid-flight LLM call froze the menu for seconds. | `stop()` is now non-blocking (schedule cancel + clear working memory, return). The daemon thread drains + closes the writer in `AgentLoop.run`'s `finally`. A restart waits for the previous writer to close **inside the new background thread** (`_prev.join()` in `_run`) — never opening a 2nd SQLite writer, never blocking the UI. Quit calls a new `service.join()` on a **worker** thread, then `AppHelper.callAfter(rumps.quit_application)`. (Native/`pragma`; verified by reasoning, not a hermetic test.) |
| 19 | **P1** | **`reason()` injected reflection text uncapped** while retrieved memories were capped at `_PER_MEMORY_CHARS` — a long consolidated reflection could dominate/blow the prompt budget. | `build_answer_prompt` now truncates each reflection with `[:_PER_MEMORY_CHARS]`, matching the memory lines. Regression test. |
| 20 | **P1** | **`Memory.close()` raced in-flight chat recall.** The desktop closes the shared read connection on Quit while the chat server's request threads may still be querying it; `close()` didn't take the read lock the recall path holds. | `close()` now acquires `self._lock` before closing. Deterministic regression test asserts `close()` blocks while the lock is held and proceeds when freed. |

## 2b. Second-pass claims verified and REJECTED

- **"`_TrackingService` start/stop cancel is a no-op race" (`_cancel` runs before
  `_task` is set).** False — `_run` creates `self._task` synchronously *before*
  `run_until_complete` starts the loop, and `call_soon_threadsafe(_cancel)` only
  executes once the loop runs, so `_task` is always set when `_cancel` fires.
- **"Window-close leaves a mixed state — chat server + `read_memory` stay alive."**
  Working as intended: the chat server is a process-lifetime daemon; with no window
  pointed at it it's inert, and a later Start reuses it. The "one state" contract is
  about *tracking ⇔ window*, which `sync` upholds. Not a bug.
- **"Adaptive drain violates the ≈-constant prompt budget at the 600-char floor."**
  Real but intended: the floor trades a bounded prompt-size increase for not dropping
  observations under a burst. Documented in REVIEW #7; no change.
- **Also re-confirmed clean:** `remember()` index↔vector alignment, migration
  idempotency, consolidation ordering, `WorkingMemory` locking, restart reusing a
  closed loop (Start assigns a fresh loop).

## 1c. Third review pass (2026-07-21) — production/packaging branch

Scope: everything on `feat/production-single-artifact` (PR #8) — the first-run
runtime, desktop lifecycle changes, window-title sensor fix, auto-consolidation,
and the entire py2app/sign/notarize pipeline. Method as before: two fresh-context
adversarial reviewers (product code; packaging/build — the latter verified its
claims *empirically*: a live `codesign --deep` experiment, `otool` sweeps of the
built bundle, py2app source reading), plus build-pipeline findings from actually
running the build. Every finding verified against source before fixing.

### Fixed — product code (all regression-tested where testable)
| # | Sev | Finding | Fix |
|---|---|---|---|
| 21 | **P0** | **Eager MLX load defeated the whole first-run flow.** `MLXBackend.__init__` called `mlx_lm.load()`; `run_app` constructs the backend before the menu exists → first launch = a silent multi-minute download with no UI (or a dead app offline). The provisioning state machine never got to run. | `MLXBackend` is now lazy (loads on first use, after the ready-gate) and **serialized** (an internal lock — mlx-lm is not thread-safe across tracker/chat/consolidation). |
| 22 | **P0** | **`stop()` after a tracker crash raised `RuntimeError: Event loop is closed`** — the crash badge never appeared, the menu wedged "on", and Quit stopped working (shutdown thread died on the same raise). | `stop()` guards `call_soon_threadsafe` against a closed loop. |
| 23 | **P0** | **A startup crash was never flagged**: `make_loop` ran *outside* `_run`'s try, so a model/DB failure killed the thread silently with `healthy()` still True — the exact case the health predicate was built for. | Whole `_run` body inside try/except; memory close guarded. |
| 24 | **P1** | **Stale `_failed` killed fresh restarts**: reset happened inside `_run` *after* the multi-second model load; the 2s menu sync saw the old crash flag and stopped the new session. Restart-after-crash could never succeed under MLX. | `_failed` reset synchronously in `start()`. Also (P2) `stop()` nulling `self._loop` mid-startup could crash `_run`: the loop is now passed to the thread by value. |
| 25 | **P1** | **Source runs were bricked by provisioning**: `_provision` HF-cache-checked the *Ollama* tag, then `snapshot_download("qwen2.5:7b-instruct")` → `HFValidationError` (or ImportError) → "Setup failed", Start gated forever — with Ollama running fine. | Model provisioning now applies only to `backend == "mlx"`. |
| 26 | **P1** | **Denied Screen Recording still reported "Cortana is ready"** — tracking then recorded nothing, silently (every capture `captured=False`). | Grant re-checked after the request; if absent, ready is NOT set and the status says grant + relaunch (macOS applies the grant on relaunch). |
| 27 | **P1** | **Consolidation ran the LLM generate on the db executor** — a minutes-long generate stalls every write, including backpressure persistence: capture halts. | `_AsyncMemory.consolidate` splits stages: recall/write on the db pool, generate on the llm pool. Thread-asserting regression test. |
| 28 | **P1** | **Consolidation effectively never ran in production**: it required 24h of *continuous uptime* (reset each launch; monotonic clocks pause in macOS sleep), and the shipped .app has no CLI for manual `digest`. Reflections were a dead feature. Also re-digested the same newest-200 rows. | Wall-clock due-check every 10 min (newest reflection > 24h old → run), `since`-bounded to episodes after the last consolidated period. Tested (due / not-due / bounded). |
| 29 | **P1** | **Two resident 7B models** (~2×4 GB): `run_app` and every `service.start` each built their own `MLXBackend`. | One shared backend: `make_loop(backend=…)`; the tracker reuses the chat/recommend instance. Tested. |
| 30 | **P2** | **Volatile window titles defeated compaction**: "(3) Slack", "42%", spinner glyphs flip every second → a stored row + LLM-call share per second on an idle screen (the exact churn change-detection exists to prevent). | `_VOLATILE_TITLE_RE` strips counters/percentages/Braille-spinner frames from the *title only* (OCR content keeps its numbers). Tested. |
| 31 | **P2** | **`embed = true` off-Ollama silently no-oped** (frozen MLX app has no Ollama; every embed ECONNREFUSED'd into a log nobody sees; hybrid recall quietly became keyword-only). | `make_loop` disables embeddings loudly (WARNING log) when `backend != "ollama"`. Tested. |
| 32 | **P2** | Crash badge swallowed when the user closed the window *after* a crash (window-close branch ran first and cleared nothing). | Window-close branch now sets `failed = not healthy()`. Tested both ways. |
| 33 | **P2** | `backend = "mlx"` + default (Ollama) model tag = invalid HF repo id → crash at first use. | `apply_production_defaults` swaps in the MLX default model for that combination, frozen or source. Tested. |

### Fixed — packaging/build pipeline
| # | Sev | Finding | Fix |
|---|---|---|---|
| 34 | **P0** | **`codesign --deep` does not sign Mach-Os under `Resources/`** (verified empirically) — py2app puts the entire Python runtime there; notarization would reject every unsigned `.so`/`.dylib`. The pipeline could never produce its own deliverable. | Inside-out signing: every nested `.so`/`.dylib`, then frameworks, then executables (entitlements on executables only), then the bundle. |
| 35 | **P0** | **The frozen chat window could never open**: in a py2app bundle `sys.executable` is the bundled *plain interpreter* (`Contents/MacOS/python`), so the old spawn ran a bare REPL that exited instantly → the 2s sync saw "window closed" and stopped tracking. The app could not stay on. | One spawn path for frozen+source: `-m cortana chat-window` with the parent's `sys.path` exported via `PYTHONPATH` when frozen. Dead `CORTANA_CHILD=chat-window` re-exec branch removed. |
| 36 | **P0** | **py2app cannot freeze `mlx`** (PEP 420 namespace pkg): `packages` crashes its finder; `includes` synthesized a *regular* `mlx` in `python314.zip` that shadowed the filesystem portion → `mlx._reprlib_fix` missing → the .app died at boot (and the self-check step hung the pipeline on py2app's GUI error alert). | `excludes=["mlx"]` + the release script rsyncs the complete wheel package (all submodules + `lib/` dylibs + metallib) into `lib-dynload/mlx`; self-check wrapped in a 180s timeout so a boot failure can never hang the build again. |
| 37 | **P0** | **`rm -rf dist` deleted the tracked LaunchAgent plist** (`dist/com.cortana.tracker.plist`) that `install.sh` templates from — one `git add -A` away from repo damage. | Plist moved to `launchd/`; `install.sh` updated; `dist/`, `build/`, `.venv-build/` gitignored (the 486 MB bundle is no longer stageable). |
| 38 | **P1** | **The `.app` inside the DMG was never stapled** (only the DMG was) — a dragged-out app can't verify offline on first launch. | Notarize + staple the `.app` (via zip) first, then build/notarize/staple the DMG. |
| 39 | **P1** | **Fully floating deps**: a rebuild months later would ship a different, untested inference stack (and the import-only self-check wouldn't catch a generate-API break). | `bundle/constraints.txt` pinned from the tested venv; the script refuses to call an unpinned build shippable. |
| 40 | **P2** | `disable-library-validation` entitlement was load-bearing cover for the unsigned-dylib problem — a dylib-hijack surface on an app holding a Screen Recording grant. | Dropped (valid once #34 signs everything same-team). |
| 41 | **P2** | Blanket `*.dist-info` copy shipped pip/setuptools/py2app metadata and duplicate distributions. | Build-tool metadata blacklisted. |
| 42 | **P2** | Self-check didn't import the GUI/sensor stack — a bundle missing `rumps`/`Vision` would ship "verified". | Self-check now imports rumps/webview/Quartz/Vision/AppKit (import-only, headless-safe). |
| 43 | **P2** | Docs told users to run raw py2app (→ broken bundle, missing dylib/metadata steps) and `notarytool submit` on a bare `.app` (invalid — needs zip/dmg/pkg). | DESKTOP.md defers to `build_release.sh`; stale plist path fixed. |

### Verified and REJECTED / accepted as intended (do not "fix")
- **"`_spawn_chat_window` env leak via CORTANA_CHILD"** — no persistence path; env is per-`Popen`.
- **Lazy in-function `import Quartz/Vision` untraced by py2app** — false; modulegraph
  traces bytecode imports; all pyobjc wrappers verified present in the zip.
- **Wheel `package-data` for `cortana.webui`** despite the setuptools warning — works;
  `index.html` verified in the wheel and the bundle; `importlib.resources` resolves.
- **Config TOML vs in-code defaults drift** — checked key-by-key: currently identical,
  so the unshipped bundle TOML is latent dead weight, not an active bug (left as a
  known follow-up: make `DEFAULT_CONFIG_PATH` bundle-aware or drop the resource).
- **MLX chat holding the generation lock across a full streamed reply** — accepted:
  bounded contention beats corrupting a non-thread-safe model.

### Verification
- `./ci/run.sh` — **265 passed**, branch coverage 96.1%, hermetic.
- Release pipeline exercised end-to-end unsigned on this Mac: fresh venv → py2app →
  mlx rsync → metadata → headless boot self-check through the real app binary.
- Still requires a real signed run + GUI session (docs/PRODUCTION.md checklist):
  notarization acceptance, TCC grant flow, live MLX inference, menu interactions.

## 1d. On-device verification pass (2026-08-03, v0.1.0)

Method: run the built `.app` on real hardware and *observe* it (lsof, process
inspection, live chat, the actual TCC flow) — the layer no hermetic test or code
review can reach. Two shipped bugs found and fixed:

| # | Sev | Finding | Fix |
|---|---|---|---|
| 44 | **P1** (privacy) | **The app phoned home with the model fully cached.** `lsof` on the running app showed three outbound HTTPS connections (huggingface.co via AWS/CloudFront): every lazy `mlx_lm.load` revalidates the cached model against the Hub — violating "the first-run download is the ONE network exception". | `runtime.enforce_offline()` (`HF_HUB_OFFLINE=1` + telemetry off), set synchronously at launch when the model is cached and right after a fresh download otherwise. Re-verified live: after a model load the app's only socket is the `localhost:8808` listener. (PR #10) |
| 45 | **P0** (first-run) | **The Screen Recording gate could never pass.** pyobjc's `Quartz` module does not bind `CGPreflightScreenCaptureAccess`/`CGRequestScreenCaptureAccess`; the fail-soft wrapper swallowed the `AttributeError` into a permanent "not granted" — setup wedged regardless of what the user granted. Undetectable in CI (the whole path is native); undetected by both reviewers (plausible-looking API). | Call CoreGraphics directly via ctypes (the repo's existing pattern for `IsSecureEventInputEnabled`). Verified on device through the full grant → relaunch → ready → tracking flow. (PR #11) |

Also learned on device (process, not code): hot-patching a built bundle breaks its
code-signature seal and silently divorces it from its TCC grant — fixes must go
through a rebuild, never an in-place edit of a bundle a user has granted.

## 1e. Final pre-publication review (2026-08-05)

Two fresh-context reviewers over the post-pass-4 delta (bugs/complexity; docs +
install UX as a new user). Notable:

| Sev | Finding | Disposition |
|---|---|---|
| **P1** | **`enforce_offline()` was a no-op in the first-download session**: huggingface_hub freezes `HF_HUB_OFFLINE` into module constants at import time, and `ensure_model` imports it *before* the env vars are set — so the very session the offline promise was built for stayed online. (The cached-model path, which is what the on-device lsof check exercised, was correct — masking this.) | Fixed: `enforce_offline()` now also patches the live module's constants. Regression test with a stubbed module. |
| P2 | "Get Recommendation" wasn't gated on `ready` — a click mid-download lazy-loads the model, silently blocking minutes and duplicating hub requests. | Fixed: same gate + alert as Start. (Pre-ready chat via a hand-typed URL remains possible — accepted; hf file locks prevent cache corruption.) |
| P2 | User-level `~/.config/cortana/cortana.toml` silently shadowed the repo config with no doc/log trail; one config test was tautological (passed even if fall-through was broken). | Docs corrected (config.py docstring, README); test rewritten around a sentinel value. |
| P0 (docs) | README demo commands used `python` (absent on stock macOS) with no 3.11+ note — the first thing a new user runs would die cryptically. | `python3` + explicit requirement. |
| P1 (docs) | `setup.sh` printed a build command pointing at a deleted path (`packaging/`) that also produces a broken bundle; AGENT_LOOP.md/MEMORY.md claimed shipped features (asyncio loop, embeddings, consolidation, hybrid recall) "not built yet"; DESKTOP.md referenced the removed `--read-focused-text`. | All corrected; AGENT_LOOP.md re-headered as a shipped historical record (DESIGN.md pattern). |

Verified clean by the same pass: frozen-bundle config path resolution, ctypes TCC
calls, PYTHONPATH child spawn, `set -e` behavior of the signing loop, icon/build
config, the dead-code prune (zero dangling code references), and the four install
paths (each distinct-audience; demo pipeline runs green on bare Python 3.11+).

## 3. Remaining recommendations (not done — prioritized)

1. ~~**(P1) Surface tracking-thread death in the menu.**~~ **DONE** (#22/#23 —
   crash badge + health predicate). If the loop crashes (e.g.
   prune raises on a locked DB near the 2 GiB cap), the thread dies silently while
   the menu still shows "⏸ Stop Tracking". Wrap `_TrackingService._run` and flip the
   controller state + a menu badge on failure. *(Small; needs a Mac to verify the
   rumps side, which is why it wasn't done blind.)*
2. **(P1) Consolidate the four prompt-builders.** `reasoning`/`advisor`/
   `consolidation`/`chatapp` each format `[{ts}] app=…: body` with drifted caps
   (400 vs 300 chars) and empty-case handling. One `format_memory_lines(memories,
   cap)` helper ends the drift.
3. ~~**(P2) Auto-schedule consolidation.**~~ **DONE** (#28 — wall-clock due-check). `cortana digest` is manual; run it from the
   loop's `_maintenance` daily so reflections accumulate without user action.
4. **(P2) Keyword-only recall ignores BM25 rank** (`ORDER BY ts DESC` even under
   `MATCH`). Fine for recency-flavored use; hybrid path ranks properly. Consider
   `ORDER BY rank` + a recency tiebreak for the keyword path.
5. **(P2) `prune()` size estimate measures pre-VACUUM page count** (over-deletes
   somewhat after age-pruning; conservative direction). Use `page_count −
   freelist_count`, and account for embeddings JSON (~15 KB/row) in the average.
6. **(P2) Embeddings lack a model tag.** #4 makes a model switch *loud*; a `model`
   column + filter (or a re-index command) would make it *seamless*.
7. ~~**(P3) Dead code sweep:**~~ **DONE** (PR #14 — forget/counts/since/
   drops_total/clear/perceive removed; docstrings fixed). `Memory.forget`/`counts` (test-only), `WorkingMemory.
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

- `./ci/run.sh` — **236 passed** (231 first pass + 5 second pass), branch coverage
  96.0% (above the 95% gate), hermetic (no PyObjC/Ollama/network).
- New regression tests: `tests/test_review_fixes.py` (privacy exclusion, cosine
  guard, reflections-in-prompt, reflection pruning, backlog drain, live chat
  context, consolidation ordering) plus the updated cadence test.
- **Not verified here (needs a real Mac):** the rumps quit path, worker-thread
  alert, and live sensor exclusion — the native shell is `# pragma: no cover` by
  design; the logic behind each is tested.
