# System Productization (N6): single-site coupling inventory and the site profile

Affiliate AI runs one production site today. N6 separates the reusable core from the
site-specific parts without building multi-tenant SaaS. **Behaviour without a profile is
unchanged:** production still reads `.env` and `app/config/*.json`.

- Code:
  - `app/sites/profile.py` (profile schema, secret and data boundaries, per-site Settings)
  - `app/sites/dry_run.py` (bootstrap, network-guarded dry-run)
- CLI: `scripts/site_profile.py validate|bootstrap|dry-run <profile>`
- Second site: `sites/example-local/site.json` (its database lives under git-ignored `data/`)
- Contract tests: `tests/unit/test_site_profile.py`

## Coupling inventory (source inspection, 2026-09-30)

| Area | Where the single site is baked in | Classification | N6 boundary |
|---|---|---|---|
| site | WordPress host from `.env` (`WORDPRESS_BASE_URL`); brand `BizFluxLab` in the email subject prefix default | site | profile: `wordpress` capability (off = all WordPress fields empty); brand is a setting |
| account | WordPress user, Threads user / token, Google Ads customer, GA4 property, Search Console property, Make token | site + secret | fail-closed per capability; values only in the site's own env file |
| path | `D:\Logs\affiliate-ai` was hard-coded in `system_health_service.WORKER_LOG`, `project_state/runtime_records.py`, `operations/threads_worker_task.py`, `plan_threads_worker_schedule.py`; `D:\Backups\affiliate-ai` in runbooks | site | profile `paths.logs` for sites; the Python defaults now follow the launchers' `AFFILIATE_AI_LOG_DIR` and fall back to the same production path (`app/sites/paths.py`); backups stay a runbook path |
| provider | Google Ads, WordPress, Search Console, GA4, Threads, OpenAI, Make, SMTP / webhook | core adapters, site configuration | existing providers only; enabled per site with `capabilities`; no new adapters |
| config | `app/config/*.json` (26 files) is loaded from fixed paths: operations, health, nightly, growth, change-apply and product policies (**core defaults**) next to content clusters, subjects, discovery / keyword seeds, affiliate catalog / match rules, Threads style / growth facts (**this site's content policy**) | mixed | the content-intelligence inputs (clusters, portfolio, discovery seeds) are **per profile** through the optional `content_policy` block (injected into content intelligence, the nightly plan and discovery; no loader changed). Without the block a profile inherits this site's `app/config` files, and the dry-run says so. Affiliate match fit / tiers / catalog hygiene, keyword idea seeds / expansion rules, Threads style policy / growth facts are **per profile** through the optional `policies` block (`app/sites/policies.py`; each file is parsed by its real loader; unnamed files are inherited and reported as such). The example site has its own empty Threads growth facts so it never reuses this site's public facts. Production still loads `app/config` unchanged. |
| secret | `.env` (module-level `settings = get_settings()` in `app/config/settings.py`) | secret | profile Settings are built with `_env_file` = the site's env file or none; the production `.env` is refused; profiles cannot contain secret-like keys or values; every secret-like setting belongs to a capability (contract test) |
| DB | `DATABASE_URL`; module-level `engine` / `SessionLocal` in `app/config/database.py`; services take an injected `Session` (correction, 2026-09-30: the 4 write services `affiliate_link_target_service`, `article_link_substitution_service`, `article_publication_artifact_service`, `wordpress_content_update_execution_service` were listed as opening `SessionLocal` themselves; source inspection shows they already take the session in `__init__` and mention `SessionLocal` only in comments) | site | one SQLite database per profile; the production path is refused; any service can run against a site by passing that site's session |
| scheduler | `affiliate-ai-operations-daily/weekly`, `affiliate-ai-threads-worker`, `affiliate-ai-nightly-analysis`; `.cmd` launchers; contracts in `project_state/invariants.py` | site (runtime) | `scheduler` / `resident_worker` capabilities (off for the second site); nothing registers tasks for a profile |
| content policy | content clusters, subjects, portfolio, seeds, affiliate rules, Threads style / facts | site | clusters / portfolio / discovery seeds per profile (done); the rest see *config* |
| notification | SMTP / webhook settings, subject prefix, alert policy | site config + secret | `email` / `webhook` capabilities (off = no notifier configured) |
| approval settings | mobile approval relay secrets, runtime shared secret, probe state file | site + secret | `approval_relay` capability |
| external integration | Make ASP API, WordPress relay plugin, affiliate `/go/` tracking | site | `make` / `approval_relay` / `wordpress` capabilities; tracking stays unchanged until C11 |
| timezone | `operations_policy.json` / `nightly_analysis_policy.json` (`Asia/Tokyo`) | core default | profile states its `timezone`; core policy still decides it (covered by the overlay step) |

## Boundary

- **Reusable core:** `app/` services, pure modules, migrations and core policy defaults.
- **Site configuration:** `sites/<id>/site.json`, the site's own env file (secrets) and the
  site's own database.
- **Secret boundary:** secrets live only in env files.
  - A profile names its env file path, never values.
  - A disabled capability empties every related setting, even if the process environment has a
    value.
- **Data isolation:** one database per site. Every site's database is migrated with the same
  Alembic chain (per-site `alembic upgrade head` through `DATABASE_URL`).
- **Capabilities:**
  - `wordpress`, `search_console`, `ga4`, `google_ads`, `threads`, `openai`, `email`, `webhook`,
    `make`, `approval_relay`;
  - `scheduler` and `resident_worker` (runtime).

  All must be stated explicitly.
- **Install / bootstrap:** `site_profile.py bootstrap` creates the directories and migrates the
  site's database to head. It never touches the production database.
- **Dry-run:** `site_profile.py dry-run` runs these read-only steps against the site's settings
  and database, with **every network connection refused** in-process:
  - schema;
  - system health (no worker, no scheduled task expected);
  - content intelligence;
  - the nightly plan;
  - the Google Ads refresh plan;
  - the discovery plan;
  - the growth orchestrator;
  - the note ledger;
  - the manual metrics ledger.

  It also checks that the production config fingerprint is unchanged.

## Definition of Done status

The strong DoD asks for the second local site profile to bootstrap and dry-run without code
changes, with production untouched. **It is met for the read-only core (2026-09-30), and the second site uses its own content
policy (1 next-article candidate from its placeholder cluster instead of this site's 43):**

- `example-local` bootstrapped to head `a4a74a5bcb8b`;
- the 9 dry-run steps passed;
- 0 network attempts;
- production config unchanged;
- the production database and scheduled tasks were not used.

N6 hardening (2026-09-30, production behaviour unchanged):

- per-profile affiliate / keyword / Threads policy files (`policies` block; dry-run step
  `site_policies` reports profile vs inherited for each of the 7 files);
- write services: no change needed (they already take an injected session; see *DB*);
- log-path defaults: `app/sites/paths.py` follows the launchers' `AFFILIATE_AI_LOG_DIR` and
  falls back to `D:/Logs/affiliate-ai` (production does not set the variable). Used by the
  health check, the project-state worker log and the worker task plan.

Not done: wiring each consuming service to a site's policy files. The loaders accept the path;
the services are wired when a second real site exists (human decision).

A second **real** site (real accounts) is a human decision.
