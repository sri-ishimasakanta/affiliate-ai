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

Last updated: 2026-09-30 (N1–N7 buildable-now work; N6 closed).

---

## Now

| Unit | Status | Production |
|---|---|---|
| C9-A Growth Action Operations & Stable Prioritization | **COMPLETED** | DEPLOYED (stable identity); Growth digest sending **NOT ENABLED** (first real send = human decision) |
| C9-B Safe Downstream Handoffs | **COMPLETED** | migration `4fe83827d695` **DEPLOYED** (2026-09-29); targeted Threads consumption **ENABLED** (`consume_in_stock_maintenance: true`) |
| C9-C Measurement Feedback Hardening | **COMPLETED** | DEPLOYED (worker-local follow-up measurement, read-only; no migration) |
| **C9 Growth Engine (A–C)** | **COMPLETED** (closed) | remaining capabilities are placed explicitly below (C9-B limits → C10-D/E) |
| C10-A Analysis Foundation | **COMPLETED** | DEPLOYED (read-only foundation; commercial_intent v2 backfilled 30 keywords from stored data; no migration, no new external calls) |
| **C10-2** Content Intelligence + Nightly Discovery + Next Article Orchestrator (C10-B + C10-C + C10-D) | **COMPLETED** | migration `c1d0e233e180` **DEPLOYED** (2026-09-30); nightly task **ENABLED** (`affiliate-ai-nightly-analysis`, daily 03:30 JST); Google Ads batch refresh **DONE once** (38 terms, 1 call; 0 Keywords created) |
| C10-B Content Intelligence | **COMPLETED** | DEPLOYED (read-only) |
| C10-C Nightly Analysis | **COMPLETED** | DEPLOYED; scheduled task **ENABLED** (daily 03:30 JST, separate from the worker; interactive logon like the other project tasks) |
| C10-D Next Article Orchestrator | **COMPLETED** (planning; apply paths moved to C10-E) | DEPLOYED (read-only PLAN; never creates articles) |
| **C10-3** Site Growth Orchestrator + Operations (C10-E + C10-F) | **COMPLETED** | DEPLOYED (read-only orchestration, discovery promotion CLI, health + alerts from the 06:30 monitoring step, `system_status.py`); no migration |
| C10-E Site Growth Orchestrator | **COMPLETED** | action matrix DEPLOYED; body `text_edit` apply **PENDING HUMAN** (first production apply); meta description write **PENDING HUMAN** (first production write); affiliate placement **DEFERRED → C11** |
| C10-F Operations | **COMPLETED** | DEPLOYED; Growth digest first email **PENDING HUMAN**; mobile Growth review **DEFERRED** (relay deploy = human) |
| **C10 Growth platform** | **COMPLETED** (CLOSED) | human-gated items kept under *C10 activation pending* and *C10 recurring human operations* |
| N1–N5, N7 (note pilot → note workflow → measurement → Productized Knowledge → Digital Product Automation; SaaS Validation) | **ACTIVE** — build **COMPLETE** (2026-09-30) | Done waits for external gates: note account + publications, migration `a4a74a5bcb8b` apply, human product approvals, real pilot users (see *N — Additional revenue track*) |
| **N6** System Productization | **COMPLETED** (DoD met 2026-09-30) | second local site profile dry-runs without code changes; production untouched |
| N8 SaaS / Service | PLANNED | waits for the N7 decision; nothing buildable |
| C11 Affiliate Revenue Attribution | PLANNED (after N8) | — |

**Development order (human decision, 2026-09-30):** C10 (CLOSED) → N1 → N2 → N3 → N4 → N5 →
N6 → N7 → N8 → (T6.5 re-evaluation checkpoint) → C11. The T6.5C–H Threads trend-intelligence
phases stay PLANNED as a separate track that does not block this chain.

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

### C9-C — Measurement Feedback Hardening — COMPLETED

| Field | Value |
|---|---|
| implementation | COMPLETED (2026-09-30) |
| migration | none (measurement is derived from stored data; worker keeps results in memory only) |
| production | DEPLOYED: the existing `growth_opportunity_evaluation` subsystem measures due anchors; 0 anchors at rollout (no conversions yet) |

