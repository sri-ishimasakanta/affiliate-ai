<?php
/**
 * Plugin Name: BizFluxLab Approval Relay
 * Description: Public decision relay for human approval of affiliate-ai change
 *              proposals. Shows one sanitized immutable proposal on a phone and
 *              records exactly one explicit human decision. It is NOT the content
 *              authority and can never edit an article.
 * Version:     0.1.0-dev
 * Requires PHP: 8.0
 *
 * SECURITY / SCOPE NOTES
 * ---------------------------------------------------------------------------
 * - Authored in the affiliate-ai repository. NOT deployed by C8.8. Pure
 *   components live in ./bizfluxlab-approval-relay/lib-core.php and are executed
 *   by ./bizfluxlab-approval-relay/tests/run.php under `php` CLI (no live
 *   WordPress). Before deployment, dbDelta / table creation must be verified on
 *   XServer.
 * - affiliate-ai is authoritative. This relay holds only what a human needs to
 *   read on a phone plus one decision. It never edits posts, never calls the
 *   WordPress content API, and has no code path that could.
 * - The authenticated endpoints reuse the EXISTING HMAC-SHA256 request format
 *   (BFL_Hmac from the affiliate runtime plugin, X-BFL-Timestamp /
 *   X-BFL-Content-SHA256 / X-BFL-Signature). No weaker mechanism is introduced.
 * - The KEY IS NOT SHARED with the affiliate runtime (C8.8.1). Redirect traffic
 *   and human approval authority are different trust domains: leaking one key
 *   must not hand over the other. This plugin reads ONLY
 *   BFL_APPROVAL_RELAY_SECRET; the affiliate runtime keeps using ONLY
 *   BFL_AFFILIATE_RUNTIME_SECRET. There is NO fallback between them. If
 *   BFL_APPROVAL_RELAY_SECRET is missing or empty, every authenticated endpoint
 *   FAILS CLOSED (the public read-only shell still renders, because it reads
 *   nothing and can decide nothing).
 * - GET can never decide. The review page GET reads nothing from the database:
 *   the capability is in the URL fragment, which browsers never send. Mail
 *   scanners and link preview bots cannot reach a state transition.
 * - The raw capability is never stored, never logged, never returned. Only its
 *   sha256 digest is kept.
 * - No IP, User-Agent, Referer, cookie beyond the short review session, or any
 *   other surveillance data is stored.
 * - Nothing here touches /go/ or the affiliate runtime tables.
 * ---------------------------------------------------------------------------
 */

defined( 'ABSPATH' ) || exit;

require_once __DIR__ . '/bizfluxlab-approval-relay/lib-core.php';

if ( ! defined( 'BFL_APPROVAL_RELAY_LOADED' ) ) {
	define( 'BFL_APPROVAL_RELAY_LOADED', true );
	define( 'BFL_APPROVAL_SCHEMA_VERSION', 1 );
	define( 'BFL_APPROVAL_SCHEMA_OPTION', 'bfl_approval_relay_schema_version' );

	define( 'BFL_APPROVAL_PAGE_PREFIX', '/bfl-approval/' );
	define( 'BFL_APPROVAL_NS', 'affiliate-ai/v1' );
	define( 'BFL_APPROVAL_SESSIONS_ROUTE', '/wp-json/affiliate-ai/v1/approval-sessions' );
	define( 'BFL_APPROVAL_DECISIONS_ROUTE', '/wp-json/affiliate-ai/v1/approval-decisions' );
	// The browser-facing routes and the confirmation cookie scope are derived
	// together from bfl_approval_rest_base() so they cannot drift apart (C8.8.2).
	// The HMAC-signed routes above stay literal: affiliate-ai signs those exact
	// paths, so they must not follow a differently-configured REST prefix.
	define( 'BFL_APPROVAL_SESSION_COOKIE', 'bfl_approval_review' );
	// The browser review session is deliberately short: it exists only for the
	// minutes a human spends reading one proposal.
	define( 'BFL_APPROVAL_REVIEW_TTL', 900 );
	define( 'BFL_APPROVAL_SESSION_ID_REGEX', '/\A[a-f0-9]{32}\z/' );
}

