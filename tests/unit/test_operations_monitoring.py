"""app.operations.monitoring / notifications の単体テスト (C8)。

pin する要点 (どれも「意味のない通知を出さない」ための防具):

- **まだ一度も届いていない** データを「壊れた」と言わない。
- provider の既知の遅延の範囲内では通知しない。
- 低優先度の構造的候補を毎日通知しない。
- 消えた候補を「直った」と呼ばない。
- 通知 payload から secret を必ず落とす。
"""

from __future__ import annotations

from datetime import date

from app.operations.monitoring import (
    AUTOMATION_HEALTH,
    CANDIDATE_CHANGE,
    CHANGE_NEW,
    CHANGE_NO_LONGER_PRESENT,
    CHANGE_PERSISTED,
    CHANGE_PRIORITY_DECREASED,
    CHANGE_PRIORITY_INCREASED,
    DATA_STALE,
    IMPORT_FAILURE,
    INDEXABILITY_REGRESSION,
    MONETIZATION_STRUCTURE_REGRESSION,
    compare_candidates,
    evaluate_article_health,
    evaluate_automation_health,
    evaluate_candidate_changes,
    evaluate_data_staleness,
    evaluate_import_failures,
    evaluate_monetization_regression,
    fingerprint,
)
from app.operations.notifications import (
    LogNotifier,
    NotificationMessage,
    WebhookNotifier,
    build_notifiers,
    sanitize_payload,
)
from app.operations.policy import load_policy

_POLICY = load_policy()
_TODAY = date(2026, 9, 23)


# ==================== import failure ==========================================
def test_failed_step_produces_an_alert() -> None:
    drafts = evaluate_import_failures(
        step_results=[{"step_name": "import_ga4", "status": "failed", "error_category": "X"}],
        policy=_POLICY,
    )
    assert len(drafts) == 1
    assert drafts[0].alert_type == IMPORT_FAILURE
    assert drafts[0].severity == "error"


def test_successful_steps_produce_no_alert() -> None:
    assert (
        evaluate_import_failures(
            step_results=[{"step_name": "import_ga4", "status": "succeeded"}], policy=_POLICY
        )
        == []
    )


# ==================== data staleness ==========================================
def test_ga4_that_never_had_data_does_not_alert() -> None:
    """GA4 は 2026-09-22 に計測開始したばかり -- これは故障ではない。"""

    assert (
        evaluate_data_staleness(
            source="ga4",
            data_through=None,
            ever_had_data=False,
            today=_TODAY,
            policy=_POLICY,
        )
        is None
    )


def test_ga4_that_previously_had_data_and_stopped_does_alert() -> None:
    draft = evaluate_data_staleness(
        source="ga4",
        data_through=date(2026, 9, 1),
        ever_had_data=True,
        today=_TODAY,
        policy=_POLICY,
    )
    assert draft is not None
    assert draft.alert_type == DATA_STALE
    assert draft.evidence["ever_had_data"] is True


def test_search_console_within_its_reporting_lag_does_not_alert() -> None:
    draft = evaluate_data_staleness(
        source="search_console",
        data_through=_TODAY - __import__("datetime").timedelta(days=3),
        ever_had_data=True,
        today=_TODAY,
        policy=_POLICY,
    )
    assert draft is None


def test_source_without_a_staleness_gate_never_alerts() -> None:
    assert (
        evaluate_data_staleness(
            source="affiliate_clicks",
            data_through=date(2020, 1, 1),
            ever_had_data=True,
            today=_TODAY,
            policy=_POLICY,
        )
        is None
    )


# ==================== article health ==========================================
def test_healthy_articles_produce_no_alert() -> None:
    rows = [{"article_id": 1, "live_state": "LIVE_HEALTHY"}]
    assert evaluate_article_health(rows=rows, policy=_POLICY) == []


def test_http_error_is_an_article_http_alert() -> None:
    rows = [{"article_id": 1, "url": "https://x.test/a/", "live_state": "LIVE_HTTP_ERROR"}]
    draft = evaluate_article_health(rows=rows, policy=_POLICY)[0]
    assert draft.alert_type == "ARTICLE_HTTP_HEALTH"
    assert draft.severity == "error"


def test_noindex_is_an_indexability_regression() -> None:
    rows = [{"article_id": 1, "url": "https://x.test/a/", "live_state": "LIVE_NOINDEX"}]
    assert evaluate_article_health(rows=rows, policy=_POLICY)[0].alert_type == (
        INDEXABILITY_REGRESSION
    )


