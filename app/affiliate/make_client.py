"""Make affiliate API への最小 read-only HTTP client (Phase E1, envelope/zone
契約は Phase E1.2 で公式ドキュメントに合わせて修正)。

scope:
- ``GET {base}/affiliate/commissions`` (厳格検証 + :class:`AffiliateCommissionFact`
  取り込みの入力)
- ``GET {base}/affiliate/commission-info`` (軽量検証 read-only。現時点では DB に
  永続化しない -- 呼び出し側が必要になった時点で別フェーズで配線する)
- ``GET {base}/affiliate/stats`` (同上)

payout を実行する POST エンドポイントは Make 公式契約にも本フェーズのスコープにも
存在しない -- このモジュールは **GET 以外のメソッドを一切公開しない**。

zone (Phase E1.2):
Make API の base URL は zone 依存 (``https://eu1.make.com/api/v2`` /
``https://eu2.make.com/api/v2`` / ``https://us1.make.com/api/v2`` 等) で、
``/api/v2`` を含む。この client はエンドポイント path 定数に ``/api/v2`` を
含めない (``/affiliate/commissions`` のみ) -- ``MAKE_API_BASE_URL`` 自体が
既に zone-specific な ``/api/v2`` までを含む前提であり、ここで固定 zone
(eu1 等) をハードコードしたり ``/api/v2`` を二重に付与したりしない。

認証は ``Authorization: Token <api-token>`` ヘッダのみ (HMAC 署名は不要 -- Make 側の
契約)。token は :class:`app.config.settings.Settings` からのみ読み、このモジュールの
外へ値を構築・露出しない。エラーメッセージ・repr・ログには一切含めない。

自動リトライはしない (semantic failure を隠さないため)。redirect は追わない。
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from app.affiliate.make_commission_rows import (
    MakeCommissionRow,
    validate_make_commissions_page,
)
from app.config.settings import Settings
from app.exceptions import AffiliateCommissionImportError, ProviderNotConfiguredError

_PROVIDER = "make"
_TIMEOUT_SECONDS = 15.0
# MAKE_API_BASE_URL 自体が zone-specific な "/api/v2" までを含む前提 -- ここでは
# 付与しない (二重付与防止、§5)。
_COMMISSIONS_PATH = "/affiliate/commissions"
_COMMISSION_INFO_PATH = "/affiliate/commission-info"
_STATS_PATH = "/affiliate/stats"

#: Make commissions ページネーションの安全上限 (unbounded loop 防止)。
MAKE_COMMISSIONS_MAX_PAGES = 200
MAKE_COMMISSIONS_MAX_LIMIT = 500


class MakeAffiliateClient:
    """Make affiliate API への read-only GET client。書き込みメソッドは持たない。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.make_api_configured:
            raise ProviderNotConfiguredError(_PROVIDER)
        base_url = (settings.make_api_base_url or "").strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise AffiliateCommissionImportError(
                "Make API client requires an https base URL"
            )
        self._base_url = base_url
        self._token = settings.make_api_token or ""
        self._transport = transport

    @property
    def target_base_url(self) -> str:
        return self._base_url

    # -- commissions (validated, used for import) --------------------------
    def get_commissions(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        status_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ) -> tuple[list[MakeCommissionRow], bool]:
        """1 ページの commission 行を取得・検証する。``(rows, has_more)`` を返す。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or not (
            1 <= limit <= MAKE_COMMISSIONS_MAX_LIMIT
        ):
            raise AffiliateCommissionImportError(
                f"limit must be between 1 and {MAKE_COMMISSIONS_MAX_LIMIT}"
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise AffiliateCommissionImportError("offset must be a non-negative integer")

        params: dict[str, str | int] = {"pg[offset]": offset, "pg[limit]": limit}
        if date_from is not None:
            params["dateFrom"] = date_from
        if date_to is not None:
            params["dateTo"] = date_to
        if status_id is not None:
            params["statusId"] = status_id
        if sort_by is not None:
            params["pg[sortBy]"] = sort_by
        if sort_dir is not None:
            params["pg[sortDir]"] = sort_dir

        response = self._send("GET", _COMMISSIONS_PATH, params=params)
        _check_status(response)
        payload = _parse_json(response)
        return validate_make_commissions_page(payload, requested_limit=limit)

    # -- lightweight read-only methods (no DB persistence yet) -------------
    def get_commission_info(
        self, *, date_from: str | None = None, date_to: str | None = None
    ) -> dict:
        """軽量検証のみ (JSON object であることのみ確認する)。フィールド単位の型は
        Make 公式ドキュメントで未確定 (``earningsRange`` 等の正確な shape が不明)
        のため、確実な数値フィールド以外は推測して型付けしない -- 呼び出し側が
        必要な値だけを安全に読む。"""

        params: dict[str, str] = {}
        if date_from is not None:
            params["dateFrom"] = date_from
        if date_to is not None:
            params["dateTo"] = date_to
        response = self._send("GET", _COMMISSION_INFO_PATH, params=params)
        _check_status(response)
        payload = _parse_json(response)
        if not isinstance(payload, dict):
            raise AffiliateCommissionImportError(
                "commission-info response is not a JSON object", http_status=200
            )
        return payload

    def get_stats(
        self, *, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict]:
        """軽量検証のみ。公式契約の envelope ``{"stats": [...]}`` を要求する
        (bare array は受け付けない -- commissions と同じ理由、§1)。"""

        params: dict[str, str] = {}
        if date_from is not None:
            params["dateFrom"] = date_from
        if date_to is not None:
            params["dateTo"] = date_to
        response = self._send("GET", _STATS_PATH, params=params)
        _check_status(response)
        payload = _parse_json(response)
        if not isinstance(payload, dict):
            raise AffiliateCommissionImportError(
                "stats response is not a JSON object", http_status=200
            )
        rows = payload.get("stats")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise AffiliateCommissionImportError(
                "stats response is missing a 'stats' array", http_status=200
            )
        return rows

    # -- transport ----------------------------------------------------------
    def _send(
        self, method: str, path: str, *, params: dict | None = None
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Token {self._token}"}
        try:
            with httpx.Client(
                transport=self._transport,
                verify=True,
                timeout=_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                return client.request(method, url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise AffiliateCommissionImportError(
                "Make API request timed out"
            ) from exc
        except httpx.TransportError as exc:
            raise AffiliateCommissionImportError(
                "Make API connection failed"
            ) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - httpx 内部の他エラー
            raise AffiliateCommissionImportError(
                "Make API request failed"
            ) from exc


def _check_status(response: httpx.Response) -> None:
    if response.is_redirect:
        raise AffiliateCommissionImportError("Make API returned a redirect")
    if response.status_code != 200:
        raise AffiliateCommissionImportError(
            f"Make API returned HTTP {response.status_code}",
            http_status=response.status_code,
        )


def _parse_json(response: httpx.Response) -> object:
    """JSON body を parse する。``parse_float=Decimal`` により、JSON の小数値は
    binary float を経由せず、その lexical 表現から直接 ``Decimal`` になる
    (Phase E1.1 -- ``Decimal(float_value)`` の丸め誤差混入を構造的に避ける)。
    """

    try:
        return response.json(parse_float=Decimal)
    except ValueError as exc:
        raise AffiliateCommissionImportError(
            "Make API returned a non-JSON 200 response", http_status=200
        ) from exc
