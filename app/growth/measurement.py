"""Growth Action の追跡の観測 (C9-C、``growth-measurement/1``、pure)。**因果を言わない。**

C9 Batch 3 の観測 (``outcome.py``: 窓・成熟・状態・中立の言葉) の上に、流れをまたいだ次の 4 つを
足す:

- **流れの一連の状態** (``Lifecycle``): 変更の依頼 (準備 → 依頼 → 承認 → 適用)、Threads の生成の
  依頼 (依頼 → 提案 → 承認 → 公開)、記事の計画 (依頼 → 記事 → 公開)。状態は混ぜない。
- **効果の始まり** (``effective_event``): 外に見える状態が実際に変わった時刻だけ。適用の成功
  (``change_applications.finished_at``)・Threads の公開・記事の公開。Growth Action の承認・変換・
  依頼の作成・提案の承認・準備は **使わない** (``None``)。
- **出所ごとの届き方** (``SourceCoverage`` / ``source_window_state``): チェックポイントの時刻に
  達しても、出所のデータがその日まで届いていなければ ``waiting`` (遅れの目安の中) か
  ``stale_data`` (目安を過ぎた)。欠測は 0 にしない。未来のデータは使わない。
- **次に観測する時刻** (``next_measurement_at``) と、人向けの「何を待っているか」
  (``waiting_summary``)、証拠へ戻す観測 (``followup_item``)。

1 つの成功の点数・勝ち負け・「提案した行動だから良かった」は作らない。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.growth import outcome as go

MEASUREMENT_SCHEMA = "growth-measurement/1"

# -- 効果の始まりの出来事 ---------------------------------------------------------------------
EV_CHANGE_APPLIED = "change_application_succeeded"
EV_THREADS_PUBLISHED = "threads_publication"
EV_ARTICLE_PUBLISHED = "article_publication"
EFFECTIVE_EVENTS = (EV_CHANGE_APPLIED, EV_THREADS_PUBLISHED, EV_ARTICLE_PUBLISHED)

# -- 流れ ------------------------------------------------------------------------------------
WF_CHANGE_REQUEST = "change_request"
WF_THREADS = "threads_generation_request"
WF_ARTICLE = "article_planning_request"
WF_PREPARATION = "change_preparation_request"
WORKFLOWS = (WF_CHANGE_REQUEST, WF_THREADS, WF_ARTICLE, WF_PREPARATION)

# -- 出所 ------------------------------------------------------------------------------------
SRC_SEARCH_CONSOLE = "search_console"
SRC_GA4 = "ga4"
SRC_AFFILIATE = "affiliate_clicks"
SRC_THREADS = "threads_insights"
ARTICLE_SOURCES = (SRC_SEARCH_CONSOLE, SRC_GA4, SRC_AFFILIATE)
#: 取り込みの遅れの目安 (日)。この日数までは「まだ届いていない」を待つ (それを過ぎたら古い)。
EXPECTED_LAG_DAYS = {SRC_SEARCH_CONSOLE: 3, SRC_GA4: 2, SRC_AFFILIATE: 1}
#: 出所ごとの指標 (届いていない出所の値は None にする。0 にしない)。
SOURCE_METRICS = {
    SRC_SEARCH_CONSOLE: ("impressions", "clicks", "position"),
    SRC_GA4: ("ga4_sessions", "ga4_organic_sessions"),
    SRC_AFFILIATE: ("affiliate_clicks",),
}
#: 待っている・古いとき、次に確かめるまでの間隔。
RECHECK = timedelta(hours=6)
#: Threads のチェックポイント (時間)。
THREADS_HOURS = {"24h": 24, "72h": 72}
#: これ以上変わらない状態 (次の観測は要らない)。
FINAL_STATES = frozenset({go.COMPLETED_WINDOW, go.OBSERVABLE, go.INSUFFICIENT,
                          go.NOT_APPLICABLE})  # fmt: skip


@dataclass(frozen=True)
class Stage:
    """流れの 1 段 (状態と時刻は、その実体の記録のまま)。"""

    name: str
    state: str | None
    at: str | None = None
    entity_id: int | str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Lifecycle:
    workflow: str
    stages: tuple[Stage, ...]
    effective_at: str | None
    effective_event: str | None
    #: まだ効果が始まっていないとき、何を待っているか (人向け)。
    waiting_for: str | None = None

    def as_dict(self) -> dict:
        return {"workflow": self.workflow, "stages": [s.as_dict() for s in self.stages],
                "effective_at": self.effective_at, "effective_event": self.effective_event,
                "waiting_for": self.waiting_for}  # fmt: skip


def _first(times: Iterable[str | None]) -> str | None:
    present = sorted(t for t in times if t)
    return present[0] if present else None


def change_lifecycle(*, request_state: str | None, approval: str | None,
                     applications: Sequence[Mapping], request_id=None,
                     preparation: Stage | None = None) -> Lifecycle:  # fmt: skip
    """変更の依頼: 承認・適用の試みでは始まらない。適用が **成功した** 時刻だけ。"""

    applied = [a for a in applications if a.get("outcome") in ("succeeded", "reconciled")]
    at = _first(str(a.get("finished_at") or a.get("attempted_at") or "") for a in applied)
    stages = ((preparation,) if preparation else ()) + (
        Stage("change_request", request_state, entity_id=request_id),
        Stage("approval", approval),
        Stage("application", "succeeded" if at else (
            applications[-1].get("outcome") if applications else None), at))
    waiting = None if at else ("approval of the change request" if approval != "approved"
                               else "a successful application")  # fmt: skip
    return Lifecycle(WF_PREPARATION if preparation else WF_CHANGE_REQUEST, stages, at,
                     EV_CHANGE_APPLIED if at else None, waiting)


def preparation_lifecycle(*, request_state: str, downstream: Sequence[Mapping]) -> Lifecycle:
    """変更の準備: 具体的な変更 (変更の依頼) が **適用されたとき** だけ始まる。

    編集の版・リンクの置き換えは手元の記録で、WordPress への適用の時刻を持たない (始まりにしない)。
    """

    prep = Stage("preparation", request_state)
    for d in downstream:
        if d.get("type") == "change_request":
            applied_at = d.get("applied_at")
            apps = [{"outcome": "succeeded", "finished_at": applied_at}] if applied_at else []
            return change_lifecycle(request_state=d.get("state"), approval=None,
                                    applications=apps, request_id=d.get("id"), preparation=prep)
    stages = (prep,) + tuple(Stage(d.get("type"), d.get("state"), entity_id=d.get("id"))
                             for d in downstream)
    waiting = ("a concrete change linked to the preparation" if not downstream else
               "an applied change request (this downstream has no WordPress application time)")
    return Lifecycle(WF_PREPARATION, stages, None, None, waiting)


def threads_lifecycle(*, request_state: str, proposals: Sequence[Mapping]) -> Lifecycle:
    """Threads の生成の依頼: 提案ができた・承認されただけでは始まらない。公開の時刻だけ。"""

    stages = [Stage("generation_request", request_state)]
    for p in proposals:
        stages.append(Stage("proposal", p.get("state"), entity_id=p.get("id")))
        stages.append(Stage("publication", "published" if p.get("published_at") else None,
                            p.get("published_at"), p.get("id")))
    at = _first(p.get("published_at") for p in proposals)
    if at:
        waiting = None
    elif not proposals:
        waiting = "a proposal from the stock maintenance"
    elif any(p.get("state") == "approved" for p in proposals):
        waiting = "publication"
    else:
        waiting = "approval of the proposal"
    return Lifecycle(WF_THREADS, tuple(stages), at, EV_THREADS_PUBLISHED if at else None,
                     waiting)


def article_lifecycle(*, request_state: str, articles: Sequence[Mapping]) -> Lifecycle:
    """記事の計画: 計画の依頼・記事の作成・下書きでは始まらない。記事の公開の時刻だけ。"""

    stages = [Stage("planning_request", request_state)]
    for a in articles:
        stages.append(Stage("article", a.get("state"), entity_id=a.get("id")))
        stages.append(Stage("publication", "published" if a.get("published_at") else None,
                            a.get("published_at"), a.get("id")))
    at = _first(a.get("published_at") for a in articles if a.get("state") == "published")
    waiting = None if at else ("human approval of the planning request"
                               if request_state == "pending" else
                               "an article created in the existing plan flow"
                               if not articles else "publication of the article")  # fmt: skip
    return Lifecycle(WF_ARTICLE, tuple(stages), at, EV_ARTICLE_PUBLISHED if at else None,
                     waiting)


# == 出所の届き方 =================================================================================
@dataclass(frozen=True)
class SourceCoverage:
    name: str
    #: その日までのデータが届いている (出所の暦日)。
    data_through: date | None
    #: 最後に取り込めた時刻。
    observed_at: str | None = None
    #: 運用の鮮度の判定 (``fresh`` / ``stale`` / ``unavailable``)。
    freshness: str = "fresh"

    @property
    def expected_lag_days(self) -> int:
        return EXPECTED_LAG_DAYS.get(self.name, 1)

    def as_dict(self) -> dict:
        return {"name": self.name,
                "data_through": self.data_through.isoformat() if self.data_through else None,
                "observed_at": self.observed_at, "freshness": self.freshness,
                "expected_lag_days": self.expected_lag_days}  # fmt: skip


def source_window_state(coverage: SourceCoverage | None, *, window_end: date,
                        today: date) -> tuple[str, str]:  # fmt: skip
    """1 つの出所が、窓の終わりまで届いているか。返り値: (状態, 理由)。

    - 出所が無い: ``not_applicable`` (欠測。0 にしない)
    - 窓がまだ終わっていない: ``waiting``
    - 届いていない・遅れの目安の中: ``waiting``
    - 届いていない・目安を過ぎた、または運用の判定で古い: ``stale_data``
    - 届いた: ``completed_window`` (量の判定は呼ぶ側)
    """

    if today <= window_end:
        return go.WAITING, f"the window ends {window_end.isoformat()}"
    if coverage is None or coverage.freshness == "unavailable":
        return go.NOT_APPLICABLE, f"{coverage.name if coverage else 'source'} is unavailable"
    through = coverage.data_through
    if through is not None and through >= window_end:
        return go.COMPLETED_WINDOW, f"{coverage.name} data through {through.isoformat()}"
    shown = through.isoformat() if through else "nothing yet"
    if today <= window_end + timedelta(days=coverage.expected_lag_days) and (
            coverage.freshness != "stale"):
        return go.WAITING, (f"waiting for {coverage.name} data (through {shown}; expected lag "
                            f"{coverage.expected_lag_days} day(s))")
    return go.STALE_DATA, (f"stale {coverage.name} import: data through {shown}, the window "
                           f"ends {window_end.isoformat()}")


def mask_uncovered(metrics: Mapping, states: Mapping[str, str]) -> dict:
    """届いていない出所の指標を None にする (欠測は欠測のまま)。"""

    out = dict(metrics)
    for source, names in SOURCE_METRICS.items():
        if states.get(source) != go.COMPLETED_WINDOW:
            for name in names:
                if name in out:
                    out[name] = None
    return out


# == 次に観測する時刻 =============================================================================
def _aware(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def checkpoint_due_at(effective_at, name: str, *, tz: ZoneInfo | None = None,
                      threads: bool = False) -> datetime:  # fmt: skip
    """チェックポイントに達する時刻。

    Threads は公開からの時間。窓の日数のもの (記事・変更) は、窓の最後の日の翌日の 0 時
    (レポートのタイムゾーンの暦日。変更日は窓に含めない)。
    """

    start = _aware(effective_at)
    if threads:
        return start + timedelta(hours=THREADS_HOURS[name])
    days = dict(go.CHECKPOINTS)[name]
    tz = tz or ZoneInfo("UTC")
    change_date = start.astimezone(tz).date()
    # 窓は変更日の翌日から ``days`` 日 (変更日は含めない)。窓の最後の日が終わった時刻。
    window_end = change_date + timedelta(days=days)
    return datetime.combine(window_end + timedelta(days=1), time.min, tzinfo=tz)


def next_measurement_at(checkpoints: Sequence[Mapping], *, now: datetime) -> str | None:
    """まだ終わっていないチェックポイントのうち、次に状態が変わりうる時刻 (無ければ None)。"""

    times = []
    for c in checkpoints:
        state = c.get("state")
        if state in FINAL_STATES:
            continue
        due = c.get("due_at")
        due = _aware(due) if due else None
        if due is not None and due > now:
            times.append(due)
        else:
            times.append(now + RECHECK)  # 届くのを待つ・古い: 間を空けて確かめ直す
    return min(times).isoformat() if times else None


def is_due(schedule_at: str | None, *, now: datetime) -> bool:
    return schedule_at is not None and _aware(schedule_at) <= now


# == 人向けのまとめ ===============================================================================
def waiting_summary(lifecycle: Lifecycle, checkpoints: Sequence[Mapping]) -> list[str]:
    """何を待っているか・何が終わったか (1 つの点数・勝ち負けではない)。"""

    if lifecycle.effective_at is None:
        return [f"waiting for {lifecycle.waiting_for or 'the change to take effect'}"]
    out = []
    for c in checkpoints:
        state, name = c.get("state"), c.get("name")
        if state == go.COMPLETED_WINDOW:
            out.append(f"completed {name} checkpoint")
        elif state == go.OBSERVABLE:
            out.append(f"{name} checkpoint observable (after-only; nothing to compare)")
        elif state == go.INSUFFICIENT:
            out.append(f"insufficient data at {name}: " + "; ".join(c.get("reasons") or ()))
        elif state == go.STALE_DATA:
            out.append(f"{name}: " + "; ".join(r for r in c.get("reasons") or () if "stale" in r))
        elif state == go.WAITING:
            source_waits = [r for r in c.get("reasons") or () if r.startswith("waiting for")]
            out.append(f"{name}: " + ("; ".join(source_waits) if source_waits
                                      else f"waiting for the {name} checkpoint"))
    return out


# == 証拠へ戻す観測 ===============================================================================
def _direction(value) -> str | None:
    if value is None:
        return None
    return "higher" if value > 0 else "lower" if value < 0 else "unchanged"


def followup_item(anchor: Mapping, lifecycle: Lifecycle, checkpoints: Sequence[Mapping],
                  sources: Mapping[str, Mapping]) -> dict:  # fmt: skip
    """証拠に戻す 1 件の追跡の観測。**観測だけ** (識別・指紋・分類に入れない)。

    終わったチェックポイント (最後のもの) の前後・差の向き・出所の鮮度・足りない理由を持つ。
    「提案した行動だから良かった」は作らない (成功の点数・因果の言葉は無い)。
    """

    final = [c for c in checkpoints if c.get("state") in (go.COMPLETED_WINDOW, go.OBSERVABLE)]
    last = final[-1] if final else None
    return {
        "schema": MEASUREMENT_SCHEMA,
        "growth_action_id": anchor.get("growth_action_id"),
        "action_type": anchor.get("action_type"),
        "downstream_type": anchor.get("downstream_type"),
        "downstream_id": anchor.get("downstream_id"),
        "effective_at": lifecycle.effective_at,
        "effective_event": lifecycle.effective_event,
        "evidence_version": anchor.get("evidence_version"),
        "latest_checkpoint": last.get("name") if last else None,
        "state": last.get("state") if last else go.measurement_state(
            [go.Checkpoint(c["name"], c.get("days"), c["state"]) for c in checkpoints]),
        "before": dict(last.get("before") or {}) if last else {},
        "after": dict(last.get("after") or {}) if last else {},
        "direction": {k: _direction(v) for k, v in (last.get("differences") or {}).items()}
        if last else {},
        "source_freshness": {k: {"state": v.get("state"), "data_through": v.get("data_through")}
                             for k, v in sorted(sources.items())},
        "data_quality": sorted({r for c in checkpoints if c.get("state") in (
            go.INSUFFICIENT, go.STALE_DATA) for r in c.get("reasons") or ()}),
        "downstream_context": [s.as_dict() for s in lifecycle.stages],
        "causal_claim": "none",
        "score": None,
    }


@dataclass(frozen=True)
class MeasuredAnchor:
    """1 つの anchor の観測 (流れ・チェックポイント・出所・次の時刻)。"""

    anchor: Mapping
    lifecycle: Lifecycle
    checkpoints: tuple[dict, ...]
    sources: Mapping[str, Mapping] = field(default_factory=dict)
    next_measurement_at: str | None = None
    measured_at: str | None = None

    @property
    def measurement_state(self) -> str:
        return go.measurement_state([go.Checkpoint(c["name"], c.get("days"), c["state"])
                                     for c in self.checkpoints])

    @property
    def final_signature(self) -> tuple:
        """終わったチェックポイントの印 (変われば、成長の評価をやり直す合図)。"""

        return tuple((c["name"], c["state"]) for c in self.checkpoints
                     if c["state"] in FINAL_STATES)

    def as_dict(self) -> dict:
        return {"schema": MEASUREMENT_SCHEMA, "anchor": dict(self.anchor),
                "lifecycle": self.lifecycle.as_dict(),
                "measurement_state": self.measurement_state,
                "checkpoints": [dict(c) for c in self.checkpoints],
                "sources": {k: dict(v) for k, v in sorted(self.sources.items())},
                "next_measurement_at": self.next_measurement_at,
                "measured_at": self.measured_at,
                "waiting": waiting_summary(self.lifecycle, self.checkpoints),
                "followup": followup_item(self.anchor, self.lifecycle, self.checkpoints,
                                          self.sources),
                "causal_claim": "none", "score": None,
                "notes": list(go.NOTES)}  # fmt: skip


__all__ = ["ARTICLE_SOURCES", "EFFECTIVE_EVENTS", "EV_ARTICLE_PUBLISHED", "EV_CHANGE_APPLIED",
           "EV_THREADS_PUBLISHED", "EXPECTED_LAG_DAYS", "FINAL_STATES", "Lifecycle",
           "MEASUREMENT_SCHEMA", "MeasuredAnchor", "RECHECK", "SOURCE_METRICS", "SRC_AFFILIATE",
           "SRC_GA4", "SRC_SEARCH_CONSOLE", "SRC_THREADS", "SourceCoverage", "Stage",
           "THREADS_HOURS", "WORKFLOWS", "WF_ARTICLE", "WF_CHANGE_REQUEST", "WF_PREPARATION",
           "WF_THREADS", "article_lifecycle", "change_lifecycle", "checkpoint_due_at",
           "followup_item", "is_due", "mask_uncovered", "next_measurement_at",
           "preparation_lifecycle", "source_window_state", "threads_lifecycle",
           "waiting_summary"]
