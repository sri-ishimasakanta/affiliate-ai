"""app/article/draft_prompt_canonical.py の pure テスト。"""

from app.article.draft_prompt_canonical import (
    compute_prompt_input_hash,
    compute_rendered_prompt_hash,
    semantic_package_for_hash,
)


def test_semantic_package_drops_audit_only() -> None:
    pkg = {"a": 1, "b": {"x": 2}, "audit": {"built_at": "T"}}
    assert semantic_package_for_hash(pkg) == {"a": 1, "b": {"x": 2}}


def test_prompt_hash_ignores_audit_and_key_order() -> None:
    a = {"v": "x", "n": [1, 2], "audit": {"built_at": "T1"}}
    b = {"n": [1, 2], "v": "x", "audit": {"built_at": "T2"}}
    assert compute_prompt_input_hash(a) == compute_prompt_input_hash(b)


def test_prompt_hash_changes_on_semantic_change() -> None:
    base = {"v": "x", "audit": {}}
    changed = {"v": "y", "audit": {}}
    assert compute_prompt_input_hash(base) != compute_prompt_input_hash(changed)


def test_prompt_hash_list_order_matters() -> None:
    a = {"v": [1, 2, 3], "audit": {}}
    b = {"v": [3, 2, 1], "audit": {}}
    assert compute_prompt_input_hash(a) != compute_prompt_input_hash(b)


def test_rendered_hash_is_exact_string_sha256() -> None:
    import hashlib

    text = "hello\nworld\n"
    assert (
        compute_rendered_prompt_hash(text)
        == hashlib.sha256(text.encode("utf-8")).hexdigest()
    )
    assert compute_rendered_prompt_hash(text) != compute_rendered_prompt_hash(
        text + " "
    )
