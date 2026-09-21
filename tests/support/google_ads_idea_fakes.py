"""Google Ads Keyword Ideas を実通信せずにテストするための fake。

- ``FakeIdeasClient``: SDK client の最小形 (``GenerateKeywordIdeas`` request の捕捉 / 応答 / 失敗)
- ``FakeIdeasPager``: SDK pager の挙動を模す。``results`` は最初のページのみを指し、それ以降の
  ページを **反復すると追加 request が発生する** (= ``extra_pages_fetched`` が増える)。
- ``FakeGoogleAdsException`` / ``fake_failure``: GoogleAdsException / grpc 風の失敗

実 credential は絶対に書かない。dummy 値のみ。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tests.support.google_ads_fakes import keyword_metrics


class _FakeKeywordSeed:
    def __init__(self) -> None:
        self.keywords: list[str] = []


class _FakeIdeasRequest:
    def __init__(self) -> None:
        self.customer_id: str | None = None
        self.language: str | None = None
        self.geo_target_constants: list[str] = []
        self.keyword_plan_network: str | None = None
        self.keyword_seed = _FakeKeywordSeed()
        self.page_size: int | None = None


class _FakeEnums:
    class KeywordPlanNetworkEnum:
        GOOGLE_SEARCH = "GOOGLE_SEARCH"


class FakeIdeasPager:
    def __init__(self, first_page: list[Any], extra_pages: list[list[Any]] | None = None) -> None:
        self.results = list(first_page)  # 最初のページ (SDK では __getattr__ で委譲される)
        self._extra_pages = [list(p) for p in (extra_pages or [])]
        self.extra_pages_fetched = 0

    def __iter__(self):
        yield from self.results
        for page in self._extra_pages:
            self.extra_pages_fetched += 1  # 次ページ = 追加 API request
            yield from page

    @property
    def pages(self):
        yield SimpleNamespace(results=self.results)
        for page in self._extra_pages:
            self.extra_pages_fetched += 1
            yield SimpleNamespace(results=page)


class FakeIdeasClient:
    """`get_service` が返すサービスも自分自身が兼ねる最小 fake。"""

    def __init__(self, *, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls = 0
        self.requests: list[_FakeIdeasRequest] = []
        self.enums = _FakeEnums()

    def get_service(self, name: str) -> FakeIdeasClient:
        assert name == "KeywordPlanIdeaService"
        return self

    def get_type(self, name: str) -> _FakeIdeasRequest:
        assert name == "GenerateKeywordIdeasRequest"
        return _FakeIdeasRequest()

    def generate_keyword_ideas(self, *, request: _FakeIdeasRequest) -> Any:
        self.calls += 1
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._response


def idea_row(text: str, metrics: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(text=text, keyword_idea_metrics=metrics)


def idea_row_with_metrics(text: str, **overrides: Any) -> SimpleNamespace:
    return idea_row(text, keyword_metrics(**overrides))


# --- 失敗の fake ---------------------------------------------------------------
class RefreshError(Exception):
    """google.auth.exceptions.RefreshError と同名 (分類は型名で行う)。"""


class FakeGoogleAdsException(Exception):
    """GoogleAdsException 風: ``failure.errors[].error_code`` の oneof 名で分類される。"""

    def __init__(self, oneof: str, message: str = "") -> None:
        super().__init__(message)
        code = SimpleNamespace(WhichOneof=lambda _field, _name=oneof: _name)
        self.failure = SimpleNamespace(errors=[SimpleNamespace(error_code=code)])


class FakeGrpcError(Exception):
    """grpc.RpcError 風: ``code().name`` の status 名で分類される。"""

    def __init__(self, status: str, message: str = "") -> None:
        super().__init__(message)
        self._status = status

    def code(self) -> SimpleNamespace:
        return SimpleNamespace(name=self._status)
