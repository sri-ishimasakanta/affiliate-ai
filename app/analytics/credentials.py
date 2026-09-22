"""GA4 Data API 用の service-account credential 読み込み。

:mod:`app.search_console.credentials` のファイル検証 (repo 外にあること / JSON で
あること / 必須キーがあること) を **再利用** し、scope だけを GA4 read-only に
固定する。Search Console 側の scope は一切変えない -- 別の能力には別の固定 scope を
与える、という既存の方針をそのまま踏襲する。

private_key / private_key_id / access token を print / 例外文言に含めない。
"""

from __future__ import annotations

from app.exceptions import SearchConsoleCredentialError
from app.search_console.credentials import ServiceAccountFacts, validate_credentials_file

# GA4 Data API / Admin API の read-only scope。設定で広げられない。
ANALYTICS_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


def load_analytics_readonly_credentials(raw_path: str | None):
    """``analytics.readonly`` scope の service-account Credentials を返す。"""

    facts: ServiceAccountFacts = validate_credentials_file(raw_path)
    try:
        from google.oauth2 import service_account  # 遅延 import
    except ImportError as exc:  # pragma: no cover - 依存未導入時のみ
        raise SearchConsoleCredentialError("google-auth is not available") from exc
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(facts.path), scopes=[ANALYTICS_READONLY_SCOPE]
        )
    except Exception as exc:  # 署名鍵不正など。詳細 (鍵) は載せない。
        raise SearchConsoleCredentialError(
            "failed to build service-account credentials from the file"
        ) from None or exc
    return creds, facts
