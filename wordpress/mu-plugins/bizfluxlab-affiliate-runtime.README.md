# BizFluxLab Affiliate Runtime (MU-plugin)

Authored in the `affiliate-ai` repository for review. **Not deployed** by phases
D-C0 / D-C0.1. No production WordPress request, table, route, or secret is
created.

## What it is

The public runtime for first-party affiliate redirects on `bizfluxlab.com`
(WordPress / XServer). `affiliate-ai` remains the control plane and sole source
of truth for target identity, token, destination URL, destination-host approval,
status, link identity, projection hashes, and Human approval. This plugin only:

- stores an **immutable / versioned projection** of approved targets
  (`{$wpdb->prefix}bfl_affiliate_targets`),
- resolves `GET /go/{token}` to a **302** to the exact approved destination,
- appends a **minimal raw click event** (`{$wpdb->prefix}bfl_outbound_clicks`:
  `id`, `affiliate_target_id`, `token`, `clicked_at` — nothing else),
- exposes an authenticated read-only click export.

No IP / hashed IP / User-Agent / Referer / cookie / session / user id / email /
query string / device data is stored anywhere.

## Files

| Path | Purpose |
|---|---|
| `bizfluxlab-affiliate-runtime.php` | WordPress bootstrap: `ABSPATH` guard, `require_once` of lib-core, REST routes, `/go`, `$wpdb` repos, admin-only schema installer |
| `bizfluxlab-affiliate-runtime/lib-core.php` | **pure** core (`BFL_Canonical`, `BFL_Hmac`, `BFL_Destination_Validator`, `bfl_affiliate_decide`, timestamp/entry validators). No `ABSPATH` guard, no `$wpdb`, no superglobals — directly executable under `php` CLI. |
| `bizfluxlab-affiliate-runtime/tests/run.php` | pure PHP verification harness (no WordPress, no MySQL, no network) |
| `bizfluxlab-affiliate-runtime.fixtures/*.json` | Python-generated golden fixtures (`scripts/gen_affiliate_projection_fixtures.py`) — synthetic only; no real ASP URL / token / secret |

## Local PHP verification (D-C0.1)

PHP 8.2.33 CLI (official php.net Windows build, installed via
`winget install PHP.PHP.8.2`). Loaded extensions used: `json`, `hash`, `pcre`,
`date`. **`mbstring` is NOT loaded** — the plugin therefore uses no mbstring
functions (ASCII host check is a `\x21-\x7e` byte-range regex).

```
php -l wordpress/mu-plugins/bizfluxlab-affiliate-runtime.php            # No syntax errors
php -l wordpress/mu-plugins/bizfluxlab-affiliate-runtime/lib-core.php   # No syntax errors
php -l wordpress/mu-plugins/bizfluxlab-affiliate-runtime/tests/run.php  # No syntax errors
php wordpress/mu-plugins/bizfluxlab-affiliate-runtime/tests/run.php     # 101 passed, 0 failed  (exit 0)
```

`tests/unit/test_wp_affiliate_runtime_php_harness.py` runs the same harness from
`uv run pytest` (skips only if no `php` on the machine).

### What the harness proves (executed, not asserted from comments)

- **canonical JSON parity** — `BFL_Canonical::encode` byte-matches Python
  `canonical_json` for every primitive: ASCII, `/` unescaped, `"`/`\` escaped,
  CJK raw UTF-8, **U+2028 / U+2029 raw UTF-8**, control chars `\uXXXX`, key
  sorting, list order, `{}` vs `[]`, nested objects.
- **projection hash parity** — PHP `entry_hash` and `snapshot_hash` equal the
  Python golden values for all 5 projection fixtures (empty, active v1,
  disabled v2, two-target token-sorted, non-ASCII path).
- **HMAC** — 3 Python-signed vectors accepted; body / timestamp / path / method /
  secret mutation and an expired timestamp each rejected; empty secret →
  `secret_not_configured`.
- **decision table** — all 7 outcomes match D-B2 semantics.
- **destination validator** — 13 cases (valid, case-normalized host, host
  mismatch, subdomain confusion, userinfo, port 443/8443, http, non-ASCII host,
  self-host, control char, U+2028) + URL-string-unchanged.
- **canonical UTC timestamp validator** — shape + real-calendar checks.

## Cross-runtime canonical JSON (matches Python — verified by execution)

`app/article/draft_input_canonical.canonical_json` =
`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
allow_nan=False)` → UTF-8 → SHA-256. Values in the projection schema are only
string / int / null / array / object (no float, no bool).

| rule | Python | PHP (`BFL_Canonical`) |
|---|---|---|
| object key order | sorted ascending by code point | `ksort($assoc, SORT_STRING)` (keys ASCII) |
| whitespace | none | `json_encode` default |
| `/` | not escaped | `JSON_UNESCAPED_SLASHES` |
| non-ASCII (CJK etc.) | raw UTF-8 | `JSON_UNESCAPED_UNICODE` |
| **U+2028 / U+2029** | **raw UTF-8** (not escaped with `ensure_ascii=False`) | **`JSON_UNESCAPED_LINE_TERMINATORS`** (D-C0 had this inverted; corrected in D-C0.1) |
| `"` `\` / control `<0x20` | `\"` `\\` / `\uXXXX` `\n\t\r\b\f` | same |
| `<` `>` `&` `'` | not escaped | not escaped (no `JSON_HEX_*`) |
| `{}` vs `[]` | `dict` → `{}`, `list` → `[]` | `stdClass` / non-empty assoc → `{}`, sequential array → `[]` |
| `targets` order | token-sorted by builder | re-sorted by `token` (`strcmp`) before hashing |

