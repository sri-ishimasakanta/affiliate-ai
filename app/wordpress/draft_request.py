"""WordPress draft-create request の deterministic な組み立てと hash 化 (pure)。

DB / network / 認証情報 非依存。将来 ``POST /wp-json/wp/v2/posts`` へ送る **logical な
JSON body** だけを扱う。HTTP ヘッダ・base URL・Authorization・タイムスタンプ・
idempotency key・レスポンスは一切含まない。

V1 の post status は **``draft`` 固定** — この module に publish を組み立てる経路は無い。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.article.draft_input_canonical import canonical_json

METHOD = "POST"
ENDPOINT_PATH = "/wp-json/wp/v2/posts"
V1_POST_STATUS = "draft"  # V1 は draft のみ。publish 経路は存在しない。

# V1 で送るキーはちょうどこの 5 つ。
_V1_PAYLOAD_KEYS = ("title", "content", "excerpt", "slug", "status")


@dataclass(frozen=True)
class WordPressDraftRequest:
    method: str
    endpoint_path: str
    payload: MappingProxyType
    payload_hash: str
    request_identity_hash: str
    canonical_body_hash: str
    canonical_meta_hash: str
    renderer_version: str
    rendered_content_hash: str

    def payload_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def _sha256_canonical(obj: object) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def build_wordpress_draft_request(
    *,
    article_id: int,
    source_promotion_id: int,
    title: str,
    content: str,
    excerpt: str,
    slug: str,
    canonical_body_hash: str,
    canonical_meta_hash: str,
    renderer_version: str,
    rendered_content_hash: str,
) -> WordPressDraftRequest:
    """Article #1 first draft 用の exact request package を組む。

    ``content`` は Human 承認済み rendered HTML そのもの (整形・正規化しない)。
    ``status`` は常に ``"draft"``。
    """

    payload: dict[str, Any] = {
        "title": title,
        "content": content,
        "excerpt": excerpt,
        "slug": slug,
        "status": V1_POST_STATUS,
    }
    assert tuple(sorted(payload)) == tuple(sorted(_V1_PAYLOAD_KEYS))

    payload_hash = _sha256_canonical(payload)

    identity = {
        "article_id": article_id,
        "source_promotion_id": source_promotion_id,
        "canonical_body_hash": canonical_body_hash,
        "canonical_meta_hash": canonical_meta_hash,
        "renderer_version": renderer_version,
        "rendered_content_hash": rendered_content_hash,
        "method": METHOD,
        "endpoint_path": ENDPOINT_PATH,
        "payload_hash": payload_hash,
    }
    request_identity_hash = _sha256_canonical(identity)

    return WordPressDraftRequest(
        method=METHOD,
        endpoint_path=ENDPOINT_PATH,
        payload=MappingProxyType(dict(payload)),
        payload_hash=payload_hash,
        request_identity_hash=request_identity_hash,
        canonical_body_hash=canonical_body_hash,
        canonical_meta_hash=canonical_meta_hash,
        renderer_version=renderer_version,
        rendered_content_hash=rendered_content_hash,
    )


def canonical_payload_json(request: WordPressDraftRequest) -> str:
    """payload_hash と同じ canonical JSON エンコーディング (artifact 書き出し用)。"""

    return canonical_json(request.payload_dict())
