# Threads manual posts (manual-post coexistence)

Decided by the human on 2026-10-02 (migration design approved; production migration, worker
restart and activation are separate human steps). A prerequisite of the T6.6 Threads Performance
Snapshot (see `docs/roadmap.md`).

**Manual posting is supported.** The human may post from the Threads app or a browser at any
time. affiliate-ai keeps working, never double-posts the same content, never mistakes a manual
post for one it published, and keeps approval and publication history clean.

## What a manual post does and does not do

| A manual post on our account… | |
|---|---|
| is included in analysis (insights, performance dataset, content / angle analysis, T6.6 snapshots) | **yes**, with `origin = manual` |
| counts toward system quotas (article posts per day, the one Growth post per JST day, same-day Growth retry / replacement, approval count, publication success, system-generated post count) | **no** — these keep reading `threads_publications` only |
| affects recent-post timing (cooldown / posting density) | **yes** — both lanes wait `account_posts.cooldown_minutes` (60) after it |
| feeds duplicate / recent topic / recent wording checks | **yes** — normal: identical text and topic overlap; Growth: the growth-duplication-2 wording rules |
| becomes an approved proposal's "published" | **never** |

The cooldown defers, it never cancels: the queue's next evaluation moves to the end of the wait
(inside the publication window) and approved proposals stay approved.

## Origin

One row per post of our own account in `threads_account_posts`, identity = the Threads post id
(never the body text; same text with a different id is a different post).

| origin | meaning |
|---|---|
| `system` | the post id matches a `published` row in `threads_publications` (publication evidence) |
| `manual` | found on Threads, no publication record and no publication in flight |
| `unknown` | cannot be decided safely: a publication with that id is not `published` yet, or a publication without a post id is in flight (creating / publishing / uncertain) |

Reconciliation: system evidence found later turns the same row into `system` (no second row).
`system` is never turned back into `manual`; `manual` becomes `system` only with a matching
publication record. Every change is an append-only event in `threads_account_post_events`
(`discovered`, `reconciled_system`, `origin_changed`, `classification_changed`, `text_changed`,
`missing_from_listing`, `reappeared`, `metrics_unavailable`, `proposal_superseded`; updates and
deletes are refused).

Edits and deletions: a changed text is a `text_changed` event (the latest text is used for the
checks). A post that should have been in the listing window but was not is
`missing_from_listing` — never assumed deleted; it is kept and `reappeared` clears it.
Unavailable metrics are a failed snapshot plus one `metrics_unavailable` event; nothing raises.

## Discovery

- **periodic:** every 15 minutes (`worker.subsystems.account_post_discovery.interval_minutes`)
  when the worker may read Threads (`--collect-insights` or `--auto-publish`). Read-only
  `GET /{user-id}/threads` with the same fields as the existing single-post read, one page, up to
  100 posts.
- **mandatory before publication:** right before any automatic publication (article and Growth
  lanes) the list is read again regardless of the last periodic read, then the queue and the T3
  plan are evaluated again from that state.
- **read failure:** fail closed — no Threads write, the proposal stays approved (not published, not
  rejected), outcome `account_post_refresh_failed` with the reason in the worker log, and the next
  evaluation is scheduled. A failing periodic read never stops the worker.

## Proposal collisions

When a newly found manual post (or its edit) substantially duplicates an approved / awaiting
proposal under the existing duplicate policy, the proposal is set to the existing state
`superseded` with `status_reason` "manual Threads post <id> made proposal unnecessary (<rule>);
not a system publication" and a `proposal_superseded` event on the manual post. No publication row
is created. Proposals that already have a publication (attempt) are never touched. Only manual
posts published in the last 14 days can supersede a proposal, so old posts found on the first
read (made before this feature) never remove current proposals.

## Post kind

System posts take their kind from the publication (article → `normal`, no article + angle
`account_growth` → `growth`, otherwise `unknown`). Manual posts start as `unknown` and are never
auto-classified. The human may classify them (append-only `classification_changed`):

```
uv run python scripts/manage_threads_account_posts.py classify <id> --kind growth --by human --reason "..."            # PLAN
uv run python scripts/manage_threads_account_posts.py classify <id> --kind growth --by human --reason "..." --execute
```

Other commands: `list [--origin manual]`, `show <id>` (with events), `dataset` (origin-aware
analysis rows), `discover [--execute]` (read-only listing; PLAN by default).

## Insights and analysis (T6.6-ready)

Every insight snapshot belongs to an account post (`threads_account_post_id`, required); system
snapshots also keep `threads_publication_id`. Manual / unknown posts are observed on the same
maturity schedule as system posts. Same account post + observed_at is stored once.
`ThreadsAccountPostService.dataset()` gives, per post: account post id, Threads post id, origin,
post kind, angle (unknown unless a system publication says otherwise), published_at, local hour,
text features and every observation with observed_at, post age and metrics (missing = NULL).
The T6.6 snapshots (03:00 / 09:00 / 15:00 / 21:00 JST, up to 100 posts) will run per account post
and keep the origin, so manual vs system and growth vs normal can be compared on initial
velocity, 6h / 12h / 24h, long tail and time of day. The T5 diagnostic now lists manual / unknown
posts as untracked from the ledger.

## Migration `3d5382e2a6bd` (additive)

