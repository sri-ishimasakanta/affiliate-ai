<?php
/**
 * Plugin Name: BizFluxLab Affiliate Runtime
 * Description: Public runtime for first-party affiliate redirects. Receives an
 *              immutable/versioned target projection from the affiliate-ai control
 *              plane over an HMAC-signed REST endpoint, resolves /go/{token} to a
 *              temporary redirect, and appends minimal raw click events.
 * Version:     0.2.0-dev
 * Requires PHP: 8.0
 *
 * SECURITY / SCOPE NOTES
 * ---------------------------------------------------------------------------
 * - Authored in the affiliate-ai repository. NOT deployed by phase 3C-5F-D-C0 /
 *   D-C0.1. Pure components live in ./bizfluxlab-affiliate-runtime/lib-core.php
 *   and are executed by ./bizfluxlab-affiliate-runtime/tests/run.php under
 *   `php` CLI (no live WordPress). Before deployment (D-C1) dbDelta / table
 *   creation / indexes must still be verified on XServer.
 * - affiliate-ai is authoritative for target identity, token, destination URL,
 *   destination-host approval, status, link identity and projection hashes.
 *   This runtime only stores an immutable/versioned projection.
 * - The shared HMAC secret MUST be provided via a wp-config.php constant
 *   BFL_AFFILIATE_RUNTIME_SECRET. There is NO hardcoded / fallback secret.
 *   If missing/empty the projection + export endpoints FAIL CLOSED. /go never
 *   needs the secret (it is the public path).
 * - Schema install is NOT triggered by anonymous REST or /go traffic. It runs
 *   only on admin_init for a manage_options user, or lazily inside the already
 *   HMAC-authenticated projection endpoint before the table is required.
 * - No IP / hashed IP / User-Agent / Referer / cookie / session / user id /
 *   email / query string / device data is ever stored.
 * - No seed target, no default destination, no demo redirect.
 * ---------------------------------------------------------------------------
 */

defined( 'ABSPATH' ) || exit;

require_once __DIR__ . '/bizfluxlab-affiliate-runtime/lib-core.php';

if ( ! defined( 'BFL_AFFILIATE_RUNTIME_LOADED' ) ) {
	define( 'BFL_AFFILIATE_RUNTIME_LOADED', true );
	define( 'BFL_AFFILIATE_RUNTIME_SCHEMA_VERSION', 1 );
	define( 'BFL_AFFILIATE_RUNTIME_SCHEMA_OPTION', 'bfl_affiliate_runtime_schema_version' );

	define( 'BFL_AFFILIATE_PROJECTION_ROUTE', '/wp-json/affiliate-ai/v1/target-projections' );
	define( 'BFL_AFFILIATE_CLICKS_ROUTE', '/wp-json/affiliate-ai/v1/outbound-clicks' );
	define( 'BFL_AFFILIATE_GO_PREFIX', '/go/' );
	define( 'BFL_AFFILIATE_TOKEN_REGEX', '/\A[A-Za-z0-9_-]{16,64}\z/' );
	define( 'BFL_AFFILIATE_CLICK_EXPORT_MAX', 1000 );
}

/* =========================================================================
 * $wpdb-prepared projection + click repositories.
 * ========================================================================= */
final class BFL_Runtime_Repo {

	public static function targets_table() : string {
		global $wpdb;
		return $wpdb->prefix . 'bfl_affiliate_targets';
	}

	public static function clicks_table() : string {
		global $wpdb;
		return $wpdb->prefix . 'bfl_outbound_clicks';
	}

	public static function get_by_token( string $token ) : ?array {
		global $wpdb;
		$table = self::targets_table();
		$row   = $wpdb->get_row(
			$wpdb->prepare( "SELECT * FROM `{$table}` WHERE token = %s", $token ),
			ARRAY_A
		);
		return $row ?: null;
	}

	/** @param string[] $tokens @return array<string,array> */
	public static function get_map( array $tokens ) : array {
		global $wpdb;
		if ( empty( $tokens ) ) {
			return array();
		}
		$table        = self::targets_table();
		$placeholders = implode( ',', array_fill( 0, count( $tokens ), '%s' ) );
		$sql          = $wpdb->prepare(
			"SELECT * FROM `{$table}` WHERE token IN ({$placeholders})",
			$tokens
		);
		$out = array();
		foreach ( (array) $wpdb->get_results( $sql, ARRAY_A ) as $row ) {
			$out[ (string) $row['token'] ] = $row;
		}
		return $out;
	}

