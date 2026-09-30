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
