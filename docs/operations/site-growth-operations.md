# Site Growth Orchestrator + Operations (C10-3 = C10-E + C10-F)

C10-3 ties the growth paths together and adds one health view, so a human does not need to
watch CLIs day to day. It **routes** through the existing services and does not replace them.
Every approval stays where it already is.

- Code:
  - `app/growth/orchestration.py` (action matrix, stages)
  - `app/services/site_growth_orchestrator_service.py`
  - `app/services/discovery_promotion_service.py`
  - `app/change/text_edit.py`
  - `app/services/meta_description_apply_service.py`
  - `app/operations/system_health.py`
  - `app/services/system_health_service.py`
- Policies:
  - `app/config/change_apply_policy.json` (both apply paths are `false`)
  - `app/config/system_health_policy.json`
- CLIs:
  - `scripts/system_status.py` (read-only)
  - `scripts/manage_discovery_candidates.py`
- Tests: `tests/integration/test_site_growth_operations.py`, `tests/integration/test_change_request_v2.py`

## 1. Action matrix (C10-E)

For each action, the table shows where the handoff goes, who approves downstream, how it executes,
and whether it is live in production.

| Action | Handoff | Downstream approval | Execution | Production |
|---|---|---|---|---|
| review_internal_links | change request (`add_internal_link`) | `manage_change_requests.py approve --proposal-hash` | `apply_approved_change.py --execute` | **ENABLED** |
| create_new_article | C9-B article_planning request | handoff approve-plan, then the existing plan approval | existing drafting / review / publication | **HUMAN_DRIVEN** (never creates an Article automatically) |
| create_regular_threads_post | targeted threads_generation request | existing Threads proposal approval | existing stock maintenance + publication | **ENABLED** (no direct model call, no direct publish) |
| create_threads_alternative_angle | same, with the angle frozen | same | same | **ENABLED** |
| update_existing_article | change preparation (body_update) → `text_edit` change request | human-written body, `awaiting_approval` | ChangeApplication with text_edit gates | **PENDING_ACTIVATION** (`text_edit_apply_enabled: false`) |
| improve_search_snippet | change preparation → `meta_description` change request | human-written meta, `awaiting_approval` | `MetaDescriptionApplyService` → `update_post_excerpt_exact` | **PENDING_ACTIVATION** (`meta_description_apply_enabled: false`) |
| review_affiliate_placement | change preparation (affiliate_placement) | manual link mapping (existing) | — | **DEFERRED → C11** (no tracking URL / SubID change) |
| create_growth_post | — (the Growth lane owns generation) | — | Growth lane (daily cap, purpose gate) | **PLAN_ONLY** |
| wait_for_more_data / investigate_data_quality | — | — | — | informational |

Each Growth Action has a stage, computed deterministically:

`observed → awaiting_review → review_pending → approved_not_converted → handed_off → downstream_in_progress → effective → closed`

`SiteGrowthOrchestratorService.status()` shows the stage and the next human step for every
action. `system_status.py workflow` shows the counts.

**A Growth approval alone never applies anything.** Every write still needs its own downstream
approval and apply step.

### Apply paths (implemented, not enabled)

- **Body text edit (`text_edit`)**
  - The human writes the new body.
  - `ChangeRequestService.propose_text_edit` stores a change-request-v2 proposal (proposal hash,
    version, dedupe).
  - ChangeApplication then enforces these gates:
    - the policy flag is on;
    - the change request is approved with the same hash;
    - the local body is unchanged since the proposal;
    - every existing link is kept and no new link is added;
    - the PR disclosure stays in the first 700 characters;
    - the new body is neither empty nor identical to the current one.
  - **STOP before the first production apply** (human decision: set
    `text_edit_apply_enabled: true`, then apply one approved request).
- **Meta description (`meta_description`)**
  - The human writes the meta text: 20–220 characters (60–160 recommended), no HTML.
  - `MetaDescriptionApplyService` checks: approved hash, local drift, WordPress-side excerpt drift.
  - It then sends one exact `{"excerpt": ...}` POST and reads the result back.
  - A mismatch or an ambiguous outcome is recorded as `outcome_unknown`, never as success.
  - This is a **new WordPress write form**. **STOP before the first production write** (human
    decision: set `meta_description_apply_enabled: true`).

## 2. Discovery candidate promotion

A discovery candidate is not a Keyword. The nightly analysis never promotes one.

1. `manage_discovery_candidates.py plan` shows 3–5 candidates. The order is:
   - the candidate fills a gap;
   - then Google Ads volume was observed;
   - then there are Search Console impressions;
   - then everything else.

   Each cluster's best candidate comes first. Candidates blocked by an overlap
   (cannibalization) are counted but not shown. There is no composite score.
