"""app/search_console/credentials.py の検証ロジック (実 Google 認証なし)。"""

from __future__ import annotations

import json

import pytest

from app.exceptions import SearchConsoleCredentialError
from app.search_console.credentials import (
    SEARCH_CONSOLE_READONLY_SCOPE,
    validate_credentials_file,
)

_GOOD = {
    "type": "service_account",
    "project_id": "p",
    "private_key_id": "kid",
    "private_key": "-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n",
    "client_email": "gsc-probe@p.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _write(tmp_path, doc, name="gsc.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def test_scope_constant_is_readonly_only() -> None:
    assert SEARCH_CONSOLE_READONLY_SCOPE == (
        "https://www.googleapis.com/auth/webmasters.readonly"
    )


def test_valid_file_returns_safe_facts_only(tmp_path) -> None:
    facts = validate_credentials_file(_write(tmp_path, _GOOD))
    assert facts.client_email == "gsc-probe@p.iam.gserviceaccount.com"
    # secret は facts に載らない
    assert not hasattr(facts, "private_key")
    assert "private_key" not in facts.__slots__


def test_missing_path_rejected() -> None:
    with pytest.raises(SearchConsoleCredentialError):
        validate_credentials_file(None)


def test_nonexistent_file_rejected(tmp_path) -> None:
    with pytest.raises(SearchConsoleCredentialError):
        validate_credentials_file(str(tmp_path / "nope.json"))


def test_directory_rejected(tmp_path) -> None:
    with pytest.raises(SearchConsoleCredentialError):
        validate_credentials_file(str(tmp_path))


def test_invalid_json_rejected(tmp_path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SearchConsoleCredentialError):
        validate_credentials_file(str(p))


def test_wrong_type_rejected(tmp_path) -> None:
    doc = {**_GOOD, "type": "authorized_user"}
    with pytest.raises(SearchConsoleCredentialError):
        validate_credentials_file(_write(tmp_path, doc))


def test_missing_client_email_rejected(tmp_path) -> None:
    doc = {k: v for k, v in _GOOD.items() if k != "client_email"}
    with pytest.raises(SearchConsoleCredentialError):
        validate_credentials_file(_write(tmp_path, doc))


def test_missing_private_key_rejected(tmp_path) -> None:
    doc = {k: v for k, v in _GOOD.items() if k != "private_key"}
    with pytest.raises(SearchConsoleCredentialError):
        validate_credentials_file(_write(tmp_path, doc))


def test_file_inside_repo_rejected() -> None:
    # このテストファイル自身は repo 内 -> 拒否される
    with pytest.raises(SearchConsoleCredentialError):
        validate_credentials_file(__file__)


def test_error_messages_do_not_leak_secret(tmp_path) -> None:
    doc = {**_GOOD, "type": "authorized_user"}
    with pytest.raises(SearchConsoleCredentialError) as exc:
        validate_credentials_file(_write(tmp_path, doc))
    msg = str(exc.value)
    assert _GOOD["private_key"] not in msg
    assert "private_key_id" not in msg or "kid" not in msg
