"""Search Console provider の narrow なインターフェース。

C0 では **実装を持たない** (Google client / OAuth / HTTP なし)。
テストは ``FakeSearchConsoleProvider`` を注入する。C1 で実 Google 実装を追加する。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from app.search_console.rows import SearchConsolePageRow, SearchConsoleQueryRow


@runtime_checkable
class SearchConsoleProvider(Protocol):
    """Search Console Search Analytics の read-only 取得を抽象化する。"""

    def fetch_page_daily(
        self, *, property_uri: str, start_date: date, end_date: date
    ) -> list[SearchConsolePageRow]:
        """dimensions = ["date", "page"] の日次行を返す。"""
        ...

    def fetch_query_daily(
        self, *, property_uri: str, start_date: date, end_date: date
    ) -> list[SearchConsoleQueryRow]:
        """dimensions = ["date", "page", "query"] の日次行を返す。"""
        ...
