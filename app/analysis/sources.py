"""出所 (source) の状態の共通の形と規則 (C10-A、pure)。

分析のどの部分でも同じ言葉で「その出所のデータは使えるか」を言うための共通の契約:

- ``SourceStatus``: 出所・提供元・最後の観測 (``observed_at``)・最後の試み (``collected_at``)・
  どの日まで届いたか (``data_through``)・遅れの目安・鮮度の状態・欠けている理由・品質の印・
  出どころ (``provenance``)・手に入れ方 (``access``)。
- 鮮度の状態 (``FRESHNESS_STATES``): ``fresh`` / ``waiting`` / ``stale`` / ``missing`` /
  ``insufficient`` / ``provider_error``。**値が無いことを 0 にしない。古い値を最新として黙って
  使わない** (``usable`` は fresh / waiting だけ)。
- 既存の言葉との対応 (変えない): C9 の証拠の鮮度 ``fresh`` / ``stale`` / ``unavailable``
  (``legacy_state``。運用の ``evaluate_source_refresh`` と同じ判定)、C9-C の窓の
  ``waiting`` / ``stale_data``。
- 数字 (遅れ・古さの境) は ``operations_policy.json`` の ``imports`` (取り込みの出所) と
  ``source_policy.json`` (それ以外) にだけ置く。
- ``access``: ``local`` (手元のデータから導く) / ``cached`` (外のデータを既に保存してある。
  読むのに呼び出しは要らない) / ``unavailable``。``needs_external_refresh`` は、古い・無い
  ときに新しいデータを得るには外の取り込みが要ること (分析はそれを呼ばない)。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

FRESH = "fresh"
WAITING = "waiting"
STALE = "stale"
MISSING = "missing"
INSUFFICIENT = "insufficient"
PROVIDER_ERROR = "provider_error"
FRESHNESS_STATES = (FRESH, WAITING, STALE, MISSING, INSUFFICIENT, PROVIDER_ERROR)
USABLE_STATES = frozenset({FRESH, WAITING})

ACCESS_LOCAL = "local"
ACCESS_CACHED = "cached"
ACCESS_UNAVAILABLE = "unavailable"

#: C9 の証拠の鮮度の言葉 (変えない)。
LEGACY_FRESH = "fresh"
LEGACY_STALE = "stale"
LEGACY_UNAVAILABLE = "unavailable"

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "source_policy.json"
SOURCES = ("search_console", "ga4", "affiliate_clicks", "make_commissions", "google_ads",
           "threads_insights", "url_inspection")  # fmt: skip


@lru_cache(maxsize=1)
def load_source_policy(path: str | None = None) -> dict:
    return json.loads(Path(path or POLICY_PATH).read_text(encoding="utf-8"))


def source_definition(name: str) -> dict:
    return dict(load_source_policy()["sources"].get(name) or {})


def expected_lag_days(name: str) -> int:
    """出所の遅れの目安 (日)。取り込みの出所は運用の方針の値を使う (1 か所にだけ置く)。"""

    definition = source_definition(name)
    config_name = definition.get("import_config")
    if config_name:
        from app.operations.policy import get_policy as get_ops_policy

        value = get_ops_policy().import_config(config_name).get("expected_lag_days")
        if value is not None:
            return int(value)
    return int(definition.get("expected_lag_days") or 0)


@dataclass(frozen=True)
class SourceStatus:
    source: str
    provider: str
    freshness_state: str
    access: str = ACCESS_CACHED
    observed_at: str | None = None
    collected_at: str | None = None
    data_through: str | None = None
    expected_lag_days: int = 0
    missing_reason: str | None = None
    quality_flags: tuple[str, ...] = ()
    provenance: str | None = None
    #: C9 の証拠の鮮度 (fresh / stale / unavailable)。C9 と同じ判定のまま。
    legacy_state: str | None = None
    #: この出所で影響を受ける件数 (例: 保存済みの値がある keyword の数)。
    affected: dict = field(default_factory=dict)
    refresh: str | None = None

    @property
    def usable(self) -> bool:
        return self.freshness_state in USABLE_STATES

    @property
    def needs_external_refresh(self) -> bool:
        return self.access == ACCESS_CACHED and self.freshness_state in (
            STALE, MISSING, PROVIDER_ERROR)

    def as_dict(self) -> dict:
        return {**asdict(self), "quality_flags": list(self.quality_flags),
                "usable": self.usable, "needs_external_refresh": self.needs_external_refresh}


def age_state(observed_at: datetime | None, *, now: datetime, stale_after_days: float | None = None,
              stale_after_hours: float | None = None) -> str:
    """最後の観測の古さだけで決める出所 (Google Ads・Threads・URL Inspection) の状態。"""

    if observed_at is None:
        return MISSING
    age = (now - observed_at).total_seconds() / 3600
    if age < 0:
        return FRESH  # 時計のずれ。未来の観測としては扱わない (呼ぶ側で除く)
    limit = stale_after_hours if stale_after_hours is not None else (
        stale_after_days * 24 if stale_after_days is not None else None)
    return STALE if limit is not None and age > limit else FRESH


def legacy_from(state: str) -> str:
    """共通の状態 → C9 の言葉 (新しい出所のため。取り込みの出所は運用の判定をそのまま使う)。"""

    if state in (MISSING,):
        return LEGACY_UNAVAILABLE
    if state in (STALE, PROVIDER_ERROR):
        return LEGACY_STALE
    return LEGACY_FRESH


__all__ = ["ACCESS_CACHED", "ACCESS_LOCAL", "ACCESS_UNAVAILABLE", "FRESH", "FRESHNESS_STATES",
           "INSUFFICIENT", "LEGACY_FRESH", "LEGACY_STALE", "LEGACY_UNAVAILABLE", "MISSING",
           "PROVIDER_ERROR", "SOURCES", "STALE", "SourceStatus", "USABLE_STATES", "WAITING",
           "age_state", "expected_lag_days", "legacy_from", "load_source_policy",
           "source_definition"]
