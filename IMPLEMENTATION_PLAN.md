# IMPLEMENTATION_PLAN.md — status: COMPLETE

There is no active execution plan. Every planned phase shipped and is on `main`:

| Phase | Delivered | Record |
|---|---|---|
| 0–2 | Test spine, tiered Memory (SQLite/FTS5), Perception + meaning extraction | PR #1 |
| 3–5 | Agent loop (asyncio, single-writer, backpressure), recall/reasoning, chat UI | PRs #2–#7 |
| 6+ | One unified menu-bar app, working memory, hybrid retrieval, reflections, packaging (signed/notarized DMG pipeline) | PR #8 |

- Architecture: [`docs/DESIGN.md`](docs/DESIGN.md) · memory deep-dive: [`docs/MEMORY.md`](docs/MEMORY.md)
- Review history (4 adversarial passes, every finding verified): [`REVIEW.md`](REVIEW.md)
- Ship checklist (signing + on-device verification): [`docs/PRODUCTION.md`](docs/PRODUCTION.md)

When new work starts, write the new plan here first (per `CLAUDE.md`: design →
checklist → implement, TDD throughout, `./ci/run.sh` green before done).
