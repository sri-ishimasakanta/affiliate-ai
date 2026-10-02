# Affiliate infrastructure & revenue attribution (C11)

C11 first half (2026-10-01): inventory of ASPs, programs and links, coverage, ASP operations
detections, attribution readiness, and the human action queue. **Read-only and local.** Nothing
is sent to any provider; no tracking URL is created; no parameter is added; no link is replaced;
WordPress / note / Threads are untouched; no migration.

- code: `app/revenue/affiliate_inventory.py` (pure rules), `app/services/affiliate_inventory_service.py`
  (reads the DB, the human verification file), `scripts/affiliate_inventory.py` (CLI)
- provider capabilities: `app/config/affiliate_provider_capabilities.json`
- human verifications: `data/affiliate/program_verifications.jsonl` (append-only, gitignored)
- tests: `tests/unit/test_affiliate_inventory.py` (synthetic fixtures only)

## What already existed (not duplicated)

| Layer | Existing piece |
|---|---|
| program catalog | `affiliate_programs` (provider label, catalog status `active / paused / ended / unknown`, commission, landing page, tracking URL) |
| article ↔ program | `article_affiliate_programs` (primary flag) |
| tracked link | `affiliate_link_targets` (token, one active per article × program) → WordPress MU-plugin `/go/{token}` redirect |
| placement | `article_link_substitution_mappings` (external-link occurrence ordinal in the article body) |
| clicks | `affiliate_outbound_clicks` (token only; joined to article × program through the target) |
| conversions | `affiliate_commission_facts` from the Make importer (no click reference) |
| detections | `revenue_optimization_candidates` (C7) and `attribution_readiness` (C10-A) |

C11 reads these. It adds no table and no column.

## Unified model without a migration

Fields the DB does not have are kept outside it, each with its source:

| Field | Where | Rule |
|---|---|---|
| provider capability (SubID, conversion reference, import API, manual report, program filter, cookie window) | capability JSON | `true` / `false` only with repository evidence (code that exercises it); otherwise `"unknown"` with `evidence: null`. Nothing comes from public knowledge of an ASP. |
| status at the provider, provider program id, SubID support, cookie window, tracking URL obtained, commission terms confirmed | verification JSONL | entered by a human from the provider dashboard (`verify`, PLAN by default); the latest record per program wins; no URL, secret or personal data accepted; observed time needs a timezone and cannot be in the future |
| last verified | verification JSONL | never verified = `never_verified`; a stale judgement only when `--max-verification-age-days` is given |

Missing stays missing: a program with no provider label is `unrecorded` (all capabilities
unknown); 0 commission facts are not read as "no conversions" when conversions cannot be read.

## Coverage

Per published article: `linked` (at least one active placement), `program_status_unknown`,
`program_without_link`, `supporting_no_program` (editorial intent: supporting articles send
readers to affiliate articles; not a gap), `no_program` (an affiliate / unclassified article with
no program: a gap). Linked articles that still have assigned programs without a link are listed
separately. Placement granularity is the external-link occurrence ordinal only (CTA / section
positions are not recorded).

## ASP operations detections (`ops`)

active program without tracking URL · tracking URL without placement · placement whose program is
not active · never verified / stale verification · paused or ended · unknown status · duplicate
destination or tracking URL across programs · one token mapped several times in one article ·
articles without a monetization path · supporting articles without a program · articles needing
replacement (inactive program assigned) · clicks whose token is not a link target (counts only) ·
providers that need a human at the provider dashboard. Not checked here: destination reachability
(no HTTP request is made) and program end at the provider (only known from a human verification).

## Attribution readiness

| Class | Rule |
|---|---|
| FULL | SubID accepted **and** conversions return the reference (both verified) **and** an active tracked link |
| MANUAL | an active tracked link; conversions only in the provider dashboard (verified), no import API |
| PARTIAL | an active tracked link: article-level clicks through `/go/`; conversions cannot be tied to an article |
| NONE | SubID and conversion reference both verified absent, and no tracked link |
| UNKNOWN | anything else (not enough verified information) |

A human verification of `subid_supported` overrides the provider default for that program.

## SubID policy

Only for a provider whose SubID support is verified. Today no provider is verified, so the design
state is `not_applicable_yet`: no tracking identity is appended to any URL. Internally the `/go/`
token already identifies article × program; when a provider is verified, the SubID value will be
derived from that identity (no personal data, no click-level user id) and the change to the
outgoing link is a separate, human-approved step.

## Manual revenue import — needs a migration (STOP, not done)

The N3 manual metric ledger (`manual_metric_entries`) has a DB CHECK constraint
`subject_kind IN ('note_piece', 'channel', 'product', 'pilot')`. Recording manual ASP revenue per
program needs a new subject kind (for example `affiliate_program`), which is a production migration
and therefore a human stop condition. Nothing was entered; missing revenue stays missing (≠ 0).

