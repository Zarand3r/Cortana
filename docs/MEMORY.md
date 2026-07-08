# Cortana Memory System

> How Cortana remembers: the architecture we ship today, how it maps to the
> cognitive-science taxonomy the field uses, a survey of the current frontier of
> chatbot/agent memory (2023–2026), and a prioritized gap analysis. Everything is
> **local and on-device** — no memory leaves the machine.

---

## 1. Design philosophy

Cortana is a perceptual agent, so memory is the product, not a feature. Principles:

- **Tiered, like human memory** — a fast, small *working* memory of the present
  moment; a durable, searchable *long-term* store of the past. (This is the
  standard cognitive decomposition — see §7.)
- **On-device & private** — SQLite on local disk; redaction before write; no cloud,
  no embeddings API. Any frontier technique we adopt must run locally.
- **Bounded** — every tier has an explicit cap (RAM buffer size; DB age + size).
- **Visible, not silent** — dropped/degraded writes are recorded, never lost quietly.

---

## 2. The memory tiers (what we ship today)

| Tier | Cognitive analogue | Where it lives | Lifetime | Code |
|---|---|---|---|---|
| **Sensory buffer** | iconic/sensory register | `asyncio.Queue(maxsize=queue_max)` + the consumer `batch` | milliseconds–seconds (drained continuously) | `agent.py` |
| **Working (short-term) memory** | working memory | `WorkingMemory` — bounded in-RAM deque of recent *changed* observations | the recent session (rolling, `working_memory_max=200` distinct activities, ~1–2 MB RAM) | `working_memory.py` |
| **Long-term — episodic** | episodic memory | SQLite `context` table (per-perception rows) | days (retention: 90d / 2 GB) | `memory.py` |
| **Long-term — semantic** | semantic memory | SQLite `summaries` table (one LLM summary per batch, FK-referenced) | days (same retention) | `memory.py` |

We do **not** yet have: *procedural* memory (learned skills), a *reflection/
consolidation* layer, or *semantic (vector) retrieval*. See §8–§9.

---

## 3. Data model

```sql
-- Episodic: one row per perception (screenshot → OCR → metadata).
context(
  id, ts, app_name, bundle_id, window_title, ocr_text,
  captured, skip_reason,      -- '', 'unchanged', 'dropped_backpressure', 'ocr_error'
  content_hash,               -- normalized-content hash for change detection
  summary_id → summaries.id   -- FK; the batch's semantic summary
)

-- Semantic: one LLM summary per batch window, stored ONCE (not per row).
summaries(id, window_start_ts, window_end_ts, summary, model, created_at)

-- Retrieval index: FTS5 external-content mirror of context.ocr_text,
-- kept in sync by insert/delete/update triggers.
context_fts(ocr_text)  USING fts5(content='context', content_rowid='id')
```

Working memory holds `Observation` objects in a `collections.deque(maxlen=…)` — no
serialization, pure RAM.

**Why normalized:** one summary row per batch referenced by FK (not copied onto every
event) is the fix for the original v0 bug where a batch summary was duplicated across
rows. Episodic rows are the *what/when/where*; the summary is the *meaning*.

---

## 4. Write path (encoding)

```
every ~interval (1s by default), serially:
  perceive()  → Observation (app, title, screenshot→OCR text)   [perception.py]
  redact()    → scrub secrets before anything is stored          [redaction.py]
  change-detect: content_hash(normalize(app,title,ocr))          [perception.py]
     unchanged?  → count it, store NOTHING  (compaction — see below)
     changed?    → working_memory.add(obs)   ← short-term memory
                 → enqueue for summarization (drop-oldest + persist on backpressure)
  consumer: collect a batch (≤ batch_size or ≤ batch_window)
          → extract_meaning(batch) via local LLM  → one Semantic
          → Memory.remember(batch, semantic)  (atomic: summary + events in one txn)
```