2. A human promotes **one** candidate with
   `promote <id> --fingerprint <sha> --execute`. This is refused when:
   - the evidence changed since the plan (fingerprint);
   - there is an overlap;
   - a Keyword already has the same phrase.
3. Promotion:
   - creates one Keyword through the existing `KeywordService`;
   - builds search_demand / commercial_intent signals from the **cached** Google Ads evidence
     (keeping the original observation time, with no new call);
   - freezes the evidence in `evidence_json["promotion"]`.

   It creates no Article, no planning request and no Growth approval.
4. `dismiss <id> --reason ... --execute` removes a candidate for good.

There is no approve-all entry point.

## 3. Health model (C10-F)

`SystemHealthService.evaluate()` is read-only and makes no external calls.

| Area | Checks |
|---|---|
| nightly | never ran (info), failed, missed (3h grace after 03:30), stale (> 36h), candidate explosion (> 3× budget, info), Google Ads refresh backlog (info) |
| worker | stopped (heartbeat > 45 min or lock released), ERROR lines in the last 24h |
| data | url_inspection / threads_insights stale or provider error (import sources are already covered by `DATA_STALE` / `IMPORT_FAILURE`); google_ads stale (info); never configured = summary only |
| workflow | Growth reviews > 7 days (info), open handoffs > 14 days, change requests > 14 days (info), Threads proposals > 72h (info), conversion failures in 7 days, measurements due (info) |
| db | alembic current ≠ code head |

**Alerting rules.** Only `warning` and `error` findings become operations alerts.

- Alerts are recorded with source `c10_health` and type `SYSTEM_HEALTH`.
- The fingerprint is `C10_HEALTH|check|subject`, with no date, so one problem is one alert.
- The existing `OperationsAlertService` does the recording, with these rules for `c10_health`:
  - notify when the alert is new;
  - re-notify at most every `renotify_after_hours` (168);
  - never re-notify an alert a human **acknowledged**.
- Health alerts are recorded without `operations_run_id`, so they stay out of the daily incident
  email.
- When the condition clears, the alert is **auto-resolved** (health alerts only; other sources
  are never touched).
- Recording happens in the daily operations monitoring step (06:30 JST). Only
  `scripts/run_operations.py` enables it, so nothing notifies overnight. A failing health probe
  never breaks monitoring.

## 4. Operator summary

```
uv run python scripts/system_status.py            # summary: healthy, or the actionable problems first
uv run python scripts/system_status.py nightly    # latest run, counts, duration, refresh backlog
uv run python scripts/system_status.py data       # per-source freshness
uv run python scripts/system_status.py workflow   # backlogs, Growth stages, discovery, digest readiness
uv run python scripts/system_status.py alerts     # open / acknowledged alerts
```

The CLI never writes, calls out or sends; `--format json` is available.

**Growth digest readiness** is re-checked here. `sending_enabled` stays `false`, and the first
real email is a human decision.

## 5. Other components

- **Google Ads refresh:** a batched backlog.
  - `refresh_google_ads_metrics.py` is PLAN by default.
  - `--execute --expect-terms N` makes one call per 1000 terms.
  - The call is never automatic; the backlog appears in `system_status.py nightly`.
- **SaaS fact refresh:** plan only (`plan_content_clusters.py` shows each fact-research requirement). No automatic
  research.
- **Mobile Growth review:** DEFERRED. It needs a CHECK-constraint migration plus a WordPress
  relay redeploy, and a relay/WordPress deployment is a human stop.

## 6. Autonomy classification (A–E)

| Class | Meaning | Examples |
|---|---|---|
| **A** automatic, read-only | runs without a human; writes only local analysis / history | nightly analysis, content intelligence, health evaluation, follow-up measurement, Threads insights, approval sync |
| **B** automatic inside a human-approved policy | runs on its own only within caps set by a human | Threads publication of approved proposals, proposal stock generation, Growth lane posts (daily cap), targeted Threads consumption |
| **C** human approval per item, then the existing executor | one item at a time, hash-bound approval | internal link change requests, Threads proposals, article planning requests, discovery promotion, Growth reviews |
| **D** implemented, waits for a human activation | code and tests exist; a flag or a first run is a human decision | body text_edit apply, meta description write, Growth digest first email, Google Ads refresh execute, mobile review (relay deploy) |
| **E** excluded or deferred | not automated by design | affiliate placement apply / tracking URL / SubID (C11), automatic Keyword creation from discovery, Threads replies, approve-all |

**Automation boundary.** The system may analyse, measure, plan, notify about actionable problems,
and run already-approved work inside approved caps.

It never does any of these on its own:
- approve;
- create Keywords, Articles or planning requests;
- write to WordPress beyond an approved change request;
- change tracking;
- send a first-of-its-kind email;
- register or modify scheduled tasks.
