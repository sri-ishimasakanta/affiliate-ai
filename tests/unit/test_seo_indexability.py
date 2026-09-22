"""app.seo.indexability の単体テスト (C5.1)。

この module の要点は「4 つの別概念を混ぜない」こと。特に:

- Search Analytics に行が無いことは「未インデックス」を意味しない。
- URL Inspection が未取得 (``GSC_UNKNOWN``) は「未インデックス」ではない。
- 手動のインデックス登録リクエストは、技術的に健全で sitemap に載っていて、かつ
  Google がまだ認識していない URL に限る。
"""

from __future__ import annotations

from app.seo.indexability import (
    ACTION_INVESTIGATE,
    ACTION_MANUAL_INDEX_REQUEST,
    ACTION_NONE,
    ACTION_TECHNICAL_FIX,
    ACTION_WAIT,
    GSC_CRAWLED_NOT_INDEXED,
    GSC_DISCOVERED_NOT_INDEXED,
    GSC_EXCLUDED,
    GSC_INDEXED,
    GSC_NOT_KNOWN_TO_GOOGLE,
    GSC_UNKNOWN,
    LIVE_CANONICAL_MISMATCH,
    LIVE_HEALTHY,
    LIVE_HTTP_ERROR,
    LIVE_NOINDEX,
    LIVE_REDIRECTED,
    LIVE_ROBOTS_BLOCKED,
    LIVE_THIN_OR_EMPTY,
    LIVE_UNREACHABLE,
    SITEMAP_MISSING,
    SITEMAP_PRESENT,
    assess_google_index_state,
    assess_live,
    decide_action,
)

_URL = "https://bizfluxlab.com/rpa-tools/"


def _live(**over):
    kwargs = dict(
        expected_url=_URL,
        final_url=_URL,
        final_status=200,
        redirected=False,
        declares_noindex=False,
        canonical=_URL,
        robots_allowed=True,
        text_length=5000,
    )
    kwargs.update(over)
    return assess_live(**kwargs)


# ==================== live technical state ====================================
def test_healthy_page() -> None:
    assert _live().state == LIVE_HEALTHY


def test_trailing_slash_difference_is_not_a_redirect() -> None:
    assert _live(final_url=_URL.rstrip("/")).state == LIVE_HEALTHY


def test_noindex_wins_over_everything_else() -> None:
    assert _live(declares_noindex=True).state == LIVE_NOINDEX


def test_robots_block_is_reported_before_noindex() -> None:
    assert _live(robots_allowed=False, declares_noindex=True).state == LIVE_ROBOTS_BLOCKED


def test_http_error() -> None:
    assert _live(final_status=404).state == LIVE_HTTP_ERROR


def test_unreachable() -> None:
    assert _live(final_status=None, error="ConnectError").state == LIVE_UNREACHABLE


def test_redirect_is_flagged() -> None:
    assert _live(redirected=True, final_url="https://bizfluxlab.com/other/").state == (
        LIVE_REDIRECTED
    )


def test_canonical_pointing_elsewhere_is_flagged() -> None:
    assert _live(canonical="https://bizfluxlab.com/other/").state == LIVE_CANONICAL_MISMATCH


def test_missing_canonical_is_only_a_note() -> None:
    result = _live(canonical=None)
    assert result.state == LIVE_HEALTHY
    assert any("canonical" in issue for issue in result.issues)


def test_thin_page_is_flagged() -> None:
    assert _live(text_length=10).state == LIVE_THIN_OR_EMPTY


# ==================== Google-reported index state =============================
def test_missing_inspection_is_unknown_not_unindexed() -> None:
    state, facts = assess_google_index_state(None)
    assert state == GSC_UNKNOWN
    assert facts == {}


def test_pass_verdict_is_indexed() -> None:
    state, _ = assess_google_index_state({"indexStatusResult": {"verdict": "PASS"}})
    assert state == GSC_INDEXED


def test_fail_verdict_is_excluded() -> None:
    state, _ = assess_google_index_state({"indexStatusResult": {"verdict": "FAIL"}})
    assert state == GSC_EXCLUDED


