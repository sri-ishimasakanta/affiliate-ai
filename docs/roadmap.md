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

Last updated: 2026-09-29 (C9-B).

---

## Now

| Unit | Status | Production |
|---|---|---|
| C9-A Growth Action Operations & Stable Prioritization | **COMPLETED** | stable identity DEPLOYED (worker reloaded); Growth digest sending **NOT ENABLED** (first real send = human decision) |
| C9-B Safe Downstream Handoffs | **COMPLETED** | migration `4fe83827d695` **DEPLOYED** (2026-09-29); targeted Threads consumption **ENABLED** (`consume_in_stock_maintenance: true`) |
| C9-C Measurement Feedback Hardening | **ACTIVE** | — |

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

### C9-B — Safe Downstream Handoffs — COMPLETED

| Field | Value |
|---|---|
| implementation | COMPLETED (2026-09-29) |
| migration | `4fe83827d695` (`growth_handoff_requests`, additive) — **DEPLOYED** 2026-09-29 23:44 JST (human approved; backup `D:/Backups/affiliate-ai/affiliate_ai.pre-c9b.20260929T144335Z.db`; integrity ok, FK 0, existing tables unchanged) |
| production targeted Threads consumption | **ENABLED** (`growth_action_policy.json` `threads_generation_requests.consume_in_stock_maintenance: true`, human approved 2026-09-29; consumed only when the stock would generate anyway — no new cadence, no extra calls) |
| production executions | none at activation (0 approved Growth Action reviews, 0 handoff requests) |

Delivered (`docs/operations/growth-actions.md`, section C9-B):

- **Targeted Threads GenerationRequest**: `create_regular_threads_post` /
  `create_threads_alternative_angle` → a local `threads_generation` request (alternative angle
  frozen at conversion, rechecked against tried / published angles). The existing proposal-stock
  maintenance uses it only when it would generate anyway (floor, per-cycle cap, pending-request,
  cooldown and recent-angle rules unchanged; no extra calls) → proposal `awaiting_approval` →
  existing approval / publication. No LLM call inside the conversion. `create_growth_post` stays
  plan_only (the Growth lane owns it).
- **ChangeRequest V2** as typed change-preparation requests (`body_update`, `meta_description`,
  `affiliate_placement`): frozen body / meta hashes (+ affiliate program / target / mapping ids,
  never URLs), execute needs the plan's source hash (drift fails closed), no generated content,
  no empty ChangeRequest. A human links the concrete downstream (change request / editorial
  revision / link mapping) made from the frozen source; its own approval and apply stay separate.
  The Batch 3 internal-link conversion is unchanged.
- **Durable article planning request** for `create_new_article`: never creates an Article;
  rechecks existing articles (keyword id or normalized text), open requests and cannibalization
  (existing read-only article plan); human approve / reject, then link the Article created by the
  existing plan approval (`materialize`, strict checks).
- Unified conversion targets `change_request` / `threads_generation_request` /
  `article_planning_request` / `change_preparation_request`; idempotent on both sides; common
  execute-time checks + per-target conflict checks; lifecycles observed separately
  (`manage_growth_actions.py show / explain / history` + `handoff ...`). The worker never converts.

Known limits kept (not changed in C9-B; still real capabilities to build, not removed):

- `body_update` is preparation / linkage only: `ChangeApplicationService` V1 applies only a
  single inserted internal link; text change requests are blocked at apply (fail closed).
- `affiliate_placement` is preparation / linkage only (placement change requests are blocked at
  apply; link-mapping substitution of existing links stays the manual path).
- `meta_description` has no WordPress apply path (content-only updates).
- `create_growth_post` stays plan_only (the Growth lane owns generation).
- Article plan approval is REST-only and records no approver / plan hash (the Article row is the
  approval); the planning request only links it.

### C9-C — Measurement Feedback Hardening — NEXT

- scheduled follow-up measurement (checkpoints evaluated on a schedule, not only on demand)
- evidence feedback: outcome checkpoints fed back into Growth Evidence as observations (never
  "worked because we did it"), source-specific maturity
- automatic re-evaluation of converted actions when their windows complete
- cross-workflow measurement: Threads-publication anchors (T6.5 24h/72h), article publication
  anchors, change-application anchors — including C9-B handoffs
- stale / missing data handling hardening (stale sources, missing downstream rows)
- operator summary (long-window follow-up review list, 28d completed windows, no success scores)

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
- Apply paths for text edits / affiliate placement change requests and meta description updates
  (WordPress excerpt) — see C9-B known limits.
