"""Threads 連携の健全性アラート (T4、pure)。

pin する契約:

- **成績ではアラートを 1 件も出さない。** view 0 も、いいね 0 も障害ではない。
- 資格情報が使えないときは即座に警告する (待っても直らないため)。
- 一時的な失敗は 1 回では警告しない。続いたときだけ回数を事実として出す。
- 承認された文面と違うものが公開されていたら、必ず警告する。
- 証拠に token を入れない。
"""

from __future__ import annotations

from app.operations.monitoring import AUTOMATION_HEALTH, IMPORT_FAILURE
from app.operations.policy import SEVERITY_ERROR, SEVERITY_WARNING
from app.operations.threads_health import ThreadsHealthInput, build_threads_alert_drafts


def test_a_healthy_publication_produces_no_alert() -> None:
    drafts = build_threads_alert_drafts([ThreadsHealthInput(publication_id=1)])
    assert drafts == []


def test_zero_engagement_is_never_an_alert() -> None:
    """指標の値はそもそも入力に無い。**構造的に成績アラートを作れない。**"""

    assert "views" not in ThreadsHealthInput.__dataclass_fields__
    assert "likes" not in ThreadsHealthInput.__dataclass_fields__


def test_auth_failure_alerts_immediately() -> None:
    drafts = build_threads_alert_drafts(
        [
            ThreadsHealthInput(
                publication_id=1,
                failure_category="threads_auth",
                failure_reason="token rejected",
                consecutive_failures=1,
            )
        ]
    )
    assert len(drafts) == 1
    assert drafts[0].alert_type == IMPORT_FAILURE
    assert drafts[0].severity == SEVERITY_ERROR


def test_a_single_transient_failure_is_not_an_alert() -> None:
    drafts = build_threads_alert_drafts(
        [
            ThreadsHealthInput(
                publication_id=1, failure_category="threads_rate_limit", consecutive_failures=1
            )
        ]
    )
    assert drafts == []


def test_repeated_transient_failures_alert_once_they_persist() -> None:
    drafts = build_threads_alert_drafts(
        [
            ThreadsHealthInput(
                publication_id=1, failure_category="threads_server", consecutive_failures=3
            )
        ]
    )
    assert len(drafts) == 1
    assert drafts[0].severity == SEVERITY_WARNING
    assert drafts[0].evidence["consecutive_failures"] == 3


def test_unreadable_media_alerts() -> None:
    drafts = build_threads_alert_drafts(
        [ThreadsHealthInput(publication_id=7, media_readable=False)]
    )
    assert [d.alert_type for d in drafts] == [AUTOMATION_HEALTH]


def test_text_drift_alerts_at_error_severity() -> None:
    """人が承認した文面と違うものが出ているなら、承認の意味が失われている。"""

    drafts = build_threads_alert_drafts(
        [ThreadsHealthInput(publication_id=7, text_matches_approved=False)]
    )
    assert len(drafts) == 1
    assert drafts[0].severity == SEVERITY_ERROR


def test_the_same_problem_keeps_one_fingerprint() -> None:
    first = build_threads_alert_drafts(
        [ThreadsHealthInput(publication_id=1, failure_category="threads_auth")]
    )
    second = build_threads_alert_drafts(
        [ThreadsHealthInput(publication_id=2, failure_category="threads_auth")]
    )
    assert first[0].fingerprint == second[0].fingerprint


def test_no_token_appears_in_evidence() -> None:
    drafts = build_threads_alert_drafts(
        [
            ThreadsHealthInput(
                publication_id=1,
                failure_category="threads_auth",
                failure_reason="access_token=[redacted] rejected",
            )
        ]
    )
    rendered = str(drafts[0].evidence) + drafts[0].summary + drafts[0].title
    assert "THAAA" not in rendered
    assert "[redacted]" in drafts[0].evidence["reason"]