Delivered (`docs/operations/growth-actions.md`, section C9-C):

- cross-workflow lifecycles and anchors (change request, change preparation, Threads generation
  request, article planning request; Growth candidate → review → conversion → handoff → effect)
- `effective_at` only from real external state changes (successful application, Threads
  publication, article publication); never approval / conversion / request creation / proposal
  approval / preparation / article creation
- scheduled follow-up: `next_measurement_at` per anchor, worker re-measures only due anchors,
  re-evaluation trigger `followup_measured` when a checkpoint completes (no new subsystem, no
  Task Scheduler change)
- per-source freshness: data-through, observed_at, expected lag, freshness state; waiting vs
  stale_data; missing stays missing (never zero); no future data; trusted clicks only
- feedback into GrowthEvidence as observation only (`followup`: before / after / direction /
  freshness / data quality / downstream context / evidence version; no score, no causal claim);
  never part of identity or fingerprints, so it cannot create revisions or self-reinforce
- operator summary (`analyze_growth_action_outcomes.py summary / list --due / show`) and
  lifecycle chain in `manage_growth_actions.py show / history`
- fix: Threads 24h / 72h checkpoints are reached by the T6.5 row age (Batch 3 expected a key the
  T6.5 rows never had)

Not in C9-C (kept for later units): durable per-checkpoint history rows (not needed while
measurement is deterministic; revisit with C10-C nightly analysis), cohort comparison for Threads
outcomes (T6.5F/G), attribution of revenue to articles (C11).

### C9 closure — remaining capabilities and where they go

| Capability | State | Placed in |
|---|---|---|
| `body_update` apply path (text edits through ChangeApplication) | implemented in C10-E (`text_edit`), **PENDING HUMAN** first apply | C10-E |
| `affiliate_placement` apply path (placement change requests) | preparation / linkage only (link-mapping substitution stays manual) | **C11** (deferred in C10-E: no tracking URL / SubID change without attribution) |
| `meta_description` WordPress path (excerpt update) | implemented in C10-E, **PENDING HUMAN** first production write | C10-E |
| `create_growth_post` handoff | plan_only (Growth lane owns it) | stays with the Growth lane (T6.3.3) |
| Growth digest first real email | NOT ENABLED (readiness re-checked in C10-F) | human decision |
| Mobile Growth Action review | DEFERRED (CHECK migration + relay redeploy = human) | after C10 (human deployment decision) |

---

## C10 — Growth platform — COMPLETED (declared by the human in T7A: T7 before C10, only after maturity)

### C10-A Analysis Foundation — COMPLETED

Detail: `docs/operations/analysis-foundation.md`. No migration; analysis never calls external
providers.

| Component | Implementation | Production |
|---|---|---|
| Google Ads `commercial_intent` | COMPLETED: the existing deterministic signal hardened to normalizer v2 (zero bids / UNSPECIFIED competition are missing, not zero; outlier cap; quality flags; search volume not an input; organic difficulty not conflated) + PLAN-by-default re-derivation from stored values | DEPLOYED: 30 keywords re-derived from stored Google Ads values (30 appended signals, 3 value changes, 0 rescores, 0 API calls); **PENDING HUMAN**: 12 keywords without stored Google Ads metrics need the existing bulk fetch (`run_keyword_analysis.py`) |
| URL / index state | COMPLETED: provider-neutral raw vs normalized state; latest *inspecting* run is used (daily `GSC_UNKNOWN` no longer overrides it); staleness; raw inspection fields kept by the weekly step | DEPLOYED: uses the already-operating weekly URL Inspection (existing read-only scope; no new call pattern); 25 articles known (24 indexed, 1 discovered-not-indexed) |
| Source freshness / data quality | COMPLETED: one `SourceStatus` contract (fresh / waiting / stale / missing / insufficient / provider_error) for 7 sources; numbers only in `operations_policy.json` + `source_policy.json`; C9 evidence and C9-C measurement use it | DEPLOYED (worker reloaded); findings: Make commissions `insufficient` (imports succeed, 0 rows) |
| Affiliate attribution readiness | COMPLETED (assessment): truth table + per-program readiness; clicks A_direct, Make commissions B_provider, nothing allocated to articles | assessment only; C11 needs a per-click reference (SubID / clickref) = human decision; 13 of 21 trusted clicks are flagged as possible instrumentation bursts (not excluded) |
| Unified evidence contract + operator CLI | COMPLETED: per-component `CandidateEvidence` (no composite score) with access class and a per-provider batched refresh plan; `scripts/analyze_signal_health.py` | DEPLOYED (read-only) |

