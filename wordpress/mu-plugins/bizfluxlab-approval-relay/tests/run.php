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

fwrite( STDOUT, "\n{$PASS} passed, {$FAIL} failed\n" );
exit( $FAIL > 0 ? 1 : 0 );
