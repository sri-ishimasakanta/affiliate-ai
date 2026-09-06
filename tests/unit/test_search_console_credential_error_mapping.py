"""SearchConsoleCredentialError の HTTP マッピング (503 / 安定 code)。"""

from __future__ import annotations

from app.api.exception_handlers import _ERROR_MAP
from app.exceptions import ApplicationError, SearchConsoleCredentialError


def test_credential_error_is_application_error() -> None:
    assert issubclass(SearchConsoleCredentialError, ApplicationError)


def test_credential_error_mapped_to_503_with_stable_code() -> None:
    entry = next(
        (row for row in _ERROR_MAP if row[0] is SearchConsoleCredentialError), None
    )
    assert entry is not None, "SearchConsoleCredentialError missing from _ERROR_MAP"
    _, status_code, code = entry
    assert status_code == 503
    assert code == "search_console_credential_error"


def test_credential_error_message_carries_only_the_reason() -> None:
    exc = SearchConsoleCredentialError("credentials file does not exist")
    assert exc.reason == "credentials file does not exist"
    assert "does not exist" in str(exc)
    # secret 由来の文言は型として持ち得ない
    assert "private_key" not in str(exc)
