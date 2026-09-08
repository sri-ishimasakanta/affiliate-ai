<?php
/**
 * BizFluxLab Affiliate Runtime -- pure PHP verification harness.
 *
 *   php wordpress/mu-plugins/bizfluxlab-affiliate-runtime/tests/run.php
 *
 * No WordPress, no MySQL, no network. Executes the real pure implementation
 * (lib-core.php) against the Python-generated golden fixtures. Exit 0 on all
 * pass, 1 on any failure.
 */

declare( strict_types = 1 );

require __DIR__ . '/../lib-core.php';

$FIX = __DIR__ . '/../../bizfluxlab-affiliate-runtime.fixtures';

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

function load_json( string $path, bool $assoc = true ) {
	$raw = file_get_contents( $path );
	if ( false === $raw ) {
		fwrite( STDERR, "cannot read {$path}\n" );
		exit( 2 );
	}
	return json_decode( $raw, $assoc, 512, JSON_THROW_ON_ERROR );
}

/* ---- 1. canonical JSON primitives ----------------------------------- */
// Decode with assoc=false so a JSON object value keeps its object-ness
// (a bare `{}` must encode back to `{}`, not `[]`).
$prim = load_json( "{$FIX}/canonical_primitives.json", false );
foreach ( $prim->cases as $c ) {
	$encoded = BFL_Canonical::encode( $c->value );
	eq( $encoded, $c->canonical_json, "canonical.encode bytes: {$c->name}" );
	eq( hash( 'sha256', $encoded ), $c->canonical_json_sha256, "canonical.sha256: {$c->name}" );
}

/* ---- 2. projection fixture parity (entry + snapshot hashes) --------- */
foreach (
	array(
		'empty_snapshot.json',
		'active_v1_snapshot.json',
		'disabled_v2_snapshot.json',
		'two_target_snapshot.json',
		'unicode_path_snapshot.json',
	) as $file
) {
	$fx      = load_json( "{$FIX}/{$file}" );
	$body    = $fx['request_body'];
	$targets = $body['targets'];

	foreach ( $targets as $i => $t ) {
		eq(
			BFL_Canonical::entry_hash( $t ),
			$fx['expected']['entries'][ $i ]['projection_entry_hash'],
			"{$file}: entry_hash[{$i}]"
		);
		eq(
			BFL_Canonical::entry_hash( $t ),
			$t['projection_entry_hash'],
			"{$file}: entry_hash[{$i}] == wire"
		);
	}
	eq(
		BFL_Canonical::snapshot_hash( 1, $targets ),
		$fx['expected']['projection_snapshot_hash'],
		"{$file}: snapshot_hash"
	);
	eq(
		BFL_Canonical::snapshot_hash( 1, $targets ),
		$body['projection_snapshot_hash'],
		"{$file}: snapshot_hash == wire"
	);
}

