"""運用メールの送信プロバイダ (C8.7)。

既存の :class:`~app.operations.notifications.Notifier` を置き換えず、**追加の
プロバイダ** として並べる。ログ通知は常に残るので、メールが落ちても運用記録は
失われない。

安全上の約束 (Webhook と同じ水準を守る):

- password は :class:`EmailConfig` の外へ出さない。``describe()`` は「設定済みか」
  だけを返し、値も長さも出さない。
- 例外メッセージは種別だけを残し、SMTP の応答本文はそのまま載せない
  (認証失敗の応答に資格情報が混ざりうるため)。
- 本文に載せるのは :func:`sanitize_payload` を通した運用情報だけ。tracking URL や
  ``/go/`` token は最終防壁で落ちる。
- TLS は必須。検証を無効化するスイッチは **作らない**。STARTTLS に失敗したら
  平文へ落ちずに諦める。
- 設定が揃っていなければ **SMTP に接続しない**。理由を sanitized な文字列で返す。
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate

from app.operations.notifications import (
    NotificationMessage,
    NotificationResult,
    sanitize_payload,
)

#: SMTP の待ち時間。無期限に待たない (スケジューラを止めないため)。
SMTP_TIMEOUT_SECONDS = 20.0

PROVIDER_NAME = "email"


class EmailConfigError(Exception):
    """設定が不足している。**接続を試みる前に** 失敗させるための例外。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"operations email is not configured: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class EmailConfig:
    """検証済みの SMTP 設定。password はここから外へ出ない。"""

    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    use_starttls: bool
    subject_prefix: str
    username: str | None = None
    password: str | None = None

    @property
    def authenticates(self) -> bool:
        return bool(self.username and self.password)

    def describe(self) -> dict:
        """人が設定を確認するための表現。**password は含めない**。"""

        return {
            "enabled": True,
            "smtp_host": self.host,
            "smtp_port": self.port,
            "use_starttls": self.use_starttls,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "username_configured": bool(self.username),
            "password_configured": bool(self.password),
            "subject_prefix": self.subject_prefix,
        }

    def subject(self, text: str) -> str:
        return f"[{self.subject_prefix}] {text}" if self.subject_prefix else text

    def severity_subject(self, severity: str, text: str) -> str:
        label = (severity or "").upper()
        if not label or label == "INFO":
            return self.subject(text)
        return (
            f"[{self.subject_prefix}][{label}] {text}"
            if self.subject_prefix
            else f"[{label}] {text}"
        )


