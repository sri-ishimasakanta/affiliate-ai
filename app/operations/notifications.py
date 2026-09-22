"""通知プロバイダ (C8)。

外部の通知サービスを **前提にしない**。既定は常に使えるログ通知で、Webhook は
明示的に設定されたときだけ有効になる。

安全上の約束:

- Webhook URL を **DB にもログにも出力しない** (設定済みかどうかだけを示す)。
- 送信する payload は運用情報のみ。credential / service account / アフィリエイトの
  tracking URL / ``/go/`` token は決して含めない (:func:`sanitize_payload` が
  最終防壁として落とす)。
- 通知の失敗は取り込みや候補評価を壊さない。送信結果は別の事実として返すだけで、
  例外を上位に投げない。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

_TIMEOUT_SECONDS = 15.0

#: payload から必ず落とすキー (部分一致)。
_FORBIDDEN_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "private_key",
    "authorization",
    "api_key",
    "tracking_url",
    "webhook",
)
#: 値に含まれていたら伏せる URL パターン (/go/ の token と ASP の tracking URL)。
_FORBIDDEN_VALUE_RE = re.compile(r"https?://[^\s\"']*/go/[^\s\"']+", re.IGNORECASE)
_REDACTED = "[redacted]"


@dataclass
class NotificationMessage:
    """通知 1 件。運用情報だけを持つ。"""

    severity: str
    title: str
    summary: str
    alert_type: str
    fingerprint: str
    operations_run_id: int | None = None
    article_id: int | None = None
    article_slug: str | None = None
    affiliate_program_id: int | None = None
    occurred_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["occurred_at"] = self.occurred_at.isoformat() if self.occurred_at else None
        return sanitize_payload(payload)


@dataclass(frozen=True)
class NotificationResult:
    provider: str
    delivered: bool
    detail: str | None = None


class Notifier(Protocol):
    name: str

    def send(self, message: NotificationMessage) -> NotificationResult: ...


def sanitize_payload(value: Any) -> Any:
    """secret になりうるキー/値を再帰的に落とす (最終防壁)。"""

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in _FORBIDDEN_KEY_PARTS):
                out[str(key)] = _REDACTED
            else:
                out[str(key)] = sanitize_payload(item)
        return out
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return _FORBIDDEN_VALUE_RE.sub(_REDACTED, value)
    return value


class LogNotifier:
    """常に使える既定のプロバイダ。標準出力へ 1 行で書く。"""

    name = "log"

    def __init__(self, writer=print) -> None:
        self._writer = writer

    def send(self, message: NotificationMessage) -> NotificationResult:
        payload = message.as_payload()
        try:
            self._writer(
                f"[ALERT {payload['severity'].upper()}] {payload['alert_type']} "
                f"{payload['title']} :: {payload['summary']}"
            )
        except Exception as exc:  # noqa: BLE001 - 通知失敗は上位を壊さない
            return NotificationResult(self.name, False, type(exc).__name__)
        return NotificationResult(self.name, True)


class WebhookNotifier:
    """設定済みのときだけ有効になる汎用 Webhook。

    URL は :class:`Settings` から読むだけで、保存も出力もしない。
    """

    name = "webhook"

    def __init__(self, url: str, *, http_client: httpx.Client | None = None) -> None:
        self._url = url
        self._http_client = http_client

    def send(self, message: NotificationMessage) -> NotificationResult:
        owns = self._http_client is None
        http = self._http_client or httpx.Client(timeout=_TIMEOUT_SECONDS)
        try:
            response = http.post(self._url, json=message.as_payload())
        except Exception as exc:  # noqa: BLE001 - 送信失敗は上位を壊さない
            return NotificationResult(self.name, False, type(exc).__name__)
        finally:
            if owns:
                http.close()
        if 200 <= response.status_code < 300:
            return NotificationResult(self.name, True, f"HTTP {response.status_code}")
        # 応答本文は載せない (URL / secret が混ざりうる)。
        return NotificationResult(self.name, False, f"HTTP {response.status_code}")


def build_notifiers(settings, *, writer=print, http_client=None) -> list[Notifier]:
    """設定から利用可能なプロバイダを作る。Webhook は未設定なら **作らない**。"""

    notifiers: list[Notifier] = [LogNotifier(writer=writer)]
    url = getattr(settings, "operations_webhook_url", None)
    if isinstance(url, str) and url.strip().startswith("https://"):
        notifiers.append(WebhookNotifier(url.strip(), http_client=http_client))
    return notifiers
