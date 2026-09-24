<?php
/**
 * BizFluxLab Approval Relay -- pure PHP verification harness.
 *
 *   php wordpress/mu-plugins/bizfluxlab-approval-relay/tests/run.php
 *
 * No WordPress, no MySQL, no network. Executes the real pure implementation
 * (lib-core.php). Exit 0 on all pass, 1 on any failure.
 */

declare( strict_types = 1 );

require __DIR__ . '/../lib-core.php';

$PASS = 0;
$FAIL = 0;

function ok( string $label ) : void {
	global $PASS;
	++$PASS;
	fwrite( STDOUT, "ok   - {$label}\n" );
}

function bad( string $label, string $detail = '' ) : void {
	global $FAIL;
	++$FAIL;
	fwrite( STDOUT, "FAIL - {$label}" . ( '' !== $detail ? "  ({$detail})" : '' ) . "\n" );
}

function eq( $got, $want, string $label ) : void {
	if ( $got === $want ) {
		ok( $label );
	} else {
		bad( $label, 'got=' . var_export( $got, true ) . ' want=' . var_export( $want, true ) );
	}
}

function contains( string $haystack, string $needle, string $label ) : void {
	if ( false !== strpos( $haystack, $needle ) ) {
		ok( $label );
	} else {
		bad( $label, "missing: {$needle}" );
	}
}

function lacks( string $haystack, string $needle, string $label ) : void {
	if ( false === strpos( $haystack, $needle ) ) {
		ok( $label );
	} else {
		bad( $label, "unexpectedly present: {$needle}" );
	}
}

$NOW    = 1790000000;
$SECRET = 'oO3kJq7Yv2bN8xW1zR5tL9pF4dS6gH0aC3eU7iM2nB4k';   // 43 chars, URL-safe.
$DIGEST = hash( 'sha256', $SECRET );

$session = array(
	'relay_session_id'  => str_repeat( 'a', 32 ),
	'subject_type'      => 'change_request',
	'subject_id'        => 12,
	'subject_hash'      => str_repeat( 'b', 64 ),
	'subject_version'   => 1,
	'capability_digest' => $DIGEST,
	'state'             => BFL_Approval_State::PENDING,
	'expires_at_unix'   => $NOW + 3600,
);

/* -- capability ---------------------------------------------------------- */
list( $ok, $reason ) = BFL_Approval_Capability::verify( $SECRET, $session, $NOW );
eq( array( $ok, $reason ), array( true, 'ok' ), 'a matching capability verifies' );

list( $ok, $reason ) = BFL_Approval_Capability::verify( 'short', $session, $NOW );
eq( $reason, BFL_Approval_Capability::REASON_MALFORMED, 'a malformed capability is rejected' );

$other = 'zZ9yX8wV7uT6sR5qP4oN3mL2kJ1iH0gF9eD8cB7aZ6y';
list( $ok, $reason ) = BFL_Approval_Capability::verify( $other, $session, $NOW );
eq( $reason, BFL_Approval_Capability::REASON_MISMATCH, 'another capability cannot be reused' );

list( $ok, $reason ) = BFL_Approval_Capability::verify( $SECRET, $session, $NOW + 7200 );
eq( $reason, BFL_Approval_Capability::REASON_EXPIRED, 'an expired capability cannot decide' );

eq( strlen( BFL_Approval_Capability::digest( $SECRET ) ), 64, 'the digest is sha256 hex' );
eq(
	BFL_Approval_Capability::binding( 'change_request', 12, str_repeat( 'b', 64 ), 1, '2026-09-25T00:00:00+00:00' ),
	hash( 'sha256', implode( chr( 31 ), array( 'change_request', '12', str_repeat( 'b', 64 ), '1', '2026-09-25T00:00:00+00:00' ) ) ),
	'the binding matches the Python definition'
);

/* -- state machine ------------------------------------------------------- */
list( $ok, $reason ) = BFL_Approval_State::can_decide( $session, $NOW );
eq( array( $ok, $reason ), array( true, 'ok' ), 'a pending session can receive one decision' );

