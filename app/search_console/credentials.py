"""Search Console service-account credential の読み込みと検証。

- credential JSON の **中身** は Settings / DB / response_snapshot に一切保存しない。
- private_key / private_key_id / access token を print / 例外文言に含めない。
- credential file が repo working tree 内にある場合は拒否する。
- scope は :data:`SEARCH_CONSOLE_READONLY_SCOPE` 固定 (設定で広げられない)。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.exceptions import SearchConsoleCredentialError

# Search Console read-only。ここ以外で scope を渡さない (production 設定で広げさせない)。
SEARCH_CONSOLE_READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REQUIRED_JSON_KEYS = ("client_email", "private_key")


class ServiceAccountFacts:
    """検証済み credential から取り出した **安全な** 事実のみ。secret は持たない。"""

    __slots__ = ("client_email", "path")

    def __init__(self, *, client_email: str, path: Path) -> None:
        self.client_email = client_email
        self.path = path


def _is_inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(_REPO_ROOT)
        return True
    except ValueError:
        return False


def validate_credentials_file(raw_path: str | None) -> ServiceAccountFacts:
    """path とファイル内容を検証し、安全な事実だけを返す。secret は返さない。"""

    if not raw_path:
        raise SearchConsoleCredentialError("no credentials file configured")

    path = Path(raw_path)
    if not path.exists():
        raise SearchConsoleCredentialError("credentials file does not exist")
    if not path.is_file():
        raise SearchConsoleCredentialError("credentials path is not a regular file")
    if _is_inside_repo(path):
        raise SearchConsoleCredentialError(
            "credentials file must not be located inside the repository working tree"
        )

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise SearchConsoleCredentialError("credentials file is not valid JSON") from None

    if not isinstance(doc, dict) or doc.get("type") != "service_account":
        raise SearchConsoleCredentialError(
            "credentials JSON type is not 'service_account'"
        )
    missing = [k for k in _REQUIRED_JSON_KEYS if not doc.get(k)]
    if missing:
        raise SearchConsoleCredentialError(
            f"credentials JSON is missing required field(s): {', '.join(missing)}"
        )

    return ServiceAccountFacts(client_email=str(doc["client_email"]), path=path)


def load_readonly_credentials(raw_path: str | None):
    """webmasters.readonly scope の service-account Credentials を返す。

    ``google.oauth2.service_account`` を遅延 import する (Settings import 時に読まない)。
    """

    facts = validate_credentials_file(raw_path)
    try:
        from google.oauth2 import service_account  # 遅延 import
    except ImportError as exc:  # pragma: no cover - 依存未導入時のみ
        raise SearchConsoleCredentialError(
            "google-auth is not available"
        ) from exc
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(facts.path), scopes=[SEARCH_CONSOLE_READONLY_SCOPE]
        )
    except Exception as exc:  # 署名鍵不正など。詳細 (鍵) は載せない。
        raise SearchConsoleCredentialError(
            "failed to build service-account credentials from the file"
        ) from exc
    return creds, facts
