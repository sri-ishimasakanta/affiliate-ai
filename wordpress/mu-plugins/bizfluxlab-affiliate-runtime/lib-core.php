<?php
/**
 * BizFluxLab Affiliate Runtime -- PURE core.
 *
 * No WordPress dependency, no ABSPATH guard, no superglobals, no I/O.
 * Directly executable under `php` CLI (see tests/run.php). The WordPress
 * MU-plugin `bizfluxlab-affiliate-runtime.php` require_once's this file.
 *
 * Contains:
 *   BFL_Canonical             -- canonical JSON + entry/snapshot hash (must
 *                                byte-match app/article/draft_input_canonical.
 *                                canonical_json + app/affiliate/projection.py)
 *   BFL_Hmac                  -- HMAC-SHA256 request verification (D-B2 contract)
 *   BFL_Destination_Validator -- request-time destination revalidation
 *   bfl_affiliate_decide()    -- port of decide_projection_upsert
 *   bfl_affiliate_is_canonical_utc() / bfl_affiliate_is_hex64()
 *
 * PHP requirement: 8.0+ (uses union-free syntax, str_starts_with polyfilled below
 * is unnecessary on 8.0; array_is_list polyfilled for < 8.1).
 */

if ( ! function_exists( 'array_is_list' ) ) {
	/** Polyfill for PHP < 8.1. */
	function array_is_list( array $a ) : bool {
		$i = 0;
		foreach ( $a as $k => $_ ) {
			if ( $k !== $i++ ) {
				return false;
			}
		}
		return true;
	}
}

/* =========================================================================
 * Canonical JSON + semantic hash recomputation.
 *
 * Python reference (verified by execution, not comments):
 *   json.dumps(obj, ensure_ascii=False, sort_keys=True,
 *              separators=(",", ":"), allow_nan=False) -> UTF-8 -> sha256
 *
 * Observed byte-level rules for the constrained projection schema
 * (values are ONLY string / int / null / array / object -- no float, no bool):
 *   - object keys sorted ascending by code point (all keys are ASCII here)
 *   - no insignificant whitespace
 *   - '/'                 NOT escaped        -> JSON_UNESCAPED_SLASHES
 *   - non-ASCII (e.g. CJK) raw UTF-8         -> JSON_UNESCAPED_UNICODE
 *   - U+2028 / U+2029      raw UTF-8 bytes    -> JSON_UNESCAPED_LINE_TERMINATORS
 *       (D-C0 wrongly assumed Python escaped these; it does NOT with
 *        ensure_ascii=False -- corrected in D-C0.1)
 *   - '"' '\' -> \" \\ ; control < 0x20 -> \uXXXX / \n\t\r\b\f
 *   - '<' '>' '&' "'"    NOT escaped (no JSON_HEX_* flags)
 *   - dict -> {}  (assoc array cast to stdClass) ; list -> []
 * ========================================================================= */
final class BFL_Canonical {

	private const ENTRY_HASH_KEYS = array(
		'token',
		'destination_url',
		'destination_host',
		'status',
		'link_identity_hash',
		'projection_version',
		'activated_at',
		'disabled_at',
	);

	private const ENTRY_PAYLOAD_KEYS = array(
		'token',
		'destination_url',
		'destination_host',
		'status',
		'link_identity_hash',
		'projection_version',
		'activated_at',
		'disabled_at',
		'projection_entry_hash',
	);

	public static function encode( $value ) : string {
		return json_encode(
			self::normalize( $value ),
			JSON_UNESCAPED_SLASHES
				| JSON_UNESCAPED_UNICODE
				| JSON_UNESCAPED_LINE_TERMINATORS
				| JSON_THROW_ON_ERROR
		);
	}

	private static function normalize( $value ) {
		// JSON objects decoded with assoc=false arrive as stdClass; keep their
		// object-ness so `{}` never collapses to `[]` (PHP's json_decode(assoc=true)
		// ambiguity). The projection endpoint decodes with assoc=true, but every
		// object it builds is non-empty, so the is_array() branch is sufficient
		// there.
		if ( $value instanceof stdClass ) {
			return self::normalize_assoc( (array) $value );
		}
		if ( is_array( $value ) ) {
			if ( array_is_list( $value ) ) {
				return array_map( array( self::class, 'normalize' ), $value );
			}
			return self::normalize_assoc( $value );
		}
		if ( is_string( $value ) || is_int( $value ) || is_null( $value ) ) {
			return $value;
		}
		throw new InvalidArgumentException( 'canonical json: unsupported value type' );
	}

	private static function normalize_assoc( array $assoc ) {
		ksort( $assoc, SORT_STRING );
		$obj = new stdClass();
		foreach ( $assoc as $k => $v ) {
			$obj->{ (string) $k } = self::normalize( $v );
		}
		return $obj;
	}

	/** Recompute projection_entry_hash from an entry's identity/state fields. */
	public static function entry_hash( array $entry ) : string {
		$payload = array();
		foreach ( self::ENTRY_HASH_KEYS as $k ) {
			$payload[ $k ] = ( 'projection_version' === $k )
				? (int) $entry[ $k ]
				: $entry[ $k ];
		}
		return hash( 'sha256', self::encode( $payload ) );
	}

