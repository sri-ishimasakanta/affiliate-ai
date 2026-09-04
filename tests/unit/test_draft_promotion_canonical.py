"""app/article/draft_promotion_canonical.py の pure テスト。"""

from __future__ import annotations

from app.article.draft_promotion_canonical import (
    canonical_candidate,
    compute_candidate_content_hash,
    compute_text_hash,
)

_BODY = "## 見出し\n本文です。日本語テキスト。¥1,300／ユーザー／月。"
_META = "業務効率化ツールのおすすめを比較して選び方を解説します。本記事はPRを含みます。"


def _cch(**kw) -> str:
    base = dict(article_id=1, source_run_id=1, body_markdown=_BODY, meta_description=_META)
    base.update(kw)
    return compute_candidate_content_hash(**base)


def test_text_hash_is_sha256_of_exact_utf8() -> None:
    import hashlib

    assert compute_text_hash(_BODY) == hashlib.sha256(_BODY.encode("utf-8")).hexdigest()
    assert len(compute_text_hash(_BODY)) == 64


def test_same_candidate_same_hashes() -> None:
    assert _cch() == _cch()
    assert compute_text_hash(_BODY) == compute_text_hash(_BODY)


def test_one_char_body_change_changes_body_and_content_hash() -> None:
    h1_body = compute_text_hash(_BODY)
    h1_cch = _cch()
    changed = _BODY + "。"
    assert compute_text_hash(changed) != h1_body
    assert _cch(body_markdown=changed) != h1_cch


def test_one_char_meta_change_changes_meta_and_content_hash() -> None:
    h1_meta = compute_text_hash(_META)
    h1_cch = _cch()
    changed = _META + "。"
    assert compute_text_hash(changed) != h1_meta
    assert _cch(meta_description=changed) != h1_cch


def test_content_hash_ignores_ids_only_via_explicit_fields() -> None:
    # article_id / source_run_id ARE part of the content hash (identity binding)
    assert _cch(article_id=2) != _cch(article_id=1)
    assert _cch(source_run_id=99) != _cch(source_run_id=1)


def test_canonical_candidate_shape_is_minimal() -> None:
    c = canonical_candidate(
        article_id=1, source_run_id=1, body_markdown=_BODY, meta_description=_META
    )
    assert set(c) == {"article_id", "source_run_id", "body_markdown", "meta_description"}


def test_japanese_utf8_is_deterministic_across_calls() -> None:
    hs = {_cch() for _ in range(5)}
    assert len(hs) == 1
    assert all(len(h) == 64 for h in hs)


def test_whitespace_is_significant_not_normalized() -> None:
    assert compute_text_hash(_BODY + " ") != compute_text_hash(_BODY)
    assert compute_text_hash(_BODY + "\n") != compute_text_hash(_BODY)
    assert _cch(body_markdown=_BODY + "\n") != _cch()
