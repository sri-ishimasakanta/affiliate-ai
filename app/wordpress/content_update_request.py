"""既存 WordPress post の content を update する request の deterministic な組み立てと
hash 化 (pure)。

DB / network / 認証情報 非依存。``POST /wp-json/wp/v2/posts/{id}`` へ送る **exact な
JSON body** は ``{"content": <tracked_html>}`` 固定。title / excerpt / slug / status /
meta / categories / tags 等は一切含めない。

D-D5A / D-D5A.1 で確定した、この module が **絶対に混同してはならない** 2 つの hash
namespace:

- ``request_content_hash``
  = ``compute_text_hash(artifact.tracked_html)`` — **意図した (submit する) content**
  の identity。WordPress へは一切問い合わせない。

- ``*_wordpress_raw_content_hash`` (expected/observed/response のいずれも)
  = WordPress の ``GET .../posts/{id}?context=edit`` が返す ``content.raw`` の
  SHA-256 — **WordPress が実際に保存している content** の identity。

WordPress は保存時に inline style 等を正当に sanitize するため、この 2 つは
**同じ入力からでも一致しない** (D-D5A で実証済み: Article #1 の submitted hash
``0765a976...`` と WordPress stored hash ``ee300c1d...`` は異なる)。この module の
どの関数も、この 2 namespace を等値比較しない。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.article.draft_input_canonical import canonical_json
from app.article.draft_promotion_canonical import compute_text_hash

METHOD = "POST"

# V1 の content-update payload はちょうどこのキー 1 つ。
_PAYLOAD_KEYS = ("content",)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def endpoint_path_for_post(wordpress_post_id: str | int) -> str:
    return f"/wp-json/wp/v2/posts/{wordpress_post_id}"


def build_content_update_payload_json(tracked_html: str) -> str:
    """``{"content": tracked_html}`` の exact な canonical JSON 文字列
    (UTF-8, 余白なし, 改行なし)。tracked_html は一切正規化・加工しない。"""

    payload = {"content": tracked_html}
    assert tuple(sorted(payload)) == tuple(sorted(_PAYLOAD_KEYS))
    return canonical_json(payload)


def compute_content_update_payload_hash(update_payload_json: str) -> str:
    return _sha256(update_payload_json)


def compute_request_content_hash(tracked_html: str) -> str:
    """意図した (submit する) content の identity。

    ``compute_text_hash`` (``app.article.draft_promotion_canonical``) をそのまま
    再利用する — 独自に再実装しない。WordPress の content.raw とは一切比較しない。
    """

    return compute_text_hash(tracked_html)


def compute_content_update_request_identity_hash(
    *,
    wordpress_post_id: str,
    article_publication_artifact_id: int,
    artifact_hash: str,
    request_content_hash: str,
    update_payload_hash: str,
    method: str,
    endpoint_path: str,
) -> str:
    """どの post を / どの承認済み artifact から / どの exact request で update するか
    を束縛する hash。credential / timestamp / run id / response / status / raw-content
    baseline / idempotency key は含めない。"""

    identity = {
        "wordpress_post_id": wordpress_post_id,
        "article_publication_artifact_id": article_publication_artifact_id,
        "artifact_hash": artifact_hash,
        "request_content_hash": request_content_hash,
        "update_payload_hash": update_payload_hash,
        "method": method,
        "endpoint_path": endpoint_path,
    }
    return _sha256(canonical_json(identity))


def compute_target_content_update_request_identity_hash(
    *, content_update_request_identity_hash: str, target_base_url: str
) -> str:
    """already-computed content-update request identity を exact な WordPress 設置先
    へ束縛する。credential は含めない (target_base_url は呼び出し側が既に
    ``app.wordpress.target.canonicalize_wordpress_base_url`` で正規化済みのものを渡す
    こと -- この関数自身は正規化しない)。"""

    canonical = canonical_json(
        {
            "content_update_request_identity_hash": content_update_request_identity_hash,
            "target_base_url": target_base_url,
        }
    )
    return _sha256(canonical)


@dataclass(frozen=True)
class WordPressContentUpdateRequest:
    method: str
    endpoint_path: str
    update_payload_json: str
    update_payload_hash: str
    request_content_hash: str
    content_update_request_identity_hash: str
    target_content_update_request_identity_hash: str


def build_wordpress_content_update_request(
    *,
    wordpress_post_id: str,
    article_publication_artifact_id: int,
    artifact_hash: str,
    tracked_html: str,
    target_base_url: str,
) -> WordPressContentUpdateRequest:
    """承認済み artifact の ``tracked_html`` を、既存 WordPress post へ update する
    exact request package を組む。ネットワークへは一切送らない。"""

    payload_json = build_content_update_payload_json(tracked_html)
    payload_hash = compute_content_update_payload_hash(payload_json)
    endpoint_path = endpoint_path_for_post(wordpress_post_id)
    request_content_hash = compute_request_content_hash(tracked_html)

    identity = compute_content_update_request_identity_hash(
        wordpress_post_id=wordpress_post_id,
        article_publication_artifact_id=article_publication_artifact_id,
        artifact_hash=artifact_hash,
        request_content_hash=request_content_hash,
        update_payload_hash=payload_hash,
        method=METHOD,
        endpoint_path=endpoint_path,
    )
    target_identity = compute_target_content_update_request_identity_hash(
        content_update_request_identity_hash=identity,
        target_base_url=target_base_url,
    )
    return WordPressContentUpdateRequest(
        method=METHOD,
        endpoint_path=endpoint_path,
        update_payload_json=payload_json,
        update_payload_hash=payload_hash,
        request_content_hash=request_content_hash,
        content_update_request_identity_hash=identity,
        target_content_update_request_identity_hash=target_identity,
    )
