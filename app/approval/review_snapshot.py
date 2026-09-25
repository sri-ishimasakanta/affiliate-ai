"""中継へ渡すレビュー用スナップショット (C8.8、pure)。

公開側に置くのは **人が読んで判断するための最小限** だけである。ローカル DB の
中身を公開側へ写すのではない。

- secret / 認証情報 / tracking URL / ``/go/`` token は載せない
  (:func:`~app.operations.notifications.sanitize_payload` を最終防壁として通す)。
- 本文全体は載せない。差分と前後の文脈だけを載せる。
- ここで作るのは **データ** であって HTML ではない。描画側が必ずエスケープする。

subject 固有の知識はこのモジュールの adapter に閉じ込める。中継側の token / 決定
の仕組みには記事固有の前提を持ち込まない (あとで ``threads_post`` を足せるように)。
"""

from __future__ import annotations

from app.models import SUBJECT_CHANGE_REQUEST, SUBJECT_THREADS_POST
from app.operations.notifications import sanitize_payload

#: 差分が長くなりすぎないように切る (携帯で読める量に収める)。
MAX_DIFF_LINES = 40
MAX_CONTEXT_CHARS = 240


class UnsupportedSubjectError(Exception):
    """まだ封筒に載せられない subject。"""

    def __init__(self, subject_type: str) -> None:
        super().__init__(f"subject type {subject_type!r} cannot be reviewed yet")
        self.subject_type = subject_type


def build_snapshot(*, subject_type: str, subject, article=None, target_article=None) -> dict:
    """subject に応じた sanitized スナップショットを作る。"""

    if subject_type == SUBJECT_CHANGE_REQUEST:
        snapshot = _change_request_snapshot(subject, article, target_article)
    elif subject_type == SUBJECT_THREADS_POST:
        snapshot = _threads_post_snapshot(subject, article)
    else:
        raise UnsupportedSubjectError(subject_type)
    return sanitize_payload(snapshot)


def _change_request_snapshot(request, article, target_article) -> dict:
    """C9 の変更要求 1 件を、携帯で判断できる形に縮約する。"""

    proposal = request.proposal_json or {}
    diff = (proposal.get("unified_diff") or "").split("\n")
    truncated = len(diff) > MAX_DIFF_LINES
    return {
        "subject_type": SUBJECT_CHANGE_REQUEST,
        "subject_id": request.id,
        "subject_hash": request.proposal_hash,
        "subject_hash_short": request.proposal_hash[:16],
        "subject_version": request.proposal_version,
        "status": request.status,
        "change_type": request.change_type,
        "article_id": request.article_id,
        "article_title": getattr(article, "title", None),
        "article_url": getattr(article, "published_url", None),
        "target_article_id": request.target_article_id,
        "target_article_title": getattr(target_article, "title", None),
        "target_url": proposal.get("target_url"),
        "candidate_type": request.source_candidate_type,
        "priority": request.source_candidate_priority,
        "rationale": request.rationale,
        "anchor_text": proposal.get("anchor_text"),
        "inserted_paragraph": proposal.get("inserted_paragraph"),
        "context_before": _clip(proposal.get("context_before")),
        "context_after": _clip(proposal.get("context_after")),
        "insertion_line": proposal.get("insertion_line"),
        "diff_lines": diff[:MAX_DIFF_LINES],
        "diff_truncated": truncated,
        "warnings": list(proposal.get("warnings") or []),
    }


def _threads_post_snapshot(proposal, article) -> dict:
    """Threads 投稿案 1 件を、携帯で判断できる形にする (T2)。

    **人が見るのは、実際に投稿されるそのままの文字列** である。要約や整形をした
    ものを見せて、別の文章を公開することがあってはならない。
    """

    return {
        "subject_type": SUBJECT_THREADS_POST,
        "subject_id": proposal.id,
        "subject_hash": proposal.proposal_hash,
        "subject_hash_short": proposal.proposal_hash[:16],
        # Threads 提案には版が無いので、生成規則の版を identity の補助に使う。
        "subject_version": 1,
        "status": proposal.status,
        "angle": proposal.angle,
        "article_id": proposal.source_article_id,
        "article_title": getattr(article, "title", None),
        "article_url": getattr(article, "published_url", None),
        "source_article_body_hash": proposal.source_article_body_hash,
        "source_article_body_hash_short": proposal.source_article_body_hash[:16],
        "policy_version": proposal.policy_version,
        "generator_version": proposal.generator_version,
        "link_mode": proposal.link_mode,
        "destination_url": proposal.destination_url,
        # 公開される文字列そのもの。切らない。
        "publish_text": proposal.content_text,
        "character_count": proposal.character_count,
        "warnings": list(proposal.warnings_json or []),
    }


def review_text_problems(*, subject_type: str, subject, snapshot: dict) -> list[str]:
    """人が判断する本文が、スナップショットにそのまま載っているか (T6.1、fail closed)。

    Threads なら ``publish_text`` が保存済みの ``content_text`` と完全に一致すること、
    C9 なら ``inserted_paragraph`` があること。空・欠落・不一致なら、依頼を出さない理由を返す。
    本文をここで作り直したり整形したりはしない。
    """

    if subject_type == SUBJECT_THREADS_POST:
        text = getattr(subject, "content_text", None)
        if not isinstance(text, str) or not text.strip():
            return ["the proposal has no text for the human to review"]
        if snapshot.get("publish_text") != text:
            return ["the review snapshot does not carry the exact proposal text"]
        return []
    if subject_type == SUBJECT_CHANGE_REQUEST:
        paragraph = snapshot.get("inserted_paragraph")
        if not isinstance(paragraph, str) or not paragraph.strip():
            return ["the change request has no inserted paragraph for the human to review"]
    return []


def _clip(value) -> str | None:
    if not value:
        return None
    text = str(value)
    return text if len(text) <= MAX_CONTEXT_CHARS else text[: MAX_CONTEXT_CHARS - 3] + "..."