Kept for later units: ~~search_demand stores 0.0 for no-volume keywords~~ (resolved in C10-2);
~~C6 is not fed index state~~ (resolved in C10-2); Make commission program tagging (C11);
same-second click bursts stay flags only (no provable exclusion rule; C11).

### C10-2 — Content Intelligence + Nightly Discovery + Next Article Orchestrator — COMPLETED

One development unit covering C10-B, C10-C and C10-D (detail:
`docs/operations/content-intelligence.md`). Everything reads local and stored data only; no
Keyword, Article, planning request or Growth Action is created; no composite score.

| Item | State | Production |
|---|---|---|
| search_demand missing semantics (C10-A deferred) | RESOLVED: normalizer v2 separates observed / observed_zero / missing / insufficient; missing signals are no longer written; stored v1 zeros without history are read as missing (scoring and evidence) | no write needed (2 keywords affected, neither scored) |
| C6 index integration (C10-A deferred) | RESOLVED: C6 receives the saved inspected index state (unknown / stale → GSC_UNKNOWN); window aligned to 8 days | DEPLOYED; 0 new Growth revisions |
| migration `c1d0e233e180` | `nightly_analysis_runs` + `content_discovery_candidates` (additive; downgrade guard) — rehearsed on a production copy | **PENDING HUMAN** (not applied) |

### C10-B Content Intelligence — COMPLETED
- Topic Cluster: one registry merging `content_clusters.json` and `content_portfolio.json`
  (E stays deferred with its portfolio members); stable `cluster:<id>` identity separate from
  evidence; explicit-then-unique-theme membership. Production: 5 clusters, all 25 articles placed.
- Content Gap: evidence-based only (no_article, missing_pillar, missing_<role>, weak internal
  linking, outdated facts, index gap, monetization gap). Production: 20 gaps.
- Continuous Keyword Discovery: GSC queries, portfolio plan / reserve, cluster config, manual
  seeds; dedup and cannibalization suppression; candidates are never Keywords. Production: 26 new.
- article types (content-type strategy): comparison, roundup, how-to, practical workflow,
  implementation, pricing, informational, category landing (the two workflow roles reuse the
  how_to template); deterministic reasons; `undetermined` when evidence is missing.
- reusable SaaS facts: cross-article view of `article_facts` (latest value, source article,
  freshness, conflicts; unknown is not a value; stale is not current); research is PLAN only.

### C10-C Nightly Analysis — COMPLETED (implementation)
- dedicated nightly batch, once per day, separate from the resident Threads worker
  (`scripts/run_nightly_analysis.py`, PLAN by default)
- about 80 candidates analysed per run as an operating guideline (80 is **not** a strict quota;
  fewer is fine, nothing is padded; explainable preselection order, deferred items keep a reason)
- compressed at the end into a small number of Growth Actions (existing C9 identity; the human
  still sees the C9-A inbox / digest, not 80 items)
- cost tiers: local / cached / refresh-required; refreshes are batched per provider and never run
  by the batch (Google Ads: 38 terms → 1 call, run once by the human decision on 2026-09-30; 12
  Keywords + 26 discovery phrases, evidence stored on the candidates, 0 Keywords created)
- run history + retry-safe idempotency (`nightly:<date>`); scheduled task
  `affiliate-ai-nightly-analysis` **ENABLED** 2026-09-30 (daily 03:30 JST; first run 43 analysed
  of 85, 42 prefilter-skipped, 0 deferred, 0.6 s)