	public static function insert_projection( array $e, string $now_utc ) : bool {
		global $wpdb;
		// activated_at / disabled_at stored as the EXACT canonical protocol
		// string (VARCHAR column) -- no implicit MySQL DATETIME coercion.
		return false !== $wpdb->insert(
			self::targets_table(),
			array(
				'token'                 => $e['token'],
				'destination_url'       => $e['destination_url'],
				'destination_host'      => $e['destination_host'],
				'status'                => $e['status'],
				'link_identity_hash'    => $e['link_identity_hash'],
				'projection_version'    => (int) $e['projection_version'],
				'activated_at'          => $e['activated_at'],
				'disabled_at'           => $e['disabled_at'],
				'projection_entry_hash' => $e['projection_entry_hash'],
				'created_at'            => $now_utc,
				'updated_at'            => $now_utc,
			),
			array( '%s', '%s', '%s', '%s', '%s', '%d', '%s', '%s', '%s', '%s', '%s' )
		);
	}

	public static function update_projection_to_disabled( array $e, string $now_utc ) : bool {
		global $wpdb;
		return false !== $wpdb->update(
			self::targets_table(),
			array(
				'status'                => 'disabled',
				'projection_version'    => 2,
				'disabled_at'           => $e['disabled_at'],
				'projection_entry_hash' => $e['projection_entry_hash'],
				'updated_at'            => $now_utc,
			),
			array( 'token' => $e['token'] ),
			array( '%s', '%d', '%s', '%s', '%s' ),
			array( '%s' )
		);
	}

	public static function append_click( int $affiliate_target_id, string $token, string $now_utc ) : bool {
		global $wpdb;
		return false !== $wpdb->insert(
			self::clicks_table(),
			array(
				'affiliate_target_id' => $affiliate_target_id,
				'token'               => $token,
				'clicked_at'          => $now_utc,
			),
			array( '%d', '%s', '%s' )
		);
	}

	/** @return array<int,array{id:int,token:string,clicked_at:string}> */
	public static function clicks_since( int $since_id, int $limit ) : array {
		global $wpdb;
		$table = self::clicks_table();
		$rows  = $wpdb->get_results(
			$wpdb->prepare(
				"SELECT id, token, clicked_at FROM `{$table}` WHERE id > %d ORDER BY id ASC LIMIT %d",
				$since_id,
				$limit
			),
			ARRAY_A
		);
		$out = array();
		foreach ( (array) $rows as $r ) {
			$out[] = array(
				'id'         => (int) $r['id'],
				'token'      => (string) $r['token'],
				'clicked_at' => (string) $r['clicked_at'],
			);
		}
		return $out;
	}
}

/* =========================================================================
 * Schema install / update.
 *
 * D-C0.1 hardening: NOT hooked to rest_api_init. Anonymous REST traffic and
 * /go traffic can NEVER trigger DDL. Runs only when an admin (manage_options)
 * hits admin_init, or lazily inside the HMAC-authenticated projection endpoint.
 * No DROP. No destructive migration. Idempotent (dbDelta).
 * ========================================================================= */
final class BFL_Schema {

	/** Admin path: only a manage_options user, only in wp-admin. */
	public static function maybe_install_for_admin() : void {
		if ( ! function_exists( 'current_user_can' ) || ! current_user_can( 'manage_options' ) ) {
			return;
		}
		self::ensure_current();
	}

	/** Authenticated-endpoint path: called AFTER HMAC verification succeeds. */
	public static function ensure_current() : void {
		$current = (int) get_option( BFL_AFFILIATE_RUNTIME_SCHEMA_OPTION, 0 );
		if ( $current === BFL_AFFILIATE_RUNTIME_SCHEMA_VERSION ) {
			return;
		}
		self::install();
		update_option( BFL_AFFILIATE_RUNTIME_SCHEMA_OPTION, BFL_AFFILIATE_RUNTIME_SCHEMA_VERSION );
	}

