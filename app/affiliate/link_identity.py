"""AffiliateLinkTarget の link identity hash (pure)。

承認された「article + program + **exact** destination_url 文字列」の binding を表す。
token / status / timestamps / superseded_by_id は **含めない**
(token は別に不変な routing 識別子、status/timestamps は lifecycle)。
"""

from __future__ import annotations

import hashlib

from app.article.draft_input_canonical import canonical_json


def compute_link_identity_hash(
    *, article_id: int, affiliate_program_id: int, destination_url: str
) -> str:
    """SHA-256 hex (64 文字)。``destination_url`` は無改変の exact 文字列を渡すこと。"""

    identity = {
        "article_id": article_id,
        "affiliate_program_id": affiliate_program_id,
        "destination_url": destination_url,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