/* ---- 3. HMAC v1 verification -------------------------------------- */
$hv     = load_json( "{$FIX}/hmac_vectors.json" );
$secret = $hv['shared_secret'];
foreach ( $hv['vectors'] as $n => $v ) {
	$body = $v['body_utf8'];
	$bh   = $v['body_sha256'];
	$ts   = (int) $v['timestamp'];

	list( $r_ok, $r_reason ) = BFL_Hmac::verify(
		$secret, $v['method'], $v['signed_path'], (string) $ts, $bh, $v['signature'], $body, $ts + 5
	);
	eq( array( $r_ok, $r_reason ), array( true, 'ok' ), "hmac[{$n}]: valid accepted" );

	// body mutation
	list( $b_ok ) = BFL_Hmac::verify(
		$secret, $v['method'], $v['signed_path'], (string) $ts, hash( 'sha256', $body . 'x' ), $v['signature'], $body . 'x', $ts + 5
	);
	eq( $b_ok, false, "hmac[{$n}]: body mutation rejected" );

	// timestamp mutation (still in window but signature bound)
	list( $t_ok ) = BFL_Hmac::verify(
		$secret, $v['method'], $v['signed_path'], (string) ( $ts + 1 ), $bh, $v['signature'], $body, $ts + 6
	);
	eq( $t_ok, false, "hmac[{$n}]: timestamp mutation rejected" );

	// path mutation
	list( $p_ok ) = BFL_Hmac::verify(
		$secret, $v['method'], $v['signed_path'] . 'x', (string) $ts, $bh, $v['signature'], $body, $ts + 5
	);
	eq( $p_ok, false, "hmac[{$n}]: path mutation rejected" );

	// method mutation
	$other_method = 'GET' === $v['method'] ? 'POST' : 'GET';
	list( $m_ok ) = BFL_Hmac::verify(
		$secret, $other_method, $v['signed_path'], (string) $ts, $bh, $v['signature'], $body, $ts + 5
	);
	eq( $m_ok, false, "hmac[{$n}]: method mutation rejected" );

	// wrong secret
	list( $s_ok ) = BFL_Hmac::verify(
		'wrong-secret', $v['method'], $v['signed_path'], (string) $ts, $bh, $v['signature'], $body, $ts + 5
	);
	eq( $s_ok, false, "hmac[{$n}]: wrong secret rejected" );

	// expired timestamp
	list( $e_ok, $e_reason ) = BFL_Hmac::verify(
		$secret, $v['method'], $v['signed_path'], (string) $ts, $bh, $v['signature'], $body, $ts + 400
	);
	eq( array( $e_ok, $e_reason ), array( false, 'timestamp_outside_window' ), "hmac[{$n}]: expired rejected" );
}
// empty secret -> fail closed
list( $ns_ok, $ns_reason ) = BFL_Hmac::verify( '', 'POST', '/x', '1', str_repeat( '0', 64 ), '0', '', 1 );
eq( array( $ns_ok, $ns_reason ), array( false, 'secret_not_configured' ), 'hmac: empty secret fails closed' );

/* ---- 4. projection decision table ------------------------------- */
function mk( array $over ) : array {
	return array_merge(
		array(
			'token'                 => 'TKN0000000000000000',
			'destination_url'       => 'https://aff.example.test/x',
			'destination_host'      => 'aff.example.test',
			'link_identity_hash'    => str_repeat( 'a', 64 ),
			'status'                => 'active',
			'projection_version'    => 1,
			'projection_entry_hash' => str_repeat( 'b', 64 ),
		),
		$over
	);
}
eq( bfl_affiliate_decide( null, mk( array() ) ), 'insert', 'decide: insert v1' );
eq( bfl_affiliate_decide( null, mk( array( 'status' => 'disabled', 'projection_version' => 2 ) ) ), 'insert', 'decide: insert initial disabled v2' );
eq( bfl_affiliate_decide( mk( array() ), mk( array() ) ), 'noop', 'decide: same version+hash noop' );
eq(
	bfl_affiliate_decide( mk( array() ), mk( array( 'projection_entry_hash' => str_repeat( 'c', 64 ) ) ) ),
	'conflict_hash_mismatch',
	'decide: same version different hash conflict'
);
eq(
	bfl_affiliate_decide( mk( array( 'status' => 'disabled', 'projection_version' => 2 ) ), mk( array() ) ),
	'forbidden_reactivation',
	'decide: v2 -> active forbidden reactivation'
);
eq(
	bfl_affiliate_decide( mk( array() ), mk( array( 'status' => 'disabled', 'projection_version' => 2 ) ) ),
	'update',
	'decide: v1 -> disabled v2 update'
);
eq(
	bfl_affiliate_decide(
		mk( array( 'status' => 'disabled', 'projection_version' => 2 ) ),
		mk( array( 'status' => 'disabled', 'projection_version' => 1 ) )
	),
	'conflict_stale_version',
	'decide: stale version'
);
eq(
	bfl_affiliate_decide( mk( array() ), mk( array( 'destination_url' => 'https://aff.example.test/DIFFERENT' ) ) ),
	'conflict_immutable_identity_drift',
	'decide: immutable identity drift'
);