	public static function install() : void {
		global $wpdb;
		require_once ABSPATH . 'wp-admin/includes/upgrade.php';
		$charset = $wpdb->get_charset_collate();
		$targets = BFL_Runtime_Repo::targets_table();
		$clicks  = BFL_Runtime_Repo::clicks_table();

		// activated_at / disabled_at are VARCHAR(32): they hold the EXACT
		// canonical protocol string (YYYY-MM-DDTHH:MM:SS+00:00), not a MySQL
		// DATETIME. They are projection/audit state, never queried as dates.
		dbDelta(
			"CREATE TABLE {$targets} (
				id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
				token VARCHAR(64) NOT NULL,
				destination_url TEXT NOT NULL,
				destination_host VARCHAR(255) NOT NULL,
				status VARCHAR(20) NOT NULL,
				link_identity_hash CHAR(64) NOT NULL,
				projection_version SMALLINT UNSIGNED NOT NULL,
				activated_at VARCHAR(32) NOT NULL,
				disabled_at VARCHAR(32) NULL,
				projection_entry_hash CHAR(64) NOT NULL,
				created_at DATETIME NOT NULL,
				updated_at DATETIME NOT NULL,
				PRIMARY KEY  (id),
				UNIQUE KEY token (token),
				KEY status (status)
			) {$charset};"
		);

		// NOTE: no ip / hashed ip / user_agent / referer / cookie / session /
		// user_id / email / query / device columns -- by design.
		dbDelta(
			"CREATE TABLE {$clicks} (
				id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
				affiliate_target_id BIGINT UNSIGNED NOT NULL,
				token VARCHAR(64) NOT NULL,
				clicked_at DATETIME NOT NULL,
				PRIMARY KEY  (id),
				KEY affiliate_target_id (affiliate_target_id),
				KEY clicked_at (clicked_at)
			) {$charset};"
		);
	}
}

/* =========================================================================
 * Shared WP-coupled helpers.
 * ========================================================================= */
function bfl_affiliate_secret() : string {
	if ( ! defined( 'BFL_AFFILIATE_RUNTIME_SECRET' ) ) {
		return '';
	}
	return trim( (string) constant( 'BFL_AFFILIATE_RUNTIME_SECRET' ) );
}

function bfl_affiliate_now_utc() : string {
	return gmdate( 'Y-m-d H:i:s' );
}

function bfl_affiliate_header( string $name ) : string {
	$key = 'HTTP_' . strtoupper( str_replace( '-', '_', $name ) );
	return isset( $_SERVER[ $key ] ) ? (string) $_SERVER[ $key ] : '';
}

/* =========================================================================
 * REST routes: projection UPSERT + click export.
 * ========================================================================= */
add_action(
	'rest_api_init',
	static function () {
		register_rest_route(
			'affiliate-ai/v1',
			'/target-projections',
			array(
				'methods'             => 'POST',
				'permission_callback' => '__return_true', // auth is HMAC, enforced in the callback
				'callback'            => 'bfl_affiliate_projection_endpoint',
			)
		);
		register_rest_route(
			'affiliate-ai/v1',
			'/outbound-clicks',
			array(
				'methods'             => 'GET',
				'permission_callback' => '__return_true',
				'callback'            => 'bfl_affiliate_clicks_export_endpoint',
			)
		);
	}
);

function bfl_affiliate_error( int $status, string $code ) : WP_REST_Response {
	return new WP_REST_Response( array( 'error' => array( 'code' => $code ) ), $status );
}

