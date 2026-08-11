# Productivity Coach — design

> Status: **design, pre-implementation.** Grounded in a multi-source research pass
> (2026-08-11; 5 search angles, 20 sources fetched, 23 claims surviving 3-vote
> adversarial verification, 2 killed — see §7 for what that changed). Governed by
> `STYLE.md`; builds strictly on the existing memory tiers (`docs/MEMORY.md`), no
> new capture machinery.

## 1. Goal

Focus Cortana's recommendation surface on **productivity**: measure it from the
screen-episode stream the agent already records, and coach the user with
evidence-based, self-directed interventions. Cortana's structural advantages:

- **Semantic, not app-level, activity data.** Trackers like RescueTime see app
  names; Cortana's LLM summaries see *tasks* — which is the unit the fragmentation
  literature actually measures (Mark's "working spheres"), and catches
  same-app/different-task switches and "YouTube: lecture vs. cat videos".
- **Local-only by architecture.** Workplace analytics die on surveillance
  perception (Microsoft Productivity Score backlash, 2020). Cortana's data never
  leaves the machine and answers to no employer — coaching is self-directed by
  construction.

## 2. Evidence base (verified findings)

| # | Finding | Source |
|---|---|---|
| E1 | RescueTime's five productivity levels (Focus Work / Other Work / Neutral / Personal / Distracting) roll into a 0–100 time-weighted "pulse" (weights 0/25/50/75/100) | RescueTime primary docs |
| E2 | Interruption cost = **stress, not slower output**: interrupted work finished *faster* with equal quality but significantly higher NASA-TLX stress/frustration/effort | Mark et al., CHI 2008 |
| E3 | Fragmentation baselines: ~11 min avg in a working sphere before switching; ~57% of segments end in interruption; resumption ≈ 25.5 min through ~2.26 intervening spheres (the popular "23 min" quote is a misquote) | Mark, González & Harris, CHI 2005 |
| E4 | **Flow is not log-detectable** (identical logs labeled flow/not-flow by feel); *focused work* is — via embedding **relatedness** of consecutive activities in a sliding window | Google, CHI 2023 |
| E5 | Focus-session **frequency** predicts self-reported flow (p<.0001, n=13,383); total focus **hours** do not (p=.27) — report/optimize counts, not hours | Google, CHI 2023 |
| E6 | JITAI framework: intervene only at detected vulnerability/opportunity states when the user is receptive; **withholding** unneeded nudges is itself a design principle (habituation, burnout) | Nahum-Shani & Murphy, Ann. Rev. Psych. |
| E7 | Delivery empirics (1,585 workplace nudges): in-meeting nudges engage worse (OR 0.62); user-deferred nudges engage better (OR 1.77); frequency trades likability against efficacy — don't tune to ratings alone | Suh et al., JMIR Mental Health 2024 *(single deployment — medium confidence)* |
| E8 | Best-evidenced nudge content = **contingent if-then plans** (implementation intentions): d=.43 vs .29 for schedule-format across 642 tests; effects far larger with prior goal buy-in (moderator d=.79); app-delivered < human-delivered; **bias-corrected effects are d≈.15–.35** — design for modest per-nudge effect compounded over time | Sheeran et al. 2024; Wang et al. 2021 (meta-analyses) |

## 3. Metric layer (computed from existing tiers; no new capture)

| Metric | Definition | Computed from | Tier |
|---|---|---|---|
| **Category time** | five-level classification (E1), assigned by the LLM during batch summarization, user-overridable per app/category in TOML | `summaries` + `context.app_name` | episodic → daily |
| **Daily pulse** | 0–100 time-weighted rollup of category time (E1) | category time | reflection |
| **Focus-session length** | contiguous run of episodes with high task-relatedness (E4): embedding similarity of consecutive summaries above threshold | `embeddings` (existing, `embed=true`) | derived |
| **Focus-session count / % days with ≥1** | the headline focus stat (E5 — counts, not hours) | sessions | reflection |
| **Switch rate** | task switches per hour (episode boundaries where relatedness drops), vs the user's own trailing baseline — not vs the 2004 population norms (E3 is the *construct* source, not a target) | episode stream | derived |
| **Fragmentation strain** | high switch-rate periods framed as *stress* cost, never "slowness" (E2) | switch rate | reflection |
| **Resumption** | interrupted task resumed within the day? lag + intervening tasks (E3) | episode stream + summaries | derived |

Explicitly **not** metrics: flow detection (E4 kills it), total focus hours as a
target (E5), any single score as an optimization target (Goodhart — the pulse is
descriptive only).

## 4. Coaching layer

**Architecture = JITAI** (E6), three surfaces in order of increasing interruption
risk:

1. **Daily reflection report** (passive; chat + reflection tier): pulse, focus
   sessions, fragmentation strain, one observation. Zero reactance risk.
2. **Weekly review** (reflective): trends vs the user's own baselines; proposes
   1–2 **if-then plans** (E8) the user accepts/edits — securing the buy-in
   moderator. E.g. *"If I open Slack during a focus session, then I finish the
   current paragraph first."*
3. **Just-in-time nudges** (interruptive; last to ship): threshold rules on
   derived states — distraction spiral (sustained Distracting-category run),
   fragmentation spike vs baseline, or an *opportunity* (long focus streak →
   suggest a break). Gated on context (never mid-meeting/screen-share — visible on
   screen, E7), always deferrable ("after this task"), sparse by default with a
   hard daily cap. Frequency is a user setting whose default errs low (E6), not
   tuned to likability alone (E7).

**Onboarding**: the user states their goals (what "productive" means to them,
which categories matter); the coach only ever coaches against that (E8 buy-in;
self-determination). No goals stated → metrics only, no advice.

## 5. What we will NOT build (evidence-driven exclusions)

- **No flow claims** (E4). "Focused work," never "flow state."
- **No focus-hours leaderboarding** (E5) or pulse-maximization framing (Goodhart).
- **No context-weighted switch penalties**: both directions of "topically-similar
  interruptions cost less / are beneficial" were **refuted** in verification (0-3
  and 1-2) — the literature is unsettled; treat all task switches equally until
  Cortana's own data says otherwise.
