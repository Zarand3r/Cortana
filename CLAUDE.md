# CLAUDE.md — Cortana

## Skills — use these automatically

This repo is wired to the [`eng-skills`](https://github.com/Zarand3r/claude-skills)
plugin (auto-installed via `.claude/settings.json` when you trust the folder).
The skills below feed Claude's reasoning and provide autonomous harnesses.
**Before and during any coding work, load the skill(s) whose trigger matches the
task** and follow their guidance. `karpathy-guidelines` applies to essentially
all coding; the others are routed to by task type. When in doubt, start with
`principal-production-engineer` — it is the single entry point that routes to the
rest. Claude also auto-invokes them by description; invoke explicitly with the
namespaced form (e.g. `/eng-skills:elves`).

| Skill | Load it when… |
|---|---|
| **karpathy-guidelines** | Always, for any writing/reviewing/refactoring of code. Avoid overcomplication, make surgical changes, surface assumptions, define verifiable success criteria. |
| **principal-production-engineer** | Implementing, reviewing, refactoring, or hardening production code in any language. Single entry point — enforces simple design, dense data, explicit ownership, visible failure, minimal abstraction, honest verification, pipeline discipline. Routes to the skills below. |
| **strategic-engineering-planner** | *Before* implementation when work is architecturally significant, ambiguous, multi-file, distributed, performance-sensitive, concurrency-heavy, or likely to need multiple passes. Produces a written roadmap first. Skip for trivial fixes and obvious CRUD. |
| **implementation-plan** | *After* the design is locked, *before* code. Turns a design doc into a checklist-first `IMPLEMENTATION_PLAN.md` with vertical-slice steps and binary acceptance gates. |
| **cpp-systems-internals** | Writing or reviewing C++ where hardware behavior, codegen cost, ownership vocabulary, API style, or kernel paging matters (lambdas, templates, cache lines, vtables, smart pointers/spans/arenas, `mmap`/`madvise`, AoS/SoA). Load only the relevant topic file. |
| **auto-research** | Iteratively optimizing a measurable outcome unattended/overnight — loss, latency (p50/p95/p99), throughput, MFU, memory/binary/model size, compile time. Enforces a fixed eval harness, append-only results log, keep-on-improvement / reset-on-regression. |
| **elves** | Executing a *development plan* unattended/overnight — user says "run overnight," "implement this plan," "keep going without me," "I'll be back in the morning." Breaks the plan into sprint-sized batches, implements with tests + PR-based review, and keeps durable memory (survival guide, learnings, execution log) for compaction recovery. Requires `git` + `gh`. |

**How to apply:** for a non-trivial task, the default flow is
`strategic-engineering-planner` (plan) → `implementation-plan` (checklist) →
`principal-production-engineer` (implement, routing into `cpp-systems-internals`
as needed), with `karpathy-guidelines` governing throughout. For
unattended/overnight runs pick by goal: **`auto-research`** when success is *one
number on a fixed harness* (optimize a metric), **`elves`** when success is *a
development plan with test/PR gates* (build features across batches). Read a
skill's `SKILL.md` before acting on its domain.

### Running the elves overnight harness

Before the first elves run, get the repo harness-ready (one-time prerequisites):
git + an authenticated `gh` with push access, a verification gate (test/lint/build
command that exits 0 on a clean checkout), and optionally `docs/constitution.md`
(the ungameable promises the Judge enforces each batch). Full checklist:
[`templates/harness-setup.md`](https://github.com/Zarand3r/claude-skills/blob/main/templates/harness-setup.md)
in the skills repo.
