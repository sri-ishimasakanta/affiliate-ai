"""ソースの鮮度判定 (C8.5、pure)。

**取り込みの網羅範囲** と **データの活動** は別物である。ここを混同したのが
C8.4 の誤検知だった: Search Console は 2026-09-22 まで正常に問い合わせできて
いたのに、2026-09-15 以降に表示回数が 1 件も無かったため、最新の metric 行が
2026-09-15 のまま止まり、それを「データが古い = 異常」と報告してしまった。

そこで 3 つを明確に分ける:

``coverage_through``
    成功した取り込みが **実際に問い合わせ終えた** ソース側の最終日。
    0 行で成功した問い合わせもここを前進させる (行を捏造はしない)。

``latest_observed_data_date``
    実際に metric / event / click / commission の行が存在する最終日。
    新規サイトや低トラフィックでは前進しないのが正常。

``last_successful_import_at``
    直近で取り込みが成功した運用上の時刻 (wall-clock)。

運用上の異常 (= 通知に値する) は **取り込みが動いていないこと** であって、
「活動が無いこと」ではない。活動の有無は情報であって故障ではない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.operations.monitoring import AlertDraft, fingerprint
from app.operations.policy import SEVERITY_INFO, OperationsPolicy

#: 取り込みが動いていない (運用上の異常)。
SOURCE_REFRESH_STALE = "DATA_STALE"
#: 直近の活動が無い (情報。既定では通知しない)。
NO_RECENT_ACTIVITY = "NO_RECENT_ACTIVITY"


@dataclass(frozen=True)
class SourceFreshness:
    """1 ソースの鮮度。3 つの概念を分けたまま持つ。"""

    source: str
    #: 成功した取り込みが問い合わせ終えた最終日 (0 行成功でも前進する)。
    coverage_through: date | None
    #: 実データの行が存在する最終日 (活動が無ければ前進しない)。
    latest_observed_data_date: date | None
    #: 直近で取り込みが成功した時刻。
    last_successful_import_at: datetime | None
    #: これまでに一度でも行が届いたことがあるか。
    ever_had_data: bool
    #: 取り込みが一度でも成功したことがあるか。
    ever_imported: bool

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "coverage_through": (
                self.coverage_through.isoformat() if self.coverage_through else None
            ),
            "latest_observed_data_date": (
                self.latest_observed_data_date.isoformat()
                if self.latest_observed_data_date
                else None
            ),
            "last_successful_import_at": (
                self.last_successful_import_at.isoformat()
                if self.last_successful_import_at
                else None
            ),
            "ever_had_data": self.ever_had_data,
            "ever_imported": self.ever_imported,
        }


def evaluate_source_refresh(
    *,
    freshness: SourceFreshness,
    today: date,
    now: datetime,
    policy: OperationsPolicy,
) -> AlertDraft | None:
    """**取り込みが動いていない** ときだけアラートにする。

    判定は 2 つだけ:

    1. 直近の成功した取り込みが ``refresh_stale_after_hours`` より古い。
    2. 網羅範囲 (``coverage_through``) が、provider の想定遅延を考慮しても
       ``coverage_stale_after_days`` より遅れている。

    「最新の metric 行が古い」ことは **理由にしない** -- それは活動の話であり、
    低トラフィックのサイトでは正常だから。
    """

    config = policy.import_config(freshness.source)
    if not freshness.ever_imported:
        # 一度も取り込んでいない = 初期状態。故障の証拠ではない。
        return None

    reasons: list[str] = []
    refresh_hours = config.get("refresh_stale_after_hours")
    if refresh_hours is not None:
        if freshness.last_successful_import_at is None:
            reasons.append("no successful import has ever finished")
        else:
            last = _as_utc(freshness.last_successful_import_at)
            age_hours = (now - last).total_seconds() / 3600
            if age_hours > float(refresh_hours):
                reasons.append(
                    f"last successful import was {age_hours:.1f}h ago (gate {refresh_hours}h)"
                )

    coverage_days = config.get("coverage_stale_after_days")
    if coverage_days is not None:
        expected_lag = int(config.get("expected_lag_days", 0))
        if freshness.coverage_through is None:
            reasons.append("no successful import has recorded a coverage date")
        else:
            coverage_age = (today - freshness.coverage_through).days
            if coverage_age > int(coverage_days) + expected_lag:
                reasons.append(
                    f"coverage only reaches {freshness.coverage_through} "
                    f"({coverage_age} day(s) old; gate "
                    f"{int(coverage_days) + expected_lag})"
                )

    if not reasons:
        return None

    return AlertDraft(
        alert_type=SOURCE_REFRESH_STALE,
        severity=policy.severity_for(SOURCE_REFRESH_STALE),
        source=freshness.source,
        title=f"{freshness.source} refresh is stale",
        summary=f"{freshness.source}: " + "; ".join(reasons),
        fingerprint=fingerprint(SOURCE_REFRESH_STALE, freshness.source),
        evidence={
            **freshness.as_dict(),
            "reasons": reasons,
            "gate_refresh_stale_after_hours": refresh_hours,
            "gate_coverage_stale_after_days": coverage_days,
            # 活動の古さは判定に使っていないことを証跡に残す。
            "activity_considered": False,
        },
    )


def evaluate_recent_activity(
    *,
    freshness: SourceFreshness,
    today: date,
    policy: OperationsPolicy,
) -> AlertDraft | None:
    """最近の活動が無いことを **情報として** 記録する (既定では無効)。

    新規/低トラフィックのサイトでは活動が無いのが普通なので、これを既定で
    有効にすると毎日同じ情報が並ぶだけになる。有効化はポリシーの
    ``activity_alerts_enabled`` で明示的に行う。
    """

    if not bool(policy.gate("alerts", "activity_alerts_enabled", False)):
        return None
    config = policy.import_config(freshness.source)
    gate = config.get("activity_quiet_after_days")
    if gate is None or not freshness.ever_had_data:
        return None
    if freshness.latest_observed_data_date is None:
        return None
    age = (today - freshness.latest_observed_data_date).days
    if age <= int(gate):
        return None

    return AlertDraft(
        alert_type=NO_RECENT_ACTIVITY,
        # 情報であって故障ではない -- 通知の既定閾値 (warning) を下回る。
        severity=SEVERITY_INFO,
        source=freshness.source,
        title=f"{freshness.source} has no recent activity",
        summary=(
            f"{freshness.source} refreshed successfully but the most recent data row is "
            f"{freshness.latest_observed_data_date} ({age} day(s) ago). This is normal "
            "for a new or low-traffic site and is not a source failure."
        ),
        fingerprint=fingerprint(NO_RECENT_ACTIVITY, freshness.source),
        evidence={**freshness.as_dict(), "activity_age_days": age, "gate_quiet_days": gate},
    )


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def within(moment: datetime | None, *, hours: float, now: datetime) -> bool:
    """``moment`` が ``hours`` 以内か (``None`` は False)。"""

    if moment is None:
        return False
    return now - _as_utc(moment) <= timedelta(hours=hours)