`threads_account_posts`, `threads_account_post_events`; `threads_insight_snapshots` gains
`threads_account_post_id` (NOT NULL) and `threads_publication_id` becomes nullable. Backfill: every
published publication → one system account post with its existing Threads post id; every
snapshot is linked. The migration stops **before any change** if an identity cannot be resolved
(published without post id, snapshot post id mismatch, snapshot on a non-published
publication). Downgrade refuses while manual / unknown posts or publication-less snapshots exist.

### Rehearsal on a production copy (2026-10-02, final run after the last migration change)

Copy taken with the SQLite backup API (the production file was only read):

| check | result |
|---|---|
| before | alembic `a4a74a5bcb8b`; publications 55 (51 normal, 4 Growth); proposals 65; snapshots 4156 |
| upgrade | `a4a74a5bcb8b -> 3d5382e2a6bd` ok; alembic current = head = `3d5382e2a6bd` |
| account posts | 55 system (51 normal, 4 growth); 55 `reconciled_system` backfill events |
| orphans / duplicate post ids / unlinked or mismatched snapshots | 0 / 0 / 0 |
| `threads_publications`, `threads_post_proposals`, existing snapshot columns | content hashes identical before and after (quotas and counts unchanged) |
| foreign_key_check / integrity_check | clean / ok |
| rollback | downgrade to `a4a74a5bcb8b` ok (snapshots 4156, new tables gone), upgrade again ok |
| `alembic check` on the upgraded copy | no new upgrade operations (models = migration) |

## Production record (2026-10-02, human-approved)

| step | result |
|---|---|
| preflight | `main` `d523e7a` → feature `3d68bc7` fast-forward only; tree clean; DB `a4a74a5bcb8b`; worker task Running (15-minute recovery trigger, IgnoreNew); nightly / daily / weekly tasks next run tomorrow or later |
| isolated test | `test_export_article_plan_cli` 4 passed against an isolated migrated copy (it opens the default DB; the worktree had none) |
| worker stop | 14:13:24–14:13:30 JST: task temporarily **Disabled** (so the recovery trigger could not restart old code during the migration; actions / profile / trigger unchanged, task XML hash kept), then the worker process tree only (uv / python); 0 worker processes left |
| backup | `D:/Backups/affiliate-ai/affiliate_ai.pre-3d5382e2a6bd.20261002T051343Z.db` (SQLite backup API, sha256 `b3530ed35ea7563af937284d2bd5606fc487e071e9c6a55500f9356b3cb55aca`), integrity ok, foreign keys clean, 67 tables with the same row counts as production |
| merge | `git merge --ff-only` → `main` = `3d68bc7` |
| migration | `uv run alembic upgrade head` → `3d5382e2a6bd (head)`; `alembic check` no diff |
| post-check | publications 55 (51 normal, 4 Growth), proposals 65 and snapshots 4192 (existing columns) identical to the backup; the 65 other tables identical; 55 system account posts (51 normal, 4 growth); 55 backfill events; unlinked / mismatched snapshots 0; orphans 0; published without account post 0; duplicate post ids 0; foreign keys clean; integrity ok; daily article / Growth counts 2026-09-23..10-02 identical before and after |
| pending registry | `3d5382e2a6bd` removed from `PENDING_PRODUCTION_MIGRATIONS` |
| first discovery (read-only, before restart) | listed 55; all matched the 55 system rows; new 0, manual 0, unknown 0, origin changes 0, text changes 0, missing 0, reappeared 0, proposals superseded 0; proposal states unchanged; Threads writes 0 |
| policy in effect | cooldown 60 min, supersede cap 14 days, pre-publication refresh mandatory, listing 100, discovery every 15 min |

Snapshots grew from 4156 (rehearsal copy) to 4192 before the migration because the worker kept
importing insights until it was stopped; the migration preserved the 4192 exactly.

## Production activation (human steps)

The code must not run against the production DB before the migration: the scheduled tasks and the
worker run from the production working tree, so the branch is merged only together with the
migration.

1. **Approve** the migration and the restart.
2. **Stop writers:** end the `affiliate-ai-threads-worker` task; make sure the operations / nightly
   tasks are not running.
3. **Backup:** consistent copy with the SQLite backup API to
   `D:/Backups/affiliate-ai/affiliate_ai.pre-3d5382e2a6bd.<UTC>.db`; check `integrity_check` ok and
   the row counts (publications 55+, snapshots 4156+).
4. **Merge** `feat/threads-manual-post-coexistence` into `main` in the production working tree.
5. **Migrate:** `uv run alembic upgrade head`; check `alembic current` = `3d5382e2a6bd` and the
   rehearsal checks (orphans 0, unlinked snapshots 0, integrity ok).
6. **Record:** remove `3d5382e2a6bd` from `PENDING_PRODUCTION_MIGRATIONS` with the backup name.
7. **Restart** the worker with the same profile; in the worker log check `account_post_discovery`
   (first run: existing posts reconcile as `system`, any manual posts appear as `manual`), then
   `manage_threads_account_posts.py list --origin manual` and any `proposal_superseded` events.
8. **Rollback:** stop the worker, check out the previous `main`, restore the backup (or
   `alembic downgrade a4a74a5bcb8b` while no manual post was recorded), restart.

Human approval points: the migration, the restart, the 60-minute cooldown value, reviewing the
first manual posts found and any superseded proposals, and that every automatic publication now
costs one extra read-only call (the pre-publication listing).