function bfl_affiliate_projection_endpoint( WP_REST_Request $request ) {
	$secret = bfl_affiliate_secret();
	if ( '' === $secret ) {
		return bfl_affiliate_error( 403, 'secret_not_configured' );
	}

	$raw = $request->get_body();
	list( $ok, $reason ) = BFL_Hmac::verify(
		$secret,
		'POST',
		BFL_AFFILIATE_PROJECTION_ROUTE,
		bfl_affiliate_header( 'X-BFL-Timestamp' ),
		bfl_affiliate_header( 'X-BFL-Content-SHA256' ),
		bfl_affiliate_header( 'X-BFL-Signature' ),
		$raw,
		time()
	);
	if ( ! $ok ) {
		return bfl_affiliate_error( 401, $reason );
	}

	// Authenticated: safe to lazily ensure the schema before we touch the table.
	BFL_Schema::ensure_current();

	try {
		$body = json_decode( $raw, true, 32, JSON_THROW_ON_ERROR );
	} catch ( Throwable $e ) {
		return bfl_affiliate_error( 400, 'invalid_json' );
	}
	if ( ! is_array( $body ) || 1 !== ( $body['schema_version'] ?? null ) ) {
		return bfl_affiliate_error( 400, 'bad_schema_version' );
	}
	$targets = $body['targets'] ?? null;
	if ( ! is_array( $targets ) || ( ! empty( $targets ) && ! array_is_list( $targets ) ) ) {
		return bfl_affiliate_error( 400, 'bad_targets' );
	}

	// ---- semantic validation (no DB yet) ------------------------------
	foreach ( $targets as $e ) {
		list( $vok, $vreason ) = bfl_affiliate_validate_entry( $e );
		if ( ! $vok ) {
			return bfl_affiliate_error( 422, $vreason );
		}
		// recompute entry hash (do NOT trust the supplied hash).
		if ( ! hash_equals( BFL_Canonical::entry_hash( $e ), (string) $e['projection_entry_hash'] ) ) {
			return bfl_affiliate_error( 409, 'entry_hash_mismatch' );
		}
		// request-time destination validation (never rewrites the URL).
		list( $dok ) = BFL_Destination_Validator::validate(
			(string) $e['destination_url'],
			(string) $e['destination_host']
		);
		if ( ! $dok ) {
			return bfl_affiliate_error( 409, 'destination_validation_failed' );
		}
	}

	$recomputed_snapshot = BFL_Canonical::snapshot_hash( 1, $targets );
	if ( ! hash_equals( $recomputed_snapshot, (string) ( $body['projection_snapshot_hash'] ?? '' ) ) ) {
		return bfl_affiliate_error( 409, 'snapshot_hash_mismatch' );
	}

	// ---- decide (atomic): reject the whole batch on any conflict -----
	$tokens  = array_map( static fn( $e ) => (string) $e['token'], $targets );
	$current = BFL_Runtime_Repo::get_map( $tokens );
	$plan    = array();
	foreach ( $targets as $e ) {
		$decision = bfl_affiliate_decide( $current[ (string) $e['token'] ] ?? null, $e );
		if ( ! in_array( $decision, array( 'insert', 'noop', 'update' ), true ) ) {
			return bfl_affiliate_error( 409, $decision );
		}
		$plan[] = array( $decision, $e );
	}

	// ---- one DB transaction for the accepted batch ------------------
	global $wpdb;
	$now      = bfl_affiliate_now_utc();
	$inserted = 0;
	$updated  = 0;
	$noop     = 0;
	$wpdb->query( 'START TRANSACTION' );
	foreach ( $plan as list( $decision, $e ) ) {
		if ( 'insert' === $decision ) {
			if ( ! BFL_Runtime_Repo::insert_projection( $e, $now ) ) {
				$wpdb->query( 'ROLLBACK' );
				return bfl_affiliate_error( 500, 'persist_failed' );
			}
			++$inserted;
		} elseif ( 'update' === $decision ) {
			if ( ! BFL_Runtime_Repo::update_projection_to_disabled( $e, $now ) ) {
				$wpdb->query( 'ROLLBACK' );
				return bfl_affiliate_error( 500, 'persist_failed' );
			}
			++$updated;
		} else {
			++$noop;
		}
	}
	$wpdb->query( 'COMMIT' );

	return new WP_REST_Response(
		array(
			'schema_version'           => 1,
			'projection_snapshot_hash' => $recomputed_snapshot,
			'received_count'           => count( $targets ),
			'inserted_count'           => $inserted,
			'updated_count'            => $updated,
			'unchanged_count'          => $noop,
		),
		200
	);
}

