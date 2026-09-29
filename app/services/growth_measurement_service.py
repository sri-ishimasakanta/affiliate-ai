"""変換した Growth Action の追跡の観測 (C9-C)。**読むだけ・外に問い合わせない。**

- ``anchors``: 変換の記録 → その先 (変更の依頼・Threads の生成の依頼・記事の計画・変更の準備) の
  一連の状態 (``Lifecycle``) と、実際に外に見える状態が変わった時刻 (``effective_at``)。
- ``measure``: ``effective_at`` がある anchor だけを、チェックポイント (24h / 72h / 7d / 14d /
  28d) ごとに、出所 (GSC / GA4 / 信頼できるクリック / Threads の観測) の届き方を守って観測する。
  届いていない出所は 0 にしない (``None``)。観測の時点より後のデータは使わない。
- ``refresh``: worker が使う。前の観測の ``next_measurement_at`` が来たものだけを観測し直す
  (毎回すべてを重く計算しない)。終わったチェックポイントが増えたら ``changed`` で知らせる
  (成長の評価をやり直す合図)。
- ``followup_by_article``: 証拠の流れへ戻す観測 (記事ごと)。**観測だけ** で、候補の識別・指紋・
  分類には入れない (自分の行動の観測で、自分の候補を強めない)。

DB に書かない。WordPress・Threads・OpenAI・メールに触れない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.change.effect import assess_maturity, build_windows, delta, local_effective_date
from app.growth import measurement as gm
from app.growth import outcome as go
from app.models import (
    PUB_PUBLISHED,
    Article,
    ChangeApplication,
    ChangeRequest,
    ChangeRequestApproval,
    ThreadsPublication,
)
from app.models.growth_action import GrowthActionCandidate, GrowthActionConversion

#: 窓の差を取る指標 (ChangeEffect と同じ)。
_DELTA_METRICS = ("impressions", "clicks", "position", "ga4_sessions", "ga4_organic_sessions",
                  "affiliate_clicks")  # fmt: skip
#: Threads の観測がチェックポイントの近くに届くのを待つ時間 (それを過ぎたら足りない)。
_THREADS_OBSERVATION_GRACE = timedelta(hours=6)


def _iso(value) -> str | None:
    return ensure_aware(value).isoformat() if value is not None else None


def measurement_ready(session: Session) -> bool:
    names = set(inspect(session.connection()).get_table_names())
    return "growth_action_conversions" in names


class GrowthMeasurementService:
    def __init__(self, session: Session, *, settings, timezone: ZoneInfo | None = None,
                 coverage=None) -> None:  # fmt: skip
        self._session = session
        self._settings = settings
        #: 出所の届き方の出どころ (``now`` → {出所: ``SourceCoverage``})。None なら DB から。
        self._coverage_provider = coverage
        if timezone is None:
            from app.operations.policy import get_policy as get_ops_policy

            timezone = get_ops_policy().timezone
        self._tz = timezone
        self._threads_rows: dict | None = None

    # -- anchors (流れの一連の状態) -------------------------------------------------------------
    def anchors(self) -> list[dict]:
        """変換ごと・その先ごとの anchor と流れ。変換が無ければ空。"""

        if not measurement_ready(self._session):
            return []
        out = []
        for conversion in self._session.scalars(select(GrowthActionConversion)
                                                .order_by(GrowthActionConversion.id)):
            row = self._session.get(GrowthActionCandidate, conversion.candidate_id)
            base = {"growth_action_id": conversion.candidate_id, "action_type": row.action_type,
                    "subject_id": row.subject_id, "conversion_id": conversion.id,
                    "evidence_version": f"rev{row.revision}:{row.candidate_fingerprint[:12]}"}
            for downstream_id in conversion.downstream_ids_json or []:
                if conversion.downstream_type == gm.WF_CHANGE_REQUEST:
                    out.append(self._change_anchor(base, downstream_id))
                else:
                    item = self._handoff_anchor(base, conversion.downstream_type, downstream_id)
                    if item is not None:
                        out.append(item)
        return out

    def _change_anchor(self, base: dict, request_id: int) -> dict:
        request = self._session.get(ChangeRequest, request_id)
        approval = self._session.scalars(select(ChangeRequestApproval).where(
            ChangeRequestApproval.change_request_id == request_id)
            .order_by(ChangeRequestApproval.id.desc())).first()  # fmt: skip
        apps = [{"outcome": a.outcome, "finished_at": _iso(a.finished_at),
                 "attempted_at": _iso(a.attempted_at)}
                for a in self._session.scalars(select(ChangeApplication).where(
                    ChangeApplication.change_request_id == request_id)
                    .order_by(ChangeApplication.id))]  # fmt: skip
        lifecycle = gm.change_lifecycle(
            request_state=request.status if request else "missing",
            approval=approval.decision if approval else None, applications=apps,
            request_id=request_id)  # fmt: skip
        anchor = {**base, "handoff_id": None, "downstream_type": gm.WF_CHANGE_REQUEST,
                  "downstream_id": request_id, "article_id": request.article_id if request
                  else None, "downstream_state": request.status if request else "missing",
                  "effective_at": lifecycle.effective_at,
                  "effective_event": lifecycle.effective_event}  # fmt: skip
        return {"anchor": anchor, "lifecycle": lifecycle}

    def _handoff_anchor(self, base: dict, target: str, request_id: int) -> dict | None:
        from app.services.growth_handoff_service import GrowthHandoffService, handoff_ready

        if not handoff_ready(self._session):
            return None
        service = GrowthHandoffService(self._session)
        try:
            row = service.get(request_id)
        except Exception:  # noqa: BLE001 - 無い依頼は観測しない
            return None
        observed = service.observe(row)
        downstream = observed.get("downstream") or []
        article_id = row.article_id
        if target == gm.WF_THREADS:
            proposals = []
            for d in downstream:
                published = self._first_publication(d["id"])
                proposals.append({"id": d["id"], "state": d.get("state"),
                                  "published_at": _iso(published.published_at)
                                  if published else None,
                                  "publication_id": published.id if published else None})
            lifecycle = gm.threads_lifecycle(request_state=row.status, proposals=proposals)
            first = next((p for p in proposals if p["published_at"] == lifecycle.effective_at
                          and lifecycle.effective_at), None)  # fmt: skip
            kind, entity = (("threads_publication", first["publication_id"]) if first
                            else (target, request_id))  # fmt: skip
            state = "published" if first else row.status
        elif target == gm.WF_ARTICLE:
            articles = [{"id": d["id"], "state": d.get("state"),
                         "published_at": d.get("published_at")}
                        for d in downstream if d.get("type") == "article"]  # fmt: skip
            lifecycle = gm.article_lifecycle(request_state=row.status, articles=articles)
            article_id = articles[0]["id"] if articles else None
            kind, entity = (("article", article_id) if lifecycle.effective_at
                            else (target, request_id))  # fmt: skip
            state = "published" if lifecycle.effective_at else row.status
        else:
            lifecycle = gm.preparation_lifecycle(request_state=row.status, downstream=downstream)
            linked = next((d for d in downstream if d.get("type") == "change_request"), None)
            kind, entity = (("change_request", linked["id"]) if lifecycle.effective_at and linked
                            else (target, request_id))  # fmt: skip
            state = linked.get("state") if (linked and lifecycle.effective_at) else row.status
        anchor = {**base, "handoff_id": request_id, "downstream_type": kind,
                  "downstream_id": entity, "article_id": article_id, "downstream_state": state,
                  "effective_at": lifecycle.effective_at,
                  "effective_event": lifecycle.effective_event}  # fmt: skip
        return {"anchor": anchor, "lifecycle": lifecycle}

    def _first_publication(self, proposal_id: int) -> ThreadsPublication | None:
        return self._session.scalars(select(ThreadsPublication).where(
            ThreadsPublication.proposal_id == proposal_id,
            ThreadsPublication.status == PUB_PUBLISHED)
            .order_by(ThreadsPublication.published_at)).first()  # fmt: skip

    # -- measure ----------------------------------------------------------------------------
    def measure(self, item: dict, *, now: datetime | None = None,
                checkpoint: str | None = None) -> gm.MeasuredAnchor:  # fmt: skip
        """1 つの anchor を観測する (読むだけ)。効果が始まっていなければ観測しない。"""

        now = ensure_aware(now or datetime.now(UTC))
        anchor, lifecycle = item["anchor"], item["lifecycle"]
        names = [n for n, _d in go.CHECKPOINTS if checkpoint in (None, n)]
        if lifecycle.effective_at is None:
            reason = f"waiting for {lifecycle.waiting_for or 'the change to take effect'}"
            checkpoints = tuple({"name": n, "days": dict(go.CHECKPOINTS)[n], "due_at": None,
                                 "state": go.WAITING, "reasons": [reason], "before": {},
                                 "after": {}, "differences": {}, "observations": [],
                                 "sources": {}} for n in names)  # fmt: skip
            return gm.MeasuredAnchor(anchor, lifecycle, checkpoints, {}, None, now.isoformat())
        if lifecycle.effective_event == gm.EV_THREADS_PUBLISHED:
            checkpoints, sources = self._threads(anchor, lifecycle, names, now)
        else:
            checkpoints, sources = self._article(anchor, lifecycle, names, now)
        return gm.MeasuredAnchor(anchor, lifecycle, tuple(checkpoints), sources,
                                 gm.next_measurement_at(checkpoints, now=now), now.isoformat())

    def _coverage(self, now: datetime) -> dict[str, gm.SourceCoverage]:
        if self._coverage_provider is not None:
            return dict(self._coverage_provider(now))
        from app.services.source_health_service import SourceHealthService

        # C10-A: 出所の状態は 1 か所 (``SourceHealthService``) で決める (data-through の規則・
        # 観測の時点より後の日を使わない・GA4 の設定が無ければ使えない、を含む)。
        statuses = SourceHealthService(self._session, settings=self._settings,
                                       timezone=self._tz).collect(now=now)
        out = {}
        for name in gm.ARTICLE_SOURCES:
            status = statuses.get(name)
            if status is None:
                continue
            through = date.fromisoformat(status.data_through) if status.data_through else None
            out[name] = gm.SourceCoverage(name, through, status.observed_at,
                                          status.legacy_state or "fresh")  # fmt: skip
        return out

    def _article(self, anchor, lifecycle, names, now) -> tuple[list[dict], dict]:
        from app.revenue.policy import load_policy
        from app.services.change_effect_service import ChangeEffectService

        article = self._session.get(Article, anchor.get("article_id")) if anchor.get(
            "article_id") else None  # fmt: skip
        coverage = self._coverage(now)
        sources = {k: v.as_dict() for k, v in coverage.items()}
        if article is None:
            return [{"name": n, "days": dict(go.CHECKPOINTS)[n], "due_at": None,
                     "state": go.INSUFFICIENT, "reasons": ["article missing"], "before": {},
                     "after": {}, "differences": {}, "observations": [], "sources": {}}
                    for n in names], sources  # fmt: skip
        publication = lifecycle.effective_event == gm.EV_ARTICLE_PUBLISHED
        effective = gm._aware(lifecycle.effective_at)
        today = local_effective_date(now, self._tz)
        trusted_from = load_policy().trusted_measurement_start_at
        effects = ChangeEffectService(self._session, settings=self._settings)
        gsc = coverage.get(gm.SRC_SEARCH_CONSOLE)
        out = []
        for name in names:
            days = dict(go.CHECKPOINTS)[name]
            windows = build_windows(change_at=effective, reporting_timezone=self._tz, today=today,
                                    window_days=days,
                                    coverage_through=gsc.data_through if gsc else None)
            pre, post = windows.pre, windows.post
            states = {}
            reasons = []
            for source in gm.ARTICLE_SOURCES:
                state, why = gm.source_window_state(coverage.get(source), window_end=post.end,
                                                    today=today)
                states[source] = {"state": state, "reason": why}
                if state in (go.WAITING, go.STALE_DATA):
                    reasons.append(why)
            after_m = effects.measure_window(post, article, trusted_from=trusted_from,
                                             through=today)
            after = gm.mask_uncovered(after_m.as_dict(), {k: v["state"]
                                                          for k, v in states.items()})
            if publication:
                before, differences = {}, {}
            else:
                before_m = effects.measure_window(pre, article, trusted_from=trusted_from,
                                                  through=today)
                before = gm.mask_uncovered(before_m.as_dict(), {
                    k: (go.COMPLETED_WINDOW if (coverage.get(k) and coverage[k].data_through
                                                and coverage[k].data_through >= pre.end)
                        else go.WAITING) for k in gm.ARTICLE_SOURCES})  # fmt: skip
                differences = {m: delta(before.get(m), after.get(m)) for m in _DELTA_METRICS}
                if not (before_m.affiliate_clicks_trusted and after_m.affiliate_clicks_trusted):
                    differences["affiliate_clicks"] = None
            gsc_state = states[gm.SRC_SEARCH_CONSOLE]["state"]
            source_states = {v["state"] for v in states.values()}
            if go.WAITING in source_states:
                # 使える出所のどれかがまだ届いていない: 届くまで待つ (届いた出所だけで閉じない)。
                state = go.WAITING
            elif go.STALE_DATA in source_states:
                state = go.STALE_DATA
            elif gsc_state == go.NOT_APPLICABLE:
                state, reasons = go.INSUFFICIENT, reasons + ["search_console is unavailable"]
            else:
                impressions = after.get("impressions") or 0
                if publication:
                    # 公開は前の窓が無い (記事が無かった): 後の窓の観測だけ。比べない。
                    if impressions < 30:
                        state = go.INSUFFICIENT
                        reasons.append("BELOW_MINIMUM_VOLUME")
                    else:
                        state = go.OBSERVABLE
                        reasons.append("after-only: the article did not exist before")
                else:
                    maturity = assess_maturity(
                        pre=pre, post=post, today=today,
                        pre_impressions=before.get("impressions") or 0,
                        post_impressions=impressions, pre_rows=before_m.rows)
                    state = (go.COMPLETED_WINDOW if maturity.sufficient else go.INSUFFICIENT)
                    reasons += list(maturity.reasons)
            untrusted = after_m.affiliate_clicks_trusted is False or (
                not publication and before_m.affiliate_clicks_trusted is False)
            if untrusted:
                reasons.append("affiliate clicks before the trusted measurement start are not "
                               "counted")
            cp = go.Checkpoint(name, days, state, tuple(reasons), before, after, differences)
            observations = list(go._observations(name, before, after, differences, state))
            out.append({"name": name, "days": days,
                        "due_at": gm.checkpoint_due_at(effective, name, tz=self._tz).isoformat(),
                        "state": cp.state, "reasons": list(cp.reasons), "before": before,
                        "after": after, "differences": differences,
                        "observations": observations, "sources": states,
                        "pre_window": pre.as_dict(), "post_window": post.as_dict()})
        return out, sources

    def _threads(self, anchor, lifecycle, names, now) -> tuple[list[dict], dict]:
        from app.services.threads_performance_service import ThreadsPerformanceService

        if self._threads_rows is None:
            report = ThreadsPerformanceService(self._session, settings=self._settings).report(
                as_of=now)
            self._threads_rows = {r["publication_id"]: r for r in report.get("publications", [])}
        row = self._threads_rows.get(anchor.get("downstream_id"))
        latest = (row or {}).get("latest") or {}
        sources = {gm.SRC_THREADS: {"name": gm.SRC_THREADS, "state": "fresh" if row else
                                    "unavailable",
                                    "data_through": latest.get("observed_at_local"),
                                    "observed_at": latest.get("observed_at_local")}}
        effective = gm._aware(lifecycle.effective_at)
        out = []
        for name in names:
            days = dict(go.CHECKPOINTS)[name]
            if name not in go.THREADS_CHECKPOINTS:
                out.append({"name": name, "days": days, "due_at": None,
                            "state": go.NOT_APPLICABLE,
                            "reasons": ["Threads checkpoints are 24h and 72h (T6.5)"],
                            "before": {}, "after": {}, "differences": {}, "observations": [],
                            "sources": {}})  # fmt: skip
                continue
            due = gm.checkpoint_due_at(effective, name, threads=True)
            cp = go.threads_checkpoint(name, row)
            state, reasons = cp.state, list(cp.reasons)
            if state == go.INSUFFICIENT and now < due + _THREADS_OBSERVATION_GRACE:
                state = go.WAITING
                reasons = ["waiting for threads_insights observation near the checkpoint"]
            out.append({"name": name, "days": days, "due_at": due.isoformat(), "state": state,
                        "reasons": reasons, "before": {}, "after": dict(cp.after),
                        "differences": {}, "observations": list(cp.observations),
                        "sources": {gm.SRC_THREADS: {"state": state, "reason": "; ".join(
                            reasons)}}})  # fmt: skip
        return out, sources

    # -- worker / 証拠 -----------------------------------------------------------------------
    @staticmethod
    def cache_key(anchor: dict) -> tuple:
        return (anchor["conversion_id"], anchor.get("handoff_id"), anchor["downstream_type"],
                anchor["downstream_id"], anchor.get("effective_at"))

    def refresh(self, *, now: datetime | None = None,
                cache: dict | None = None) -> tuple[list[gm.MeasuredAnchor], dict]:  # fmt: skip
        """期日が来た anchor だけを観測し直す。返り値: (観測, 統計)。

        ``cache`` は worker のメモリ (DB には書かない)。効果が始まっていない anchor は、流れを
        見るだけ (観測しない)。終わったチェックポイントが変わった anchor は ``changed``。
        """

        now = ensure_aware(now or datetime.now(UTC))
        cache = {} if cache is None else cache
        measured, stats = [], {"anchors": 0, "effective": 0, "measured": 0, "reused": 0,
                               "changed": [], "next_measurement_at": None}  # fmt: skip
        live = set()
        for item in self.anchors():
            stats["anchors"] += 1
            key = self.cache_key(item["anchor"])
            live.add(key)
            previous = cache.get(key)
            if item["lifecycle"].effective_at is None:
                result = self.measure(item, now=now)
            elif previous is not None and not gm.is_due(previous.next_measurement_at, now=now):
                result = previous
                stats["reused"] += 1
            else:
                result = self.measure(item, now=now)
                stats["measured"] += 1
                if previous is None or previous.final_signature != result.final_signature:
                    if result.final_signature:
                        stats["changed"].append(item["anchor"]["growth_action_id"])
            if item["lifecycle"].effective_at is not None:
                stats["effective"] += 1
            cache[key] = result
            measured.append(result)
        for key in [k for k in cache if k not in live]:
            del cache[key]
        pending = [m.next_measurement_at for m in measured if m.next_measurement_at]
        stats["next_measurement_at"] = min(pending) if pending else None
        return measured, stats

    def followup_by_article(self, measured: list[gm.MeasuredAnchor] | None = None, *,
                            now: datetime | None = None) -> dict[int, tuple[dict, ...]]:
        """証拠の流れへ戻す観測 (記事ごと)。効果が始まった anchor だけ。"""

        if measured is None:
            measured, _stats = self.refresh(now=now)
        out: dict[int, list[dict]] = {}
        for m in measured:
            aid = m.anchor.get("article_id")
            if aid is None or m.lifecycle.effective_at is None:
                continue
            out.setdefault(int(aid), []).append(gm.followup_item(
                m.anchor, m.lifecycle, m.checkpoints, m.sources))
        return {k: tuple(v) for k, v in sorted(out.items())}


def summarize(measured: list[gm.MeasuredAnchor], *, now: datetime) -> dict:
    """人向けのまとめ: 何が観測待ちで、何が終わったか (1 つの点数・勝ち負けは作らない)。"""

    from collections import Counter

    now = ensure_aware(now)
    groups = {k: Counter() for k in ("waiting", "stale", "insufficient", "observable",
                                     "completed")}
    due = []
    for m in measured:
        if m.lifecycle.effective_at is None:
            groups["waiting"][f"waiting for {m.lifecycle.waiting_for or 'the change'}"] += 1
            continue
        for c in m.checkpoints:
            name, state = c["name"], c["state"]
            if state == go.COMPLETED_WINDOW:
                groups["completed"][f"completed {name} checkpoint"] += 1
            elif state == go.OBSERVABLE:
                groups["observable"][f"{name} observable (after-only)"] += 1
            elif state == go.INSUFFICIENT:
                why = "insufficient trusted clicks / volume" if any(
                    "BELOW_MINIMUM_VOLUME" in r or "trusted" in r for r in c["reasons"]) else (
                    "; ".join(c["reasons"]) or "insufficient")
                groups["insufficient"][f"{name}: {why}"] += 1
            elif state == go.STALE_DATA:
                sources = sorted(k for k, v in (c.get("sources") or {}).items()
                                 if v.get("state") == go.STALE_DATA)
                groups["stale"][f"{name}: stale {', '.join(sources) or 'source'} import"] += 1
            elif state == go.WAITING:
                sources = sorted(k for k, v in (c.get("sources") or {}).items()
                                 if v.get("state") == go.WAITING and "data" in (
                                     v.get("reason") or ""))  # fmt: skip
                label = (f"waiting for {', '.join(sources)} data (lag)" if sources
                         else f"waiting for the {name} checkpoint")
                groups["waiting"][f"{name}: {label}"] += 1
        if gm.is_due(m.next_measurement_at, now=now):
            due.append(m.anchor["growth_action_id"])
    return {"anchors": len(measured),
            "effective": sum(1 for m in measured if m.lifecycle.effective_at),
            "by_state": dict(sorted(Counter(m.measurement_state for m in measured).items())),
            **{k: dict(sorted(v.items())) for k, v in groups.items()},
            "due_now": sorted(set(due)),
            "notes": ["no single success score; no winner/loser; no cause is claimed"]}


__all__ = ["GrowthMeasurementService", "measurement_ready", "summarize"]