def test_neutral_with_crawl_history_is_crawled_not_indexed() -> None:
    state, facts = assess_google_index_state(
        {"indexStatusResult": {"verdict": "NEUTRAL", "lastCrawlTime": "2026-09-20T00:00:00Z"}}
    )
    assert state == GSC_CRAWLED_NOT_INDEXED
    assert facts["last_crawl_time"] == "2026-09-20T00:00:00Z"


def test_neutral_with_sitemap_evidence_is_discovered() -> None:
    state, _ = assess_google_index_state(
        {
            "indexStatusResult": {
                "verdict": "NEUTRAL",
                "sitemap": ["https://bizfluxlab.com/wp-sitemap.xml"],
            }
        }
    )
    assert state == GSC_DISCOVERED_NOT_INDEXED


def test_neutral_without_any_evidence_is_unknown_to_google() -> None:
    state, _ = assess_google_index_state({"indexStatusResult": {"verdict": "NEUTRAL"}})
    assert state == GSC_NOT_KNOWN_TO_GOOGLE


# ==================== action ==================================================
def _action(**over):
    kwargs = dict(
        live_state=LIVE_HEALTHY,
        sitemap_state=SITEMAP_PRESENT,
        google_index_state=GSC_NOT_KNOWN_TO_GOOGLE,
        days_since_publication=30,
    )
    kwargs.update(over)
    return decide_action(**kwargs)[0]


def test_indexed_article_needs_no_action() -> None:
    assert _action(google_index_state=GSC_INDEXED) == ACTION_NONE


def test_unknown_index_state_waits_rather_than_requesting() -> None:
    assert _action(google_index_state=GSC_UNKNOWN) == ACTION_WAIT


def test_crawled_not_indexed_waits() -> None:
    assert _action(google_index_state=GSC_CRAWLED_NOT_INDEXED) == ACTION_WAIT


def test_unknown_to_google_after_the_grace_period_is_a_manual_request_candidate() -> None:
    assert _action() == ACTION_MANUAL_INDEX_REQUEST


def test_just_published_article_waits_instead_of_requesting() -> None:
    """公開直後に未インデックスなのは正常 -- 一律に申請を勧めない。"""

    assert _action(days_since_publication=0) == ACTION_WAIT
    assert _action(days_since_publication=0, google_index_state=GSC_DISCOVERED_NOT_INDEXED) == (
        ACTION_WAIT
    )


def test_discovered_but_stale_is_a_manual_request_candidate() -> None:
    assert _action(google_index_state=GSC_DISCOVERED_NOT_INDEXED) == ACTION_MANUAL_INDEX_REQUEST


def test_grace_period_boundary_is_exclusive() -> None:
    assert _action(days_since_publication=2) == ACTION_WAIT
    assert _action(days_since_publication=3) == ACTION_MANUAL_INDEX_REQUEST


def test_noindex_article_is_a_technical_fix_not_a_request() -> None:
    assert _action(live_state=LIVE_NOINDEX) == ACTION_TECHNICAL_FIX


def test_robots_blocked_article_is_a_technical_fix_not_a_request() -> None:
    assert _action(live_state=LIVE_ROBOTS_BLOCKED) == ACTION_TECHNICAL_FIX


def test_http_error_is_a_technical_fix_not_a_request() -> None:
    assert _action(live_state=LIVE_HTTP_ERROR) == ACTION_TECHNICAL_FIX


def test_redirect_is_investigated_not_requested() -> None:
    assert _action(live_state=LIVE_REDIRECTED) == ACTION_INVESTIGATE


def test_canonical_mismatch_is_investigated_not_requested() -> None:
    assert _action(live_state=LIVE_CANONICAL_MISMATCH) == ACTION_INVESTIGATE


def test_healthy_page_absent_from_sitemap_is_a_technical_fix() -> None:
    assert _action(sitemap_state=SITEMAP_MISSING) == ACTION_TECHNICAL_FIX


def test_excluded_by_google_is_investigated() -> None:
    assert _action(google_index_state=GSC_EXCLUDED) == ACTION_INVESTIGATE


def test_live_defect_outranks_a_missing_sitemap_entry() -> None:
    action, reason = decide_action(
        live_state=LIVE_NOINDEX,
        sitemap_state=SITEMAP_MISSING,
        google_index_state=GSC_NOT_KNOWN_TO_GOOGLE,
        days_since_publication=30,
    )
    assert action == ACTION_TECHNICAL_FIX
    assert LIVE_NOINDEX in reason
