"""運用メールの設定検証と送信経路 (C8.7、SMTP には接続しない)。

pin する契約:

- 明示的に有効化されるまで **SMTP に接続しない**。
- 設定が欠けていれば、接続する前に sanitized な理由で落ちる。
- password は describe / subject / 本文 / 例外のどこにも出ない。
- STARTTLS と認証が実際に呼ばれる。
- 平文 SMTP へフォールバックしない。証明書検証を切る経路を持たない。
"""

from __future__ import annotations

import smtplib

import pytest

from app.operations.email import (
    EmailConfigError,
    EmailNotifier,
    build_email_config,
    build_email_notifier,
    describe_email_settings,
    render_alert_body,
)
from app.operations.notifications import NotificationMessage, build_notifiers

_PASSWORD = "super-secret-app-password"


class _Settings:
    operations_email_enabled = True
    operations_email_smtp_host = "smtp.example.com"
    operations_email_smtp_port = 587
    operations_email_username = "ops@example.com"
    operations_email_password = _PASSWORD
    operations_email_from = "ops@example.com"
    operations_email_use_starttls = True
    operations_email_subject_prefix = "BizFluxLab"
    operations_webhook_url = None

    @property
    def operations_email_recipients(self):
        return ["owner@example.com"]


def _settings(**overrides):
    cls = type("S", (_Settings,), overrides)
    return cls()