### C10-D Next Article Orchestrator — COMPLETED (planning); apply paths DEFERRED
- NextArticleCandidate: topic, cluster, content type, cluster role, gap filled, monetization role,
  rationale, evidence, blockers, freshness, cannibalization, refresh needs, handoff readiness
  (ready_for_growth_review / needs_signals / needs_keyword_promotion / blocked); same identity as
  the Growth `create_new_article` action; never creates an Article (human review → C9-B planning
  request → existing plan approval → Article). Production: 11 ready, 4 need signals, 26 need keyword
  promotion, 2 blocked by cannibalization.
- **DEFERRED → C10-E**: body `text_edit` apply (reuses the existing content write form but needs
  text-edit proposal / staleness / review gates in ChangeApplication) and meta description apply
  (a **new** WordPress write form: excerpt update of existing posts; human decision). Deferred so
  the read-only C10-2 core was not blocked.

### C10-3 = C10-E + C10-F — COMPLETED
Detail: [site-growth-operations.md](operations/site-growth-operations.md) (action matrix, discovery
promotion, health model, `system_status.py`, autonomy classes A–E, automation boundary).

### C10-E Site Growth Orchestrator — COMPLETED
- action matrix for all 10 actions (handoff → downstream approval → execution → effective →
  measurement) with a deterministic stage per Growth Action; routes through existing services
- discovery promotion: 3–5 shown, one at a time, fingerprint-bound, overlap / duplicate refused,
  signals from cached Google Ads evidence; no Article / planning request / approval (production:
  26 tracked, 0 promoted)
- body `text_edit` apply (change-request-v2, link / disclosure / drift gates) — **PENDING HUMAN**
  first production apply (`text_edit_apply_enabled: false`)
- meta description (excerpt) WordPress write with drift check and read-back — **PENDING HUMAN**
  first production write (`meta_description_apply_enabled: false`)
- affiliate placement apply — **DEFERRED → C11**; Growth post stays plan_only; Threads paths use
  only the existing generation request / approval / publication

### C10-F Operations / Monitoring — COMPLETED
- health model (nightly / worker / data / workflow / db); actionable-only alerts (source
  `c10_health`, stable fingerprints, 168 h re-notify, acknowledged stays quiet, auto-resolve),
  recorded by the 06:30 monitoring step and kept out of the daily incident email
- `scripts/system_status.py` read-only operator summary; nightly task in the project-state
  scheduler contract; Growth digest readiness re-checked (sending stays disabled)
- Google Ads refresh = batched backlog (no automatic call); SaaS fact refresh = plan only
- **PENDING HUMAN**: Growth digest first email; mobile Growth review (relay deploy); switching the
  nightly task to a stored-password logon if it must run while logged off

### C10 closure
C10 is CLOSED. What remains is human activation, not development (listed below). Affiliate
placement apply moves to C11.

### C10 activation pending (human-gated; kept after closure)
One-time activations. Each needs the human's decision; nothing here is enabled by code.

| Item | State | Human stop |
|---|---|---|
| body `text_edit` first production apply | implemented, `text_edit_apply_enabled: false` | first production apply |
| meta description first production write | implemented, `meta_description_apply_enabled: false` | first production write (new WordPress write form) |
| Growth digest first real email | readiness re-checked, `sending_enabled: false` | first Growth digest production email |
| mobile Growth Action review deployment | DEFERRED (CHECK migration + relay redeploy) | production migration + relay / WordPress deployment |
| nightly task logon mode (only if needed) | Interactive logon: runs only while logged on (see below) | changing the logon / credentials |

### C10 recurring human operations
Normal, repeated operations, not pending activations.

