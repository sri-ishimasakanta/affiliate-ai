"""app/wordpress/content_update_request.py の pure テスト (D-D5B)。

D-D5A/D-D5A.1 で確定した「2 つの独立した hash namespace」を pin する:

- ``request_content_hash``               -- 意図した (submit する) content の identity。
- ``*_wordpress_raw_content_hash``       -- WordPress が実際に保存している content
  の identity (この module では扱わない -- 別 namespace であることをここで固定する)。

D-D5A で実証済みの通り、この 2 つは同じ入力からでも一致しない。この module のどの
関数も両者を等値比較しない -- そのことをテストでも明示的に確認する。
"""

from __future__ import annotations

import hashlib
import json

from app.wordpress.content_update_request import (
    METHOD,
    build_content_update_payload_json,
    build_wordpress_content_update_request,
    compute_content_update_payload_hash,
    compute_content_update_request_identity_hash,
    compute_request_content_hash,
    compute_target_content_update_request_identity_hash,
    endpoint_path_for_post,
)

_BASE = dict(
    wordpress_post_id="25",
    article_publication_artifact_id=1,
    artifact_hash="a" * 64,
    tracked_html="<p>本文</p>\n<h2>見出し</h2>\n",
    target_base_url="https://bizfluxlab.com",
)


def _b(**over):
    return build_wordpress_content_update_request(**{**_BASE, **over})


# ==================== payload contract (§8/§9/§27) ===========================
def test_method_and_endpoint() -> None:
    r = _b()
    assert r.method == METHOD == "POST"
    assert r.endpoint_path == endpoint_path_for_post("25") == "/wp-json/wp/v2/posts/25"


def test_payload_keys_exactly_one_content_only() -> None:
    payload_json = build_content_update_payload_json(_BASE["tracked_html"])
    payload = json.loads(payload_json)
    assert list(payload.keys()) == ["content"]
    assert payload["content"] == _BASE["tracked_html"]


def test_payload_excludes_title_excerpt_slug_status_meta() -> None:
    payload_json = build_content_update_payload_json(_BASE["tracked_html"])
    payload = json.loads(payload_json)
    assert set(payload).isdisjoint(
        {"title", "excerpt", "slug", "status", "meta", "categories", "tags"}
    )


