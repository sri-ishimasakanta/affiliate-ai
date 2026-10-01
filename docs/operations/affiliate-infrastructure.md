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
uv run python scripts/affiliate_inventory.py verify <id> --status approved --source "provider dashboard" --by human --observed-at 2026-10-02T10:00:00+09:00 [--execute]
```

Output never contains a tracking URL, a destination URL, a token value or a secret (presence and
counts only).

## Baseline (2026-10-01, production DB, read-only)

19 programs across 6 provider labels (direct 6, PartnerStack 5, Impact 4, FirstPromoter 2, make 1,
multi_network 1); catalog status active 16 / unknown 2 / paused 1; status verified at the provider
0 / 19; tracking URL 1 / 19 (Make); attribution PARTIAL 1 (Make), UNKNOWN 18, FULL 0. Published
articles 25: linked 3, program without link 9, supporting without program 13, affiliate without a
monetization path 0; `monetization_mode` missing on 1 (article 1, which is linked
through Make while 6 more assigned programs have no link).
Clicks 50 (49 on Make targets, 1 on a token that is not a link target); commission facts 0.
Human action queue 60 items: P1 8, P2 10, P3 1, P4 41.