$decided = array_merge( $session, array( 'state' => BFL_Approval_State::DECIDED ) );
list( $ok, $reason ) = BFL_Approval_State::can_decide( $decided, $NOW );
eq( array( $ok, $reason ), array( false, 'already_decided' ), 'a decided session cannot decide again' );

$consumed = array_merge( $session, array( 'state' => BFL_Approval_State::CONSUMED ) );
list( $ok, $reason ) = BFL_Approval_State::can_decide( $consumed, $NOW );
eq( $reason, 'already_decided', 'a consumed session cannot decide again' );

$revoked = array_merge( $session, array( 'state' => BFL_Approval_State::REVOKED ) );
list( $ok, $reason ) = BFL_Approval_State::can_decide( $revoked, $NOW );
eq( $reason, 'session_not_pending', 'a revoked session cannot decide' );

list( $ok, $reason ) = BFL_Approval_State::can_decide( $session, $NOW + 7200 );
eq( $reason, 'session_expired', 'an expired session cannot decide' );

list( $ok, $reason ) = BFL_Approval_State::can_review( $revoked, $NOW );
eq( $reason, 'session_not_pending', 'a revoked session cannot even be reviewed' );

eq( BFL_Approval_State::is_valid_decision( 'approved' ), true, 'approved is a decision' );
eq( BFL_Approval_State::is_valid_decision( 'rejected' ), true, 'rejected is a decision' );
eq( BFL_Approval_State::is_valid_decision( 'applied' ), false, 'apply is not a decision the relay accepts' );

/* -- rendering ----------------------------------------------------------- */
$evil  = '<script>alert(1)</script>';
$shell = BFL_Approval_Render::shell( str_repeat( 'a', 32 ), '/x', '/y', 'n0nce' );
contains( $shell, 'noindex, nofollow, noarchive', 'the page declares noindex' );
contains( $shell, 'script nonce="n0nce"', 'the inline script carries the CSP nonce' );
// The guarantee is that the GET renders WITHOUT reading the database: the shell
// is a pure function of the session id, so it can carry no proposal values.
lacks( $shell, str_repeat( 'b', 64 ), 'the GET shell carries no proposal hash value' );
lacks( $shell, 'あわせて読みたい', 'the GET shell carries no proposal text' );
eq(
	BFL_Approval_Render::shell( str_repeat( 'a', 32 ), '/x', '/y', 'n0nce' ),
	$shell,
	'the shell is a pure function of the session id (no database read on GET)'
);

eq( BFL_Approval_Render::esc( $evil ), '&lt;script&gt;alert(1)&lt;/script&gt;', 'dynamic text is escaped' );
eq( BFL_Approval_Render::esc( '"quoted"' ), '&quot;quoted&quot;', 'quotes are escaped' );

$unavailable = BFL_Approval_Render::unavailable();
contains( $unavailable, 'noindex', 'the unavailable page is also noindex' );
lacks( $unavailable, 'change_request', 'the unavailable page reveals no subject metadata' );

/* -- headers ------------------------------------------------------------- */
$headers = bfl_approval_headers( 'abc123' );
eq( $headers['X-Robots-Tag'], 'noindex, nofollow, noarchive', 'X-Robots-Tag is set' );
contains( $headers['Cache-Control'], 'no-store', 'Cache-Control is no-store' );
eq( $headers['Referrer-Policy'], 'no-referrer', 'Referrer-Policy is no-referrer' );
eq( $headers['X-Frame-Options'], 'DENY', 'framing is denied' );
eq( $headers['X-Content-Type-Options'], 'nosniff', 'nosniff is set' );
contains( $headers['Content-Security-Policy'], "frame-ancestors 'none'", 'CSP denies framing' );
contains( $headers['Content-Security-Policy'], "'nonce-abc123'", 'CSP binds the inline script by nonce' );
lacks( $headers['Content-Security-Policy'], "unsafe-inline'; script", 'CSP does not allow arbitrary inline script' );

