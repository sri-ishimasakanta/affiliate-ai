# Master development roadmap (affiliate-ai)

**Read this file first before every development unit.** It is the version-controlled master
roadmap: what is done, what is active, what is next, and what was deliberately deferred or
excluded. Keep it in sync with reality at the end of each unit (status + production state are
separate fields: code can be complete while production activation is still pending).

- Machine-verified phase ledger for the T / W / N / T7 phases: `docs/project-roadmap.json`
  (checked by `scripts/generate_project_state.py`). The C9 / C10 / C11 units below are tracked
  here; mirroring them into the JSON ledger is a deferred item (see *Deferred*).
- Operations detail lives in `docs/operations/*.md` (linked per unit).

Status words: `COMPLETED`, `ACTIVE`, `NEXT`, `PLANNED`, `DEFERRED`, `INTENTIONALLY_EXCLUDED`.
Production words: `DEPLOYED`, `NOT ENABLED`, `PENDING HUMAN`, `N/A`.

Last updated: 2026-09-29 (C9-A).

---

## Now

| Unit | Status | Production |
|---|---|---|
| C9-A Growth Action Operations & Stable Prioritization | **COMPLETED** | stable identity DEPLOYED (worker reloaded); Growth digest sending **NOT ENABLED** (first real send = human decision) |
| C9-B Safe Downstream Handoffs | **NEXT** | — |
| C9-C Measurement Feedback Hardening | PLANNED | — |

---

## C9 — Growth Engine (evidence → action → review → handoff → measurement)

### Completed batches

| Batch | What | Status | Production |
|---|---|---|---|
| C9 Batch 1 | Unified Growth Evidence (SEO C6 / revenue C7 / measurement / Threads T6.5 / keywords), opportunity classification, action candidates, separate priority components, read-only CLI (`analyze_growth_opportunities.py`) — `docs/operations/growth-opportunities.md` | COMPLETED | DEPLOYED (read-only) |
| C9 Batch 2 | Durable Growth Action history (revisions, supersede, dedup), inbox / digest PLAN, human review CLI, conversion PLAN, worker subsystem `growth_opportunity_evaluation` — `docs/operations/growth-actions.md` | COMPLETED | DEPLOYED (migration 74bfaf6c9c9f; subsystem enabled) |
| C9 Batch 3 | Approved-action conversion (internal links → ChangeRequest awaiting_approval only), downstream observation, outcome measurement (effective_at = real application), closed-loop suppression | COMPLETED | DEPLOYED (migration 74dbecaa4bb2); approved reviews 0, conversions 0 |

### Reorganized plan (old batch numbers kept for traceability)

The former fine-grained batches B3.1 / B4 / B5 / B6 / B7 / B8 are now grouped into three units.
Nothing was dropped:

| New unit | Old batches | Content |
|---|---|---|
| **C9-A** Growth Action Operations & Stable Prioritization | B3.1 + B4 + B8 | stable opportunity identity (no churn from soft preferences), prioritization for humans (few, explained, diverse), weekly Growth Action digest with notification history |
| **C9-B** Safe Downstream Handoffs | B5 + B6 | targeted Threads GenerationRequest (article + angle inside the proposal-stock rules), ChangeRequest V2 (text_edit / meta-snippet / affiliate placement as reviewable local requests) |
| **C9-C** Measurement Feedback Hardening | B7 (remaining) | outcome windows fed back as ordinary evidence with source-specific maturity, Threads-downstream outcome anchors, long-window follow-up review |

### C9-A — Growth Action Operations & Stable Prioritization — COMPLETED

| Field | Value |
|---|---|
| implementation | COMPLETED (2026-09-29) |
| production stable identity | DEPLOYED (worker reloaded to the C9-A code) |
| production Growth digest PLAN | available (`manage_growth_actions.py digest-plan`) |
| production Growth digest sending | **NOT ENABLED** (`growth_action_policy.json` `sending_enabled: false`; the first real send is a human stop condition) |
| migration | none |

Delivered:

