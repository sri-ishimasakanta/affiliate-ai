# SaaS Validation (N7): pilot plan and measurement infrastructure

N7 finds out, **with real pilot users**, whether the productized system (N6) is worth something
to others, and prepares the SaaS / Managed Service / Hybrid decision. Until real pilots exist,
only the plan and the measurement infrastructure are built. **No usage, cost or
willingness-to-pay number is ever invented.**

- Code: `app/n_track/pilot.py` (evidence summary), `app/n_track/metrics.py` (pilot metrics).
- Policy: `app/config/pilot_policy.json`. Its go / no-go thresholds are **a proposal**
  (`status: proposed`); the human sets them before the first pilot.
- CLI:
  - `scripts/record_manual_metric.py record --kind pilot --ref pilot-01 …` (enter numbers);
  - `scripts/pilot_evidence.py summary` (read-only).
- Tests: `tests/unit/test_pilot_evidence.py` (clearly labelled synthetic fixtures only).
- Storage: the N3 ledger (`manual_metric_entries`, migration `a4a74a5bcb8b`, production apply
  pending). No new table.

## Ready for real pilots (2026-10-01): the pilot record and evidence rules

**State: ready to accept real pilots; no real pilot yet (evidence pending).** N7 is not complete
until real pilots ran and the human recorded the decision.

- Code: `app/n_track/pilot_registry.py` (events, rules, summary, N8 gate),
  `app/services/pilot_registry_service.py`, CLI `scripts/manage_pilots.py` (PLAN by default;
  `--execute` writes). Tests: `tests/unit/test_pilot_registry.py`.
- Storage (no migration): lifecycle and qualitative evidence are an **append-only event file**
  `data/n7/pilot_events.jsonl` (local, git-ignored, like the database). Numbers stay in the N3
  ledger (`manual_metric_entries`, `subject_kind = pilot`). A new pilot metric
  `payment_received_jpy` records an actual payment.

### Evidence rules

