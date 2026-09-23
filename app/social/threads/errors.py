"""Threads 連携の失敗の語彙 (T1、pure)。

**token を絶対に外へ出さない。** Meta の API は access token をリクエスト
パラメータとして受け取るため、URL や body をそのまま例外やログに載せると資格情報が
漏れる。したがってこのモジュールは:

- 例外に URL / body / ヘッダの生値を入れない
- 保持するのは分類・HTTP status・API 側のコード/サブコードだけ
- 出力する文字列は必ず :func:`redact` を通す

呼び出し側が「何が起きて、次に何をすべきか」を判断できるだけの情報は残す。
"""

from __future__ import annotations

import re

#: token らしき値を落とすためのパターン (クエリ・body・JSON のいずれの形でも)。
_TOKEN_PATTERNS = (
    re.compile(r"(?i)(access_token|client_secret|refresh_token)=([^&\s\"']+)"),
    re.compile(r"(?i)\"(access_token|client_secret|refresh_token)\"\s*:\s*\"[^\"]*\""),
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
)
REDACTED = "[redacted]"


def redact(text: str) -> str:
    """診断に出す前に資格情報を落とす。"""

    out = text or ""
    out = _TOKEN_PATTERNS[0].sub(r"\1=" + REDACTED, out)
    out = _TOKEN_PATTERNS[1].sub(r'"\1":"' + REDACTED + '"', out)
    out = _TOKEN_PATTERNS[2].sub(r"\1" + REDACTED, out)
    return out


class ThreadsError(Exception):
    """Threads 連携の基底。**資格情報を含めない。**"""

    category = "threads_error"

    def __init__(self, reason: str, *, status: int | None = None, api_code: str | None = None):
        self.reason = redact(reason)
        self.status = status
        self.api_code = api_code
        super().__init__(f"{self.category}: {self.reason}")

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "reason": self.reason,
            "status": self.status,
            "api_code": self.api_code,
        }


class ThreadsNotConfiguredError(ThreadsError):
    """設定が足りない。**外部へ問い合わせる前に** 失敗させる。"""

    category = "threads_not_configured"


class ThreadsAuthError(ThreadsError):
    """認証できない (token が無効・期限切れ)。再取得が必要。"""

    category = "threads_auth"


class ThreadsPermissionError(ThreadsError):
    """権限 (scope) が足りない。Meta 側の設定を直す必要がある。"""

    category = "threads_permission"


class ThreadsRateLimitError(ThreadsError):
    """レート制限。時間を空けて再試行する (自動で叩き続けない)。"""

    category = "threads_rate_limit"


class ThreadsServerError(ThreadsError):
    """Meta 側の障害。こちらの設定の問題ではない。"""

    category = "threads_server"


class ThreadsResponseError(ThreadsError):
    """応答が想定の形でない。推測で埋めずに失敗させる。"""

    category = "threads_response"


class ThreadsTimeoutError(ThreadsError):
    """時間内に応答が無かった。"""

    category = "threads_timeout"


def classify(status: int, api_code: str | None, api_subcode: str | None) -> type[ThreadsError]:
    """HTTP status と API のコードから、呼び出し側が判断できる分類へ落とす。"""

    if status in (401, 403):
        # 190 系は token、200 系は権限 (Graph API 共通の慣習)。
        if api_code and str(api_code).startswith("190"):
            return ThreadsAuthError
        return ThreadsPermissionError
    if status == 429:
        return ThreadsRateLimitError
    if status >= 500:
        return ThreadsServerError
    if status >= 400:
        if api_subcode and str(api_subcode) in ("463", "467"):
            return ThreadsAuthError
        return ThreadsResponseError
    return ThreadsResponseError