def test_payload_json_deterministic_and_canonical() -> None:
    a = build_content_update_payload_json(_BASE["tracked_html"])
    b = build_content_update_payload_json(_BASE["tracked_html"])
    assert a == b
    canon = json.dumps(
        {"content": _BASE["tracked_html"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert a == canon


def test_payload_hash_deterministic() -> None:
    payload_json = build_content_update_payload_json(_BASE["tracked_html"])
    h1 = compute_content_update_payload_hash(payload_json)
    h2 = compute_content_update_payload_hash(payload_json)
    assert h1 == h2
    assert h1 == hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    assert len(h1) == 64


def test_one_char_tracked_html_change_changes_payload_hash() -> None:
    assert (
        _b(tracked_html=_BASE["tracked_html"] + " ").update_payload_hash != _b().update_payload_hash
    )


# ==================== request_content_hash contract (§10) ====================
def test_request_content_hash_uses_established_text_hash_helper() -> None:
    from app.article.draft_promotion_canonical import compute_text_hash

    assert compute_request_content_hash(_BASE["tracked_html"]) == compute_text_hash(
        _BASE["tracked_html"]
    )


def test_request_content_hash_does_not_renormalize_html() -> None:
    # 空白/改行を変えれば hash も変わる -- 正規化していない証拠
    raw = "<p>a</p>"
    spaced = "<p>a</p>\n"
    assert compute_request_content_hash(raw) != compute_request_content_hash(spaced)


# ==================== request identity (§11) ==================================
def test_request_identity_deterministic() -> None:
    a, b = _b(), _b()
    assert a.content_update_request_identity_hash == b.content_update_request_identity_hash
    assert len(a.content_update_request_identity_hash) == 64


def test_request_identity_depends_on_each_binding_field() -> None:
    base = _b().content_update_request_identity_hash
    assert _b(wordpress_post_id="26").content_update_request_identity_hash != base
    assert _b(article_publication_artifact_id=2).content_update_request_identity_hash != base
    assert _b(artifact_hash="b" * 64).content_update_request_identity_hash != base
    assert _b(tracked_html=_BASE["tracked_html"] + "!").content_update_request_identity_hash != base


def test_request_identity_excludes_raw_baseline_and_response_fields() -> None:
    # identity は wordpress_post_id / article_publication_artifact_id / artifact_hash /
    # request_content_hash / update_payload_hash / method / endpoint_path のみで決まる
    # -- expected/observed raw baseline や response は一切含まれない (別 namespace)。
    identity = compute_content_update_request_identity_hash(
        wordpress_post_id="25",
        article_publication_artifact_id=1,
        artifact_hash="a" * 64,
        request_content_hash="c" * 64,
        update_payload_hash="d" * 64,
        method="POST",
        endpoint_path="/wp-json/wp/v2/posts/25",
    )
    # 同じ 7 引数なら raw baseline がどうであれ同じ identity になる (呼び出しに引数自体が無い)
    identity_again = compute_content_update_request_identity_hash(
        wordpress_post_id="25",
        article_publication_artifact_id=1,
        artifact_hash="a" * 64,
        request_content_hash="c" * 64,
        update_payload_hash="d" * 64,
        method="POST",
        endpoint_path="/wp-json/wp/v2/posts/25",
    )
    assert identity == identity_again


# ==================== target identity (§12) ====================================
def test_target_identity_deterministic() -> None:
    a, b = _b(), _b()
    assert (
        a.target_content_update_request_identity_hash
        == b.target_content_update_request_identity_hash
    )
    assert len(a.target_content_update_request_identity_hash) == 64


def test_target_base_url_change_changes_target_identity() -> None:
    base = _b().target_content_update_request_identity_hash
    other = _b(
        target_base_url="https://other.example.test"
    ).target_content_update_request_identity_hash
    assert base != other


def test_target_identity_changes_when_request_identity_changes() -> None:
    base = _b().target_content_update_request_identity_hash
    other = _b(artifact_hash="b" * 64).target_content_update_request_identity_hash
    assert base != other


def test_target_identity_binds_exactly_two_keys() -> None:
    h = compute_target_content_update_request_identity_hash(
        content_update_request_identity_hash="e" * 64, target_base_url="https://x.test"
    )
    canon = json.dumps(
        {
            "content_update_request_identity_hash": "e" * 64,
            "target_base_url": "https://x.test",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert h == hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ==================== credentials never participate (§12/§20) ==================
def test_no_credential_fields_in_builder_signature() -> None:
    import inspect

    sig = inspect.signature(build_wordpress_content_update_request)
    assert set(sig.parameters).isdisjoint(
        {"username", "password", "application_password", "authorization", "token"}
    )


# ==================== hash namespace separation (D-D5A/D-D5A.1 §3) =============
def test_request_content_hash_and_raw_content_hash_are_different_namespaces() -> None:
    """D-D5A で実証済み: 同一 tracked_html から得た request_content_hash は、
    WordPress の実際の raw content hash とは一致しない (別 namespace)。この module
    はそもそも raw content hash を計算しない -- 混同のしようがないことを pin する。
    """

    r = _b()
    # Article #1 の実際の証跡 (D-D5A で確認済み)
    historical_submitted_hash = "0765a976ee7255aaac54b5d8b9eb07126d6233df9194e75608b3ad1932cdfe3a"
    historical_wordpress_raw_hash = (
        "ee300c1dd3728a2a5e247ad0fdc610653e53f1612c1e4029633db8483b2f8f33"
    )
    assert historical_submitted_hash != historical_wordpress_raw_hash
    # この module の出力は request_content_hash 系のみで、raw hash 系のフィールドを
    # 一切持たない。
    assert not hasattr(r, "wordpress_raw_content_hash")
    assert not hasattr(r, "response_content_raw_hash")


# ==================== golden vectors (§27) ======================================
_GOLDEN_TRACKED_HTML = "<p>Golden fixture content.</p>"
_GOLDEN_PAYLOAD_JSON = '{"content":"<p>Golden fixture content.</p>"}'
_GOLDEN_PAYLOAD_HASH = "2aec18af8804545b2835333903d919e69804dd873315bc8a1426d42260aaae62"
_GOLDEN_REQUEST_CONTENT_HASH = "7b8edb8a88c52b7ea967c125b72bb33aaf4dfc175387b85c6479474a568f22e1"
_GOLDEN_IDENTITY = "b231e9833b27989d5d45f9c7adfcaebaa63c91cd7099fb5c05fe3162f0f93d8f"
_GOLDEN_TARGET_IDENTITY = "aa17e2482b7e6002469a3e3c09f9ba99b264f00444c655e4b3f65a9fbede5846"


def test_golden_payload_and_request_content_hash() -> None:
    r = build_wordpress_content_update_request(
        wordpress_post_id="25",
        article_publication_artifact_id=1,
        artifact_hash="f" * 64,
        tracked_html=_GOLDEN_TRACKED_HTML,
        target_base_url="https://bizfluxlab.com",
    )
    assert r.update_payload_json == _GOLDEN_PAYLOAD_JSON
    assert r.update_payload_hash == _GOLDEN_PAYLOAD_HASH
    assert r.request_content_hash == _GOLDEN_REQUEST_CONTENT_HASH


def test_golden_request_identity_hash() -> None:
    r = build_wordpress_content_update_request(
        wordpress_post_id="25",
        article_publication_artifact_id=1,
        artifact_hash="f" * 64,
        tracked_html=_GOLDEN_TRACKED_HTML,
        target_base_url="https://bizfluxlab.com",
    )
    assert r.content_update_request_identity_hash == _GOLDEN_IDENTITY


def test_golden_target_identity_hash() -> None:
    r = build_wordpress_content_update_request(
        wordpress_post_id="25",
        article_publication_artifact_id=1,
        artifact_hash="f" * 64,
        tracked_html=_GOLDEN_TRACKED_HTML,
        target_base_url="https://bizfluxlab.com",
    )
    assert r.target_content_update_request_identity_hash == _GOLDEN_TARGET_IDENTITY


def test_wrong_key_name_changes_golden_identity() -> None:
    """identity dict のキー名が 1 つでも変われば golden hash と一致しなくなる
    (キー名自体が identity の一部であることの証拠)。"""

    wrong = {
        "wp_post_id": "25",  # 正しいキー名は "wordpress_post_id"
        "article_publication_artifact_id": 1,
        "artifact_hash": "f" * 64,
        "request_content_hash": _GOLDEN_REQUEST_CONTENT_HASH,
        "update_payload_hash": _GOLDEN_PAYLOAD_HASH,
        "method": "POST",
        "endpoint_path": "/wp-json/wp/v2/posts/25",
    }
    canon = json.dumps(
        wrong, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    wrong_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert wrong_hash != _GOLDEN_IDENTITY