/* -- public snapshot ----------------------------------------------------- */
$snapshot = bfl_approval_public_snapshot(
	array(
		'subject_id'         => 12,
		'inserted_paragraph' => 'あわせて読みたい',
		'smtp_password'      => 'must-not-appear',
		'tracking_url'       => 'https://example.com/go/abc',
		'warnings'           => array(),
	),
	'2026-09-25 09:00'
);
eq( isset( $snapshot['smtp_password'] ), false, 'unknown keys are dropped from the public snapshot' );
eq( isset( $snapshot['tracking_url'] ), false, 'tracking URLs never reach the browser' );
eq( $snapshot['expires_at_local'], '2026-09-25 09:00', 'the expiry is shown in local time' );
eq( $snapshot['subject_id'], 12, 'allowed fields survive' );

/* -- trust domain separation (C8.8.1) ------------------------------------ *
 * The relay signs and verifies with its OWN key. Reusing the affiliate
 * runtime's key must not authenticate an approval call, and vice versa.
 * BFL_Hmac lives in the affiliate runtime plugin, so the request FORMAT is
 * shared -- only the key source differs. We reproduce that format here.
 */
require_once __DIR__ . '/../../bizfluxlab-affiliate-runtime/lib-core.php';

$APPROVAL_SECRET  = 'approval-relay-key-aaaaaaaaaaaaaaaaaaaaaaaa';
$AFFILIATE_SECRET = 'affiliate-runtime-key-bbbbbbbbbbbbbbbbbbbbbb';
$BODY             = '{"relay_session_id":"' . str_repeat( 'a', 32 ) . '"}';
$TS               = $NOW;
$BODY_SHA         = hash( 'sha256', $BODY );
$PATH             = '/wp-json/affiliate-ai/v1/approval-sessions';

function signed_with( string $secret, string $path, string $body, int $ts ) : string {
	return hash_hmac( 'sha256', BFL_Hmac::signing_string( 'POST', $path, $ts, hash( 'sha256', $body ) ), $secret );
}

$sig_approval  = signed_with( $APPROVAL_SECRET, $PATH, $BODY, $TS );
$sig_affiliate = signed_with( $AFFILIATE_SECRET, $PATH, $BODY, $TS );

list( $ok, $reason ) = BFL_Hmac::verify( $APPROVAL_SECRET, 'POST', $PATH, (string) $TS, $BODY_SHA, $sig_approval, $BODY, $NOW );
eq( array( $ok, $reason ), array( true, 'ok' ), 'an approval call signed with the approval key is accepted' );

list( $ok, $reason ) = BFL_Hmac::verify( $APPROVAL_SECRET, 'POST', $PATH, (string) $TS, $BODY_SHA, $sig_affiliate, $BODY, $NOW );
eq( array( $ok, $reason ), array( false, 'signature_mismatch' ), 'the affiliate key cannot authenticate an approval call' );

$AFF_PATH       = '/wp-json/affiliate-ai/v1/target-projections';
$sig_aff_on_aff = signed_with( $AFFILIATE_SECRET, $AFF_PATH, $BODY, $TS );
$sig_app_on_aff = signed_with( $APPROVAL_SECRET, $AFF_PATH, $BODY, $TS );

list( $ok, $reason ) = BFL_Hmac::verify( $AFFILIATE_SECRET, 'POST', $AFF_PATH, (string) $TS, $BODY_SHA, $sig_aff_on_aff, $BODY, $NOW );
eq( array( $ok, $reason ), array( true, 'ok' ), 'the affiliate runtime still accepts its own key' );

list( $ok, $reason ) = BFL_Hmac::verify( $AFFILIATE_SECRET, 'POST', $AFF_PATH, (string) $TS, $BODY_SHA, $sig_app_on_aff, $BODY, $NOW );
eq( array( $ok, $reason ), array( false, 'signature_mismatch' ), 'the approval key cannot authenticate an affiliate call' );

list( $ok, $reason ) = BFL_Hmac::verify( '', 'POST', $PATH, (string) $TS, $BODY_SHA, $sig_approval, $BODY, $NOW );
eq( array( $ok, $reason ), array( false, 'secret_not_configured' ), 'a missing approval secret fails closed' );