**Compaction (why 1s capture doesn't blow up disk).** An unchanged frame is **not
persisted**. Only *distinct on-screen activities* ("episodes" — a changed screen)
become rows, so **storage scales with context switches, not wall-clock seconds**:
sit on one screen for an hour and it's one row, not 3,600. **Dwell time** is
recoverable from the gap between an episode's `ts` and the next episode's `ts` — no
per-second bookkeeping needed. (Earlier we stored an "unchanged" heartbeat per idle
tick; at 1s that would be ~86k dead rows/day, and they were already excluded from
recall — pure overhead. Removed.)

Key properties:
- **Change detection** avoids re-encoding an idle screen (the dedup gate).
- **Degradation is visible:** if the LLM fails, events are still stored (no summary)
  and `llm_errors` is counted; backpressure evictions are persisted as
  `dropped_backpressure`. Nothing is lost silently.

**Pre-OCR image dedup (on by default — safe).** OCR is the expensive per-frame step,
so `ScreenSensor` first computes an **exact hash of the screenshot's pixels**; if the
frame is **byte-identical** to the last one it returns an `unchanged` observation and
skips OCR (`metrics.ocr_skipped`) — the battery win during genuinely static periods
(reading, idle). It is **safe by construction**: any visible change (typing, scroll,
new text, even the clock) alters pixels → the hash differs → OCR runs, so content is
never missed. (An earlier *coarse perceptual* hash saw layout not text and froze
memory during same-layout editing — replaced by this exact hash.) Any hashing error
falls back to OCR. Two dedup layers stack: **image** dedup skips OCR on identical
frames; **text** dedup (OCR-content hash) then skips the LLM + storage.

---

## 5. Read path (retrieval)

Two retrieval surfaces, both `Memory.recall(query, since, until, app, limit)`:

- **Keyword full-text** — when a query is given, FTS5 `MATCH` over `ocr_text`
  (external-content index). Raw/odd queries fall back to a quoted phrase so recall
  never crashes.
- **Recency scan** — when there's no query (or no content words), the most-recent
  rows.
- Always excludes any legacy `unchanged` rows; results ordered **newest-first**; `limit`
  clamped ≥ 1; reads serialized by a lock (chat serves on multiple threads).

Consumers:
- **`cortana ask`** (`reasoning.py`) — RAG: `question_to_fts` → `recall` → LLM answers
  with citations (ts + app).
- **`cortana recommend` / "Get Recommendation"** (`advisor.py`) — grounded in
  **working memory first** (current activity), long-term store as fallback.
- **Chat** (`chatapp.py`) — the latest user turn drives `recall`; results are
  injected into the system prompt as context.

**Important limitation:** even the FTS path orders by **recency, not relevance** — we
do not currently rank by BM25 score or any semantic similarity. Retrieval is
"keyword match, newest first." This is the single biggest gap vs the frontier (§9).

---

## 6. Retention, bounds, privacy, concurrency

- **Retention** (`prune()`): delete rows older than `retention_days` (90), then evict
  oldest to stay under `max_db_bytes` (2 GB), then drop orphan summaries; FTS stays in
  sync via triggers; `VACUUM` reclaims space (isolation restored in `finally`).
- **Working memory bound:** `deque(maxlen=working_memory_max)` — O(1), fixed RAM.
- **Privacy:** `redact()` scrubs high-confidence secrets (keys, tokens, Luhn cards,
  SSNs) from `ocr_text`, `focused_text`, **and** `window_title` before write;
  secure-input and password-manager bundles are skipped at capture; at rest we rely on
  FileVault (SQLCipher is a documented opt-in). No keylogging.
- **Concurrency:** exactly one SQLite **writer** (the loop's db executor thread);
  readers (chat threads) are lock-serialized on the shared connection; working memory
  is lock-guarded. WAL mode allows readers to see committed writes.

---

## 7. How this maps to the cognitive-science taxonomy

The field standardized on **CoALA — Cognitive Architectures for Language Agents**
(Sumers et al., 2023), which adapts Tulving's trichotomy: agent memory =
**working** + long-term (**episodic**, **semantic**, **procedural**)
([paper](https://arxiv.org/abs/2309.02427), [wiki](https://agentwiki.org/cognitive_architectures_language_agents)).

| CoALA type | Definition | Cortana |
|---|---|---|
| Working | the active context of "now" | ✅ `WorkingMemory` (in-RAM recent observations) |
| Episodic | specific, time-stamped experiences | ✅ `context` table |
| Semantic | abstracted/generalized knowledge | ⚠️ partial — per-batch `summaries`; no consolidated user profile or facts |
| Procedural | learned skills / how-to | ❌ none |

Our tiers line up cleanly with CoALA's working/episodic/semantic; procedural memory
and a consolidated semantic layer are the missing pieces.

---

## 8. The frontier of chatbot / agent memory (survey)

What the best systems do as of 2025–2026:

**1. Retrieval scored by recency × importance × relevance + reflection**
*Generative Agents* (Park et al., 2023) store experiences in an append-only
**memory stream** and retrieve by a weighted sum of **recency** (exponential decay),
**importance** (LLM-rated 1–10), and **relevance** (embedding cosine similarity).
Periodically they run **reflection** — the LLM synthesizes higher-level insights from
recent memories and stores them back, so retrieval surfaces conclusions, not just raw
events ([paper](https://ar5iv.labs.arxiv.org/html/2304.03442),
[summary](https://memx.app/glossary/generative-agents/)).

**2. Hierarchical, self-managed memory (LLM-as-OS)**
*MemGPT/Letta* (Packer et al., 2023) treats the context window like RAM and external
stores like disk: an **OS-style hierarchy** (main context / recall / archival) with
**virtual context paging**, and the agent **edits its own memory** via tool calls
("memory blocks") ([paper](https://arxiv.org/pdf/2310.08560),
[Letta](https://sureprompts.com/blog/letta-memgpt-walkthrough)).

**3. Agentic, self-organizing memory**
*A-MEM* (NeurIPS 2025) builds a **Zettelkasten-style network** of atomic notes: each
new memory gets structured attributes (keywords, tags, context), is **linked** to
related notes, and can **evolve** existing notes as new information arrives
([paper](https://arxiv.org/abs/2502.12110)).

**4. Temporal knowledge graphs with fact invalidation**
*Zep/Graphiti* stores facts in a **bi-temporal knowledge graph** — every edge has
`valid_at`/`invalid_at`, so when a fact changes (user switched jobs) the old one is
marked **superseded**, not duplicated. This wins on the **LongMemEval** benchmark
(Zep 63.8% vs Mem0 49.0% with GPT-4o) precisely because it answers "what was true
*then*?" ([Zep vs Mem0](https://vectorize.io/articles/mem0-vs-zep),
[survey](https://www.graphlit.com/blog/survey-of-ai-agent-memory-frameworks)).

**5. Explicit vs. derived memory (the product pattern)**
*ChatGPT memory* (2024–2025) splits into **saved memories** — things you explicitly
asked it to remember, **auditable** and always applied — and **referenced chat
history** — insights implicitly derived from past chats, not directly inspectable
([OpenAI](https://help.openai.com/en/articles/8590148-memory-faq),
[analysis](https://embracethered.com/blog/posts/2025/chatgpt-how-does-chat-history-memory-preferences-work/)).

**6. Hybrid retrieval + reranking (the RAG baseline)**
Production retrieval is a **pipeline, not a toggle**: BM25 (keyword) **and** dense
vectors as complementary first-stage retrievers, fused with **Reciprocal Rank Fusion
(RRF)**, then a **cross-encoder reranker** for precision. Hybrid+rerank dominates any
single method (Recall@5 ≈ 0.82 vs 0.59 dense / 0.64 BM25 alone)
([reference](https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026),
[VectorHub](https://superlinked.com/vectorhub/articles/optimizing-rag-with-hybrid-search-reranking)).

**7. Consolidation & evaluation**
Surveys converge on two themes: **hybrid episodic+semantic with a consolidation step**
(summarize/abstract episodes into durable semantic knowledge) outperforms single-type
memory; and **LongMemEval** is the standard yardstick for long-horizon recall
([survey](https://arxiv.org/pdf/2605.06716),
[paper list](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)).

---

## 9. Gap analysis — where Cortana stands vs. the frontier

| Concept (frontier) | Cortana today | Gap | Priority |
|---|---|---|---|
| Tiered working + long-term memory (CoALA) | ✅ working + episodic + semantic | procedural missing | — |
| Episodic store, time-stamped | ✅ `context` | — | — |
| Change-detection / dedup on encode | ✅ (ahead of most) | — | — |
| On-device privacy + redaction | ✅ (differentiator) | — | — |
| **Semantic / vector retrieval** | ✅ local embeddings (opt-in `embed=true`) | rerank stage still absent | done (P0) |
| **Hybrid retrieval (BM25 + dense + RRF)** | ✅ FTS + embedding cosine fused by RRF; cross-encoder rerank not yet | rerank = P2 | done (P0) |
| **Reflection / consolidation** (episodes → durable insights) | ✅ `consolidate()` → `reflections` (`cortana digest`) | auto-schedule pending | done (P1) |
| Importance scoring + recency-decay ranking (Generative Agents) | ❌ newest-first only | medium | **P1** |
| **Explicit "saved" facts vs derived** (ChatGPT) + user profile | ❌ no user-facts/profile layer | medium | **P1** |
| Temporal facts with supersede/invalidation (Zep) | ❌ flat timeline | medium | P2 |
| Self-editing / agentic memory (MemGPT, A-MEM) | ❌ | low (heavier) | P2 |
| Procedural memory | ❌ | low | P3 |
| **Eval harness (LongMemEval-style)** | ✅ `benchmarks/retrieval_eval.py` (hit@k, CI-gated) | expand dataset + real-embedder run | done (P1) |

---

## 10. Recommended roadmap (prioritized, on-device)

1. ~~**Semantic + hybrid retrieval (P0).**~~ **DONE.** `cortana/embeddings.py` +
   `Memory` now embed `ocr_text` with a local model (`nomic-embed-text` via Ollama,
   opt-in `embed=true`), store vectors in a SQLite `embeddings` table (brute-force
   cosine at our scale), and `recall(query, embedder=…)` fuses **FTS (keyword) +
   vector (semantic)** via **Reciprocal Rank Fusion**. Best-effort: a missing model
   never loses rows (write) and falls back to keyword (read). *Remaining:* a
   cross-encoder **rerank** stage (P2) and a batched/`sqlite-vec` index if the linear
   scan ever gets slow.
2. ~~**A local eval harness (P1).**~~ **DONE.** `benchmarks/retrieval_eval.py` scores
   **hit@k** over a seeded question→expected-memory dataset (hermetic with
   FakeEmbedder, or point it at a real embedder); a CI test gates against a baseline
   so retrieval regressions fail the build. *Remaining:* grow the dataset and record a
   real-embedder number.
3. ~~**Reflection / consolidation (P1).**~~ **DONE.** `cortana/consolidation.py`
   `consolidate()` summarizes recent episodes into a durable **reflection** stored in
   a `reflections` table (schema v4), exposed as `cortana digest`. *Remaining:*
   auto-schedule it (daily via launchd/cron), and include reflections in retrieval so
   "what have I been working on lately" surfaces the digest.
4. **Recency × importance × relevance ranking (P1).** Score `recall` results by a
   weighted blend instead of pure recency: add an LLM/heuristic **importance** field on
   episodes and an exponential **recency decay**, combined with the §10.1 relevance
   score — the Generative-Agents formula.
5. **Explicit user-facts layer (P1).** A small `facts` table of durable, **auditable**
   user facts (ChatGPT "saved memories" pattern) — extracted with consent, editable,
   always available to chat. Aligns with our privacy stance (inspectable, deletable).
6. **Temporal fact model (P2).** Give facts `valid_from`/`invalid_at` so "current
   employer" supersedes the old one (Zep pattern) — only once the facts layer exists.
7. **Procedural / self-editing memory (P2–P3).** Deferred until a concrete need; our
   Non-Goals already fence "no generic agent runtime."

**Guiding rule:** adopt frontier *concepts*, not frontier *dependencies* — every step
above runs locally and keeps the privacy invariant.

---

## 11. References

- CoALA — Cognitive Architectures for Language Agents: https://arxiv.org/abs/2309.02427
- Generative Agents (memory stream, reflection): https://ar5iv.labs.arxiv.org/html/2304.03442
- MemGPT / Letta (LLM-as-OS, virtual context): https://arxiv.org/pdf/2310.08560
- A-MEM — Agentic Memory (NeurIPS 2025): https://arxiv.org/abs/2502.12110
- Mem0 vs Zep/Graphiti (temporal KG, LongMemEval): https://vectorize.io/articles/mem0-vs-zep
- Survey of AI agent memory frameworks (2026): https://www.graphlit.com/blog/survey-of-ai-agent-memory-frameworks
- Evolution of LLM agent memory mechanisms (survey): https://arxiv.org/pdf/2605.06716
- Agent-memory paper list: https://github.com/Shichun-Liu/Agent-Memory-Paper-List
- ChatGPT memory (saved vs referenced): https://help.openai.com/en/articles/8590148-memory-faq
- Hybrid search (BM25 + vector + RRF + rerank): https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026