class _FakeSMTP:
    """smtplib の代わり。接続もしないし、外へ 1 バイトも出さない。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.logged_in_with: tuple | None = None
        self.messages: list = []
        self.starttls_context = None

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context=None) -> None:
        self.calls.append("starttls")
        self.starttls_context = context

    def login(self, username, password) -> None:
        self.calls.append("login")
        self.logged_in_with = (username, password)

    def send_message(self, message) -> None:
        self.calls.append("send_message")
        self.messages.append(message)

    def quit(self) -> None:
        self.calls.append("quit")


# -- config validation ---------------------------------------------------------
def test_disabled_provider_is_not_built() -> None:
    settings = _settings(operations_email_enabled=False)

    assert build_email_notifier(settings) is None
    with pytest.raises(EmailConfigError, match="ENABLED is false"):
        build_email_config(settings)


def test_disabled_provider_never_reaches_smtp(monkeypatch) -> None:
    def _boom(*args, **kwargs):  # pragma: no cover - 呼ばれたら失敗させる
        raise AssertionError("a disabled provider must not open an SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", _boom)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _boom)

    notifiers = build_notifiers(_settings(operations_email_enabled=False))

    assert [n.name for n in notifiers] == ["log"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"operations_email_smtp_host": ""}, "SMTP_HOST"),
        ({"operations_email_smtp_port": 0}, "SMTP_PORT"),
        ({"operations_email_smtp_port": 99999}, "SMTP_PORT"),
        ({"operations_email_from": "not-an-address"}, "FROM"),
        ({"operations_email_password": None}, "PASSWORD"),
        ({"operations_email_username": None}, "USERNAME"),
    ],
)
def test_incomplete_config_fails_before_connecting(overrides, expected) -> None:
    with pytest.raises(EmailConfigError, match=expected):
        build_email_config(_settings(**overrides))


def test_no_recipient_is_refused() -> None:
    settings = _settings()
    type(settings).operations_email_recipients = property(lambda self: [])

    with pytest.raises(EmailConfigError, match="TO must list at least one"):
        build_email_config(settings)


def test_malformed_recipient_is_refused() -> None:
    settings = _settings()
    type(settings).operations_email_recipients = property(lambda self: ["nope"])

    with pytest.raises(EmailConfigError, match="malformed"):
        build_email_config(settings)


def test_plaintext_smtp_is_refused() -> None:
    """TLS を切る経路は用意しない (465 の暗黙 TLS だけが例外)。"""

    with pytest.raises(EmailConfigError, match="plaintext SMTP is not supported"):
        build_email_config(_settings(operations_email_use_starttls=False))

    # 465 は暗黙 TLS なので STARTTLS 不要。
    config = build_email_config(
        _settings(operations_email_use_starttls=False, operations_email_smtp_port=465)
    )
    assert config.port == 465


def test_multiple_recipients_are_supported() -> None:
    settings = _settings()
    type(settings).operations_email_recipients = property(
        lambda self: ["a@example.com", "b@example.com"]
    )

    assert build_email_config(settings).recipients == ("a@example.com", "b@example.com")


# -- the password never escapes ------------------------------------------------
def test_describe_never_exposes_the_password() -> None:
    described = describe_email_settings(_settings())

    assert _PASSWORD not in repr(described)
    assert described["password_configured"] is True
    assert "password" not in {k for k in described if k != "password_configured"}


def test_describe_of_an_unconfigured_provider_is_safe() -> None:
    described = describe_email_settings(_settings(operations_email_enabled=False))

    assert described["configured"] is False
    assert described["reason"]
    assert _PASSWORD not in repr(described)


def test_a_config_error_message_never_contains_the_password() -> None:
    with pytest.raises(EmailConfigError) as excinfo:
        build_email_config(_settings(operations_email_smtp_host=""))

    assert _PASSWORD not in str(excinfo.value)


# -- sending -------------------------------------------------------------------
def test_send_uses_starttls_and_authentication() -> None:
    smtp = _FakeSMTP()
    config = build_email_config(_settings())
    notifier = EmailNotifier(config, smtp_factory=lambda _c: smtp)

    result = notifier.send_report(subject="[BizFluxLab] test", body="hello")

    assert result.delivered
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"]
    assert smtp.starttls_context is not None
    assert smtp.logged_in_with == ("ops@example.com", _PASSWORD)


def test_the_sent_message_never_contains_the_password() -> None:
    smtp = _FakeSMTP()
    notifier = EmailNotifier(build_email_config(_settings()), smtp_factory=lambda _c: smtp)

    notifier.send_report(subject="[BizFluxLab] test", body="operational body")

    rendered = smtp.messages[0].as_string()
    assert _PASSWORD not in rendered
    assert "owner@example.com" in rendered


def test_authentication_failure_reports_only_the_category() -> None:
    class _Failing(_FakeSMTP):
        def login(self, username, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    notifier = EmailNotifier(build_email_config(_settings()), smtp_factory=lambda _c: _Failing())

    result = notifier.send_report(subject="s", body="b")

    assert result.delivered is False
    assert result.detail == "SMTPAuthenticationError"
    assert "Password" not in (result.detail or "")


def test_a_transport_failure_does_not_raise() -> None:
    def _factory(_config):
        raise TimeoutError("connection timed out")

    notifier = EmailNotifier(build_email_config(_settings()), smtp_factory=_factory)

    result = notifier.send_report(subject="s", body="b")

    assert result.delivered is False
    assert result.detail == "TimeoutError"


def test_alert_body_is_sanitized() -> None:
    message = NotificationMessage(
        severity="warning",
        title="tracking check",
        summary="a link needs review",
        alert_type="AFFILIATE_TRACKING",
        fingerprint="fp-1",
        operations_run_id=7,
        article_id=10,
        evidence={
            "tracking_url": "https://example.com/go/secret-token",
            "smtp_password": _PASSWORD,
            "clicks": 3,
            "landing": "https://bizfluxlab.com/go/abc123",
        },
    )

    body = render_alert_body(message)

    assert _PASSWORD not in body
    assert "secret-token" not in body
    assert "/go/abc123" not in body
    assert "[redacted]" in body
    assert "clicks: 3" in body


def test_severity_subject_uses_the_prefix() -> None:
    config = build_email_config(_settings())

    assert config.severity_subject("warning", "Daily operations requires attention") == (
        "[BizFluxLab][WARNING] Daily operations requires attention"
    )
    assert config.subject("Weekly Operations Report - 2026-09-23") == (
        "[BizFluxLab] Weekly Operations Report - 2026-09-23"
    )
    # info は severity ラベルを付けない (健全な週次は無印)。
    assert config.severity_subject("info", "Weekly Operations Report") == (
        "[BizFluxLab] Weekly Operations Report"
    )


def test_the_email_notifier_joins_the_existing_providers() -> None:
    notifiers = build_notifiers(_settings(), smtp_factory=lambda _c: _FakeSMTP())

    assert [n.name for n in notifiers] == ["log", "email"]