| Operation | How | Notes |
|---|---|---|
| Discovery candidate → Keyword promotion | `manage_discovery_candidates.py plan`, then `promote <id> --fingerprint … --execute` one at a time | 26 tracked, 0 promoted (2026-09-30); never automatic, no approve-all |
| Google Ads refresh plan / execution | `refresh_google_ads_metrics.py` (PLAN), then `--execute --expect-terms N` | the one-time bulk refresh of the 38 terms is **done** (2026-09-30, 1 call). What remains is **recurring**: terms re-enter the plan when their evidence is older than 45 days or new discovery terms appear; `system_status.py nightly` shows the backlog. Each execution is a human run; **automating the refresh is not enabled** and would be a new automatic external call behaviour (a human decision). |

### Known operating constraints (checked from the sources on 2026-09-30)
- **Nightly analysis runs only while the user is logged on.** `affiliate-ai-nightly-analysis` is
  registered with an Interactive logon (like `affiliate-ai-threads-worker`), daily 03:30 JST,
  IgnoreNew, StartWhenAvailable, WakeToRun off, no start on battery. If nobody is logged on at
  03:30 the run does not happen. The authentication mode is intentionally unchanged; switching to
  a stored-password logon is a human decision in the Task Scheduler UI.
- **06:30 health monitoring conditions.** The C10-F health sync runs inside the `evaluate_monitoring`
  step of `scripts/run_operations.py` (the only entry point that passes `system_health_factory`),
  which both profiles run: `affiliate-ai-operations-daily` Mon–Sat 06:30 and
  `affiliate-ai-operations-weekly` Sunday 07:30 JST. Both tasks use a stored-password logon (they
  run whether or not the user is logged on), IgnoreNew, StartWhenAvailable (a missed run starts
  when the PC is available again), WakeToRun off (the PC must be awake), no start on battery,
  72 h limit. A failing health probe is swallowed and never fails the monitoring step. Observed:
  run 12 (2026-09-30 06:30) recorded `system_health = {recorded: 0, notified: 0, resolved: 0,
  healthy: true}`.
- Consequence: a nightly run missed because nobody was logged on is detected by the next 06:30
  health check (`nightly_missed`, 3 h grace after 03:30) and notified once; a stopped worker is
  detected the same way (`worker_stopped`).

## C11 — Affiliate Revenue Attribution — PLANNED (after N8)
- article-level attribution (needs tracking / provider configuration changes: human decision)
- readiness assessed in C10-A (`analyze_signal_health.py --section attribution`): per-click
  reference (SubID / clickref) passed back by the ASP, stored on clicks and read by the
  commission import; commission import tagged / filtered by program; no historical backfill

## N — Additional revenue track (note channel first)

Full definitions (Purpose / Entry Criteria / Scope / Out of Scope / External Dependencies /
Buildable Now / Requires Real Service or Real Data / Human Stop Conditions / Definition of Done /
Outputs / Next-phase dependencies) and the dependency graph:
[n-track-plan.md](operations/n-track-plan.md). N4–N8 were defined by Claude on 2026-09-30 from
the track's purpose (no earlier definition existed); the human may revise them.

**Ground rule:** nothing is built against a provider, API, file format or sales report the
project has not actually observed, and no sales / readers / subscribers are fabricated or
estimated. Provider-independent foundations are built now; each phase's Done waits for real
evidence (build-complete ≠ Done).