/* =========================================================================
 * Storage. One narrow table; nothing else is added to WordPress.
 * ========================================================================= */
final class BFL_Approval_Repo {

	public static function table() : string {
		global $wpdb;
		return $wpdb->prefix . 'bfl_approval_sessions';
	}

	public static function install() : void {
		global $wpdb;
		$table   = self::table();
		$collate = $wpdb->get_charset_collate();
		require_once ABSPATH . 'wp-admin/includes/upgrade.php';
		dbDelta(
			"CREATE TABLE `{$table}` (
				id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
				relay_session_id CHAR(32) NOT NULL,
				subject_type VARCHAR(32) NOT NULL,
				subject_id BIGINT UNSIGNED NOT NULL,
				subject_hash CHAR(64) NOT NULL,
				subject_version INT UNSIGNED NOT NULL DEFAULT 1,
				capability_digest CHAR(64) NOT NULL,
				snapshot_json LONGTEXT NOT NULL,
				state VARCHAR(16) NOT NULL DEFAULT 'pending',
				decision VARCHAR(16) NULL,
				decision_reason TEXT NULL,
				decided_at DATETIME NULL,
				consumed_at DATETIME NULL,
				expires_at DATETIME NOT NULL,
				created_at DATETIME NOT NULL,
				PRIMARY KEY (id),
				UNIQUE KEY uq_relay_session (relay_session_id),
				UNIQUE KEY uq_capability (capability_digest),
				KEY ix_state (state)
			) {$collate};"
		);
		update_option( BFL_APPROVAL_SCHEMA_OPTION, BFL_APPROVAL_SCHEMA_VERSION );
	}

	public static function get( string $relay_session_id ) : ?array {
		global $wpdb;
		$table = self::table();
		$row   = $wpdb->get_row(
			$wpdb->prepare( "SELECT * FROM `{$table}` WHERE relay_session_id = %s", $relay_session_id ),
			ARRAY_A
		);
		if ( ! $row ) {
			return null;
		}
		$row['expires_at_unix'] = strtotime( $row['expires_at'] . ' UTC' );
		return $row;
	}

	public static function insert( array $data ) : bool {
		global $wpdb;
		return false !== $wpdb->insert( self::table(), $data );
	}

	public static function update( string $relay_session_id, array $data ) : bool {
		global $wpdb;
		return false !== $wpdb->update( self::table(), $data, array( 'relay_session_id' => $relay_session_id ) );
	}

	/** @return array<int,array<string,mixed>> */
	public static function decided() : array {
		global $wpdb;
		$table = self::table();
		$rows  = $wpdb->get_results(
			$wpdb->prepare( "SELECT * FROM `{$table}` WHERE state = %s ORDER BY id ASC LIMIT 100", BFL_Approval_State::DECIDED ),
			ARRAY_A
		);
		return is_array( $rows ) ? $rows : array();
	}
}

/* =========================================================================
 * Shared helpers.
 * ========================================================================= */
/**
 * The REST path prefix this site actually serves for our namespace, e.g.
 * "/wp-json/affiliate-ai/v1/". Derived rather than hardcoded so the cookie
 * scope follows the real prefix. Falls back to the default only if rest_url()
 * is unavailable.
 */
function bfl_approval_rest_base() : string {
	$path = '';
	if ( function_exists( 'rest_url' ) ) {
		$path = (string) parse_url( rest_url( BFL_APPROVAL_NS . '/' ), PHP_URL_PATH );
	}
	if ( '' === $path || '/' === $path ) {
		return '/wp-json/' . BFL_APPROVAL_NS . '/';
	}
	return rtrim( $path, '/' ) . '/';
}

/**
 * The approval relay's OWN secret. Never falls back to the affiliate runtime's
 * key: a fallback would silently re-merge the two trust domains.
 */
function bfl_approval_secret() : string {
	// trim() so a whitespace-only constant also fails closed, matching the
	// affiliate runtime's rule.
	return defined( 'BFL_APPROVAL_RELAY_SECRET' ) ? trim( (string) BFL_APPROVAL_RELAY_SECRET ) : '';
}