Note: U+2028 / U+2029 cannot legitimately reach a stored `destination_url` — both
runtimes reject them (`str.isspace()` in the control plane;
`BFL_Destination_Validator`'s `\x{2028}|\x{2029}` regex in PHP). The canonical
encoder still handles them correctly so there is **no silent difference**.

## Timestamp storage (D-C0.1)

`activated_at` / `disabled_at` are `VARCHAR(32)` and hold the **exact canonical
protocol string** `YYYY-MM-DDTHH:MM:SS+00:00` — never coerced into a MySQL
`DATETIME`. They are projection/audit state, never queried as dates, and are
part of `projection_entry_hash`. `bfl_affiliate_is_canonical_utc()` enforces the
exact contract (shape + real calendar instant); `disabled_at` must be `null` for
`active` and canonical UTC for `disabled`.

## Schema lifecycle (D-C0.1 hardening)

`dbDelta`, idempotent, schema version in the `bfl_affiliate_runtime_schema_version`
option. **Not** hooked to `rest_api_init`. It runs only:

- on `admin_init` for a `current_user_can('manage_options')` user, or
- lazily inside the projection endpoint **after** HMAC verification succeeds.

Anonymous REST traffic and `/go` traffic can never trigger DDL. No `DROP`, no
destructive migration.

## Endpoints

| Method / path | Auth | Notes |
|---|---|---|
| `POST /wp-json/affiliate-ai/v1/target-projections` | HMAC v1 | fails closed without the secret; recomputes every `projection_entry_hash` + the `projection_snapshot_hash` and rejects the whole batch on any mismatch/conflict; one DB transaction |
| `GET /wp-json/affiliate-ai/v1/outbound-clicks?limit=<L>&since_id=<N>` | HMAC v1 | read-only; `id ASC`; `limit` clamped `[1,1000]`; both params required and bound into the signed request target; rows = `id`, `token`, `clicked_at`; no delete/ack |
| `GET /go/{token}` | none (public) | `[A-Za-z0-9_-]{16,64}`; malformed/unknown → `404`; disabled/invalid stored target → `410`; active+valid → append click (fail-open) then `302` to the exact stored URL; ignores any `?url=`; headers `Cache-Control: no-store` + `X-Robots-Tag: noindex, nofollow` |

## HMAC v1 (matches `app/affiliate/projection_signing.py`)

Headers `X-BFL-Timestamp`, `X-BFL-Content-SHA256`, `X-BFL-Signature`. Signing
string (LF-joined): `v1`, `<METHOD>`, `<exact signed path incl. sorted query for
export>`, `<unix ts>`, `<sha256 hex of exact body bytes; empty-string sha256 for
GET>`. `HMAC-SHA256(secret, …)` lowercase hex, verified with `hash_equals`;
±300 s window. Secret is the HMAC key only — never in the payload, a signed
field, a repo, or a log.

## Deployment (future — Human-performed, NOT now)

1. `wp-config.php`: `define('BFL_AFFILIATE_RUNTIME_SECRET', '<64+ char random>');`
   — the same value in `affiliate-ai`'s `.env` (wiring is a later phase). Never
   commit it.
2. Copy `bizfluxlab-affiliate-runtime.php` **and** the
   `bizfluxlab-affiliate-runtime/` directory (for `lib-core.php`) into
   `wp-content/mu-plugins/`.
3. Ensure `bizfluxlab.com/go/...` reaches WordPress (same origin — it does).
4. Push a projection snapshot from the control plane; nothing routes until an
   `active` target exists. No seed target / default destination in this code.

## ⚠ Remaining pre-deployment gate (D-C1)

Local PHP verification is **done** (pure logic + fixture parity + HMAC +
decision + validators, PHP 8.2.33). Still required on XServer before production:

- real `dbDelta` table creation, column types, indexes, and the schema-version
  option against the actual MySQL/MariaDB version;
- the projection endpoint end-to-end against a real WordPress REST stack;
- confirmation of the XServer PHP version (source targets **PHP 8.0+**; verified
  under 8.2.33).