## Human action queue and priorities

Priority is decided only by what an item blocks:

| Priority | Blocks |
|---|---|
| P1 | monetization of a published article already assigned to the program |
| P2 | knowing whether the program is usable at all (status / tracking URL) |
| P3 | article-level conversion attribution for a program that already has links |
| P4 | metadata that sizes revenue (commission terms, cookie window, provider program id) |

Each item: provider, program, reason, required value, priority, what it blocks. The human does
the provider-side work (log in, apply, read status, obtain the tracking URL); the result is entered
with `verify` (facts) or the existing program / link flows (tracking URL, link mapping).

## CLI

```
uv run python scripts/affiliate_inventory.py providers | programs | program <id> | coverage
uv run python scripts/affiliate_inventory.py missing-links | stale | attribution | actions | ops
uv run python scripts/affiliate_inventory.py report --out reports/affiliate/inventory.json
uv run python scripts/affiliate_inventory.py verify <id> --evidence provider_dashboard --status approved --source "provider dashboard" --by human --observed-at 2026-10-02T10:00:00+09:00 [--execute]
```

Output never contains a tracking URL, a destination URL, a token value or a secret (presence and
counts only).

## Registration before tracking (2026-10-02)

**Catalog status is not the partnership status.** `affiliate_programs.status = active` means the
program is a *candidate* in this project's catalog (the program exists and is worth considering).
It does **not** mean an account exists, an application was sent, or the partnership is approved.
The partnership status at the ASP / advertiser exists only in human verification records
(`status_at_provider`, verification layer only, no migration):

`not_registered` · `not_applied` · `applied` · `pending` · `approved` · `active` · `rejected` ·
`paused` · `ended` · `unknown`

Every verification record states its evidence (`--evidence`, required, no default):

| evidence | meaning | shown as |
|---|---|---|
| `provider_dashboard` | checked now in the ASP / advertiser dashboard | provider-verified |
| `provider_email` | checked now in a message from the provider (e.g. the decision email) | provider-verified |
| `human_recollection` | remembered by the human (a past event not re-checked) | `human_reported` — never provider-verified |

Recollections are kept in a separate `reported` block: they place the program in a registration
bucket (labelled `human_reported`) but never count as a provider verification, never feed the
capabilities and never open the tracking intake. A later dashboard / email check supersedes them
(the recollection record stays).

### Registration buckets (computed, read-only)

| Bucket | Rule | Next human step |
|---|---|---|
| A_APPLY_OR_REGISTER | no status, `unknown`, `not_registered` or `not_applied` | check whether an account / application exists; register and apply if not |
| B_WAITING_REVIEW | `applied` / `pending` | wait for the result; record it with its evidence |
| C_REJECTED_OR_DEFERRED | `rejected` / `paused` / `ended` (provider-verified or human-reported) | none until reapplication conditions are met; no tracking, capability or metadata work |
| D_APPROVED_NEEDS_TRACKING | `approved` / `active` | obtain the tracking URL |
| E_READY_FOR_ONBOARDING | approved and the tracking URL obtained, or a tracking URL already registered | `onboard` and `approve-host` |
| ONBOARDED | tracking URL registered, host authorized and an active link target | existing link flows |

The human action queue follows these steps (priority still by what an item blocks); programs in
C are listed under `deferred_programs`, not in the queue. **Tracking intake is gated:** `onboard`
and `approve-host` refuse unless the latest provider-verified `status_at_provider` is `approved`
or `active` (a recollection does not open it; a later rejection closes it again).

