"""wordpress/mu-plugins/bizfluxlab-affiliate-runtime{.php,/lib-core.php} の静的契約検証。

実行検証は tests/unit/test_wp_affiliate_runtime_php_harness.py (php run.php) が担う。
ここは source が D-C0.1 のセキュリティ/スコープ契約を満たすかの静的確認。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "wordpress" / "mu-plugins"
_PLUGIN = _DIR / "bizfluxlab-affiliate-runtime.php"
_LIB = _DIR / "bizfluxlab-affiliate-runtime" / "lib-core.php"
_HARNESS = _DIR / "bizfluxlab-affiliate-runtime" / "tests" / "run.php"
_README = _DIR / "bizfluxlab-affiliate-runtime.README.md"


@pytest.fixture(scope="module")
def src() -> str:
    return _PLUGIN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lib() -> str:
    return _LIB.read_text(encoding="utf-8")


# ---- structure --------------------------------------------------------
def test_files_exist(src: str, lib: str) -> None:
    for p in (_PLUGIN, _LIB, _HARNESS, _README):
        assert p.is_file(), p
    assert src.startswith("<?php") and lib.startswith("<?php")
    assert "defined( 'ABSPATH' ) || exit;" in src
    # lib-core is WP-free and directly executable: no ABSPATH guard, no WP calls
    assert "defined( 'ABSPATH' )" not in lib
    assert "$wpdb" not in lib and "add_action(" not in lib and "get_option(" not in lib
    assert "require_once __DIR__ . '/bizfluxlab-affiliate-runtime/lib-core.php';" in src


# ---- secret handling ------------------------------------------------
def test_no_hardcoded_or_fallback_secret(src: str, lib: str) -> None:
    blob = src + lib
    assert "define( 'BFL_AFFILIATE_RUNTIME_SECRET'" not in blob
    assert "define('BFL_AFFILIATE_RUNTIME_SECRET'" not in blob
    assert "constant( 'BFL_AFFILIATE_RUNTIME_SECRET' )" in src
    assert not re.search(r"['\"][0-9a-fA-F]{32,}['\"]", blob)
    assert not re.search(r"SECRET\s*=\s*['\"][^'\"]{8,}['\"]", blob)


def test_secret_missing_fails_closed(src: str) -> None:
    assert "if ( ! defined( 'BFL_AFFILIATE_RUNTIME_SECRET' ) )" in src
    assert src.count("'secret_not_configured'") >= 2  # both machine endpoints


def test_constant_time_comparisons_only(lib: str) -> None:
    assert "hash_equals(" in lib
    assert "hash_hmac(" in lib and "'sha256'" in lib


# ---- canonical JSON (D-C0.1 correction) ---------------------------
def test_canonical_json_flags_match_python(lib: str) -> None:
    m = re.search(r"return json_encode\(\s*self::normalize\( \$value \),\s*([^;]+?)\s*\);", lib)
    assert m, "BFL_Canonical::encode json_encode call not found"
    flags = m.group(1)
    assert "JSON_UNESCAPED_SLASHES" in flags
    assert "JSON_UNESCAPED_UNICODE" in flags
    # D-C0.1: Python with ensure_ascii=False emits U+2028/U+2029 RAW -> PHP must
    # ALSO pass JSON_UNESCAPED_LINE_TERMINATORS (D-C0 had this inverted).
    assert "JSON_UNESCAPED_LINE_TERMINATORS" in flags
    assert "ksort( $assoc, SORT_STRING )" in lib
    assert "hash( 'sha256', self::encode(" in lib
    # stdClass branch so `{}` never collapses to `[]`
    assert "instanceof stdClass" in lib


# ---- schema installer hardening (D-C0.1) -------------------------
def test_schema_install_not_triggered_by_anonymous_rest_or_go(src: str) -> None:
    # NOT hooked to rest_api_init anymore
    assert "add_action( 'rest_api_init', array( 'BFL_Schema'" not in src
    assert "add_action( 'rest_api_init', 'BFL_Schema" not in src
    # admin path gated on manage_options
    assert "current_user_can( 'manage_options' )" in src
    assert "add_action( 'admin_init', array( 'BFL_Schema', 'maybe_install_for_admin' ) )" in src
    # /go handler never installs (slice just the function body)
    go = src[
        src.index("function bfl_affiliate_maybe_handle_go") : src.index(
            "/* Schema: admin-only"
        )
    ]
    assert "BFL_Schema" not in go
    assert "dbDelta" not in go
    # projection endpoint calls ensure_current ONLY after HMAC verify
    ep = src[src.index("function bfl_affiliate_projection_endpoint") :]
    verify_at = ep.index("BFL_Hmac::verify(")
    ensure_at = ep.index("BFL_Schema::ensure_current();")
    assert verify_at < ensure_at
    assert "if ( ! $ok ) {\n\t\treturn bfl_affiliate_error( 401" in ep[:ensure_at]


# ---- timestamp storage + validation (D-C0.1) --------------------
def test_timestamp_columns_are_varchar_not_datetime(src: str) -> None:
    tdef = src[src.index("CREATE TABLE {$targets}") : src.index(") {$charset};")]
    assert "activated_at VARCHAR(32) NOT NULL" in tdef
    assert "disabled_at VARCHAR(32) NULL" in tdef
    assert "activated_at DATETIME" not in tdef
    assert "disabled_at DATETIME" not in tdef


def test_timestamp_validation_requires_canonical_utc(lib: str) -> None:
    assert "function bfl_affiliate_is_canonical_utc(" in lib
    assert r"\+00:00\z" in lib
    assert "checkdate(" in lib
    # entry validator uses it, not a mere non-empty check
    ve = lib[lib.index("function bfl_affiliate_validate_entry(") :]
    assert "bfl_affiliate_is_canonical_utc( $e['activated_at'] ?? null )" in ve
    assert "'bad_activated_at'" in ve and "'bad_disabled_at'" in ve


# ---- no mbstring dependency (D-C0.1) --------------------------
def test_no_mbstring_dependency(lib: str) -> None:
    assert "mb_check_encoding" not in lib
    assert "mb_strlen" not in lib and "mb_substr" not in lib
    # extension-free ASCII host check
    assert r"preg_match( '/\A[\x21-\x7e]+\z/', $host )" in lib


# ---- destination validator ---------------------------------
def test_request_time_destination_validation(lib: str) -> None:
    for reason in (
        "'scheme_not_https'",
        "'userinfo'",
        "'bad_port'",
        "'self_host'",
        "'host_mismatch'",
        "'whitespace_or_control'",
    ):
        assert reason in lib
    assert "$norm !== strtolower( $expected_host )" in lib
    assert r"\x{2028}|\x{2029}" in lib  # line/paragraph separators rejected in URLs
    for banned in ("str_ends_with(", "fnmatch(", "LIKE '%", "substr_count("):
        assert banned not in lib


# ---- decide table --------------------------------------------
def test_decide_ports_all_projection_outcomes(lib: str) -> None:
    for outcome in (
        "'insert'",
        "'noop'",
        "'update'",
        "'conflict_hash_mismatch'",
        "'conflict_stale_version'",
        "'forbidden_reactivation'",
        "'conflict_immutable_identity_drift'",
    ):
        assert outcome in lib


# ---- /go route -------------------------------------------------
def test_go_route_status_codes_and_no_override(src: str) -> None:
    assert "bfl_affiliate_go_finish( 404 )" in src
    assert "bfl_affiliate_go_finish( 410 )" in src
    assert "status_header( 302 )" in src
    assert "header( 'Location: ' . $row['destination_url'], true, 302 )" in src
    assert "$_GET['url']" not in src
    assert "$_REQUEST" not in src
    assert "wp_redirect(" not in src and "wp_safe_redirect(" not in src


def test_go_defensive_headers(src: str) -> None:
    assert src.count("Cache-Control: no-store") >= 2
    assert src.count("X-Robots-Tag: noindex, nofollow") >= 2
    assert "Referrer-Policy" not in src or "deliberately NOT set" in src


def test_click_persistence_fail_open_after_safe_resolution(src: str) -> None:
    resolve = src.index("Safe resolution complete")
    append = src.index("BFL_Runtime_Repo::append_click(")
    redirect = src.index("header( 'Location: '")
    assert resolve < append < redirect
    assert "catch ( Throwable $e )" in src
    assert "click persistence failed (generic)" in src


def test_click_table_stores_no_pii(src: str) -> None:
    start = src.index("CREATE TABLE {$clicks}")
    schema = src[start : src.index(") {$charset};", start)].lower()
    for col in (
        "ip",
        "user_agent",
        "referer",
        "referrer",
        "cookie",
        "session",
        "user_id",
        "email",
        "device",
        "query",
    ):
        assert col not in schema, col
    for col in ("id", "affiliate_target_id", "token", "clicked_at"):
        assert col in schema


def test_clicked_at_is_server_generated(src: str) -> None:
    assert "gmdate( 'Y-m-d H:i:s' )" in src
    assert "get_param( 'clicked_at' )" not in src


def test_click_export_readonly_ordered_bounded_cursor_signed(src: str) -> None:
    assert "ORDER BY id ASC" in src
    assert "BFL_AFFILIATE_CLICK_EXPORT_MAX" in src and "1000" in src
    assert "WHERE id > %d" in src
    assert "'?limit=' . $limit . '&since_id=' . $since_id" in src
    for bad in ("DELETE FROM", "$wpdb->delete(", "acknowledge"):
        assert bad not in src
    assert "SELECT id, token, clicked_at FROM" in src


def test_prepared_sql_and_no_dangerous_constructs(src: str, lib: str) -> None:
    blob = src + lib
    assert "$wpdb->prepare(" in src
    assert not re.search(r'\$wpdb->query\(\s*"[^"]*\$_(GET|POST|REQUEST|SERVER)', blob)
    for bad in ("eval(", "unserialize(", "create_function(", "extract("):
        assert bad not in blob, bad
    assert not re.search(r"(include|require)(_once)?\s*\(?\s*\$_(GET|POST|REQUEST)", blob)


def test_machine_endpoints_do_not_use_browser_nonce(src: str) -> None:
    for bad in ("wp_verify_nonce", "check_ajax_referer", "check_admin_referer"):
        assert bad not in src


def test_no_seed_target(src: str, lib: str) -> None:
    assert "INSERT INTO {$targets}" not in src
    assert "bfl_affiliate_seed" not in (src + lib)
    assert src.count("BFL_Runtime_Repo::insert_projection(") == 1
    assert not re.search(r"['\"]https?://(?!www\.w3|schemas\.)[^'\"\s]+\.[a-z]{2,}/", src + lib)


def test_readme_records_php_execution_status() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "run.php" in readme
    assert "PHP 8.2" in readme or "php -l" in readme
