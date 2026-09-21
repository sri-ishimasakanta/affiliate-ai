"""Google Ads Keyword Ideas 連携 (read-only)。

責務:
- ``KeywordPlanIdeaService.GenerateKeywordIdeas`` を **1 呼び出し = 1 request** で実行する
- 応答を SDK に依存しない DTO (正規化した keyword + 取得できた指標) へ変換する
- auth / quota / API エラーを **固定の安全な文言** へ写像する (credential・customer id・
  request id・SDK 内部メッセージは決して出さない)

DB へは一切触れない。keyword の追加・Signal 保存・スコア計算も行わない。language / location は
既存の Historical Metrics 連携と同じ設定 (``google_ads_language_id`` / ``google_ads_geo_target_id``)
を使う。応答 pager は **反復しない** (追加ページの request を発生させない)。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.config.settings import Settings
from app.exceptions import ExternalProviderError, ProviderNotConfiguredError
from app.keyword.normalizers.site_relevance import normalize_keyword
from app.keyword.providers.google_ads import (
    GoogleAdsKeywordMetrics,
    _enum_name,
    _map_monthly_volume,
    _opt_int,
    build_google_ads_client,
)

_PROVIDER = "google_ads"

#: Google Ads の keyword_seed は 1 request あたり最大 20 keyword。
MAX_SEEDS = 20
DEFAULT_MAX_RESULTS = 200
MAX_RESULTS_LIMIT = 1000

# --- 安全な固定文言 (SDK / credential 由来の文字列は一切含めない) ------------
MSG_AUTH = (
    "authentication failed: the OAuth refresh token was rejected or is invalid "
    "(re-authorise the Google Ads credentials)"
)
MSG_AUTHZ = "authorization failed: the developer token or account access was rejected"
MSG_QUOTA = "quota exhausted or rate limited; retry later"
MSG_REQUEST = "the API rejected the request (check seeds and parameters)"
MSG_API = "Google Ads API request failed"
MSG_CLIENT = "Failed to initialise Google Ads client"
MSG_BUDGET = "keyword-idea request budget exhausted"


class GoogleAdsAuthError(ExternalProviderError):
    """OAuth refresh token 拒否 / 認証・認可エラー (人間による再認可が必要)。"""


class GoogleAdsQuotaError(ExternalProviderError):
    """quota / rate limit。時間を置いて再実行する。"""


class KeywordIdeaBudgetError(ExternalProviderError):
    """呼び出し側が設定した request 上限を超えようとした (実 API 呼び出しは行わない)。"""


# --- DTO -------------------------------------------------------------------
@dataclass(frozen=True)
class KeywordIdeaRequestParams:
    customer_id: str
    seeds: tuple[str, ...]
    geo_target_constants: tuple[str, ...]
    language: str
    page_size: int
    keyword_plan_network: str = "GOOGLE_SEARCH"


@dataclass(frozen=True)
class GoogleAdsKeywordIdea:
    """正規化済み keyword と、API が返した場合のみ指標。"""

    keyword: str
    metrics: GoogleAdsKeywordMetrics | None = None


# --- エラー分類 -------------------------------------------------------------
_ERROR_KIND_BY_ONEOF = {
    "authentication_error": "auth",
    "oauth_error": "auth",
    "authorization_error": "authz",
    "quota_error": "quota",
    "request_error": "request",
}
_ERROR_KIND_BY_GRPC = {
    "UNAUTHENTICATED": "auth",
    "PERMISSION_DENIED": "authz",
    "RESOURCE_EXHAUSTED": "quota",
    "INVALID_ARGUMENT": "request",
}
_MESSAGE_BY_KIND = {
    "auth": MSG_AUTH,
    "authz": MSG_AUTHZ,
    "quota": MSG_QUOTA,
    "request": MSG_REQUEST,
}
_MAX_CAUSE_DEPTH = 6


def _exception_chain(exc: BaseException):
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < _MAX_CAUSE_DEPTH:
        yield current
        current = current.__cause__ or current.__context__
        seen += 1


def _as_protobuf(code: object) -> object | None:
    """``ErrorCode`` を protobuf message として返す (proto-plus wrapper / 生 protobuf の両対応)。

    SDK が返す ``error_code`` は use_proto_plus=True では **proto-plus wrapper** で、
    ``WhichOneof`` を直接は持たない。protobuf 表現 (``ErrorCode.pb(code)``) から読む。
    """

    if code is None:
        return None
    if callable(getattr(code, "WhichOneof", None)):  # 生の protobuf message
        return code
    pb = getattr(type(code), "pb", None)
    if callable(pb):  # proto-plus message -> 対応する protobuf message
        return pb(code)
    return getattr(code, "_pb", None)  # proto-plus の内部 protobuf (pb() が無い場合)


def _error_code_oneof(code: object) -> str | None:
    """``ErrorCode`` の oneof 名 (例: ``authorization_error``) を返す。

    分類は best effort で、失敗しても例外は出さない (None = 分類不能 -> generic な API エラー)。
    """

    try:
        message = _as_protobuf(code)
        which = getattr(message, "WhichOneof", None)
        return which("error_code") if callable(which) else None
    except Exception:  # noqa: BLE001 - 分類のための best effort
        return None


_ENUM_CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _authorization_enum_name(code: object) -> str | None:
    """``AuthorizationError`` の enum 定数名 (例: ``USER_PERMISSION_DENIED``) だけを返す。

    値は **SDK の enum descriptor** (固定の定数名) から引く。error message / request id /
    customer id など実行時の文字列は一切読まない。定数名の形式 (大文字 + 数字 + ``_``) でない
    もの、descriptor に無い (未知の) 値は None (= 何も付けない)。
    """

    try:
        message = _as_protobuf(code)
        if message is None:
            return None
        field = message.DESCRIPTOR.fields_by_name["authorization_error"]
        value = getattr(message, "authorization_error")  # noqa: B009 - protobuf field
        name = field.enum_type.values_by_number[int(value)].name
    except Exception:  # noqa: BLE001 - 診断の best effort。失敗しても何も付けない
        return None
    return name if isinstance(name, str) and _ENUM_CONSTANT.match(name) else None


def authorization_subcode(exc: BaseException) -> str | None:
    """Google Ads ``AuthorizationError`` の enum 定数名 (無ければ None)。"""

    for item in _exception_chain(exc):
        failure = getattr(item, "failure", None)
        for error in getattr(failure, "errors", None) or []:
            code = getattr(error, "error_code", None)
            if _error_code_oneof(code) == "authorization_error":
                name = _authorization_enum_name(code)
                if name:
                    return name
    return None


def classify_failure(exc: BaseException) -> str:
    """例外から 'auth' | 'authz' | 'quota' | 'request' | 'api' を返す (文言は返さない)。"""

    for item in _exception_chain(exc):
        if type(item).__name__ == "RefreshError":
            return "auth"
        failure = getattr(item, "failure", None)
        for error in getattr(failure, "errors", None) or []:
            name = _error_code_oneof(getattr(error, "error_code", None))
            kind = _ERROR_KIND_BY_ONEOF.get(str(name)) if name else None
            if kind:
                return kind
        grpc_code = getattr(item, "code", None)
        if callable(grpc_code):
            try:
                status = getattr(grpc_code(), "name", None)
            except Exception:  # noqa: BLE001 - 分類のための best effort
                status = None
            kind = _ERROR_KIND_BY_GRPC.get(str(status)) if status else None
            if kind:
                return kind
    return "api"


def safe_failure_error(exc: BaseException, *, default: str = MSG_API) -> ExternalProviderError:
    """例外を固定文言の :class:`ExternalProviderError` (サブクラス) に写像する。"""

    kind = classify_failure(exc)
    message = _MESSAGE_BY_KIND.get(kind, default)
    if kind == "authz":
        # 認可エラーだけ、固定の enum 定数名 (例: USER_PERMISSION_DENIED) を診断用に添える。
        subcode = authorization_subcode(exc)
        if subcode:
            message = f"{message} ({subcode})"
    if kind in ("auth", "authz"):
        return GoogleAdsAuthError(_PROVIDER, message)
    if kind == "quota":
        return GoogleAdsQuotaError(_PROVIDER, message)
    return ExternalProviderError(_PROVIDER, message)


# --- provider --------------------------------------------------------------
def _clean_seeds(seeds: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in seeds:
        if not isinstance(raw, str):
            raise ValueError("seeds must be strings")
        value = raw.strip()
        key = normalize_keyword(value)
        if not value or key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    if not cleaned:
        raise ValueError("seeds must contain at least one non-empty keyword")
    if len(cleaned) > MAX_SEEDS:
        raise ValueError(f"at most {MAX_SEEDS} seed keywords are allowed per request")
    return tuple(cleaned)


def _validate_max_results(max_results: int) -> int:
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ValueError("max_results must be an integer")
    if not 1 <= max_results <= MAX_RESULTS_LIMIT:
        raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_LIMIT}")
    return max_results


class GoogleAdsKeywordIdeaProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        client: object | None = None,
        max_requests: int | None = None,
    ) -> None:
        self._settings = settings
        self._client = client  # 明示注入時はそれを使う (テスト用)。None なら遅延生成。
        self._max_requests = max_requests
        self._request_count = 0

    @property
    def request_count(self) -> int:
        """実際に API へ送った keyword-idea request の数。"""

        return self._request_count

    # -- public ---------------------------------------------------------
    def build_request_params(
        self, seeds: Sequence[str], *, max_results: int = DEFAULT_MAX_RESULTS
    ) -> KeywordIdeaRequestParams:
        if not self._settings.google_ads_configured:
            raise ProviderNotConfiguredError(_PROVIDER)
        return KeywordIdeaRequestParams(
            customer_id=str(self._settings.google_ads_customer_id),
            seeds=_clean_seeds(seeds),
            geo_target_constants=(f"geoTargetConstants/{self._settings.google_ads_geo_target_id}",),
            language=f"languageConstants/{self._settings.google_ads_language_id}",
            page_size=_validate_max_results(max_results),
        )

    def generate_keyword_ideas(
        self, seeds: Sequence[str], *, max_results: int = DEFAULT_MAX_RESULTS
    ) -> list[GoogleAdsKeywordIdea]:
        """seed keyword から idea を 1 request で取得する。"""

        params = self.build_request_params(seeds, max_results=max_results)
        if self._max_requests is not None and self._request_count >= self._max_requests:
            raise KeywordIdeaBudgetError(_PROVIDER, MSG_BUDGET)

        client = self._client if self._client is not None else self._acquire_client()
        self._request_count += 1
        try:
            response = self._call_api(client, params)
        except (ProviderNotConfiguredError, ExternalProviderError):
            raise
        except Exception as exc:  # SDK/credential/token 等の内部詳細は露出させない
            raise safe_failure_error(exc) from exc
        return self._map_response(response, limit=params.page_size)

    # -- internal -------------------------------------------------------
    def _acquire_client(self) -> object:
        try:
            return build_google_ads_client(self._settings)
        except ProviderNotConfiguredError:
            raise
        except ExternalProviderError as exc:
            # 元例外 (RefreshError 等) を分類し、固定の安全な文言だけを返す。
            raise safe_failure_error(exc, default=MSG_CLIENT) from exc

    def _call_api(self, client: Any, params: KeywordIdeaRequestParams) -> Any:
        service = client.get_service("KeywordPlanIdeaService")
        request = client.get_type("GenerateKeywordIdeasRequest")
        request.customer_id = params.customer_id
        request.language = params.language
        request.geo_target_constants.extend(params.geo_target_constants)
        request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
        request.keyword_seed.keywords.extend(params.seeds)
        request.page_size = params.page_size
        return service.generate_keyword_ideas(request=request)

    def _map_response(self, response: Any, *, limit: int) -> list[GoogleAdsKeywordIdea]:
        # pager は反復しない (反復すると次ページの API request が発生する)。``results`` は
        # 最初のページのみを指す。
        rows = list(getattr(response, "results", None) or [])[:limit]
        ideas: list[GoogleAdsKeywordIdea] = []
        seen: set[str] = set()
        for row in rows:
            text = normalize_keyword(str(getattr(row, "text", "") or ""))
            if not text or text in seen:
                continue
            seen.add(text)
            metrics = getattr(row, "keyword_idea_metrics", None)
            ideas.append(
                GoogleAdsKeywordIdea(
                    keyword=text,
                    metrics=self._map_metrics(text, metrics) if metrics is not None else None,
                )
            )
        return ideas

    @staticmethod
    def _map_metrics(text: str, metrics: Any) -> GoogleAdsKeywordMetrics:
        volumes = getattr(metrics, "monthly_search_volumes", None) or []
        return GoogleAdsKeywordMetrics(
            keyword=text,
            avg_monthly_searches=_opt_int(getattr(metrics, "avg_monthly_searches", None)),
            monthly_search_volumes=tuple(_map_monthly_volume(v) for v in volumes),
            competition=_enum_name(getattr(metrics, "competition", None)),
            competition_index=_opt_int(getattr(metrics, "competition_index", None)),
            low_top_of_page_bid_micros=_opt_int(
                getattr(metrics, "low_top_of_page_bid_micros", None)
            ),
            high_top_of_page_bid_micros=_opt_int(
                getattr(metrics, "high_top_of_page_bid_micros", None)
            ),
        )