Registration fields (`verify`): `--account-registered` (true / false / unknown), `--applied-on`,
`--decided-on` (YYYY-MM-DD), `--rejection-reason` (site_size_or_traffic / content_or_category /
region_or_language / policy / other / not_stated; only with `--status rejected`),
`--reapply-allowed` (true / false / unknown, the provider's rule), `--reapplication-plan`
(deferred / planned / not_planned / undecided, this project's decision).

### ASP signup checklist (per provider / program)

Nothing here is stored as a credential. Passwords, API keys, tokens and session data are never
entered into affiliate-ai (they stay in the human's password manager).

| Item | Where it is recorded |
|---|---|
| provider / program | catalog (`--program-id`, `--actual-provider` if the platform differs) |
| registration location | only the program information page cited in the catalog notes (2026-08-28) is known; the signup page itself is found by the human at the provider |
| account required / registered | `--account-registered` |
| application required / submitted | `--status applied` (or `not_applied`), `--applied-on` |
| website URL submitted | the site's own URL (not stored per program) |
| site / category description, traffic / site-size answers | kept by the human; not stored |
| application status | `--status` with `--evidence` |
| submitted_at | `--applied-on` |
| result | `--status approved / rejected`, `--decided-on` |
| rejection reason | `--rejection-reason` |
| reapply allowed | `--reapply-allowed`; our plan: `--reapplication-plan` |
| approval evidence source | `--evidence provider_dashboard / provider_email` and `--source` (no URL) |

## ASP intake (2026-10-02): tracking URL, program host authorization, structured verification

Local only. Nothing here logs in to an ASP, calls a provider API, creates a link target, maps a
link into an article, pushes the WordPress projection or edits an article. No migration.

- code: `app/services/affiliate_tracking_intake_service.py`, `scripts/affiliate_tracking_intake.py`,
  `app/affiliate/program_host_approvals.py`, `app/affiliate/destination_policy.py`
  (`is_destination_approved`)
- program host approvals: `app/config/affiliate_program_host_approvals.json` (version-controlled,
  append-only; empty until a human approves a host)
- tests: `tests/unit/test_affiliate_tracking_intake.py`, `tests/unit/test_affiliate_inventory.py`

### Tracking URL intake (`onboard`)

The generic version of the Make-only `onboard_make_affiliate.py`:

- PLAN by default; `--execute` writes **only** `affiliate_programs.tracking_url` (through the
  existing `AffiliateProgramService.update_program`). No link target, mapping, projection or
  WordPress change.
- the URL is never a CLI argument: hidden prompt (`getpass`, not echoed) or `--url-stdin` (first
  line of stdin, e.g. `Get-Clipboard | … --url-stdin`), so it stays out of the shell history.
- identity first: `--program-id` plus `--expect-name` / `--expect-provider` must match the catalog
  before the URL is asked for.
- validation (existing `validate_destination_url` plus intake rules): https only, no userinfo,
  port absent or 443, no backslash, no fragment, not this site; the URL is stored exactly as given.
- refused: the same URL already on another program or on another program's link target; a
  different URL when the program already has one (replacement is a separate operation, not
  provided); a program whose catalog status is not active.
- output: program identity, scheme, host, query parameter **names**, length, the first 16 hex of
  the URL's SHA-256, whether the host is authorized for the program, and the catalog landing host
  for comparison (a landing host is never an authorization). Unexpected exceptions are printed
  without their message (a database error may carry the URL as a bound parameter).

### Program-level host authorization (`approve-host` / `revoke-host`)

- `is_destination_approved` = the existing provider rule (Make → `www.make.com`, unchanged) **or** a
  program rule recorded by a human. Default deny.
- provider-level rules are never used for aggregate labels (`direct`, `multi_network`,
  `unrecorded`); the module refuses to load if one is added. A host approved for program A does
  not authorize program B, even when both are `direct`.
- a program rule is bound to `(program_id, program_name, provider, host)`: renaming the program or
  changing its provider voids it. Exact normalized host match only (no wildcard, suffix or partial
  match). Redirects are not followed and not trusted: the approval covers the tracking URL's own
  host.
- approval needs the tracking URL to be registered first, and the approved host must be that
  URL's host. The catalog landing host cannot be approved unless it *is* the tracking host. IP
  addresses, single-label names, this site and the synthetic probe host cannot be approved.
- PLAN by default; `--execute` appends a record (who, when observed, source text without URL) to
  the approvals file; review the git diff and commit it. Revocation is another appended record.
- enforced where hosts were already checked: `AffiliateLinkTargetService.create_target` and the
  publication artifact inspection (`host_policy_eligible`).

### Structured verification (`verify`, extended)

The same append-only `data/affiliate/program_verifications.jsonl` (schema
`affiliate-program-verification/1`; new optional fields). Later records add or update individual
fields; a later status-only check does not erase an earlier SubID answer (field-level merge with
per-field provenance). Leaving an option out records nothing (missing). Capabilities take
`true` / `false` / `unknown`; `unknown` means "looked, could not tell" and is never read as false.

### Catalog vs verification provenance

The inventory shows each program's `catalog` block (the DB values, provenance
`catalog (affiliate_programs)`) next to its `verified` block (fields with record id, time, person
and source). Differences (commission, currency, provider vs actual provider, landing host,
active vs paused / ended / rejected) are listed under `differences` and
`operations.catalog_differs_from_verification`. A verification never updates the catalog; a
catalog change is a separate, explicit operation.

### Capabilities (read-only materialization)

There is no existing path that writes a human observation into the provider capability config,
and none is added. Per program, the inventory uses: human verification > provider config (repository
evidence only) > unknown, and shows the source of each value. Per provider it counts the observed
values (`observed_capabilities`). Attribution uses these per-program values: FULL needs SubID
`true` and source attribution `true` with a tracked link; MANUAL needs a tracked link and
conversions visible in the dashboard without an import API.

### Synthetic probe click

The one click whose token is not a link target is the documented synthetic `/go` E2E probe
(`docs/operations/synthetic-runtime-click-e2e.md`: token SHA-256 fingerprint, `source_click_id` 1,
import run 2). The inventory now classifies it at read time by that fingerprint as
`operations.synthetic_probe_clicks` and leaves `clicks_with_unknown_token` for real unknowns.
The click row is not changed.

### Human ASP capture workflow (per program)

0. registration first (see *Registration before tracking*): the intake below starts only after
   `approved` / `active` is recorded with `--evidence provider_dashboard` or `provider_email`
1. `affiliate_inventory.py verify <id> --evidence provider_dashboard …` (PLAN) → check → same
   command with `--execute`
2. `affiliate_tracking_intake.py onboard --program-id <id> --expect-name … --expect-provider …`
   (PLAN, hidden URL) → check host / fingerprint → same with `--execute` (URL asked again)
3. `affiliate_tracking_intake.py approve-host … --host <host shown in step 2>` (PLAN) → check →
   same with `--execute` → review and commit `app/config/affiliate_program_host_approvals.json`
4. `affiliate_tracking_intake.py status --program-id <id>` and
   `affiliate_inventory.py program <id>` (read-only)

Creating link targets, mapping them into articles and pushing the projection stay separate,
later, human-approved steps (existing flows).

### What each observed value goes into

| Seen in the ASP dashboard | CLI | Field / option | Values |
|---|---|---|---|
| approval / status | `verify` | `--status` → `status_at_provider` | not_registered / not_applied / applied / pending / approved / active / rejected / paused / ended / unknown |
| how it was seen | `verify` | `--evidence` (required) | provider_dashboard / provider_email / human_recollection |
| account / application / decision | `verify` | `--account-registered`, `--applied-on`, `--decided-on` | true / false / unknown; YYYY-MM-DD |
| rejection reason / reapply | `verify` | `--rejection-reason`, `--reapply-allowed`, `--reapplication-plan` | see *Registration before tracking* |
| tracking URL obtained | `verify` | `--tracking-url-obtained` | true / false |
| tracking URL itself | `onboard` | hidden prompt / `--url-stdin` → `affiliate_programs.tracking_url` | the URL (never on the command line) |
| tracking URL host | `approve-host` | `--host` → approvals file | host shown by `onboard` |
| provider program / advertiser id | `verify` | `--provider-program-id` | short id |
| link id | `verify` | `--link-id` | short id |
| SubID / custom parameter | `verify` | `--subid-supported` | true / false / unknown |
| click reporting | `verify` | `--click-reporting` | true / false / unknown |
| conversion reporting | `verify` | `--conversion-reporting` | true / false / unknown |
| source / content attribution in conversions | `verify` | `--source-attribution` | true / false / unknown |
| commission type | `verify` | `--commission-type` | percentage / fixed / tiered / hybrid / other |
| commission value | `verify` | `--commission-value` | number |
| currency | `verify` | `--commission-currency` | 3-letter code (USD, JPY) |
| commission terms confirmed | `verify` | `--commission-terms-confirmed` | true / false |
| cookie window | `verify` | `--cookie-window-days` | whole days |
| landing page | `verify` | `--landing-host` | host only (no URL) |
| pause / end notice | `verify` | `--pause-end-notice` (+ `--notice-effective-date`) | none_seen / pause_announced / end_announced / unknown (+ YYYY-MM-DD) |
| actual provider / platform | `verify` | `--actual-provider` | short name |
| observed_at | all write commands | `--observed-at` | ISO time with timezone, not in the future |
| evidence source | all write commands | `--source` | short text, no URL / secret / personal data |

Anything not visible in the dashboard is left out (stays missing) or given as `unknown` for a
capability. Never guess.

### Manual revenue import (dependency kept)

Still needs a migration (`manual_metric_entries.subject_kind` CHECK). After the P1 capture, the
choice is made from the recorded fields: conversions returned with a source reference through an
API → provider API importer (like Make); dashboard export only → CSV import; dashboard numbers only
→ extend the manual metric ledger (migration, human decision).

## Baseline (2026-10-01, production DB, read-only)

19 programs across 6 provider labels (direct 6, PartnerStack 5, Impact 4, FirstPromoter 2, make 1,
multi_network 1); catalog status active 16 / unknown 2 / paused 1; status verified at the provider
0 / 19; tracking URL 1 / 19 (Make); attribution PARTIAL 1 (Make), UNKNOWN 18, FULL 0. Published
articles 25: linked 3, program without link 9, supporting without program 13, affiliate without a
monetization path 0; `monetization_mode` missing on 1 (article 1, which is linked
through Make while 6 more assigned programs have no link).
Clicks 50 (49 on Make targets, 1 on a token that is not a link target — since 2026-10-02 classified as the
documented synthetic probe); commission facts 0.
Human action queue 60 items: P1 8, P2 10, P3 1, P4 41.