| Kind (`--evidence-kind`) | Meaning | Counted |
|---|---|---|
| `observed_fact` | the human saw it (the pilot's screen, a log, a receipt) | yes |
| `human_reported` | the pilot told the human (interview, message) | yes |
| `measured_metric` | a number taken from a record (also entered in the N3 ledger) | yes |
| `inference` | the human's reading of the evidence | kept, **not counted** |
| `hypothesis` | something to test later | kept, **not counted** |
| *(missing)* | nothing recorded | shown as `missing`, **never 0** |

- Only pilots registered by a human (`provenance = human_entry`) with `--agreement-confirmed`
  (the human holds the pilot's agreement outside this repository) are counted. Test fixtures
  (`test_fixture`) and ledger numbers for unregistered `pilot-xx` refs are excluded and listed.
- Lifecycle events (register, onboarding, close, withdraw) must be `observed_fact` or
  `human_reported`.
- Missing is not 0: an absent number is `missing`; an absent payment is `missing`, not `false`;
  a recorded 0 is a real 0.
- Append-only: nothing is edited or deleted. A mistake gets a `correct --supersedes <id>` event;
  correcting a registration removes the pilot from the counts. After `close` or `withdraw`, only
  corrections are accepted. The file refuses hand edits (ids must stay in order).
- Never record: names, emails, phone numbers, URLs that identify a person, credentials, made-up
  or "expected" numbers, synthetic feedback, or a payment / continuation that was not confirmed.

### What the human does when a real pilot starts

1. Agree with the pilot (outside this repo) and choose a pseudonym (`pilot-01`, …).
2. Register (PLAN first, then add `--execute`):

   ```bash
   uv run python scripts/manage_pilots.py register pilot-01 --started-at 2026-10-10T10:00:00+09:00 --use-case "<short use case, no personal data>" --acquisition own_network --agreement-confirmed --source "pilot agreement (kept by the human)" --by human
   ```

3. As things happen: `onboarding pilot-01 --state in_progress|completed|blocked`, `usage`,
   `outcome`, `feedback`, `blocker --category setup|approvals|cost|trust|other`, each with
   `--note`, `--evidence-kind`, `--source`, `--by`, `--observed-at`.
4. Numbers (activated, time to first value, workflows, active days, ratings, support minutes,
   costs, willingness to pay, payment received): `record_manual_metric.py record --kind pilot
   --ref pilot-01 --metric <m> --value <v> --observed-at <time> --source "<where>" --by human`.
5. At the end: `close pilot-01` (or `withdraw pilot-01 --reason …`).
6. Check: `manage_pilots.py summary` (counts, per-pilot coverage, missing evidence, blockers,
   N8 gate).

### N8 decision gate (`summary` → `n8_gate`)

- `insufficient_evidence`: fewer than `min_pilots` (3) real pilots, or any go / no-go criterion
  lacks evidence. With no real pilot it is always this.
- `needs_human_policy`: enough evidence, but `pilot_policy.json` is still `proposed`; the human
  confirms the thresholds first (no new thresholds are invented here).
- `ready_for_human_decision`: the criteria results and the existing model-signal rule
  (`rule_reading`) are shown with the candidates SaaS / Managed Service / Hybrid. **It never
  decides**; the human records the decision with the template below, then N8 may start.

## Pilot plan (for when the human recruits pilots)

1. **Recruit** (human): a small number of real users. At least `min_pilots` (3) are needed
   before any criterion can be evaluated. Agree on the terms of the pilot with each one.
2. **Pseudonymize:** each pilot is `pilot-01`, `pilot-02`, …. The mapping from pseudonym to
   person is kept by the human outside this repository. No names, emails, phone numbers or URLs
   are recorded; the ledger refuses them.
3. **Onboard:** each pilot gets their own site profile (N6, `sites/<pilot-site>/site.json`):
   - their own database and their own env file for their credentials;
   - only the capabilities they need.

   Enabling a real provider for a pilot is a human decision (new provider connection).
4. **Measure** (by hand, from the pilot's own records and conversations):

| Metric | Unit | Meaning |
|---|---|---|
| `activated` | flag | the pilot completed onboarding and ran the first workflow |
| `time_to_first_value_hours` | hours | from the start of onboarding to the first useful result |
| `workflows_completed` | count | end-to-end workflows completed |
| `active_days` | count | days with real use in the observation period |
| `onboarding_difficulty` | 1–5 | the pilot's own rating (5 = very hard) |
| `support_minutes` / `support_contacts` | minutes / count | support burden |
| `operating_cost_jpy` / `api_cost_jpy` | jpy | cost to run for this pilot in the period |
| `willingness_to_pay_jpy` | jpy | what the pilot says they would pay per month |
| `value_rating` | 1–5 | structured feedback: how valuable (5 = very) |
| `would_continue` | flag | structured feedback: would keep using it |
| `friction_setup` / `friction_approvals` / `friction_cost` / `friction_trust` | flag | where the pilot got stuck |

   Every entry records its period, observation time, source ("pilot interview", "pilot's own
   dashboard", …) and who entered it. A free-text note is allowed but must contain no personal
   data. Corrections supersede entries; nothing is deleted.
5. **Summarize:** `pilot_evidence.py summary` shows, for each criterion, one of:
   - `met`;
   - `not_met`;
   - `insufficient` (too few pilots or no numbers).

   It then gives one overall reading:
   - `insufficient_evidence`;
   - `evidence_supports_go`;
   - `evidence_against_go`;
   - `mixed`.

   It also gives descriptive model signals: self-service (easy onboarding and little support) or
   managed (high value but hard onboarding or much support), which suggests SaaS-leaning,
   Managed-leaning or Hybrid.

## Go / no-go (the human decides)

The proposed criteria are listed in `pilot_policy.json`:

- activation rate;
- median time to first value;
- workflow completion rate;
- repeat usage;
- onboarding difficulty;
- support minutes;
- value rating;
- would-continue rate;
- cost within willingness to pay.

The summary is an input to the decision. **It never decides.**

The decision is recorded in the decision log, using the template below.

```
### N7 decision: <go | no-go | extend pilots> — <SaaS | Managed Service | Hybrid | none>
- 記録: <date>
- 領域: n-track/saas-validation
- 理由: <criteria met / not met, with n and period; what the pilots said>
- 根拠: pilot_evidence.py summary (<date>), docs/operations/saas-validation.md
- 結果の状態: <what N8 will build; for Hybrid, the self-service / managed boundary>
- 次にすること: <N8 scope, or more pilots>
```

## What N8 needs from N7

- the chosen model;
- the evidence behind it;
- for Hybrid, which responsibilities are self-service and which are managed;
- the support and cost numbers that size the service.

Nothing in N8 is built before this record exists.