// The plugin source must read its own constant and never the affiliate one.
$plugin    = (string) file_get_contents( __DIR__ . '/../../bizfluxlab-approval-relay.php' );
$body_only = substr( $plugin, (int) strpos( $plugin, 'function bfl_approval_secret' ) );
contains( $body_only, "defined( 'BFL_APPROVAL_RELAY_SECRET' )", 'the relay reads its own constant' );
lacks( $body_only, 'BFL_AFFILIATE_RUNTIME_SECRET', 'the relay never reads the affiliate constant' );


/* -- browser cookie seam (C8.8.2) ---------------------------------------- *
 * The first production rejection failed because the confirmation cookie was
 * scoped to the review page prefix while the decision POST goes to the REST
 * namespace. Nothing in the old suite modelled cookie delivery, so nothing
 * caught it. These tests model RFC 6265 directly.
 */
$REST_BASE = '/wp-json/affiliate-ai/v1/';
$ROUTES    = bfl_approval_review_routes( $REST_BASE );

eq( $ROUTES['exchange'], '/wp-json/affiliate-ai/v1/approval-review/exchange', 'the exchange route is derived from the REST base' );
eq( $ROUTES['decide'], '/wp-json/affiliate-ai/v1/approval-review/decide', 'the decide route is derived from the REST base' );

// The cookie scope and the routes come from the SAME base, so they cannot drift.
eq( bfl_approval_path_matches( $ROUTES['exchange'], $REST_BASE ), true, 'the cookie reaches the exchange endpoint' );
eq( bfl_approval_path_matches( $ROUTES['decide'], $REST_BASE ), true, 'the cookie reaches the decide endpoint' );

// The exact production bug, pinned as a regression.
eq( bfl_approval_path_matches( $ROUTES['decide'], '/bfl-approval/' ), false, 'the OLD page-prefix scope would not reach decide (the C8.8 bug)' );

// Scope stays narrow: not the whole site.
eq( $REST_BASE === '/', false, 'the cookie scope is not the site root' );
foreach ( array( '/', '/wp-admin/', '/go/example', '/bfl-approval/example', '/wp-json/other/v1/x' ) as $elsewhere ) {
	eq( bfl_approval_path_matches( $elsewhere, $REST_BASE ), false, "the cookie is not sent to {$elsewhere}" );
}

// RFC 6265 5.1.4 edge cases.
eq( bfl_approval_path_matches( '/a/b', '/a/b' ), true, 'an exact path matches' );
eq( bfl_approval_path_matches( '/a/b/c', '/a/b' ), true, 'a prefix followed by / matches' );
eq( bfl_approval_path_matches( '/a/bc', '/a/b' ), false, 'a partial segment does not match' );
eq( bfl_approval_path_matches( '/a', '' ), false, 'an empty cookie path never matches' );

/* -- the full browser sequence, without a browser ------------------------- */
$NONCE         = bin2hex( str_repeat( 'ab', 32 ) );
$STORED_DIGEST = hash( 'sha256', $NONCE );
$pending       = array( 'state' => BFL_Approval_State::PENDING, 'expires_at_unix' => $NOW + 3600 );

/** Deliver the cookie only if a real browser would. */
function cookie_for( string $request_path, string $cookie_path, string $value ) : string {
	return bfl_approval_path_matches( $request_path, $cookie_path ) ? $value : '';
}

// 1) GET shell -> sets nothing, reads nothing (already covered above).
// 2) exchange -> issues the nonce cookie scoped to $REST_BASE.
// 3) reject POST at the EXACT route the JS uses.
$delivered = cookie_for( $ROUTES['decide'], $REST_BASE, $NONCE );
eq( $delivered, $NONCE, 'the browser delivers the cookie to the decide route' );

list( $ok, $reason, $status ) = bfl_approval_decision_guard( $pending, $delivered, $STORED_DIGEST, $NONCE, $NOW );
eq( array( $ok, $reason, $status ), array( true, 'ok', 200 ), 'a real reject POST passes the decision guard' );