- **No mid-task "helpful" interruptions justified by topical relevance** — same
  refuted claim.
- **No population-norm scoring**: baselines are the user's own trailing windows;
  Mark's 2004 numbers (24 workers, included offline activity) inform the
  *constructs*, not targets.
- **No employer/export surface of any kind.**

## 6. Phased roadmap (each phase independently shippable, TDD)

| Phase | Ships | Acceptance gate |
|---|---|---|
| **P1 Measure** | category classification in the summary prompt; `productivity` fields at the reflection tier; pulse/session/switch metrics; `cortana ask "how productive was I today"` answers from them | metrics computed correctly on a scripted synthetic day (hermetic tests); classification override via TOML |
| **P2 Reflect** | daily productivity reflection (auto, reflection tier) + weekly review with 1–2 accepted-or-edited if-then plans; goals onboarding in config | review renders from a seeded week; plans stored + resurfaced; no-goals → no advice |
| **P3 Nudge** | JITAI rules (spiral / spike / streak) via the menu-bar surface; context gate; defer control; daily cap | rules fire on synthetic streams exactly at thresholds; never during excluded/meeting contexts; cap enforced |

P1/P2 are pure logic over existing tiers (hermetic, coverage-gated). P3 touches
the native shell (alerts) — logic tested, delivery `pragma` like the rest.

## 7. Honest caveats & open questions

- **Effect sizes**: bias-corrected planning-nudge effects are d≈.15–.35, and
  app-delivery underperforms human coaching — value compounds through the
  reflective surfaces (P1/P2), not through any single nudge. Set expectations
  accordingly in the UI copy.
- **Transfer risk**: the relatedness focus metric was validated on Google's
  homogeneous dev-tool logs; thresholds need calibration on Cortana's
  heterogeneous OCR stream (open question — a local self-report calibration is
  the eventual answer).
- **Receptivity detection from screen data alone is an open research problem**
  (E6 caveat) — P3's context gate is a heuristic, hence deferrable + capped.
- **Product-teardown evidence was thin**: nothing verifiable survived about how
  Rize/Reclaim/Motion's coaching performs. Cortana validates its own nudge UX.
- The nudge-delivery ORs (E7) come from one 4-week, 43-person deployment.