function bfl_affiliate_clicks_export_endpoint( WP_REST_Request $request ) {
	$secret = bfl_affiliate_secret();
	if ( '' === $secret ) {
		return bfl_affiliate_error( 403, 'secret_not_configured' );
	}

	$since_raw = (string) $request->get_param( 'since_id' );
	$limit_raw = (string) $request->get_param( 'limit' );
	if ( ! preg_match( '/\A\d{1,19}\z/', $since_raw ) || ! preg_match( '/\A\d{1,7}\z/', $limit_raw ) ) {
		return bfl_affiliate_error( 400, 'bad_cursor' );
	}
	$since_id = (int) $since_raw;
	$limit    = (int) $limit_raw;
	if ( $limit < 1 || $limit > BFL_AFFILIATE_CLICK_EXPORT_MAX ) {
		return bfl_affiliate_error( 400, 'bad_limit' );
	}

	// Canonical signed request target (keys sorted). The client must have signed
	// exactly this string, so an altered cursor breaks the signature.
	$signed_path = BFL_AFFILIATE_CLICKS_ROUTE . '?limit=' . $limit . '&since_id=' . $since_id;

	list( $ok, $reason ) = BFL_Hmac::verify(
		$secret,
		'GET',
		$signed_path,
		bfl_affiliate_header( 'X-BFL-Timestamp' ),
		bfl_affiliate_header( 'X-BFL-Content-SHA256' ),
		bfl_affiliate_header( 'X-BFL-Signature' ),
		'',
		time()
	);
	if ( ! $ok ) {
		return bfl_affiliate_error( 401, $reason );
	}

	$rows         = BFL_Runtime_Repo::clicks_since( $since_id, $limit );
	$next_since_id = empty( $rows ) ? $since_id : (int) $rows[ count( $rows ) - 1 ]['id'];

	return new WP_REST_Response(
		array(
			'schema_version' => 1,
			'count'          => count( $rows ),
			'limit'          => $limit,
			'next_since_id'  => $next_since_id,
			'rows'           => $rows, // id, token, clicked_at only
		),
		200
	);
}

/* =========================================================================
 * Public /go/{token} redirect. Bypasses WP routing for the fixed /go/ prefix.
 * Never installs schema. Never reads a destination from the request.
 * ========================================================================= */
add_action( 'muplugins_loaded', 'bfl_affiliate_maybe_handle_go', 1 );

function bfl_affiliate_go_finish( int $status ) : void {
	if ( ! headers_sent() ) {
		status_header( $status );
		header( 'Cache-Control: no-store' );
		header( 'X-Robots-Tag: noindex, nofollow' );
		header( 'Content-Type: text/plain; charset=utf-8' );
	}
	echo 404 === $status ? 'Not Found' : 'Gone';
	exit;
}

function bfl_affiliate_maybe_handle_go() : void {
	$path = (string) parse_url( $_SERVER['REQUEST_URI'] ?? '', PHP_URL_PATH );
	if ( 0 !== strpos( $path, BFL_AFFILIATE_GO_PREFIX ) ) {
		return;
	}
	$token = rtrim( substr( $path, strlen( BFL_AFFILIATE_GO_PREFIX ) ), '/' );

	if ( 1 !== preg_match( BFL_AFFILIATE_TOKEN_REGEX, $token ) || false !== strpos( $token, '/' ) ) {
		bfl_affiliate_go_finish( 404 );
	}

	$row = BFL_Runtime_Repo::get_by_token( $token );
	if ( null === $row ) {
		bfl_affiliate_go_finish( 404 );
	}
	if ( 'active' !== $row['status'] ) {
		bfl_affiliate_go_finish( 410 );
	}

	list( $dok ) = BFL_Destination_Validator::validate(
		(string) $row['destination_url'],
		(string) $row['destination_host']
	);
	if ( ! $dok ) {
		bfl_affiliate_go_finish( 410 );
	}

	// Safe resolution complete. Fail-OPEN for click persistence ONLY.
	try {
		BFL_Runtime_Repo::append_click( (int) $row['id'], $token, bfl_affiliate_now_utc() );
	} catch ( Throwable $e ) {
		error_log( 'bfl-affiliate: click persistence failed (generic)' );
	}

	if ( ! headers_sent() ) {
		status_header( 302 );
		header( 'Cache-Control: no-store' );
		header( 'X-Robots-Tag: noindex, nofollow' );
		// Exact stored string. Raw header() (not wp_redirect) so WP does not
		// sanitise / rewrite the approved affiliate URL. Referrer-Policy is
		// deliberately NOT set here (deferred until before D-F, per ASP rules).
		header( 'Location: ' . $row['destination_url'], true, 302 );
	}
	exit;
}

/* Schema: admin-only, no anonymous/REST/go trigger. */
add_action( 'admin_init', array( 'BFL_Schema', 'maybe_install_for_admin' ) );