/* ---- 5. destination validator --------------------------------- */
function dv( string $url, string $host ) : array {
	return BFL_Destination_Validator::validate( $url, $host );
}
eq( dv( 'https://aff.example.test/x?a=1#f', 'aff.example.test' ), array( true, 'ok' ), 'dv: valid https ascii' );
eq( dv( 'https://AFF.Example.Test/x', 'aff.example.test' ), array( true, 'ok' ), 'dv: host case normalized' );
eq( dv( 'https://aff.example.test:443/x', 'aff.example.test' ), array( true, 'ok' ), 'dv: port 443 ok' );
eq( dv( 'https://other.example.test/x', 'aff.example.test' )[1], 'host_mismatch', 'dv: host mismatch' );
eq( dv( 'https://evil.aff.example.test/x', 'aff.example.test' )[1], 'host_mismatch', 'dv: subdomain confusion' );
eq( dv( 'https://user@aff.example.test/x', 'aff.example.test' )[1], 'userinfo', 'dv: userinfo rejected' );
eq( dv( 'https://aff.example.test:8443/x', 'aff.example.test' )[1], 'bad_port', 'dv: bad port' );
eq( dv( 'http://aff.example.test/x', 'aff.example.test' )[1], 'scheme_not_https', 'dv: http rejected' );
eq( dv( "https://\xe6\x97\xa5.example/x", 'xn--0zwm56d.example' )[1], 'host_not_ascii', 'dv: unicode host rejected' );
eq( dv( 'https://bizfluxlab.com/go/x', 'bizfluxlab.com' )[1], 'self_host', 'dv: self host rejected' );
eq( dv( "https://aff.example.test/a\tb", 'aff.example.test' )[1], 'whitespace_or_control', 'dv: control char rejected' );
eq( dv( "https://aff.example.test/a\xe2\x80\xa8b", 'aff.example.test' )[1], 'whitespace_or_control', 'dv: U+2028 in url rejected' );
// URL argument is not mutated (validate returns only [bool, reason]).
$u = 'https://aff.example.test/x?a=1#f';
BFL_Destination_Validator::validate( $u, 'aff.example.test' );
eq( $u, 'https://aff.example.test/x?a=1#f', 'dv: url string unchanged' );

/* ---- 6. canonical UTC timestamp validator --------------------- */
eq( bfl_affiliate_is_canonical_utc( '2026-09-01T12:00:00+00:00' ), true, 'ts: canonical accepted' );
eq( bfl_affiliate_is_canonical_utc( '2026-09-01 12:00:00' ), false, 'ts: mysql form rejected' );
eq( bfl_affiliate_is_canonical_utc( '2026-09-01T12:00:00Z' ), false, 'ts: Z suffix rejected' );
eq( bfl_affiliate_is_canonical_utc( '2026-13-01T00:00:00+00:00' ), false, 'ts: bad month rejected' );
eq( bfl_affiliate_is_canonical_utc( '2026-09-01T25:00:00+00:00' ), false, 'ts: bad hour rejected' );
eq( bfl_affiliate_is_canonical_utc( '' ), false, 'ts: empty rejected' );
eq( bfl_affiliate_is_canonical_utc( null ), false, 'ts: null rejected' );
eq(
	bfl_affiliate_canonical_utc_to_sql( '2026-09-01T12:00:00+00:00' ),
	'2026-09-01 12:00:00',
	'ts: canonical -> sql form'
);

/* ---- 7. entry semantic validator ------------------------------ */
$ok_entry = load_json( "{$FIX}/active_v1_snapshot.json" )['request_body']['targets'][0];
eq( bfl_affiliate_validate_entry( $ok_entry ), array( true, 'ok' ), 'validate_entry: golden active entry ok' );
eq(
	bfl_affiliate_validate_entry( array_merge( $ok_entry, array( 'activated_at' => 'not-a-timestamp' ) ) )[1],
	'bad_activated_at',
	'validate_entry: bad activated_at'
);
eq(
	bfl_affiliate_validate_entry( array_merge( $ok_entry, array( 'token' => 'short' ) ) )[1],
	'bad_token',
	'validate_entry: bad token'
);
eq(
	bfl_affiliate_validate_entry( array_merge( $ok_entry, array( 'disabled_at' => '2026-09-03T00:00:00+00:00' ) ) )[1],
	'bad_disabled_at',
	'validate_entry: active must not carry disabled_at'
);

/* ---- summary ------------------------------------------------- */
fwrite( STDOUT, "\n{$PASS} passed, {$FAIL} failed\n" );
exit( $FAIL > 0 ? 1 : 0 );
