"""Human 採用候補 (draft promotion candidate) の canonical 化と hash 計算 (pure)。

DB / SQLAlchemy / FastAPI 非依存。採用サービスは **受け取った exact 文字列** をそのまま
hash する — 正規化・整形・改行変換・Markdown 書き換えは一切しない (§6)。

* ``body_hash``  = 本文文字列そのものの SHA-256 hex。
* ``meta_hash``  = meta_description 文字列そのものの SHA-256 hex。
* ``candidate_content_hash`` = 意味的入力 (article_id + source_run_id + body + meta) の
  canonical JSON の SHA-256 hex。created_at / promoted_at / idempotency_key /
  validation_report / human_review_notes は **含めない**。
"""

from __future__ import annotations

import hashlib

from app.article.draft_input_canonical import canonical_json


def compute_text_hash(text: str) -> str:
    """文字列そのものの SHA-256 hex (64 文字)。UTF-8。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_candidate(
    *, article_id: int, source_run_id: int, body_markdown: str, meta_description: str
) -> dict:
    """candidate_content_hash 対象の意味的入力だけを取り出す。"""

    return {
        "article_id": article_id,
        "source_run_id": source_run_id,
        "body_markdown": body_markdown,
        "meta_description": meta_description,
    }


def compute_candidate_content_hash(
    *, article_id: int, source_run_id: int, body_markdown: str, meta_description: str
) -> str:
    """採用候補の意味的入力の SHA-256 hex (64 文字)。"""

    canonical = canonical_json(
        canonical_candidate(
            article_id=article_id,
            source_run_id=source_run_id,
            body_markdown=body_markdown,
            meta_description=meta_description,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