function bfl_approval_error( string $code, int $status ) : WP_REST_Response {
	// Sanitized: a code only. Never an exception body, never a capability.
	return new WP_REST_Response( array( 'error' => array( 'code' => $code ) ), $status );
}

function bfl_approval_send_headers( string $script_nonce = '' ) : void {
	foreach ( bfl_approval_headers( $script_nonce ) as $name => $value ) {
		header( "{$name}: {$value}" );
	}
}

/**
 * Verify the authenticated affiliate-ai -> relay call using the EXISTING HMAC
 * contract. Fails closed when the shared secret is absent.
 */
function bfl_approval_verify_hmac( WP_REST_Request $request, string $path ) : array {
	$secret = bfl_approval_secret();
	if ( '' === $secret ) {
		return array( false, 'secret_not_configured' );
	}
	return BFL_Hmac::verify(
		$secret,
		$request->get_method(),
		$path,
		(string) $request->get_header( 'x-bfl-timestamp' ),
		(string) $request->get_header( 'x-bfl-content-sha256' ),
		(string) $request->get_header( 'x-bfl-signature' ),
		(string) $request->get_body(),
		time()
	);
}

/* =========================================================================
 * Public review page. GET ONLY RENDERS A SHELL -- it reads nothing, decides
 * nothing, consumes nothing. This is the scanner-safety guarantee.
 * ========================================================================= */
add_action(
	'parse_request',
	function () {
		$uri = isset( $_SERVER['REQUEST_URI'] ) ? (string) $_SERVER['REQUEST_URI'] : '';
		$path = (string) parse_url( $uri, PHP_URL_PATH );
		if ( 0 !== strpos( $path, BFL_APPROVAL_PAGE_PREFIX ) ) {
			return;
		}
		$sid = substr( $path, strlen( BFL_APPROVAL_PAGE_PREFIX ) );
		$sid = trim( $sid, '/' );

		$nonce = bin2hex( random_bytes( 16 ) );
		bfl_approval_send_headers( $nonce );
		header( 'Content-Type: text/html; charset=utf-8' );
		status_header( 200 );

		if ( 1 !== preg_match( BFL_APPROVAL_SESSION_ID_REGEX, $sid ) ) {
			echo BFL_Approval_Render::unavailable(); // phpcs:ignore WordPress.Security.EscapeOutput
			exit;
		}
		// Note: we do NOT look the session up here. A scanner learns nothing.
		$routes = bfl_approval_review_routes( bfl_approval_rest_base() );
		echo BFL_Approval_Render::shell( // phpcs:ignore WordPress.Security.EscapeOutput
			$sid,
			$routes['exchange'],
			$routes['decide'],
			$nonce
		);
		exit;
	},
	1
);

/* =========================================================================
 * REST routes.
 * ========================================================================= */
add_action(
	'rest_api_init',
	function () {
		// -- authenticated: affiliate-ai creates a session -------------------
		register_rest_route(
			BFL_APPROVAL_NS,
			'/approval-sessions',
			array(
				'methods'             => 'POST',
				'permission_callback' => '__return_true',
				'callback'            => 'bfl_approval_create_session',
			)
		);
		register_rest_route(
			BFL_APPROVAL_NS,
			'/approval-sessions/revoke',
			array(
				'methods'             => 'POST',
				'permission_callback' => '__return_true',
				'callback'            => 'bfl_approval_revoke_session',
			)
		);
		// -- authenticated: affiliate-ai polls / acknowledges ----------------
		register_rest_route(
			BFL_APPROVAL_NS,
			'/approval-decisions',
			array(
				'methods'             => 'GET',
				'permission_callback' => '__return_true',
				'callback'            => 'bfl_approval_list_decisions',
			)
		);
		register_rest_route(
			BFL_APPROVAL_NS,
			'/approval-decisions/ack',
			array(
				'methods'             => 'POST',
				'permission_callback' => '__return_true',
				'callback'            => 'bfl_approval_ack_decision',
			)
		);
		// -- browser: capability exchange (READ-ONLY) ------------------------
		register_rest_route(
			BFL_APPROVAL_NS,
			'/approval-review/exchange',
			array(
				'methods'             => 'POST',
				'permission_callback' => '__return_true',
				'callback'            => 'bfl_approval_exchange',
			)
		);
		// -- browser: the one explicit decision ------------------------------
		register_rest_route(
			BFL_APPROVAL_NS,
			'/approval-review/decide',
			array(
				'methods'             => 'POST',
				'permission_callback' => '__return_true',
				'callback'            => 'bfl_approval_decide',
			)
		);
	}
);

