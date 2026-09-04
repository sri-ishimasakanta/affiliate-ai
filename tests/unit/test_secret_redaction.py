"""秘密情報のサニタイズ (WordPress App Password / Authorization ヘッダ等)。"""

from __future__ import annotations

from app.services.draft_generation_adapters import sanitize_provider_error


def test_redacts_authorization_header_line() -> None:
    msg = "request failed\nAuthorization: Basic dXNlcjphcHBwYXNz\nstatus 401"
    out = sanitize_provider_error(msg)
    assert "dXNlcjphcHBwYXNz" not in out
    assert "[redacted line]" in out
    assert "status 401" in out


def test_redacts_wordpress_app_password_line() -> None:
    msg = "config: wordpress_app_password=abcd efgh ijkl mnop\nother: ok"
    out = sanitize_provider_error(msg)
    assert "abcd efgh" not in out
    assert "other: ok" in out


def test_redacts_application_password_phrase() -> None:
    out = sanitize_provider_error("using application password xxxx yyyy for auth")
    assert "xxxx yyyy" not in out


def test_non_secret_text_passes_through() -> None:
    msg = "HTTP 500 from https://example.com/wp-json/wp/v2/posts"
    assert sanitize_provider_error(msg) == msg
