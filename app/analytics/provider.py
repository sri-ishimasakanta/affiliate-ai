"""GA4 provider の Protocol (import service が依存する境界)。

import service は具象 HTTP client ではなくこの Protocol にのみ依存する。テストは
fake provider を注入して、ネットワークなしで取り込み経路全体を検証できる。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from app.analytics.rows import Ga4PageRow


class Ga4Provider(Protocol):
    def fetch_page_daily(
        self, *, property_id: str, start_date: date, end_date: date
    ) -> Sequence[Ga4PageRow]:
        """全トラフィックとオーガニック検索の両 scope の行を返す。"""

    def fetch_property_timezone(self, *, property_id: str) -> str | None:
        """property のタイムゾーン (取得できなければ ``None``)。"""
