"""Google Ads SDK の failure / request ログを抑止する (識別子の stdout/stderr 漏洩防止)。

SDK の ``LoggingInterceptor`` は失敗した request ごとに
``Request made: ClientCustomerId: <customer id>, ... RequestId: <request id>, FaultMessage: ...``
を logger ``google.ads.googleads.client`` へ WARNING で出力する。logging が未設定だと Python の
lastResort handler が stderr に出すため、customer id / request id がそのまま console に漏れる。

ここでは **SDK の logger だけ** に「全 record を捨てる」filter を付ける。アプリケーション自身の
logger や root logger、他ライブラリの logger には一切触れない。filter は logger 自体で評価される
ので、上位 logger の handler (root / pytest caplog / lastResort) にも record は届かない。
冪等で、何度呼んでも filter は 1 つだけ。
"""

from __future__ import annotations

import logging

#: SDK が識別子を含みうる record を出す logger。``client`` は LoggingInterceptor に渡される
#: logger (request / fault の要約と詳細)、``config`` は設定読み込み時の警告、
#: ``interceptors.logging_interceptor`` は将来 SDK が interceptor 自身の logger を使う場合の備え。
SDK_LOGGER_NAMES: tuple[str, ...] = (
    "google.ads.googleads.client",
    "google.ads.googleads.config",
    "google.ads.googleads.interceptors.logging_interceptor",
)


class _DropAllRecords(logging.Filter):
    """record を破棄する (redact ではなく suppress: SDK の要約には再利用したい情報が無い)。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        return False


def suppress_google_ads_sdk_logging() -> None:
    """SDK の logger に drop filter を (未設定なら) 付ける。"""

    for name in SDK_LOGGER_NAMES:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _DropAllRecords) for f in logger.filters):
            logger.addFilter(_DropAllRecords())