	/** Recompute projection_snapshot_hash from schema_version + token-sorted entries. */
	public static function snapshot_hash( int $schema_version, array $entries ) : string {
		usort(
			$entries,
			static function ( $a, $b ) {
				return strcmp( (string) $a['token'], (string) $b['token'] );
			}
		);
		$targets = array();
		foreach ( $entries as $e ) {
			$row = array();
			foreach ( self::ENTRY_PAYLOAD_KEYS as $k ) {
				$row[ $k ] = ( 'projection_version' === $k ) ? (int) $e[ $k ] : $e[ $k ];
			}
			$targets[] = $row;
		}
		return hash(
			'sha256',
			self::encode(
				array(
					'schema_version' => $schema_version,
					'targets'        => $targets,
				)
			)
		);
	}
}

/* =========================================================================
 * HMAC-SHA256 request verification (matches app/affiliate/projection_signing.py).
 * ========================================================================= */
final class BFL_Hmac {

	const SCHEME   = 'v1';
	const MAX_SKEW = 300;

	public static function signing_string( string $method, string $signed_path, int $ts, string $body_sha256 ) : string {
		return implode(
			"\n",
			array(
				self::SCHEME,
				strtoupper( $method ),
				$signed_path,
				(string) $ts,
				strtolower( $body_sha256 ),
			)
		);
	}

	/**
	 * @return array{0:bool,1:string} [ok, reason]
	 */
	public static function verify(
		string $secret,
		string $method,
		string $signed_path,
		string $ts_header,
		string $content_sha_header,
		string $sig_header,
		string $raw_body,
		int $now
	) : array {
		if ( '' === $secret ) {
			return array( false, 'secret_not_configured' );
		}
		if ( 1 !== preg_match( '/\A\d{1,20}\z/', $ts_header ) ) {
			return array( false, 'bad_timestamp' );
		}
		$ts            = (int) $ts_header;
		$computed_body = hash( 'sha256', $raw_body );
		if ( ! hash_equals( $computed_body, strtolower( $content_sha_header ) ) ) {
			return array( false, 'content_sha256_mismatch' );
		}
		if ( abs( $now - $ts ) > self::MAX_SKEW ) {
			return array( false, 'timestamp_outside_window' );
		}
		$expected = hash_hmac(
			'sha256',
			self::signing_string( $method, $signed_path, $ts, $computed_body ),
			$secret
		);
		if ( ! hash_equals( $expected, strtolower( $sig_header ) ) ) {
			return array( false, 'signature_mismatch' );
		}
		return array( true, 'ok' );
	}
}

/* =========================================================================
 * Request-time destination revalidation (D-B2 carry-forward #2).
 * Applied at projection ingestion AND at every /go request. Never rewrites
 * destination_url. Extension-free (no mbstring): ASCII test is a byte-range
 * regex.
 * ========================================================================= */
final class BFL_Destination_Validator {

	private const SELF_HOSTS = array( 'bizfluxlab.com', 'www.bizfluxlab.com' );

	/**
	 * @return array{0:bool,1:string}
	 */
	public static function validate( string $url, string $expected_host ) : array {
		if ( '' === $url || strlen( $url ) > 1024 ) {
			return array( false, 'empty_or_too_long' );
		}
		// ASCII C0 + space + DEL, and the Unicode line/paragraph separators
		// U+2028 / U+2029 (Python's str.isspace() rejects these at the control
		// plane too -- keep both runtimes aligned; no silent difference).
		if ( preg_match( '/[\x00-\x20\x7f]/', $url ) || preg_match( '/\x{2028}|\x{2029}/u', $url ) ) {
			return array( false, 'whitespace_or_control' );
		}
		// Authority segment must not contain userinfo.
		if ( preg_match( '#\Ahttps://([^/?\#]*)#i', $url, $m ) && false !== strpos( $m[1], '@' ) ) {
			return array( false, 'userinfo' );
		}
		$p = parse_url( $url );
		if ( false === $p || empty( $p['scheme'] ) || 'https' !== strtolower( $p['scheme'] ) ) {
			return array( false, 'scheme_not_https' );
		}
		if ( isset( $p['user'] ) || isset( $p['pass'] ) ) {
			return array( false, 'userinfo' );
		}
		if ( empty( $p['host'] ) ) {
			return array( false, 'no_host' );
		}
		$host = $p['host'];
		if ( false !== strpos( $host, '@' ) || false !== strpos( $host, ' ' ) ) {
			return array( false, 'bad_host' );
		}
		// Extension-free ASCII hostname test: printable ASCII only (0x21-0x7E).
		if ( 1 !== preg_match( '/\A[\x21-\x7e]+\z/', $host ) ) {
			return array( false, 'host_not_ascii' );
		}
		if ( isset( $p['port'] ) && 443 !== (int) $p['port'] ) {
			return array( false, 'bad_port' );
		}
		$norm = strtolower( $host );
		if ( in_array( $norm, self::SELF_HOSTS, true ) ) {
			return array( false, 'self_host' );
		}
		// EXACT match only -- no suffix / wildcard / str_ends_with / substring.
		if ( $norm !== strtolower( $expected_host ) ) {
			return array( false, 'host_mismatch' );
		}
		return array( true, 'ok' );
	}
}

