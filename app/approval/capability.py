"""モバイル承認の capability (C8.8、pure)。

レビュー URL に載せる 1 回限りの権能。推測できず、他の依頼へ流用できず、期限が
あり、決定に 1 度使ったら終わる。

守る約束:

- **256 bit の CSPRNG**。id / hash / 時刻から導出しない。
- ローカルにも中継にも **digest しか保存しない**。生の値は返り値として 1 度だけ
  呼び出し側に渡り、メールの URL fragment に入って消える。
- ``subject_type`` / ``subject_id`` / ``subject_hash`` / ``subject_version`` /
  ``expires_at`` に束縛する。提案が作り直されれば ``subject_hash`` が変わるので、
  古い capability は新しい内容に使えない。
- 期限切れ・失効・決定済みは、どれも「決定できない」に倒す。

なぜ URL の **fragment** に載せるか:

fragment (``#...``) は HTTP リクエストに送られない。したがって capability は
アクセスログにも、リバースプロキシにも、Referer にも残らない。ページの JS が
HTTPS の POST body で 1 度だけ交換し、短命なレビューセッションに変える。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

#: 256 bit。``secrets.token_urlsafe(32)`` は 43 文字の URL-safe 文字列になる。
CAPABILITY_ENTROPY_BYTES = 32
CAPABILITY_MIN_LENGTH = 43
CAPABILITY_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{43,86}\Z")

DEFAULT_TTL_HOURS = 24
#: 期限は延ばし放題にしない (置き忘れたリンクを長生きさせない)。
MAX_TTL_HOURS = 72

# -- 失敗理由 (人にも機械にも読める最小の語彙) --------------------------------
REASON_OK = "ok"
REASON_MALFORMED = "malformed_capability"
REASON_MISMATCH = "capability_mismatch"
REASON_EXPIRED = "expired"
REASON_BINDING_MISMATCH = "binding_mismatch"


@dataclass(frozen=True)
class IssuedCapability:
    """発行された capability。``secret`` は保存せず、1 度だけ運ぶ。"""

    secret: str
    digest: str
    binding: str
    expires_at: datetime

    def review_fragment(self) -> str:
        """レビュー URL の fragment 部分 (``#`` は含めない)。"""

        return self.secret


def generate_capability(
    *,
    subject_type: str,
    subject_id: int,
    subject_hash: str,
    subject_version: int,
    issued_at: datetime,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> IssuedCapability:
    """新しい capability を 1 つ作る。"""

    ttl = max(1, min(int(ttl_hours), MAX_TTL_HOURS))
    expires_at = issued_at + timedelta(hours=ttl)
    secret = secrets.token_urlsafe(CAPABILITY_ENTROPY_BYTES)
    return IssuedCapability(
        secret=secret,
        digest=capability_digest(secret),
        binding=capability_binding(
            subject_type=subject_type,
            subject_id=subject_id,
            subject_hash=subject_hash,
            subject_version=subject_version,
            expires_at=expires_at,
        ),
        expires_at=expires_at,
    )


def capability_digest(secret: str) -> str:
    """保存してよい表現。生の capability からは一方向。"""

    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def capability_binding(
    *,
    subject_type: str,
    subject_id: int,
    subject_hash: str,
    subject_version: int,
    expires_at: datetime,
) -> str:
    """capability が「どの提案の、どの版の、いつまで」かを固定する hash。

    secret は入れない -- これは束縛の記述であって、認証子ではない。
    """

    payload = chr(31).join(
        [
            str(subject_type),
            str(subject_id),
            str(subject_hash),
            str(subject_version),
            expires_at.replace(microsecond=0).isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_well_formed(secret: object) -> bool:
    return isinstance(secret, str) and CAPABILITY_PATTERN.match(secret) is not None


def verify_capability(
    *,
    presented: str,
    stored_digest: str,
    stored_binding: str,
    subject_type: str,
    subject_id: int,
    subject_hash: str,
    subject_version: int,
    expires_at: datetime,
    now: datetime,
) -> tuple[bool, str]:
    """提示された capability を検証する。``(ok, reason)`` を返す。

    失敗理由は最小限の語彙にとどめる -- 無効な capability に対して、対象の存在や
    内容を推測させる情報を返さない。
    """

    if not is_well_formed(presented):
        return False, REASON_MALFORMED
    if not hmac.compare_digest(capability_digest(presented), (stored_digest or "").lower()):
        return False, REASON_MISMATCH
    expected_binding = capability_binding(
        subject_type=subject_type,
        subject_id=subject_id,
        subject_hash=subject_hash,
        subject_version=subject_version,
        expires_at=expires_at,
    )
    if not hmac.compare_digest(expected_binding, (stored_binding or "").lower()):
        return False, REASON_BINDING_MISMATCH
    if now >= expires_at:
        return False, REASON_EXPIRED
    return True, REASON_OK


def redact(text: str, *capabilities: str) -> str:
    """ログ/例外へ出す前に capability を伏せる。

    既知の値を消すだけでなく、capability の形をした文字列も落とす (取り違えや
    将来の経路の追加で漏れないように)。
    """

    out = text or ""
    for secret in capabilities:
        if secret:
            out = out.replace(secret, "[redacted-capability]")
    return re.sub(
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43,86}(?![A-Za-z0-9_-])", "[redacted-capability]", out
    )
