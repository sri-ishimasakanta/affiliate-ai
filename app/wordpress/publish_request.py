"""既存 WordPress post を publish する request の deterministic な組み立てと hash 化 (pure)。

DB / network / 認証情報 非依存。``POST /wp-json/wp/v2/posts/{id}`` へ送る **exact な
JSON body** は ``{"status":"publish"}`` 固定。date / title / content / categories 等は
含めない (WordPress は既存 post の update endpoint で status=publish を受け付ける)。

3 つの hash:
- ``publish_payload_hash``            : 送信する exact bytes の SHA-256
- ``publication_request_identity_hash``: どの post / どの request / どの content か
- ``target_publication_request_identity_hash``: それを exact な WordPress 設置先へ束縛
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.article.draft_input_canonical import canonical_json

METHOD = "POST"
V1_PUBLISH_STATUS = "publish"
V1_EXPECTED_PRE_PUBLISH_STATUS = "draft"

# V1 の publish payload はちょうどこのキー 1 つ。
_PUBLISH_PAYLOAD = {"status": V1_PUBLISH_STATUS}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_publish_payload_json() -> str:
    """``{"status":"publish"}`` の exact な canonical JSON 文字列 (UTF-8, 余白なし, 改行なし)。"""

    return canonical_json(_PUBLISH_PAYLOAD)


def compute_publish_payload_hash(publish_payload_json: str) -> str:
    return _sha256(publish_payload_json)


def endpoint_path_for_post(wordpress_post_id: str | int) -> str:
    return f"/wp-json/wp/v2/posts/{wordpress_post_id}"


def compute_publication_request_identity_hash(
    *,
    article_id: int,
    source_wordpress_draft_run_id: int,
    wordpress_post_id: str,
    method: str,
    endpoint_path: str,
    publish_payload_hash: str,
    canonical_body_hash: str,
    canonical_meta_hash: str,
    wordpress_raw_content_hash: str,
    expected_pre_publish_status: str,
) -> str:
    """どの post を / どの exact request で / どの content identity のもとに publish するか
    を束縛する hash。credential / timestamp / run id / response / idempotency key は含めない。
    """

    identity = {
        "article_id": article_id,
        "source_wordpress_draft_run_id": source_wordpress_draft_run_id,
        "wordpress_post_id": wordpress_post_id,
        "method": method,
        "endpoint_path": endpoint_path,
        "publish_payload_hash": publish_payload_hash,
        "canonical_body_hash": canonical_body_hash,
        "canonical_meta_hash": canonical_meta_hash,
        "wordpress_raw_content_hash": wordpress_raw_content_hash,
        "expected_pre_publish_status": expected_pre_publish_status,
    }
    return _sha256(canonical_json(identity))


def compute_target_publication_request_identity_hash(
    *, publication_request_identity_hash: str, target_base_url: str
) -> str:
    """already-computed publication request identity を exact な WordPress 設置先へ束縛する。"""

    canonical = canonical_json(
        {
            "publication_request_identity_hash": publication_request_identity_hash,
            "target_base_url": target_base_url,
        }
    )
    return _sha256(canonical)


@dataclass(frozen=True)
class WordPressPublishRequest:
    method: str
    endpoint_path: str
    publish_payload_json: str
    publish_payload_hash: str
    publication_request_identity_hash: str
    target_publication_request_identity_hash: str


def build_wordpress_publish_request(
    *,
    article_id: int,
    source_wordpress_draft_run_id: int,
    wordpress_post_id: str,
    target_base_url: str,
    canonical_body_hash: str,
    canonical_meta_hash: str,
    wordpress_raw_content_hash: str,
    expected_pre_publish_status: str = V1_EXPECTED_PRE_PUBLISH_STATUS,
) -> WordPressPublishRequest:
    """Article #1 の初回 publish 用の exact request package を組む。"""

    payload_json = build_publish_payload_json()
    payload_hash = compute_publish_payload_hash(payload_json)
    endpoint_path = endpoint_path_for_post(wordpress_post_id)

    pri = compute_publication_request_identity_hash(
        article_id=article_id,
        source_wordpress_draft_run_id=source_wordpress_draft_run_id,
        wordpress_post_id=wordpress_post_id,
        method=METHOD,
        endpoint_path=endpoint_path,
        publish_payload_hash=payload_hash,
        canonical_body_hash=canonical_body_hash,
        canonical_meta_hash=canonical_meta_hash,
        wordpress_raw_content_hash=wordpress_raw_content_hash,
        expected_pre_publish_status=expected_pre_publish_status,
    )
    tpri = compute_target_publication_request_identity_hash(
        publication_request_identity_hash=pri,
        target_base_url=target_base_url,
    )
    return WordPressPublishRequest(
        method=METHOD,
        endpoint_path=endpoint_path,
        publish_payload_json=payload_json,
        publish_payload_hash=payload_hash,
        publication_request_identity_hash=pri,
        target_publication_request_identity_hash=tpri,
    )