// Under the OLD scope the same sequence fails -- this is what production hit.
$old_delivered = cookie_for( $ROUTES['decide'], '/bfl-approval/', $NONCE );
eq( $old_delivered, '', 'under the old scope no cookie is delivered' );
list( $ok, $reason, $status ) = bfl_approval_decision_guard( $pending, $old_delivered, $STORED_DIGEST, $NONCE, $NOW );
eq( array( $ok, $reason, $status ), array( false, 'session_not_found', 403 ), 'the old scope reproduces the production failure' );

/* -- negative cases ------------------------------------------------------- */
list( $ok, $reason, $status ) = bfl_approval_decision_guard( $pending, '', $STORED_DIGEST, $NONCE, $NOW );
eq( array( $reason, $status ), array( 'session_not_found', 403 ), 'a POST without the cookie is refused' );

list( $ok, $reason, $status ) = bfl_approval_decision_guard( $pending, $NONCE, $STORED_DIGEST, 'wrong-nonce', $NOW );
eq( array( $reason, $status ), array( 'session_not_found', 403 ), 'a missing/incorrect nonce header is refused' );

list( $ok, $reason, $status ) = bfl_approval_decision_guard( $pending, 'other-cookie', $STORED_DIGEST, 'other-cookie', $NOW );
eq( array( $reason, $status ), array( 'session_not_found', 403 ), 'a cookie from another session is refused' );

list( $ok, $reason, $status ) = bfl_approval_decision_guard( $pending, $NONCE, null, $NONCE, $NOW );
eq( array( $reason, $status ), array( 'session_not_found', 403 ), 'an expired review transient is refused' );

$decided = array( 'state' => BFL_Approval_State::DECIDED, 'expires_at_unix' => $NOW + 3600 );
list( $ok, $reason, $status ) = bfl_approval_decision_guard( $decided, $NONCE, $STORED_DIGEST, $NONCE, $NOW );
eq( array( $reason, $status ), array( 'already_decided', 409 ), 'a duplicate POST is idempotent, not a second decision' );

$expired = array( 'state' => BFL_Approval_State::PENDING, 'expires_at_unix' => $NOW - 1 );
list( $ok, $reason, $status ) = bfl_approval_decision_guard( $expired, $NONCE, $STORED_DIGEST, $NONCE, $NOW );
eq( array( $reason, $status ), array( 'session_expired', 409 ), 'an expired session fails safely' );

$revoked = array( 'state' => BFL_Approval_State::REVOKED, 'expires_at_unix' => $NOW + 3600 );
list( $ok, $reason, $status ) = bfl_approval_decision_guard( $revoked, $NONCE, $STORED_DIGEST, $NONCE, $NOW );
eq( array( $reason, $status ), array( 'session_not_pending', 409 ), 'a revoked session fails safely' );

/* -- the plugin wires the cookie to the same base as the routes ----------- */
$plugin_src = (string) file_get_contents( __DIR__ . '/../../bizfluxlab-approval-relay.php' );
contains( $plugin_src, "'path'     => bfl_approval_rest_base()", 'the cookie path is derived from the REST base' );
// T4.2: one digest email carries several review links. The review nonce must be
// stored and looked up per relay session, so a review opened for proposal B can
// never decide proposal A (a stale tab fails safely instead of mis-deciding).
contains( $plugin_src, "set_transient( 'bfl_approval_nonce_' . \$sid,", 'the review nonce is stored per relay session' );
contains( $plugin_src, "get_transient( 'bfl_approval_nonce_' . \$sid ),", 'the decision guard reads the nonce of the session being decided' );
contains( $plugin_src, 'bfl_approval_review_routes( bfl_approval_rest_base() )', 'the page routes come from the same base' );
lacks( $plugin_src, "'path'     => BFL_APPROVAL_PAGE_PREFIX", 'the cookie is no longer scoped to the review page prefix' );


fwrite( STDOUT, "\n{$PASS} passed, {$FAIL} failed\n" );
exit( $FAIL > 0 ? 1 : 0 );
