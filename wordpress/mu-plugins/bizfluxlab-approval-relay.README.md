# BizFluxLab Approval Relay — deployment notes

**Status: NOT DEPLOYED.** C8.8 built and tested this plugin in the repository
only. Publishing a new public approval endpoint is a production infrastructure
change and needs explicit approval before it goes to XServer.

## What it is

A decision relay. It shows one sanitized proposal on a phone and records exactly
one explicit human decision. It is **not** the content authority: affiliate-ai
owns the change request, the proposal hash/version, staleness detection, the
approval record and every WordPress mutation. This plugin contains no code path
that can edit a post.

It reuses the existing `BFL_Hmac` contract from the affiliate runtime plugin for
the authenticated affiliate-ai endpoints, and the same shared secret constant
(`BFL_AFFILIATE_RUNTIME_SECRET`). No new authentication mechanism is introduced.

## Files

| Path | Role |
|---|---|
| `bizfluxlab-approval-relay.php` | MU-plugin: storage, REST routes, review page |
| `bizfluxlab-approval-relay/lib-core.php` | Pure core: capability, state machine, rendering, headers |
| `bizfluxlab-approval-relay/tests/run.php` | PHP CLI harness (no WordPress, no MySQL, no network) |

Verify before deploying:

```
php wordpress/mu-plugins/bizfluxlab-approval-relay/tests/run.php
```

Currently: **36 passed, 0 failed**. It also runs inside pytest as
`tests/unit/test_wp_approval_relay_php_harness.py`.

## Dependency

`bizfluxlab-affiliate-runtime.php` must already be loaded — this plugin calls
`BFL_Hmac::verify()` from its `lib-core.php`. Both live in `mu-plugins/`, which
WordPress loads alphabetically, so `bizfluxlab-affiliate-runtime.php` loads
first. Do not rename either file without rechecking that order.

## Deployment steps (require explicit approval)

1. Upload `bizfluxlab-approval-relay.php` and the
   `bizfluxlab-approval-relay/` directory to `wp-content/mu-plugins/`.
   Do **not** upload `tests/` to production.
2. Confirm `BFL_AFFILIATE_RUNTIME_SECRET` is already defined in `wp-config.php`
   (the affiliate runtime uses it). No new secret is needed. If it is absent,
   the authenticated endpoints fail closed and no session can be created.
3. Log into `/wp-admin` once as an administrator. `admin_init` creates
   `wp_bfl_approval_sessions` via `dbDelta`. Anonymous traffic never triggers
   schema installation.
4. Verify the table exists and has the unique keys
   `uq_relay_session` and `uq_capability`.
5. Confirm `https://bizfluxlab.com/bfl-approval/<32-hex>` returns the shell page
   with `X-Robots-Tag: noindex, nofollow, noarchive` and
   `Cache-Control: no-store`.
6. Confirm `https://bizfluxlab.com/robots.txt` now contains
   `Disallow: /bfl-approval/`.
7. Confirm `/go/<token>` still redirects exactly as before — this plugin must
   not have changed it in any way.

## Post-deployment test procedure

Use a **non-actionable** subject first. Do not use a real change request for the
first end-to-end test.

1. On the PC, create a throwaway proposal or use a request you are willing to
   reject, and run the PLAN:
   `uv run python scripts/send_mobile_approval.py --request-id <id>`
2. Send it: `... --execute`. Exactly one email should arrive.
3. Open the link on the phone. Confirm the proposal renders, and that the
   address bar no longer shows the capability after load.
4. **Before** deciding, confirm scanner safety: fetch the same URL **without**
   the fragment (`curl -sSI https://bizfluxlab.com/bfl-approval/<id>`). It must
   return 200 with the security headers and must not change the session state.
5. Press 却下する with a reason (rejecting is the safe direction for a first
   test).
6. On the PC: `uv run python scripts/sync_mobile_approvals.py` (PLAN) — it
   should report `would_apply`.
7. `uv run python scripts/sync_mobile_approvals.py --execute` — the rejection is
   recorded through `ChangeRequestService` with `decided_by=human-mobile`.
8. Confirm the relay session is now `consumed` and a replay of the sync applies
   nothing.

## What is deliberately not stored

No IP address, no User-Agent, no Referer, no raw capability, no SMTP
credentials, no Google credentials, no affiliate tracking URL, no `/go/` token,
and no copy of the local database. The relay keeps a session id, a capability
digest, the subject identity, one sanitized snapshot, an expiry and one decision.