function bfl_approval_create_session( WP_REST_Request $request ) {
	list( $ok, $reason ) = bfl_approval_verify_hmac( $request, BFL_APPROVAL_SESSIONS_ROUTE );
	if ( ! $ok ) {
		return bfl_approval_error( $reason, 401 );
	}
	if ( (int) get_option( BFL_APPROVAL_SCHEMA_OPTION, 0 ) < BFL_APPROVAL_SCHEMA_VERSION ) {
		BFL_Approval_Repo::install();
	}
	$body = json_decode( (string) $request->get_body(), true );
	if ( ! is_array( $body ) ) {
		return bfl_approval_error( 'invalid_payload', 400 );
	}
	foreach ( array( 'relay_session_id', 'subject_type', 'subject_id', 'subject_hash', 'capability_digest', 'expires_at', 'snapshot' ) as $key ) {
		if ( ! isset( $body[ $key ] ) ) {
			return bfl_approval_error( 'invalid_payload', 400 );
		}
	}
	if ( 1 !== preg_match( BFL_APPROVAL_SESSION_ID_REGEX, (string) $body['relay_session_id'] ) ) {
		return bfl_approval_error( 'invalid_payload', 400 );
	}
	if ( 1 !== preg_match( '/\A[a-f0-9]{64}\z/', (string) $body['capability_digest'] ) ) {
		return bfl_approval_error( 'invalid_payload', 400 );
	}
	$existing = BFL_Approval_Repo::get( (string) $body['relay_session_id'] );
	if ( null !== $existing ) {
		// Idempotent: creating the same session twice is not an error.
		return new WP_REST_Response( array( 'relay_session_id' => $existing['relay_session_id'], 'state' => $existing['state'] ), 200 );
	}
	$inserted = BFL_Approval_Repo::insert(
		array(
			'relay_session_id'  => (string) $body['relay_session_id'],
			'subject_type'      => (string) $body['subject_type'],
			'subject_id'        => (int) $body['subject_id'],
			'subject_hash'      => (string) $body['subject_hash'],
			'subject_version'   => (int) ( $body['subject_version'] ?? 1 ),
			'capability_digest' => (string) $body['capability_digest'],
			'snapshot_json'     => wp_json_encode( $body['snapshot'] ),
			'state'             => BFL_Approval_State::PENDING,
			'expires_at'        => gmdate( 'Y-m-d H:i:s', strtotime( (string) $body['expires_at'] ) ),
			'created_at'        => gmdate( 'Y-m-d H:i:s' ),
		)
	);
	if ( ! $inserted ) {
		return bfl_approval_error( 'invalid_payload', 400 );
	}
	return new WP_REST_Response( array( 'relay_session_id' => (string) $body['relay_session_id'], 'state' => BFL_Approval_State::PENDING ), 201 );
}

function bfl_approval_revoke_session( WP_REST_Request $request ) {
	list( $ok, $reason ) = bfl_approval_verify_hmac( $request, BFL_APPROVAL_SESSIONS_ROUTE . '/revoke' );
	if ( ! $ok ) {
		return bfl_approval_error( $reason, 401 );
	}
	$body = json_decode( (string) $request->get_body(), true );
	$sid  = is_array( $body ) ? (string) ( $body['relay_session_id'] ?? '' ) : '';
	$row  = BFL_Approval_Repo::get( $sid );
	if ( null === $row ) {
		return bfl_approval_error( 'session_not_found', 404 );
	}
	BFL_Approval_Repo::update( $sid, array( 'state' => BFL_Approval_State::REVOKED ) );
	return new WP_REST_Response( array( 'relay_session_id' => $sid, 'state' => BFL_Approval_State::REVOKED ), 200 );
}