def test_canonical_mismatch_is_an_indexability_regression() -> None:
    rows = [{"article_id": 1, "url": "https://x.test/a/", "live_state": "LIVE_CANONICAL_MISMATCH"}]
    assert evaluate_article_health(rows=rows, policy=_POLICY)[0].alert_type == (
        INDEXABILITY_REGRESSION
    )


# ==================== monetization ============================================
def test_losing_an_active_target_is_a_regression() -> None:
    drafts = evaluate_monetization_regression(
        previous={10: {"active_target_count": 1, "active_mapping_count": 1}},
        current={10: {"active_target_count": 0, "active_mapping_count": 1}},
        policy=_POLICY,
    )
    assert drafts[0].alert_type == MONETIZATION_STRUCTURE_REGRESSION
    assert drafts[0].article_id == 10


def test_losing_a_mapping_is_a_regression() -> None:
    drafts = evaluate_monetization_regression(
        previous={11: {"active_target_count": 1, "active_mapping_count": 1}},
        current={11: {"active_target_count": 1, "active_mapping_count": 0}},
        policy=_POLICY,
    )
    assert len(drafts) == 1


def test_unchanged_healthy_monetization_produces_no_alert() -> None:
    state = {10: {"active_target_count": 1, "active_mapping_count": 1}}
    assert evaluate_monetization_regression(previous=state, current=state, policy=_POLICY) == []


def test_gaining_monetization_is_not_a_regression() -> None:
    drafts = evaluate_monetization_regression(
        previous={10: {"active_target_count": 0, "active_mapping_count": 0}},
        current={10: {"active_target_count": 1, "active_mapping_count": 1}},
        policy=_POLICY,
    )
    assert drafts == []


# ==================== candidate comparison ====================================
def _candidate(key: str, priority: str = "medium", candidate_type: str = "X") -> dict:
    return {
        "dedupe_key": key,
        "candidate_type": candidate_type,
        "priority": priority,
        "article_id": 1,
    }


def test_new_candidate_is_classified_as_new() -> None:
    changes = compare_candidates(engine="seo", previous=[], current=[_candidate("a")])
    assert changes[0].change == CHANGE_NEW


def test_unchanged_candidate_is_persisted() -> None:
    changes = compare_candidates(
        engine="seo", previous=[_candidate("a")], current=[_candidate("a")]
    )
    assert changes[0].change == CHANGE_PERSISTED


def test_disappeared_candidate_is_not_called_fixed() -> None:
    """消えた = 直った ではない。データや期間の変化でも消える。"""

    changes = compare_candidates(engine="seo", previous=[_candidate("a")], current=[])
    assert changes[0].change == CHANGE_NO_LONGER_PRESENT


def test_priority_increase_and_decrease_are_detected() -> None:
    up = compare_candidates(
        engine="seo", previous=[_candidate("a", "low")], current=[_candidate("a", "high")]
    )
    down = compare_candidates(
        engine="seo", previous=[_candidate("a", "high")], current=[_candidate("a", "low")]
    )
    assert up[0].change == CHANGE_PRIORITY_INCREASED
    assert down[0].change == CHANGE_PRIORITY_DECREASED


def test_low_priority_new_candidate_does_not_notify() -> None:
    changes = compare_candidates(engine="seo", previous=[], current=[_candidate("a", "low")])
    assert evaluate_candidate_changes(changes=changes, policy=_POLICY) == []


def test_medium_priority_new_candidate_notifies() -> None:
    changes = compare_candidates(engine="seo", previous=[], current=[_candidate("a", "medium")])
    drafts = evaluate_candidate_changes(changes=changes, policy=_POLICY)
    assert len(drafts) == 1
    assert drafts[0].alert_type == CANDIDATE_CHANGE


def test_persisted_candidate_does_not_notify_again() -> None:
    changes = compare_candidates(
        engine="seo", previous=[_candidate("a", "high")], current=[_candidate("a", "high")]
    )
    assert evaluate_candidate_changes(changes=changes, policy=_POLICY) == []


def test_priority_increase_notifies() -> None:
    changes = compare_candidates(
        engine="revenue", previous=[_candidate("a", "low")], current=[_candidate("a", "high")]
    )
    assert len(evaluate_candidate_changes(changes=changes, policy=_POLICY)) == 1


