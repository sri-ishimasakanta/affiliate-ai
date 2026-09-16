"""Article.body から canonical 外部リンク occurrence を決定的に抽出する (pure)。

D-D2: 抽出は必ず ``Article.body`` -> ``render_wordpress_html(...)`` から始まる。
``render_wordpress_html`` が既に ``external_links`` (document order, 重複保持) を
公開しているため、別の occurrence 抽出ロジックを発明しない — この list をそのまま
ordinal の真実として再利用する。renderer 自体はこのモジュールでは一切変更しない。

``external_links`` は ``_WordPressHTMLRenderer.link()`` が external scheme の
リンクを検出するたびに append するだけの単純な list なので:

- 同一 URL が複数回出現しても重複排除されない (append のみ)。
- internal link は一切 append されない (external のみが ordinal を消費する)。
- document order (mistune のトークン処理順 = 見出し/表を含む文書順) を正しく保持する。

よって ``external_links`` は同一 URL の重複出現・同一 anchor text 違う URL・
違う anchor text 同一 URL のいずれのケースでも ordinal 順序を曖昧さなく確定できる
(D-D0 の事前調査どおり)。anchor text はこのモジュールでは一切追跡しない —
renderer が生成する最終 HTML 文字列から anchor text を正規表現で再抽出するのは
(markdown 由来のネストした inline HTML を含みうるため) 確実に抽出できるとは限らず、
occurrence identity にも一切関与しないため、追跡しない。

occurrence_identity_hash は ``(occurrence_schema_version, canonical_body_hash,
renderer_version, occurrence_ordinal, original_href)`` のみの純関数。
``original_href`` は ``render_wordpress_html`` が出力した exact 文字列 (無改変 —
trim なし・小文字化なし・正規化なし・decode/re-encode なし)。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.article.draft_input_canonical import canonical_json

# occurrence identity 契約自体のバージョン。artifact_schema_version /
# renderer_version / projection_version のいずれとも独立 (混同しない)。
OCCURRENCE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LinkOccurrence:
    """1 external link occurrence の pure representation。DB には一切永続化しない
    (canonical Article.body + renderer_version + ordinal から常に再現可能な
    derived data であるため)。"""

    occurrence_ordinal: int
    occurrence_identity_hash: str
    original_href: str
    original_host: str | None


def extract_original_host(original_href: str) -> str | None:
    """表示/比較用に host だけを取り出す (``original_href`` 自体は無改変で別途保持する)。"""

    host = urlsplit(original_href).hostname
    return host.lower() if host else None


def compute_occurrence_identity_hash(
    *,
    canonical_body_hash: str,
    renderer_version: str,
    occurrence_ordinal: int,
    original_href: str,
    occurrence_schema_version: int = OCCURRENCE_SCHEMA_VERSION,
) -> str:
    """historical に再現可能な occurrence identity。呼び出し側が渡した凍結値のみの
    純関数 — 現在の mapping/target/projection acknowledgement 状態を一切参照しない。"""

    payload = {
        "occurrence_schema_version": occurrence_schema_version,
        "canonical_body_hash": canonical_body_hash,
        "renderer_version": renderer_version,
        "occurrence_ordinal": occurrence_ordinal,
        "original_href": original_href,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def extract_link_occurrences(
    *,
    external_links: Sequence[str],
    canonical_body_hash: str,
    renderer_version: str,
) -> list[LinkOccurrence]:
    """``render_wordpress_html(...).external_links`` (document order, 重複保持) から
    occurrence 列を作る。URL の重複排除・並べ替えは一切行わない。"""

    occurrences: list[LinkOccurrence] = []
    for ordinal, href in enumerate(external_links):
        identity_hash = compute_occurrence_identity_hash(
            canonical_body_hash=canonical_body_hash,
            renderer_version=renderer_version,
            occurrence_ordinal=ordinal,
            original_href=href,
        )
        occurrences.append(
            LinkOccurrence(
                occurrence_ordinal=ordinal,
                occurrence_identity_hash=identity_hash,
                original_href=href,
                original_host=extract_original_host(href),
            )
        )
    return occurrences