function bfl_approval_list_decisions( WP_REST_Request $request ) {
	list( $ok, $reason ) = bfl_approval_verify_hmac( $request, BFL_APPROVAL_DECISIONS_ROUTE );
	if ( ! $ok ) {
		return bfl_approval_error( $reason, 401 );
	}
	$out = array();
	foreach ( BFL_Approval_Repo::decided() as $row ) {
		$out[] = array(
			'relay_session_id' => $row['relay_session_id'],
			'subject_type'     => $row['subject_type'],
			'subject_id'       => (int) $row['subject_id'],
			'subject_hash'     => $row['subject_hash'],
			'subject_version'  => (int) $row['subject_version'],
			'decision'         => $row['decision'],
			'decision_reason'  => $row['decision_reason'],
			'decided_at'       => $row['decided_at'],
		);
	}
	return new WP_REST_Response( array( 'decisions' => $out ), 200 );
}

function bfl_approval_ack_decision( WP_REST_Request $request ) {
	list( $ok, $reason ) = bfl_approval_verify_hmac( $request, BFL_APPROVAL_DECISIONS_ROUTE . '/ack' );
	if ( ! $ok ) {
		return bfl_approval_error( $reason, 401 );
	}
	$body = json_decode( (string) $request->get_body(), true );
	$sid  = is_array( $body ) ? (string) ( $body['relay_session_id'] ?? '' ) : '';
	$row  = BFL_Approval_Repo::get( $sid );
	if ( null === $row ) {
		return bfl_approval_error( 'session_not_found', 404 );
	}
	// Idempotent: acknowledging twice is fine, it just stays consumed.
	BFL_Approval_Repo::update(
		$sid,
		array( 'state' => BFL_Approval_State::CONSUMED, 'consumed_at' => gmdate( 'Y-m-d H:i:s' ) )
	);
	return new WP_REST_Response( array( 'relay_session_id' => $sid, 'state' => BFL_Approval_State::CONSUMED ), 200 );
}

/**
 * Capability exchange. READ-ONLY: it validates the capability and returns the
 * snapshot plus a nonce, but does NOT consume the one-time decision right.
 */
function bfl_approval_exchange( WP_REST_Request $request ) {
	bfl_approval_send_headers();
	$body = json_decode( (string) $request->get_body(), true );
	if ( ! is_array( $body ) ) {
		return bfl_approval_error( 'invalid_payload', 400 );
	}
	$sid = (string) ( $body['relay_session_id'] ?? '' );
	$cap = (string) ( $body['capability'] ?? '' );
	$row = BFL_Approval_Repo::get( $sid );
	if ( null === $row ) {
		// Same shape as a bad capability: reveal nothing about existence.
		return bfl_approval_error( 'session_not_found', 404 );
	}
	list( $ok, $reason ) = BFL_Approval_Capability::verify( $cap, $row, time() );
	if ( ! $ok ) {
		return bfl_approval_error( 'session_not_found', 404 );
	}
	list( $viewable, $view_reason ) = BFL_Approval_State::can_review( $row, time() );
	if ( ! $viewable ) {
		return bfl_approval_error( $view_reason, 409 );
	}

	// A short review session bound to this browser. The capability is not stored
	// in the cookie -- only a random session value tied to this relay session.
	$review_nonce = bin2hex( random_bytes( 32 ) );
	set_transient( 'bfl_approval_nonce_' . $sid, hash( 'sha256', $review_nonce ), BFL_APPROVAL_REVIEW_TTL );
	setcookie(
		BFL_APPROVAL_SESSION_COOKIE,
		$review_nonce,
		array(
			'expires'  => time() + BFL_APPROVAL_REVIEW_TTL,
			// Scope the cookie to the REST namespace that actually receives it.
			// The review page never reads this cookie (it is HttpOnly), so the
			// page prefix was the wrong tree: RFC 6265 would never deliver it to
			// .../approval-review/decide. Still narrower than "/".
			'path'     => bfl_approval_rest_base(),
			'secure'   => true,
			'httponly' => true,
			'samesite' => 'Strict',
		)
	);
	$snapshot = json_decode( (string) $row['snapshot_json'], true );
	$expires_local = wp_date( 'Y-m-d H:i', (int) $row['expires_at_unix'] );
	return new WP_REST_Response(
		array(
			'snapshot' => bfl_approval_public_snapshot( is_array( $snapshot ) ? $snapshot : array(), (string) $expires_local ),
			'nonce'    => $review_nonce,
			'state'    => $row['state'],
		),
		200
	);
}

