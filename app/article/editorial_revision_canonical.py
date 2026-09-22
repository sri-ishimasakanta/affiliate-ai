"""編集改訂 (editorial revision) の canonical 化と hash 計算 (pure)。

DB / SQLAlchemy / FastAPI 非依存。改訂サービスは **受け取った exact 文字列** をそのまま
hash する — 正規化・整形・改行変換・Markdown 書き換えは一切しない
(``draft_promotion_canonical`` と同じ方針)。

``revision_content_hash`` = 意味的入力 (article_id + body + meta) の canonical JSON の
SHA-256 hex。``source_run_id`` を含めないのが promotion との違いで、改訂は生成 run ではなく
**現在の canonical 本文** を起点にするため。revision_reason / editor_notes /
idempotency_key / validation_report / 時刻は含めない (同じ内容の再適用を no-op に
できるようにするため)。
"""

from __future__ import annotations

import hashlib

from app.article.draft_input_canonical import canonical_json


def canonical_revision(
    *, article_id: int, body_markdown: str, meta_description: str
) -> dict:
    """revision_content_hash 対象の意味的入力だけを取り出す。"""

    return {
        "article_id": article_id,
        "body_markdown": body_markdown,
        "meta_description": meta_description,
    }


def compute_revision_content_hash(
    *, article_id: int, body_markdown: str, meta_description: str
) -> str:
    """改訂内容の意味的入力の SHA-256 hex (64 文字)。"""

    canonical = canonical_json(
        canonical_revision(
            article_id=article_id,
            body_markdown=body_markdown,
            meta_description=meta_description,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
