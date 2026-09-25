"""Threads 連携の健全性アラート (T4、pure)。

**成績はアラートにしない。** view が伸びないことも、いいねが 0 であることも、
運用の障害ではない。ここで警告にするのは「計測そのものが壊れている」ときだけ
である:

- 資格情報が使えない (auth / permission)
- 取り込みが繰り返し失敗している
- 公開済み投稿が読めなくなった (削除・非公開・ID 不整合)
- 公開されている文字列が、承認された文字列と一致しなくなった

閾値ではなく **状態** で判定するので、母数が少ない時期でも誤警報を出さない。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.operations.monitoring import AUTOMATION_HEALTH, IMPORT_FAILURE, AlertDraft
from app.operations.policy import SEVERITY_ERROR, SEVERITY_WARNING

#: 「設定か資格情報を直すまで、何度試しても通らない」種類の失敗。
FATAL_CATEGORIES = ("threads_auth", "threads_permission", "threads_not_configured")
#: 時間をおけば直りうる失敗。1 回では警告にしない。
TRANSIENT_CATEGORIES = ("threads_rate_limit", "threads_server", "threads_timeout")
#: 対象が見つからない (HTTP 404)。**これだけが**「投稿が読めない」を意味する。
NOT_FOUND_CATEGORY = "threads_not_found"
#: 想定外の応答 (分類できない 4xx・JSON でない・形が違う・通信の失敗)。
#: 原因が分からないので「削除・非公開」とは言わず、そのまま人に見せる。
UNEXPECTED_CATEGORY = "threads_response"


@dataclass(frozen=True)
class ThreadsHealthInput:
    """アラート判定に必要な事実だけ。指標の **値は含めない**。"""

    publication_id: int
    failure_category: str | None = None
    failure_reason: str | None = None
    consecutive_failures: int = 0
    media_readable: bool = True
    text_matches_approved: bool = True


def build_threads_alert_drafts(
    inputs: list[ThreadsHealthInput], *, repeated_failure_threshold: int = 3
) -> list[AlertDraft]:
    """計測の故障だけを警告に変える。**成績は 1 件も見ていない。**"""

    drafts: list[AlertDraft] = []
    for item in inputs:
        category = item.failure_category
        if category in FATAL_CATEGORIES:
            drafts.append(
                AlertDraft(
                    alert_type=IMPORT_FAILURE,
                    severity=SEVERITY_ERROR,
                    source="threads_insights",
                    title=f"Threads の指標を取得できない ({category})",
                    summary=(
                        "資格情報か権限の問題で Threads の指標が読めない。"
                        "直すまで再試行しても通らない。"
                    ),
                    fingerprint=f"threads_insights:{category}",
                    evidence={
                        "publication_id": item.publication_id,
                        "category": category,
                        # reason は ThreadsError 側で redact 済み。token は入らない。
                        "reason": item.failure_reason,
                    },
                )
            )
            continue
        if category in TRANSIENT_CATEGORIES and item.consecutive_failures >= (
            repeated_failure_threshold
        ):
            drafts.append(
                AlertDraft(
                    alert_type=IMPORT_FAILURE,
                    severity=SEVERITY_WARNING,
                    source="threads_insights",
                    title=f"Threads の指標取得が {item.consecutive_failures} 回続けて失敗",
                    summary="一時的な失敗が続いている。回数だけを事実として記録する。",
                    fingerprint=f"threads_insights:repeated:{item.publication_id}",
                    evidence={
                        "publication_id": item.publication_id,
                        "category": category,
                        "consecutive_failures": item.consecutive_failures,
                    },
                )
            )
            continue
        if category == UNEXPECTED_CATEGORY:
            # 旧実装はこれを「投稿が読めない (削除・非公開・ID の不整合)」と呼んでいた。
            # 実際には権限や token の失敗も混ざっていたので、原因を断定しない。
            # 警告の強さは変えない (1 回目から出す)。
            drafts.append(
                AlertDraft(
                    alert_type=AUTOMATION_HEALTH,
                    severity=SEVERITY_WARNING,
                    source="threads_insights",
                    title="Threads API が想定外の応答を返した",
                    summary=(
                        "分類できない応答で指標を取得できなかった。原因は断定しない。"
                        "reason を見て人が確認する。"
                    ),
                    fingerprint=f"threads_insights:unexpected:{item.publication_id}",
                    evidence={
                        "publication_id": item.publication_id,
                        "category": category,
                        "reason": item.failure_reason,
                        "consecutive_failures": item.consecutive_failures,
                    },
                )
            )
        if not item.media_readable:
            drafts.append(
                AlertDraft(
                    alert_type=AUTOMATION_HEALTH,
                    severity=SEVERITY_WARNING,
                    source="threads_insights",
                    title="公開済み Threads 投稿が読めない",
                    summary=(
                        "API が 404 を返した。削除・非公開・ID の不整合のいずれか。人が確認する。"
                    ),
                    fingerprint=f"threads_media_unreadable:{item.publication_id}",
                    evidence={
                        "publication_id": item.publication_id,
                        "category": category,
                        "reason": item.failure_reason,
                    },
                )
            )
        if not item.text_matches_approved:
            drafts.append(
                AlertDraft(
                    alert_type=AUTOMATION_HEALTH,
                    severity=SEVERITY_ERROR,
                    source="threads_insights",
                    title="公開中の文面が、承認された文面と一致しない",
                    summary=(
                        "人が承認したのとは違う文字列が公開されている。"
                        "承認の意味が失われるため、内容を確認する。"
                    ),
                    fingerprint=f"threads_text_drift:{item.publication_id}",
                    evidence={"publication_id": item.publication_id},
                )
            )
    return drafts


# == T4.3: publication state ===================================================
#: 公開の途中 (作成中・公開中) がこの時間を超えて続いていれば、止まったとみなす。
#: 正常な公開は 30 秒の待ちを含めて 1 分ほどで終わる。
STUCK_IN_FLIGHT_MINUTES = 10


@dataclass(frozen=True)
class PublicationHealthInput:
    """公開の状態についての事実だけ。本文も token も含めない。"""

    publication_id: int
    status: str
    reconciliation_required: bool = False
    trigger: str = "manual"
    minutes_in_state: float | None = None
    error_category: str | None = None


def build_publication_alert_drafts(inputs: list[PublicationHealthInput]) -> list[AlertDraft]:
    """公開が不確定・止まった・照合待ち のときだけ警告にする (T4.3)。

    これらは queue 全体を止める状態なので、黙って止まったままにしない。
    """

    drafts: list[AlertDraft] = []
    for item in inputs:
        evidence = {
            "publication_id": item.publication_id,
            "status": item.status,
            "trigger": item.trigger,
            "error_category": item.error_category,
        }
        stuck = (
            item.status in ("creating", "container_created", "publishing")
            and item.minutes_in_state is not None
            and item.minutes_in_state >= STUCK_IN_FLIGHT_MINUTES
        )
        if item.status == "uncertain" or stuck:
            drafts.append(
                AlertDraft(
                    alert_type=AUTOMATION_HEALTH,
                    severity=SEVERITY_ERROR,
                    source="threads_publication",
                    title="Threads の公開が出たかどうか分からない",
                    summary=(
                        "公開の応答を取りこぼした (または途中で止まった)。次の公開はすべて止まって"
                        "いる。publish_threads_post.py --reconcile で確かめる。**再送しない。**"
                    ),
                    fingerprint=f"threads_publication_uncertain:{item.publication_id}",
                    evidence=evidence,
                )
            )
        if item.reconciliation_required:
            drafts.append(
                AlertDraft(
                    alert_type=AUTOMATION_HEALTH,
                    severity=SEVERITY_ERROR,
                    source="threads_publication",
                    title="公開した内容の照合が必要",
                    summary=(
                        "読み戻した内容が送った内容と一致しない。次の公開はすべて止まっている。"
                        "内容を確かめてから照合する。"
                    ),
                    fingerprint=f"threads_publication_reconcile:{item.publication_id}",
                    evidence=evidence,
                )
            )
    return drafts


def build_autopublish_preflight_draft(category: str | None, reason: str | None) -> AlertDraft:
    """自動公開の事前確認 (読むだけ) が失敗した。コンテナは作っていない (T4.3)。"""

    return AlertDraft(
        alert_type=IMPORT_FAILURE,
        severity=SEVERITY_ERROR if category in FATAL_CATEGORIES else SEVERITY_WARNING,
        source="threads_autopublish",
        title=f"自動公開の事前確認に失敗 ({category or 'unknown'})",
        summary="読み取りの事前確認が通らなかったので、コンテナを作らずに止めた。",
        fingerprint=f"threads_autopublish_preflight:{category or 'unknown'}",
        evidence={"category": category, "reason": reason},
    )


__all__ = [
    "STUCK_IN_FLIGHT_MINUTES",
    "PublicationHealthInput",
    "build_autopublish_preflight_draft",
    "build_publication_alert_drafts",
    "FATAL_CATEGORIES",
    "NOT_FOUND_CATEGORY",
    "UNEXPECTED_CATEGORY",
    "TRANSIENT_CATEGORIES",
    "ThreadsHealthInput",
    "build_threads_alert_drafts",
]