def build_email_config(settings) -> EmailConfig:
    """Settings から検証済みの設定を作る。足りなければ送らずに落とす。"""

    if not getattr(settings, "operations_email_enabled", False):
        raise EmailConfigError("OPERATIONS_EMAIL_ENABLED is false")

    host = (getattr(settings, "operations_email_smtp_host", None) or "").strip()
    if not host:
        raise EmailConfigError("OPERATIONS_EMAIL_SMTP_HOST is missing")

    try:
        port = int(getattr(settings, "operations_email_smtp_port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    if not (0 < port < 65536):
        raise EmailConfigError("OPERATIONS_EMAIL_SMTP_PORT must be between 1 and 65535")

    sender = (getattr(settings, "operations_email_from", None) or "").strip()
    if "@" not in sender:
        raise EmailConfigError("OPERATIONS_EMAIL_FROM must be an email address")

    recipients = tuple(getattr(settings, "operations_email_recipients", ()) or ())
    if not recipients:
        raise EmailConfigError("OPERATIONS_EMAIL_TO must list at least one recipient")
    invalid = [r for r in recipients if "@" not in r]
    if invalid:
        raise EmailConfigError(f"OPERATIONS_EMAIL_TO has {len(invalid)} malformed address(es)")

    username = (getattr(settings, "operations_email_username", None) or "").strip() or None
    password = getattr(settings, "operations_email_password", None) or None
    if username and not password:
        raise EmailConfigError("OPERATIONS_EMAIL_PASSWORD is required when a username is set")
    if password and not username:
        raise EmailConfigError("OPERATIONS_EMAIL_USERNAME is required when a password is set")

    use_starttls = bool(getattr(settings, "operations_email_use_starttls", True))
    if not use_starttls and port != 465:
        # 平文で資格情報を投げさせない。465 は暗黙 TLS なので STARTTLS 不要。
        raise EmailConfigError(
            "STARTTLS may only be disabled for implicit TLS on port 465; "
            "plaintext SMTP is not supported"
        )

    return EmailConfig(
        host=host,
        port=port,
        sender=sender,
        recipients=recipients,
        use_starttls=use_starttls,
        subject_prefix=(getattr(settings, "operations_email_subject_prefix", "") or "").strip(),
        username=username,
        password=password,
    )


def describe_email_settings(settings) -> dict:
    """設定状況の read-only な説明。未設定でも例外にしない。**password は出さない**。"""

    try:
        return build_email_config(settings).describe()
    except EmailConfigError as exc:
        return {
            "enabled": bool(getattr(settings, "operations_email_enabled", False)),
            "configured": False,
            "reason": exc.reason,
            "smtp_host": (getattr(settings, "operations_email_smtp_host", None) or "") or None,
            "smtp_port": getattr(settings, "operations_email_smtp_port", None),
            "use_starttls": bool(getattr(settings, "operations_email_use_starttls", True)),
            "sender": (getattr(settings, "operations_email_from", None) or "") or None,
            "recipients": list(getattr(settings, "operations_email_recipients", ()) or ()),
            "username_configured": bool(getattr(settings, "operations_email_username", None)),
            "password_configured": bool(getattr(settings, "operations_email_password", None)),
        }


def render_alert_body(message: NotificationMessage) -> str:
    """アラート 1 件の本文 (text/plain)。巨大な JSON は載せない。"""

    payload = message.as_payload()
    lines = [
        f"severity        : {payload['severity']}",
        f"alert type      : {payload['alert_type']}",
        f"title           : {payload['title']}",
        "",
        payload["summary"],
        "",
        f"operations run  : {payload.get('operations_run_id')}",
        f"observed at     : {payload.get('occurred_at')}",
        f"source          : {payload.get('fingerprint')}",
    ]
    if payload.get("article_id"):
        slug = payload.get("article_slug") or ""
        lines.append(f"article         : {payload['article_id']} {slug}".rstrip())
    if payload.get("affiliate_program_id"):
        lines.append(f"affiliate program: {payload['affiliate_program_id']}")

    evidence = payload.get("evidence") or {}
    if evidence:
        lines.append("")
        lines.append("evidence:")
        lines.extend(_evidence_lines(evidence))

    lines += [
        "",
        "suggested check:",
        "  uv run python scripts/run_operations.py --profile daily   (plan)",
        "  uv run python scripts/report_seo_improvement_candidates.py",
        "  uv run python scripts/report_revenue_optimization_candidates.py",
        "",
        "このメールは運用情報のみを含む。認証情報・tracking URL・/go/ token は含まない。",
    ]
    return "\n".join(lines)


def _evidence_lines(evidence: dict, *, limit: int = 20) -> list[str]:
    """evidence を 1 行 1 項目で出す。長い値は切る (携帯で読めるように)。"""

    lines: list[str] = []
    for key, value in list(evidence.items())[:limit]:
        text = str(value)
        if len(text) > 160:
            text = text[:157] + "..."
        lines.append(f"  {key}: {text}")
    if len(evidence) > limit:
        lines.append(f"  ... ({len(evidence) - limit} more; see the operations run record)")
    return lines


class EmailNotifier:
    """設定済みのときだけ有効になる SMTP プロバイダ。

    ``smtp_factory`` はテスト用の注入口で、本番では :mod:`smtplib` を使う。
    テストが実 SMTP に接続することは無い。
    """

    name = PROVIDER_NAME

    def __init__(self, config: EmailConfig, *, smtp_factory=None) -> None:
        self._config = config
        self._smtp_factory = smtp_factory or _default_smtp_factory

    # -- public ---------------------------------------------------------------
    def send(self, message: NotificationMessage) -> NotificationResult:
        """アラート 1 件を送る (:class:`Notifier` プロトコル)。"""

        subject = self._config.severity_subject(message.severity, sanitize_payload(message.title))
        return self.send_report(subject=subject, body=render_alert_body(message))

    def send_report(
        self, *, subject: str, body: str, html_body: str | None = None
    ) -> NotificationResult:
        """任意の本文を送る (日次インシデント / 週次レポート / 承認依頼 共通)。

        ``html_body`` を渡したときだけ multipart になる。**plain text が本体**で、
        HTML は補助である (携帯で HTML が落ちても読める)。
        """

        mail = EmailMessage()
        mail["Subject"] = subject
        mail["From"] = self._config.sender
        mail["To"] = ", ".join(self._config.recipients)
        mail["Date"] = formatdate(localtime=True)
        mail.set_content(body)
        if html_body:
            mail.add_alternative(html_body, subtype="html")

        try:
            self._deliver(mail)
        except smtplib.SMTPAuthenticationError:
            # 応答本文は載せない (資格情報が混ざりうる)。
            return NotificationResult(self.name, False, "SMTPAuthenticationError")
        except Exception as exc:  # noqa: BLE001 - 送信失敗は上位を壊さない
            return NotificationResult(self.name, False, type(exc).__name__)
        return NotificationResult(self.name, True, f"sent to {len(self._config.recipients)}")

    # -- internals ------------------------------------------------------------
    def _deliver(self, mail: EmailMessage) -> None:
        config = self._config
        smtp = self._smtp_factory(config)
        try:
            if config.use_starttls:
                smtp.ehlo()
                # 証明書検証は既定のまま。無効化する経路は用意しない。
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if config.authenticates:
                smtp.login(config.username, config.password)
            smtp.send_message(mail)
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001 - 切断失敗は送信結果を変えない
                pass


def _default_smtp_factory(config: EmailConfig):
    if config.use_starttls:
        return smtplib.SMTP(config.host, config.port, timeout=SMTP_TIMEOUT_SECONDS)
    # STARTTLS を使わないのは 465 の暗黙 TLS のみ (build_email_config が保証)。
    return smtplib.SMTP_SSL(
        config.host,
        config.port,
        timeout=SMTP_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )


def build_email_notifier(settings, *, smtp_factory=None) -> EmailNotifier | None:
    """設定が揃っていれば notifier を作る。揃っていなければ ``None``。"""

    try:
        config = build_email_config(settings)
    except EmailConfigError:
        return None
    return EmailNotifier(config, smtp_factory=smtp_factory)