# -- diagnostic classification (2026-09-25) ------------------------------------
def test_a_permission_failure_is_not_called_unreadable_media() -> None:
    """権限の喪失は「削除・非公開」ではない。即時の error として出す (弱めない)。"""

    drafts = build_threads_alert_drafts(
        [
            ThreadsHealthInput(
                publication_id=1,
                failure_category="threads_permission",
                failure_reason="/18075152792458838/insights failed: API access blocked.",
                consecutive_failures=1,
                media_readable=True,
            )
        ]
    )
    assert len(drafts) == 1
    assert drafts[0].alert_type == IMPORT_FAILURE
    assert drafts[0].severity == SEVERITY_ERROR
    assert "threads_media_unreadable" not in drafts[0].fingerprint
    assert "API access blocked." in drafts[0].evidence["reason"]


def test_an_unexpected_response_is_reported_without_guessing_the_cause() -> None:
    drafts = build_threads_alert_drafts(
        [
            ThreadsHealthInput(
                publication_id=1,
                failure_category="threads_response",
                failure_reason="the response was not JSON",
                consecutive_failures=1,
            )
        ]
    )
    assert len(drafts) == 1
    assert drafts[0].severity == SEVERITY_WARNING  # 1 回目から出す (弱めない)
    assert drafts[0].title == "Threads API が想定外の応答を返した"
    assert "削除" not in drafts[0].summary
    assert drafts[0].evidence["category"] == "threads_response"
    assert drafts[0].evidence["reason"] == "the response was not JSON"


def test_only_a_404_means_the_post_is_unreadable() -> None:
    drafts = build_threads_alert_drafts(
        [
            ThreadsHealthInput(
                publication_id=1,
                failure_category="threads_not_found",
                failure_reason="/18075152792458838/insights failed: not found",
                consecutive_failures=1,
                media_readable=False,
            )
        ]
    )
    assert [d.fingerprint for d in drafts] == ["threads_media_unreadable:1"]
    assert drafts[0].evidence["reason"].endswith("not found")


# -- T4.3: publication state ---------------------------------------------------
from app.operations.threads_health import (  # noqa: E402
    PublicationHealthInput,
    build_autopublish_preflight_draft,
    build_publication_alert_drafts,
)


def test_an_uncertain_publication_alerts_immediately_at_error() -> None:
    drafts = build_publication_alert_drafts(
        [
            PublicationHealthInput(
                publication_id=4, status="uncertain", trigger="automatic", minutes_in_state=0.5
            )
        ]
    )
    assert [d.fingerprint for d in drafts] == ["threads_publication_uncertain:4"]
    assert drafts[0].severity == SEVERITY_ERROR
    assert "再送しない" in drafts[0].summary
    assert drafts[0].evidence["trigger"] == "automatic"


def test_a_publish_in_progress_is_not_an_alert_until_it_is_stuck() -> None:
    running = PublicationHealthInput(publication_id=4, status="publishing", minutes_in_state=1)
    stuck = PublicationHealthInput(publication_id=4, status="publishing", minutes_in_state=12)
    assert build_publication_alert_drafts([running]) == []
    assert [d.fingerprint for d in build_publication_alert_drafts([stuck])] == [
        "threads_publication_uncertain:4"
    ]


def test_a_reconciliation_flag_alerts_at_error() -> None:
    drafts = build_publication_alert_drafts(
        [PublicationHealthInput(publication_id=4, status="published", reconciliation_required=True)]
    )
    assert [d.fingerprint for d in drafts] == ["threads_publication_reconcile:4"]
    assert drafts[0].severity == SEVERITY_ERROR


def test_a_healthy_published_post_raises_nothing() -> None:
    assert (
        build_publication_alert_drafts(
            [PublicationHealthInput(publication_id=1, status="published")]
        )
        == []
    )


def test_preflight_severity_follows_the_error_class() -> None:
    fatal = build_autopublish_preflight_draft("threads_permission", "API access blocked.")
    transient = build_autopublish_preflight_draft("threads_rate_limit", "slow down")
    assert fatal.severity == SEVERITY_ERROR
    assert transient.severity == SEVERITY_WARNING
    assert fatal.fingerprint == "threads_autopublish_preflight:threads_permission"