/**
 * The one explicit human decision. Requires the review session established by
 * the exchange (cookie + matching nonce), so a bare POST from elsewhere cannot
 * decide, and a GET can never reach this at all.
 */
function bfl_approval_decide( WP_REST_Request $request ) {
	bfl_approval_send_headers();
	$body = json_decode( (string) $request->get_body(), true );
	if ( ! is_array( $body ) ) {
		return bfl_approval_error( 'invalid_payload', 400 );
	}
	$sid      = (string) ( $body['relay_session_id'] ?? '' );
	$decision = (string) ( $body['decision'] ?? '' );
	if ( ! BFL_Approval_State::is_valid_decision( $decision ) ) {
		return bfl_approval_error( 'invalid_payload', 400 );
	}

	$row = BFL_Approval_Repo::get( $sid );
	if ( null === $row ) {
		return bfl_approval_error( 'session_not_found', 404 );
	}

	// Same-origin confirmation + state, in one tested guard (see lib-core).
	$cookie = isset( $_COOKIE[ BFL_APPROVAL_SESSION_COOKIE ] ) ? (string) $_COOKIE[ BFL_APPROVAL_SESSION_COOKIE ] : '';
	list( $ok, $reason, $status ) = bfl_approval_decision_guard(
		$row,
		$cookie,
		get_transient( 'bfl_approval_nonce_' . $sid ),
		(string) $request->get_header( 'x-bfl-approval-nonce' ),
		time()
	);
	if ( ! $ok ) {
		// A duplicate POST lands here and is reported, not applied twice.
		return bfl_approval_error( $reason, $status );
	}
	// T6.1: never accept an approval for a snapshot without reviewable text.
	$stored = json_decode( (string) $row['snapshot_json'], true );
	list( $ok, $reason, $status ) = bfl_approval_decision_content_guard(
		is_array( $stored ) ? $stored : array(),
		$decision
	);
	if ( ! $ok ) {
		return bfl_approval_error( $reason, $status );
	}

	$updated = BFL_Approval_Repo::update(
		$sid,
		array(
			'state'           => BFL_Approval_State::DECIDED,
			'decision'        => $decision,
			'decision_reason' => mb_substr( (string) ( $body['reason'] ?? '' ), 0, 500 ),
			'decided_at'      => gmdate( 'Y-m-d H:i:s' ),
		)
	);
	if ( ! $updated ) {
		return bfl_approval_error( 'invalid_payload', 400 );
	}
	// The decision right is now spent: the transient is dropped and the state is
	// no longer pending, so the capability cannot decide again.
	delete_transient( 'bfl_approval_nonce_' . $sid );
	return new WP_REST_Response( array( 'relay_session_id' => $sid, 'decision' => $decision, 'state' => BFL_Approval_State::DECIDED ), 200 );
}

/* =========================================================================
 * Schema install for an administrator only. Never triggered by anonymous
 * traffic (matches the affiliate runtime's rule).
 * ========================================================================= */
add_action(
	'admin_init',
	function () {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}
		if ( (int) get_option( BFL_APPROVAL_SCHEMA_OPTION, 0 ) < BFL_APPROVAL_SCHEMA_VERSION ) {
			BFL_Approval_Repo::install();
		}
	}
);

/* The approval page must never become discoverable content. */
add_filter( 'wp_sitemaps_enabled', function ( $enabled ) { return $enabled; } );
add_action(
	'robots_txt',
	function ( $output ) {
		return $output . "\nDisallow: " . BFL_APPROVAL_PAGE_PREFIX . "\n";
	},
	10,
	1
);