/* =========================================================================
 * Shared pure helpers.
 * ========================================================================= */

function bfl_affiliate_is_hex64( $v ) : bool {
	return is_string( $v ) && 1 === preg_match( '/\A[0-9a-f]{64}\z/', $v );
}

/**
 * Exact control-plane projection timestamp contract:
 *   YYYY-MM-DDTHH:MM:SS+00:00  (canonical UTC, seconds precision)
 * Must be a real calendar instant, not just shape.
 */
function bfl_affiliate_is_canonical_utc( $v ) : bool {
	if ( ! is_string( $v ) || 1 !== preg_match( '/\A(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\+00:00\z/', $v, $m ) ) {
		return false;
	}
	if ( ! checkdate( (int) $m[2], (int) $m[3], (int) $m[1] ) ) {
		return false;
	}
	return (int) $m[4] < 24 && (int) $m[5] < 60 && (int) $m[6] < 60;
}

/**
 * Convert a validated canonical UTC string to the MySQL DATETIME form
 * `YYYY-MM-DD HH:MM:SS` (used only if a future schema stores DATETIME; the
 * D-C0.1 schema stores the exact canonical string in a VARCHAR column).
 */
function bfl_affiliate_canonical_utc_to_sql( string $canonical ) : string {
	return substr( $canonical, 0, 10 ) . ' ' . substr( $canonical, 11, 8 );
}

/**
 * Pure port of app.affiliate.projection.decide_projection_upsert.
 * @return string insert|noop|update|conflict_hash_mismatch|conflict_stale_version
 *                |forbidden_reactivation|conflict_immutable_identity_drift
 */
function bfl_affiliate_decide( ?array $current, array $incoming ) : string {
	if ( null === $current ) {
		return 'insert';
	}
	foreach ( array( 'token', 'destination_url', 'destination_host', 'link_identity_hash' ) as $f ) {
		if ( (string) $current[ $f ] !== (string) $incoming[ $f ] ) {
			return 'conflict_immutable_identity_drift';
		}
	}
	if ( 'active' === $incoming['status'] && 'disabled' === $current['status'] ) {
		return 'forbidden_reactivation';
	}
	$cv = (int) $current['projection_version'];
	$iv = (int) $incoming['projection_version'];
	if ( $iv === $cv ) {
		return hash_equals(
			(string) $current['projection_entry_hash'],
			(string) $incoming['projection_entry_hash']
		) ? 'noop' : 'conflict_hash_mismatch';
	}
	if ( $iv < $cv ) {
		return 'conflict_stale_version';
	}
	if ( 1 === $cv && 2 === $iv && 'disabled' === $incoming['status'] ) {
		return 'update';
	}
	return 'conflict_stale_version';
}

/**
 * Full semantic validation of one incoming projection entry (no DB, no hash
 * recompute -- caller does entry_hash / snapshot_hash / destination checks).
 * @return array{0:bool,1:string}
 */
function bfl_affiliate_validate_entry( $e ) : array {
	if ( ! is_array( $e ) ) {
		return array( false, 'bad_entry' );
	}
	$status = $e['status'] ?? null;
	$ver    = $e['projection_version'] ?? null;
	if ( ! is_string( $e['token'] ?? null )
		|| 1 !== preg_match( '/\A[A-Za-z0-9_-]{16,64}\z/', $e['token'] ) ) {
		return array( false, 'bad_token' );
	}
	if ( 'active' !== $status && 'disabled' !== $status ) {
		return array( false, 'bad_status' );
	}
	if ( 'active' === $status && 1 !== $ver ) {
		return array( false, 'bad_version_for_status' );
	}
	if ( 'disabled' === $status && 2 !== $ver ) {
		return array( false, 'bad_version_for_status' );
	}
	if ( ! bfl_affiliate_is_hex64( $e['link_identity_hash'] ?? null ) ) {
		return array( false, 'bad_link_identity_hash' );
	}
	if ( ! bfl_affiliate_is_hex64( $e['projection_entry_hash'] ?? null ) ) {
		return array( false, 'bad_projection_entry_hash' );
	}
	if ( ! is_string( $e['destination_url'] ?? null ) || '' === $e['destination_url'] ) {
		return array( false, 'bad_destination_url' );
	}
	if ( ! is_string( $e['destination_host'] ?? null ) || '' === $e['destination_host'] ) {
		return array( false, 'bad_destination_host' );
	}
	if ( ! bfl_affiliate_is_canonical_utc( $e['activated_at'] ?? null ) ) {
		return array( false, 'bad_activated_at' );
	}
	if ( 'active' === $status && null !== ( $e['disabled_at'] ?? null ) ) {
		return array( false, 'bad_disabled_at' );
	}
	if ( 'disabled' === $status && ! bfl_affiliate_is_canonical_utc( $e['disabled_at'] ?? null ) ) {
		return array( false, 'bad_disabled_at' );
	}
	return array( true, 'ok' );
}
