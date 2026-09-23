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

It reuses the existing `BFL_Hmac` *request format* from the affiliate runtime
plugin for the authenticated affiliate-ai endpoints. No new authentication
mechanism is introduced.

**The key is not shared.** Redirect traffic and human approval authority are
different trust domains, so leaking one key must not hand over the other:

| Domain | WordPress constant | Local `.env` |
|---|---|---|
| Affiliate redirect runtime (`/go/`) | `BFL_AFFILIATE_RUNTIME_SECRET` | `AFFILIATE_RUNTIME_SHARED_SECRET` |
| Mobile approval relay | `BFL_APPROVAL_RELAY_SECRET` | `APPROVAL_RELAY_SHARED_SECRET` |

There is **no fallback between them** in either direction. If
`BFL_APPROVAL_RELAY_SECRET` is missing or blank, every authenticated relay
endpoint fails closed; the public read-only shell still renders, because it
reads nothing and can decide nothing.

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

Currently: **43 passed, 0 failed** (including cross-secret rejection in both
directions). It also runs inside pytest as
`tests/unit/test_wp_approval_relay_php_harness.py`.

## Step 0 — generate the approval relay secret (do this first)

Generate an independent high-entropy value locally. Do **not** reuse the
affiliate runtime secret, and do not paste the value into chat or a commit:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the **same** value in two places:

1. local `.env`:
   ```
   APPROVAL_RELAY_SHARED_SECRET=<the generated value>
   ```
2. XServer `wp-config.php`, above the `/* That's all, stop editing! */` line:
   ```php
   define( 'BFL_APPROVAL_RELAY_SECRET', '<the generated value>' );
   ```

Leave `BFL_AFFILIATE_RUNTIME_SECRET` and `AFFILIATE_RUNTIME_SHARED_SECRET`
exactly as they are. Rotating the approval secret later means changing both
places together; any in-flight review session is unaffected because the
capability is independent of this key.

Confirm locally without revealing the value:

```bash
uv run python -c "from app.config.settings import get_settings as g; print('approval_relay_secret_configured =', g().approval_relay_configured)"
```

## Dependency

`bizfluxlab-affiliate-runtime.php` must already be loaded — this plugin calls
`BFL_Hmac::verify()` from its `lib-core.php`. Both live in `mu-plugins/`, which
WordPress loads alphabetically, so `bizfluxlab-affiliate-runtime.php` loads
first. Do not rename either file without rechecking that order.

## Deployment steps (require explicit approval)

Order matters: the secret goes in **before** the code, so the endpoints are
never briefly reachable with an unconfigured key.

1. Generate the independent approval relay secret (Step 0 above).
2. Put it in the local `.env` as `APPROVAL_RELAY_SHARED_SECRET`.
3. Put the same value in XServer `wp-config.php` as
   `BFL_APPROVAL_RELAY_SECRET`. Confirm `BFL_AFFILIATE_RUNTIME_SECRET` is
   still present and unchanged.
4. Upload `bizfluxlab-approval-relay.php` and the
   `bizfluxlab-approval-relay/` directory to `wp-content/mu-plugins/`.
   Do **not** upload `tests/` to production.
5. Log into `/wp-admin` once as an administrator. `admin_init` creates
   `wp_bfl_approval_sessions` via `dbDelta`. Anonymous traffic never triggers
   schema installation. Verify the table exists with the unique keys
   `uq_relay_session` and `uq_capability`.
6. Authenticated preflight: `uv run python scripts/sync_mobile_approvals.py`
   should now report `fetched = 0` instead of an `HTTP 404` relay error. That
   proves the route exists and the two secrets match.
7. Unauthenticated check: `curl -sSI https://bizfluxlab.com/bfl-approval/<32-hex>`
   returns 200 with `X-Robots-Tag: noindex, nofollow, noarchive`,
   `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
   `X-Frame-Options: DENY` and the CSP — and changes no state. Also confirm
   `https://bizfluxlab.com/robots.txt` contains `Disallow: /bfl-approval/`.
8. Confirm `/go/<token>` still redirects exactly as before — this plugin must
   not have changed it in any way, and its secret was not touched.
9. Send one safe mobile approval test (below).

If step 6 reports `signature_mismatch`, the two values differ — fix the
configuration rather than relaxing the check.

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