# ==================== automation health =======================================
def test_lock_conflict_is_reported() -> None:
    drafts = evaluate_automation_health(
        run_status="skipped",
        consecutive_failures=0,
        lock_conflict=True,
        blocking_owner_run_id=7,
        policy=_POLICY,
    )
    assert drafts[0].alert_type == AUTOMATION_HEALTH
    assert drafts[0].evidence["blocking_owner_run_id"] == 7


def test_repeated_failures_are_reported() -> None:
    drafts = evaluate_automation_health(
        run_status="failed",
        consecutive_failures=3,
        lock_conflict=False,
        blocking_owner_run_id=None,
        policy=_POLICY,
    )
    assert len(drafts) == 1


def test_a_single_failure_is_not_yet_escalated() -> None:
    assert (
        evaluate_automation_health(
            run_status="failed",
            consecutive_failures=1,
            lock_conflict=False,
            blocking_owner_run_id=None,
            policy=_POLICY,
        )
        == []
    )


# ==================== fingerprints ============================================
def test_fingerprint_is_deterministic_and_date_free() -> None:
    assert fingerprint(DATA_STALE, "ga4") == fingerprint(DATA_STALE, "ga4")
    assert fingerprint(DATA_STALE, "ga4") != fingerprint(DATA_STALE, "search_console")


# ==================== notifications ===========================================
def test_sanitize_drops_secret_like_keys() -> None:
    payload = sanitize_payload(
        {"token": "abc", "nested": {"api_key": "x", "safe": 1}, "list": [{"secret": "s"}]}
    )
    assert payload["token"] == "[redacted]"
    assert payload["nested"]["api_key"] == "[redacted]"
    assert payload["nested"]["safe"] == 1
    assert payload["list"][0]["secret"] == "[redacted]"


def test_sanitize_redacts_go_redirect_urls() -> None:
    payload = sanitize_payload({"summary": "see https://bizfluxlab.com/go/AbC123 for details"})
    assert "/go/AbC123" not in payload["summary"]
    assert "[redacted]" in payload["summary"]


def test_message_payload_is_sanitized() -> None:
    message = NotificationMessage(
        severity="error",
        title="t",
        summary="s",
        alert_type="X",
        fingerprint="f",
        evidence={"tracking_url": "https://track.test/x", "count": 2},
    )
    payload = message.as_payload()
    assert payload["evidence"]["tracking_url"] == "[redacted]"
    assert payload["evidence"]["count"] == 2


def test_log_notifier_always_delivers() -> None:
    lines: list[str] = []
    result = LogNotifier(writer=lines.append).send(
        NotificationMessage(
            severity="warning", title="t", summary="s", alert_type="X", fingerprint="f"
        )
    )
    assert result.delivered is True
    assert lines and "WARNING" in lines[0]


def test_log_notifier_failure_is_reported_not_raised() -> None:
    def boom(_line):
        raise RuntimeError("no console")

    result = LogNotifier(writer=boom).send(
        NotificationMessage(
            severity="warning", title="t", summary="s", alert_type="X", fingerprint="f"
        )
    )
    assert result.delivered is False
    assert result.detail == "RuntimeError"


def test_webhook_is_not_built_when_unconfigured() -> None:
    class _Settings:
        operations_webhook_url = None

    notifiers = build_notifiers(_Settings())
    assert [n.name for n in notifiers] == ["log"]


def test_webhook_is_built_only_for_https() -> None:
    class _Insecure:
        operations_webhook_url = "http://example.test/hook"

    class _Secure:
        operations_webhook_url = "https://example.test/hook"

    assert [n.name for n in build_notifiers(_Insecure())] == ["log"]
    assert [n.name for n in build_notifiers(_Secure())] == ["log", "webhook"]


def test_webhook_sends_a_sanitized_payload() -> None:
    import httpx

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.append(_json.loads(request.content))
        return httpx.Response(200)

    notifier = WebhookNotifier(
        "https://example.test/hook",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = notifier.send(
        NotificationMessage(
            severity="error",
            title="t",
            summary="s",
            alert_type="X",
            fingerprint="f",
            evidence={"webhook_url": "https://secret.test/hook"},
        )
    )
    assert result.delivered is True
    assert captured[0]["evidence"]["webhook_url"] == "[redacted]"


def test_webhook_failure_is_reported_without_the_body() -> None:
    import httpx

    def handler(request):
        return httpx.Response(500, text="https://secret.test/hook exploded")

    result = WebhookNotifier(
        "https://example.test/hook",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).send(
        NotificationMessage(
            severity="error", title="t", summary="s", alert_type="X", fingerprint="f"
        )
    )
    assert result.delivered is False
    assert "secret.test" not in (result.detail or "")