| Phase | Status | Buildable now (local only) | Waits for |
|---|---|---|---|
| N0 note channel foundation | **COMPLETED** | — | — |
| **N1** note pilot: first human-reviewed piece | **ACTIVE** — build **COMPLETE** (2026-09-30) | done: C10 topics, review packet, access mode, approval + publication CLIs (`manage_note_piece.py`) | **PENDING** G1 note account, G2 first human publication |
| N2 repeatable production and review workflow | **ACTIVE** — build **COMPLETE** (2026-09-30) | done: `note_pieces` + migration `a4a74a5bcb8b` (rehearsed on a production copy), sync / status / cadence / links (`manage_note_ledger.py`), published bodies for dedupe | M1 migration `a4a74a5bcb8b` **APPLIED** to production (2026-09-30, human-approved; backup `affiliate_ai.pre-a4a74a5bcb8b.20260930T031947Z.db`); **PENDING** G3 ≥3 publications |
| N3 operationalization and measurement | **ACTIVE** — build **COMPLETE** (2026-09-30) | done: `manual_metric_entries` (same migration), metric catalog, record / list / summary (`record_manual_metric.py`) | M1 applied; **PENDING** G4 real reader numbers; GA4 source dimension = human decision |
| N4 Productized Knowledge | **ACTIVE** — build **COMPLETE** (2026-09-30) | done: knowledge candidates, product spec + traceability + redaction + quality checks, human records; first product `approval-gated-automation-kit` 0.1.0 checks clean ([products.md](operations/products.md)) | **PENDING** H4 human quality review + content approval |
| N5 Digital Product Automation | **ACTIVE** — build **COMPLETE** (2026-09-30) | done: source hashes, stale plan, reproducible zip + manifest, validation, release candidate (built, `verify` reproducible), release gate | **PENDING** H5 human release gate; distribution = separate human workstream |
| N6 System Productization | **COMPLETED** — DoD met (2026-09-30) | done: coupling inventory, site profile (secret / data boundaries, fail-closed capabilities), bootstrap + network-guarded dry-run; `sites/example-local` passes 9/9 steps ([system-productization.md](operations/system-productization.md)) | per-profile content policy (clusters / portfolio / seeds) done; remaining (not blocking): affiliate / keyword / Threads policy files per profile, injected sessions for 4 write services, log-path defaults |
| N7 SaaS Validation | **ACTIVE** — build **COMPLETE** (planning / measurement only, 2026-09-30) | done: pilot plan, pilot + feedback metrics in the N3 ledger (pseudonymous, personal data refused), proposed go / no-go criteria, evidence summary (`pilot_evidence.py`), decision template ([saas-validation.md](operations/saas-validation.md)) | **PENDING** G7 real pilot users; the criteria values are a proposal for the human |
| N8 SaaS / Service | PLANNED | nothing (model not chosen) | the N7 decision, G8 |

N4–N8 are the human's formal definitions (2026-09-30), replacing the provisional ones; the useful
provisional elements were relocated (links → N1–N4 distribution, paid note → N1 / N4, digital
products → N4 / N5, newsletter / membership → distribution workstream, tool pilot → N6 / N7,
SaaS pilot → N7, SaaS / Service → N8). Product lifecycle (N4 / N5) is separate from
distribution (its own workstream; no channel is assumed).

Continuous development in the current environment is possible through the N1 → N7 builds; work
stops only at the production migration (M1), the note account and publications (G1–G4), human
release approvals (H4 / H5) and real pilot users (G7). Entry criteria do not freeze independent
build work.

## T6.5 — Threads trend intelligence (tracked in `docs/project-roadmap.json`)
- T6.5C Breakout Detector, T6.5D Pattern Miner, T6.5E Velocity, T6.5F Cross Validation,
  T6.5G Strategy Recommendations, T6.5H Controlled Feedback — PLANNED (kept)
- **Separate, non-blocking track:** T6.5C–H are not part of the C10 → N1…N8 → C11 main chain and
  never block it.
- **Re-evaluation checkpoint `T6.5-review`:** at N8 completion or before C11 starts (whichever
  comes first), the human re-evaluates T6.5C–H (keep, re-scope, or drop) with the Threads data
  accumulated by then. The checkpoint is recorded in `docs/project-roadmap.json` as C11's
  prerequisite.

## Intentionally excluded

- **Threads reply automation — INTENTIONALLY_EXCLUDED** (no automatic replies to other accounts
  or comments).

## Deferred (cross-cutting)

- Mirror the C9 units (and the C10 sub-units) into `docs/project-roadmap.json`. Since
  2026-09-30 the ledger has C10 (complete), N1–N8 and C11 (planned) with `next_phase` N1; C9 and
  the C10-A…F sub-units are still tracked only here.
- Mobile Growth Action review (relay redeploy) — see C9-A.
- Growth digest scheduled trigger — see C9-A.
- Affiliate placement change-request apply — C11 (text edit and meta description apply paths were
  implemented in C10-E and wait for their first human-approved production run).