- **Stable identity (old B3.1)**: `create_threads_alternative_angle` is one opportunity per
  article (`create_threads_alternative_angle:article:article:<id>`). The recommended angle is a
  `recommendation` (soft preference: avoid the site's most recent 1–2 regular post angles) and is
  in neither the key nor the evidence fingerprint. Material changes (reach rank crossing the
  cohort median, tried angles, source states, blockers) still create revisions. Legacy
  angle-keyed rows are kept unchanged and matched through a recomputed canonical fingerprint
  (rejected / converted / pending-review semantics preserved); one live row per opportunity.
- **Prioritization (old B8)**: eligibility (active + actionable now + not handled + not notified
  in the same state), the existing C9 component order with explanations ("stronger evidence",
  "higher opportunity", ...), soft action-family diversity, no composite score.
- **Growth Action digest (old B4)**: weekly, max 5 individual reviews (fewer allowed), only
  inside the approval notification window, notification history in `notification_deliveries`
  (type `growth_action_digest`, per-member candidate/notification fingerprints), no re-notify of
  the same state, stale-fingerprint review fails closed. PLAN by default; real sending gated by
  policy + `--execute`.

Deferred from C9-A:

- **DEFERRED — mobile Growth Action review**: the mobile approval relay only allows
  `change_request` / `threads_post` subjects (DB CHECK + WordPress relay allowlist and review-text
  guard); adding Growth Actions needs a WordPress relay redeploy. Review stays on the CLI.
- **PENDING HUMAN — first real Growth digest email** (flip `sending_enabled` after approval).
- Digest scheduling: evaluated on demand (CLI); a scheduled trigger (worker subsystem or C8 weekly
  task) is decided in C9-B/C10-F once sending is approved.

### C9-B — Safe Downstream Handoffs — NEXT

- Targeted Threads GenerationRequest adapter: request a proposal for a given article + angle
  through the existing proposal-stock rules (caps, cooldowns, recent-angle preference, approval
  flow). Converts `create_regular_threads_post` / `create_threads_alternative_angle` from
  unsupported to local handoff.
- ChangeRequest V2: text_edit, meta/snippet change and affiliate placement as reviewable local
  change requests (frozen hashes, separate approval and apply; no automatic WordPress write).
- Durable article-planning request (so `create_new_article` can be converted, not only planned).

### C9-C — Measurement Feedback Hardening — PLANNED

- Outcome checkpoints fed back into Growth Evidence as observations (never "worked because we
  did it"); source-specific maturity; Threads-publication outcome anchors (T6.5 24h/72h).
- Long-window follow-up review list (28d completed windows) without success scores.

---

## C10 — Growth platform (PLANNED; declared by the human in T7A: T7 before C10, only after maturity)

### C10-A Analysis Foundation — PLANNED
- Google Ads `commercial_intent` signal
- URL / index state (URL Inspection coverage beyond the saved snapshot)
- freshness / data quality
- affiliate attribution readiness

### C10-B Content Intelligence — PLANNED
- Topic Cluster, Content Gap, Continuous Keyword Discovery
- article types: comparison, roundup, how-to, practical workflow, implementation, pricing,
  informational, category landing
- reusable SaaS facts

### C10-C Nightly Analysis — PLANNED
- dedicated nightly batch, once per day, separate from the resident Threads worker
- about 80 candidates analysed per run as an operating guideline (80 is **not** a strict quota)
- compressed at the end into a small number of Growth Actions

### C10-D Next Article Orchestrator — PLANNED

### C10-E Site Growth Orchestrator — PLANNED

### C10-F Operations / Monitoring — PLANNED

## C11 — Affiliate Revenue Attribution — PLANNED
- article-level attribution (needs tracking / provider configuration changes: human decision)

## N — Additional revenue track (note channel)
- N0 note channel foundation — COMPLETED (see `docs/project-roadmap.json`)
- N1 note pilot — PLANNED
- N2 repeatable production and review workflow — PLANNED
- N3 operationalization and measurement — PLANNED

## T6.5 — Threads trend intelligence (tracked in `docs/project-roadmap.json`)
- T6.5C Breakout Detector, T6.5D Pattern Miner, T6.5E Velocity, T6.5F Cross Validation,
  T6.5G Strategy Recommendations, T6.5H Controlled Feedback — PLANNED

## Intentionally excluded

- **Threads reply automation — INTENTIONALLY_EXCLUDED** (no automatic replies to other accounts
  or comments).

## Deferred (cross-cutting)

- Mirror the C9 / C10 / C11 units into `docs/project-roadmap.json` (the machine ledger currently
  covers T / W / N / T7; adding units needs evidence files and updated project-state tests).
- Mobile Growth Action review (relay redeploy) — see C9-A.
- Growth digest scheduled trigger — see C9-A.
