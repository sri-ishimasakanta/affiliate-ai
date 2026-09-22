"""UrlInspectionClient の単体テスト (C5.1 / C6)。

C6 で実測した defect を pin する: Google は日本語 slug のページを **decode 済みの
表記** で認識しており、percent-encoded のまま inspect すると「未認識」と返る。
encoded のまま問い合わせると、インデックス済みのページを未インデックスと
誤判定してしまう。

予約文字の escape (``%2F`` 等) を decode すると URL の構造が変わるため、そこは
安全側に倒して元の表記のまま送る。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.exceptions import ExternalProviderError
from app.search_console.url_inspection_client import (
    UrlInspectionClient,
    canonical_inspection_url,
)

_ENCODED = (
    "https://bizfluxlab.com/%e6%a5%ad%e5%8b%99%e5%8a%b9%e7%8e%87%e5%8c%96-"
    "%e3%83%84%e3%83%bc%e3%83%ab-%e3%81%8a%e3%81%99%e3%81%99%e3%82%81-roundup/"
)
_DECODED = "https://bizfluxlab.com/業務効率化-ツール-おすすめ-roundup/"


class _Settings:
    search_console_property_uri = "sc-domain:bizfluxlab.com"
    search_console_credentials_file = "/nonexistent.json"


def _client(handler, monkeypatch) -> UrlInspectionClient:
    client = UrlInspectionClient(
        _Settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(client, "_token", lambda: "test-token")
    return client


# ==================== url normalization =======================================
def test_ascii_url_is_unchanged() -> None:
    assert canonical_inspection_url("https://x.test/rpa-tools/") == "https://x.test/rpa-tools/"


def test_percent_encoded_japanese_slug_is_decoded() -> None:
    assert canonical_inspection_url(_ENCODED) == _DECODED


def test_uppercase_hex_is_also_decoded() -> None:
    assert canonical_inspection_url(_ENCODED.replace("%e", "%E")) == _DECODED


def test_encoded_slash_is_left_alone() -> None:
    """``%2F`` を decode すると path の構造が変わるので触らない。"""

    url = "https://x.test/a%2Fb/"
    assert canonical_inspection_url(url) == url


def test_encoded_question_mark_is_left_alone() -> None:
    url = "https://x.test/a%3Fb/"
    assert canonical_inspection_url(url) == url


def test_query_string_is_preserved() -> None:
    url = "https://x.test/%e6%a5%ad/?page=2"
    assert canonical_inspection_url(url) == "https://x.test/業/?page=2"


def test_empty_input_is_safe() -> None:
    assert canonical_inspection_url("") == ""


# ==================== client ==================================================
def test_decoded_url_is_sent_to_google(monkeypatch) -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
        )

    result = _client(handler, monkeypatch).inspect(_ENCODED)

    assert sent[0]["inspectionUrl"] == _DECODED
    assert result.ok is True
    # 記事との突き合わせが崩れないよう、返す URL は呼び出し側が渡したもの。
    assert result.inspection_url == _ENCODED


def test_errors_carry_no_credentials(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(
            403, json={"error": {"status": "PERMISSION_DENIED", "message": "no access"}}
        )

    result = _client(handler, monkeypatch).inspect("https://x.test/a/")

    assert result.ok is False
    assert result.error_status == "PERMISSION_DENIED"
    assert "test-token" not in json.dumps(result.__dict__, ensure_ascii=False)


def test_transport_failure_is_reported_not_raised(monkeypatch) -> None:
    def handler(request):
        raise httpx.ConnectError("boom")

    result = _client(handler, monkeypatch).inspect("https://x.test/a/")
    assert result.error_status == "TRANSPORT_ERROR"
    assert result.http_status is None


def test_missing_property_raises(monkeypatch) -> None:
    class _NoProperty:
        search_console_property_uri = None
        search_console_credentials_file = None

    client = UrlInspectionClient(_NoProperty())
    with pytest.raises(ExternalProviderError):
        _ = client.property_uri
