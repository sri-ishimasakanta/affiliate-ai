"""ThreadsGrowthService -- 毎日 1 本の Growth Post を用意する (T6.3.3)。

**用意するだけ。** 作った提案は ``awaiting_approval`` で保存され、通常の承認のまとめ送りで
人に依頼される。承認・却下・公開はしない (公開は既存の queue と T3 の経路が行う)。

1 日 1 本の守り方 (再起動しても増えない):

1. JST の日付ごとに、その日の Growth Post の提案が 1 つでもあれば作らない (状態を問わない)。
2. その日の生成の記録 (``<日付>.openai.json``) に、呼び出しを **呼ぶ前に** 1 つずつ書く。
   呼び出しの数はこの記録から数える (再起動しても 0 に戻らない)。多くても
   ``MAX_GROWTH_MODEL_CALLS_PER_DAY`` 回 (T6.3.3c)。
3. 提案の公開の資格はその日の 07:00〜24:00 だけ (``not_before`` / ``expires_at``)。
   昨日の分は今日に持ち越さない (期限を過ぎた提案は公開されない。人の判断は書き換えない)。

T6.3.3c: 目的は「その日に、検査を通った提案を 1 本」。候補が検査に落ちたら、落ちた理由で
次を決める (``growth_strategy``):

- 最近の Growth Post に似すぎた → 同じ話の言い換えはしない。**別の書き方** で新しく書く
- 形・長さ・事実の範囲 → 同じ書き方で 1 回だけ書き直す (それでも落ちたら別の書き方)
- provider の認証・設定の失敗 → その日は止める (残りの呼び出しを使わない)
- provider の一時的な失敗 → その回は止め、次の点検 (1 時間ごと) で続ける (上限の中で)
- 呼び出しの上限・使える書き方が尽きた → 提案なしで止める (``growth_generation_exhausted``)

検査は弱めない (似ている度合いの上限も同じ)。T6.3.3c より前の記録 (書き方の版が無い) の日は、
呼び直さない (2026-09-29 の記録はそのまま)。呼び出しごとの記録を ``data/threads-growth`` に残す。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware, to_storage_utc
from app.models import (
    PUB_PUBLISHED,
    TP_AWAITING_APPROVAL,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_openai_provider import (
    GenerationError,
    OpenAIResponsesClient,
    _call_totals,
    _sanitized_output,
)
from app.social.threads import growth_strategy as gs
from app.social.threads.errors import ThreadsError, redact
from app.social.threads.growth import (
    GROWTH_ANGLE_COLUMN,
    GROWTH_FOLLOWER_TARGET,
    GROWTH_GENERATOR_VERSION,
    GROWTH_POLICY_VERSION,
    GROWTH_POSTS_ENABLED,
    RECENT_GROWTH_WINDOW,
    FollowerObservation,
    GrowthBrief,
    build_prompt,
    day_window,
    growth_date,
    growth_reason_ids,
    profile_hash,
    target_reached,
    validate,
)
from app.social.threads.prompt import parse_generated
from app.social.threads.proposal import build_publish_text, compute_proposal_hash
from app.social.threads.topic import CONTENT_KIND_ACCOUNT_GROWTH, CONTENT_KIND_KEY

DEFAULT_DIRECTORY = Path("data/threads-growth")

#: T6.4: フォロワー数を読んだ結果の理由 (内部の値 → 人に見せる日本語)。
FOLLOWER_READ_LABELS = {
    "success": "取得しました",
    "not_requested": "今回は読んでいません (--collect-insights なし)",
    "permission_denied": "権限がないため取得できませんでした",
    "api_error": "Threads の API がエラーを返しました",
    "metric_unavailable": "API から値が返されませんでした",
    "missing_field": "API の値の形が想定と違いました",
    "stale": "前に読んだ値が古いため使いませんでした",
}
FOLLOWERS_FILE = "followers.json"
STATUS_FILE = "status.json"
RECORD_SUFFIX = ".openai.json"


class ThreadsGrowthService:
    def __init__(
        self,
        session: Session,
        *,
        timezone: ZoneInfo,
        client: OpenAIResponsesClient | None = None,
        threads_service=None,
        directory: Path | str = DEFAULT_DIRECTORY,
        collect_followers: bool = False,
        follower_target: int = GROWTH_FOLLOWER_TARGET,
        enabled: bool = GROWTH_POSTS_ENABLED,
    ) -> None:
        self._session = session
        self._tz = timezone
        self._client = client
        self._threads = threads_service
        self._dir = Path(directory)
        self._collect_followers = collect_followers
        self._target = follower_target
        self._enabled = enabled
        #: T6.4: 最後にフォロワー数を読んだ結果 (理由の ID と日本語。値・秘密は入れない)。
        self._follower_read: dict | None = None

    # -- facts ---------------------------------------------------------------------------
    def growth_proposals(self) -> list[ThreadsPostProposal]:
        """Growth Post の提案 (記事の無い提案のうち、印のあるもの)。新しい順。"""

        rows = self._session.scalars(
            select(ThreadsPostProposal)
            .where(ThreadsPostProposal.source_article_id.is_(None))
            .order_by(ThreadsPostProposal.id.desc())
        ).all()
        return [r for r in rows if _growth_meta(r) is not None]

    def proposal_for(self, day) -> ThreadsPostProposal | None:
        for row in self.growth_proposals():
            if (_growth_meta(row) or {}).get("date_jst") == day.isoformat():
                return row
        return None

    def published_on(self, day) -> list[int]:
        """その日 (JST) に公開された Growth Post の公開 ID。"""

        rows = self._session.scalars(
            select(ThreadsPublication).where(
                ThreadsPublication.source_article_id.is_(None),
                ThreadsPublication.status == PUB_PUBLISHED,
            )
        ).all()
        out = []
        for row in rows:
            moment = row.published_at
            if moment is not None and growth_date(ensure_aware(moment), self._tz) == day:
                out.append(row.id)
        return out

    def schema_ready(self) -> bool:
        """記事の無い提案を保存できる DB か (migration ``c4d2e8f1a9b3``、読むだけ)。"""

        columns = inspect(self._session.connection()).get_columns("threads_post_proposals")
        return any(c["name"] == "source_article_id" and c["nullable"] for c in columns)

    def latest_observation(self) -> FollowerObservation | None:
        return FollowerObservation.from_dict(self._read_json(FOLLOWERS_FILE))

    # -- plan / maintain ------------------------------------------------------------------
    def plan(self, *, now: datetime) -> dict:
        """今日の Growth Post を作るか。**何も書かない・呼ばない** (観測も読むだけ)。"""

        now = ensure_aware(now)
        day = growth_date(now, self._tz)
        start, end = day_window(day, self._tz)
        existing = self.proposal_for(day)
        observation = self.latest_observation()
        out = {
            "date_jst": day.isoformat(),
            "eligible_from": start.isoformat(),
            "eligible_until": end.isoformat(),
            "due": False,
            "reason": None,
            "active_proposal": existing.id if existing else None,
            "published_today": self.published_on(day),
            "follower_target": self._target,
            "follower_observation": observation.as_dict() if observation else None,
            "follower_target_reached": target_reached(observation, self._target),
            "model_calls_today": len(_calls(self._record(day))),
            "model_call_budget": gs.MAX_GROWTH_MODEL_CALLS_PER_DAY,
        }
        reason = None
        if not self._enabled:
            reason = "disabled by policy"
        elif now < start:
            reason = "before 07:00 JST"
        elif existing is not None:
            reason = f"today's growth post already exists (proposal {existing.id})"
        elif out["published_today"]:
            reason = "a growth post was already published today"
        elif (finished := _finished(self._record(day))) is not None:
            reason = finished
        elif out["follower_target_reached"]:
            reason = (
                f"follower target {self._target} reached ({observation.count} observed at "
                f"{observation.observed_at.isoformat()}); a human chooses the next target"
            )
        elif not self.schema_ready():
            reason = "saving is blocked until the production migration (alembic upgrade head)"
        elif self._client is None:
            reason = "the OpenAI client is not configured (no manual fallback for growth posts)"
        out["due"] = reason is None
        out["reason"] = reason
        return out

    def maintain(self, *, now: datetime, execute: bool = False) -> dict:
        """期限が来ていれば、今日の Growth Post を 1 本だけ用意する。"""

        now = ensure_aware(now)
        plan = self.plan(now=now)
        result = {**plan, "created": None, "model_calls": 0, "executed": execute}
        if not plan["due"] or not execute:
            self._write_status(result)
            return result
        day = growth_date(now, self._tz)
        observation = self._observe_followers(now) if self._collect_followers else None
        if not self._collect_followers:
            self._set_follower_read("not_requested")
        if observation is None:
            observation = self.latest_observation()
            if observation is not None and not observation.fresh(now) and (
                self._follower_read or {}
            ).get("outcome") in ("not_requested", None):
                self._set_follower_read("stale")
        if target_reached(observation, self._target):
            result.update(
                due=False,
                follower_observation=observation.as_dict(),
                follower_target_reached=True,
                follower_read=self._follower_read,
                reason=(f"follower target {self._target} reached ({observation.count} observed); "
                        "a human chooses the next target"),
            )  # fmt: skip
            self._write_status(result)
            return result
        fresh = observation if observation and observation.fresh(now) else None
        created, calls, failure, outcome = self._generate_day(day, fresh, now)
        record = self._record(day)
        last = (_calls(record) or [{}])[-1]
        result.update(
            created=created,
            model_calls=calls,
            model_calls_today=len(_calls(record)),
            outcome=outcome,
            reason=failure,
            follower_observation=observation.as_dict() if observation else None,
            angle=(last.get("strategy") or {}).get("family"),
            strategy=last.get("strategy"),
            uses_follower_count=fresh is not None,
            follower_read=self._follower_read,
        )
        self._write_status(result)
        return result

    # -- reporting (読むだけ) ---------------------------------------------------------------
    def reliability(self, *, now: datetime, days: int = 14) -> dict:
        """Growth の生成の確かさと書き方の偏り (T6.3.3c)。**記述だけ** (原因は言わない・
        生成に戻さない・T6.5 の観察とはつなげない)。"""

        today = growth_date(ensure_aware(now), self._tz)
        rows = []
        for offset in range(days):
            day = today - timedelta(days=offset)
            record = self._record(day)
            proposal = self.proposal_for(day)
            if not record and proposal is None:
                continue
            calls = _calls(record)
            outcome = record.get("outcome") or record.get("result")
            rows.append({
                "date_jst": day.isoformat(),
                "record_version": record.get("strategy_policy_version") or "t6.3.3 (legacy)",
                "outcome": outcome,
                "valid_proposal": proposal is not None,
                "proposal_id": proposal.id if proposal else None,
                "model_calls": len(calls),
                "candidates": sum(1 for c in calls if c.get("result") == "ok"),
                "similarity_rejections": sum(
                    1 for c in calls
                    if "growth_duplicate" in ((c.get("validation") or {}).get("reason_ids") or [])
                ),
                "format_repairs": sum(1 for c in calls if c.get("purpose") == "repair"),
                "strategy_retries": sum(1 for c in calls if c.get("purpose") == "strategy_retry"),
                "strategies": [(c.get("strategy") or {}).get("signature") for c in calls
                               if c.get("strategy")],
            })  # fmt: skip
        eligible = len(rows)
        valid = sum(1 for r in rows if r["valid_proposal"])
        exhausted = sum(1 for r in rows if not r["valid_proposal"] and r["outcome"] in (
            gs.GROWTH_GENERATION_EXHAUSTED, "rejected_by_validation"))  # fmt: skip
        calls_total = sum(r["model_calls"] for r in rows)
        metrics = {
            "eligible_growth_days": eligible,
            "valid_proposal_days": valid,
            "generation_exhausted_days": exhausted,
            "total_model_calls": calls_total,
            "candidates_generated": sum(r["candidates"] for r in rows),
            "similarity_rejections": sum(r["similarity_rejections"] for r in rows),
            "format_repairs": sum(r["format_repairs"] for r in rows),
            "strategy_retries": sum(r["strategy_retries"] for r in rows),
            "average_calls_per_valid_proposal": round(calls_total / valid, 2) if valid else None,
            "proposal_success_rate": round(valid / eligible, 3) if eligible else None,
        }
        recent = self.growth_proposals()[: gs.RECENT_STRATEGY_WINDOW]
        strategies = [gs.Strategy.from_dict((_growth_meta(r) or {}).get("strategy"))
                      for r in recent]  # fmt: skip
        known = [st for st in strategies if st is not None]
        signatures = Counter(st.signature for st in known)
        similarities = [((_growth_meta(r) or {}).get("recent_similarity") or {})
                        .get("max_similarity") for r in recent]  # fmt: skip
        diversity = {
            "recent_proposals": len(recent),
            "with_strategy": len(known),
            "family": dict(Counter(st.family for st in known)),
            "family_including_legacy_angle": dict(Counter(
                h.family for h in gs.history_from_meta(
                    ((_growth_meta(r) or {}).get("date_jst"), _growth_meta(r)) for r in recent))),
            "hook": dict(Counter(st.hook for st in known)),
            "cta": dict(Counter(st.cta for st in known)),
            "structure": dict(Counter(st.structure for st in known)),
            "repeated_signatures": sorted(k for k, n in signatures.items() if n > 1),
            "recent_max_similarity": max((v for v in similarities if v is not None),
                                         default=None),
            "calls_per_valid_proposal": [r["model_calls"] for r in rows if r["valid_proposal"]],
        }  # fmt: skip
        return {
            "as_of": ensure_aware(now).isoformat(),
            "window_days": days,
            "strategy_policy_version": gs.GROWTH_STRATEGY_POLICY_VERSION,
            "max_model_calls_per_jst_day": gs.MAX_GROWTH_MODEL_CALLS_PER_DAY,
            "reliability": metrics,
            "diversity": diversity,
            "days": rows,
            "notes": [
                "descriptive only: no claim about followers, impressions or causes",
                "not fed back to generation; strategy selection stays rule-based",
                "T6.5 trend data is not connected yet (future advisory only)",
            ],
        }

    # -- internals -----------------------------------------------------------------------
    def _brief(self, day, observation: FollowerObservation | None, strategy: gs.Strategy,
               facts: tuple[str, ...], retry_direction: str | None) -> GrowthBrief:  # fmt: skip
        recent = self.growth_proposals()[:RECENT_GROWTH_WINDOW]
        return GrowthBrief(
            day=day,
            angle=strategy.family,
            follower_target=self._target,
            observation=observation,
            recent_bodies=tuple(r.content_text for r in recent),
            strategy=strategy,
            facts=facts,
            retry_direction=retry_direction,
        )

    def strategy_history(self, day) -> list[gs.HistoryItem]:
        """その日より前の、最近の Growth Post の提案の書き方 (新しい順)。"""

        rows = []
        for row in self.growth_proposals():
            meta = _growth_meta(row) or {}
            if meta.get("date_jst") and meta["date_jst"] < day.isoformat():
                rows.append((meta.get("date_jst"), meta))
        return gs.history_from_meta(rows[: gs.RECENT_STRATEGY_WINDOW])

    def _recent_items(self) -> list[dict]:
        return [
            {"ref": f"proposal #{r.id}", "text": r.content_text}
            for r in self.growth_proposals()[:RECENT_GROWTH_WINDOW]
        ]

    def _generate_day(self, day, observation: FollowerObservation | None, now: datetime
                      ) -> tuple[int | None, int, str | None, str | None]:  # fmt: skip
        """その日の Growth Post を、上限の中で 1 本だけ作る (T6.3.3c)。

        返り値: (作った提案の id, この回の呼び出しの数, 失敗の理由, その日の結果)。
        """

        rid = day.isoformat()
        record = self._record(day) or {
            "request_id": f"growth-{rid}",
            "content_kind": CONTENT_KIND_ACCOUNT_GROWTH,
            "date_jst": rid,
            "strategy_policy_version": gs.GROWTH_STRATEGY_POLICY_VERSION,
            "generator_version": GROWTH_GENERATOR_VERSION,
            "daily_call_budget": gs.MAX_GROWTH_MODEL_CALLS_PER_DAY,
            "model": self._client.model,
            "result": "in_progress",
            "outcome": None,
            "history": [],
        }
        calls = record.setdefault("history", [])
        facts = gs.active_facts(gs.load_facts(), day)
        kinds = set(facts) | ({gs.FACT_FOLLOWER_COUNT} if observation is not None else set())
        families = gs.eligible_families(kinds)
        history = self.strategy_history(day)
        recent = self._recent_items()
        made = 0
        while len(calls) < gs.MAX_GROWTH_MODEL_CALLS_PER_DAY:
            action = _next_action(day, calls, families, history)
            if action is None:
                return self._finish(record, gs.STRATEGY_EXHAUSTED, made)
            purpose, strategy, feedback, direction = action
            entry = {
                "ordinal": len(calls) + 1,
                "call_index": len(calls) + 1,
                "attempt_index": len({c["strategy"]["signature"] for c in calls
                                      if c.get("strategy")} | {strategy.signature}),
                "purpose": purpose,
                "strategy": strategy.as_dict(),
                "at": _now(),
                "requested_model": self._client.model,
                "result": "in_progress",
            }  # fmt: skip
            if feedback is not None:
                entry["repair_reason_ids"] = growth_reason_ids(feedback[1])
                entry["repair_reasons"] = redact(feedback[1])[:1500]
            if direction:
                entry["retry_direction"] = direction
            calls.append(entry)
            record.update(result="in_progress", updated_at=_now())
            self._write_record(rid, record)  # 呼ぶ前に残す (落ちても数は戻らない)
            made += 1
            used = tuple(f.text for f in facts.get(gs.FAMILIES[strategy.family].requires or "", ()))
            brief = self._brief(day, observation, strategy, used, direction)
            prompt = build_prompt(brief)
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / f"{rid}.prompt.txt").write_text(prompt, encoding="utf-8")
            entry["prompt_hash"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            try:
                result = self._client.generate(
                    prompt, angles=[brief.angle], feedback=feedback, link_mode="none"
                )
            except GenerationError as exc:
                failure_class = gs.classify_provider(exc.category)
                entry.update(result=f"failed:{exc.category}", failure_class=failure_class,
                             usage={}, http_attempts=getattr(exc, "attempts", []))  # fmt: skip
                if failure_class == gs.PROVIDER_AUTH:
                    return self._finish(record, gs.PROVIDER_AUTH, made,
                                        reason=f"generation failed ({exc.category})")  # fmt: skip
                if failure_class == gs.PROVIDER_TRANSIENT:
                    # 次の点検で続ける (残りの呼び出しを今は使わない)。
                    record.update(result="paused", last_failure=gs.PROVIDER_TRANSIENT,
                                  updated_at=_now(), **_totals(calls))  # fmt: skip
                    self._write_record(rid, record)
                    return None, made, f"generation failed ({exc.category}); will resume", None
                self._write_record(rid, record)
                continue
            entry.update(
                result="ok",
                returned_model=result.model,
                response_id=result.response_id,
                http_attempts=result.attempts,
                usage=result.usage,
                output=_sanitized_output(result.text),
            )
            check = self._check(result.text, brief, recent)
            reason_ids = growth_reason_ids("; ".join(check["problems"]))
            similarity = check.get("similarity") or {}
            entry["validation"] = {
                "ok": check["ok"],
                "reason_ids": reason_ids,
                "reasons": None if check["ok"] else "; ".join(check["problems"])[:1500],
                "audit": {"similarity": check.get("similarity"),
                          "prose_length": check.get("prose_length")},
            }  # fmt: skip
            entry["similarity"] = {"max": similarity.get("max_similarity"),
                                   "compared": (similarity.get("top") or [{}])[0].get("ref"),
                                   "threshold": similarity.get("threshold")}  # fmt: skip
            if check["ok"]:
                entry["failure_class"] = None
                created = self._persist(check["body"], brief, check, now, rid,
                                        call_index=entry["call_index"],
                                        attempt_index=entry["attempt_index"])  # fmt: skip
                record.update(result="stored" if purpose != "repair" else "repaired",
                              outcome="stored", proposal_id=created, updated_at=_now(),
                              **_totals(calls))  # fmt: skip
                self._write_record(rid, record)
                return created, made, None, "stored"
            entry["failure_class"] = gs.classify_validation(reason_ids)
            self._write_record(rid, record)
        return self._finish(record, gs.MODEL_CALL_BUDGET_EXHAUSTED, made)

    def _finish(self, record: dict, why: str, made: int, *, reason: str | None = None):
        """その日を提案なしで終える (もう呼ばない)。"""

        outcome = why if why == gs.PROVIDER_AUTH else gs.GROWTH_GENERATION_EXHAUSTED
        last = (record.get("history") or [{}])[-1]
        detail = (last.get("validation") or {}).get("reasons") or last.get("result")
        failure = reason or f"{outcome} ({why}); last: {detail}"
        record.update(result="rejected_by_validation" if outcome != gs.PROVIDER_AUTH else "failed",
                      outcome=outcome, exhaustion_reason=why, reason=redact(failure)[:500],
                      updated_at=_now(), **_totals(record.get("history") or []))  # fmt: skip
        self._write_record(record["date_jst"], record)
        return None, made, failure, outcome

    @staticmethod
    def _check(text: str, brief: GrowthBrief, recent) -> dict:
        try:
            items = parse_generated(text)
        except (ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "problems": [f"malformed output: {exc}"]}
        item = items[0]
        problems = []
        if len(items) != 1:
            problems.append("malformed output: exactly one growth post is expected")
        if item.get("angle") != brief.angle:
            problems.append("malformed output: the growth angle must not change")
        if (item.get("link_mode") or "none") != "none":
            problems.append("a Growth Post must not contain a URL, a link or {link}")
        body = build_publish_text(item["body"], None)
        verdict = validate(body, brief, recent)
        problems += verdict["problems"]
        return {**verdict, "ok": not problems, "problems": sorted(set(problems)), "body": body}

    def _persist(self, body: str, brief: GrowthBrief, check: dict, now: datetime, rid, *,
                 call_index: int | None = None, attempt_index: int | None = None) -> int:
        start, end = day_window(brief.day, self._tz)
        seed = hashlib.sha256(
            chr(31).join([CONTENT_KIND_ACCOUNT_GROWTH, brief.day.isoformat(), brief.angle, body])
            .encode("utf-8")
        ).hexdigest()
        row = ThreadsPostProposal(
            source_article_id=None,
            source_article_body_hash=profile_hash(),
            angle=GROWTH_ANGLE_COLUMN,
            link_mode="none",
            content_text=body,
            character_count=len(body),
            destination_url=None,
            content_seed=seed,
            proposal_hash=compute_proposal_hash(
                content_seed=seed, destination_url=None, publish_text=body
            ),
            policy_version=GROWTH_POLICY_VERSION,
            generator_version=GROWTH_GENERATOR_VERSION,
            status=TP_AWAITING_APPROVAL,
            status_reason="daily growth post; awaiting human approval",
            warnings_json=list(check.get("warnings") or []),
            learning_guidance_json={
                CONTENT_KIND_KEY: CONTENT_KIND_ACCOUNT_GROWTH,
                "growth": {
                    **brief.as_dict(),
                    "policy_version": GROWTH_POLICY_VERSION,
                    "request_id": f"growth-{rid}",
                    "recent_similarity": check.get("similarity"),
                    "prose_length": check.get("prose_length"),
                    # T6.3.3c: どの書き方で、その日の何回目の呼び出しで通ったか。
                    "strategy_policy_version": gs.GROWTH_STRATEGY_POLICY_VERSION,
                    "model_call_index": call_index,
                    "attempt_index": attempt_index,
                },
            },
            not_before=to_storage_utc(start),
            expires_at=to_storage_utc(end),
        )
        self._session.add(row)
        self._session.commit()
        return row.id

    def _observe_followers(self, now: datetime) -> FollowerObservation | None:
        """Threads のアカウントの指標を 1 回だけ読む (読むだけ)。読めなければ None。"""

        if self._threads is None:
            self._set_follower_read("not_requested")
            return None
        try:
            insights = self._threads.user_insights(("followers_count",))
        except ThreadsError as exc:
            self._set_follower_read(
                "permission_denied"
                if exc.category in ("threads_permission", "threads_auth")
                else "api_error",
                category=exc.category, status=exc.status, api_code=exc.api_code,
            )  # fmt: skip
            return None
        if "followers_count" not in insights.values:
            self._set_follower_read("metric_unavailable")
            return None
        value = insights.values.get("followers_count")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            self._set_follower_read("missing_field")
            return None
        self._set_follower_read("success")
        observation = FollowerObservation(count=value, observed_at=ensure_aware(now))
        self._write_json(FOLLOWERS_FILE, observation.as_dict())
        return observation

    def _set_follower_read(self, outcome: str, **detail) -> None:
        self._follower_read = {
            "outcome": outcome,
            "label": FOLLOWER_READ_LABELS.get(outcome, outcome),
            **{k: v for k, v in detail.items() if v is not None},
        }

    def _record(self, rid) -> dict:
        rid = rid.isoformat() if hasattr(rid, "isoformat") else rid
        path = self._dir / f"{rid}{RECORD_SUFFIX}"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {"unreadable": True}

    def _write_record(self, rid: str, record: dict) -> None:
        self._write_json(f"{rid}{RECORD_SUFFIX}", record)

    def _write_status(self, status: dict) -> None:
        self._write_json(STATUS_FILE, {**status, "written_at": _now()})

    def _read_json(self, name: str) -> dict | None:
        path = self._dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _write_json(self, name: str, data: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )


def _calls(record: dict | None) -> list[dict]:
    return list((record or {}).get("history") or [])


def _finished(record: dict | None) -> str | None:
    """その日の生成が終わっていれば理由 (もう呼ばない)、続けてよければ ``None``。"""

    if not record:
        return None
    if record.get("unreadable"):
        return "today's growth generation record is unreadable (no regeneration)"
    if record.get("strategy_policy_version") is None:
        # T6.3.3c より前の記録 (2026-09-29 など): 1 回の試みで終わった日。呼び直さない。
        return "today's growth generation was already attempted (no regeneration)"
    if record.get("outcome"):
        return f"today's growth generation is finished ({record['outcome']})"
    if len(_calls(record)) >= gs.MAX_GROWTH_MODEL_CALLS_PER_DAY:
        return (f"today's growth model-call budget is used "
                f"({gs.MAX_GROWTH_MODEL_CALLS_PER_DAY})")  # fmt: skip
    return None


def _next_action(day, calls: list[dict], families, history):
    """記録の最後の呼び出しから、次の 1 回を決める (決定的)。``None`` は書き方が尽きた。

    返り値: (目的, 書き方, 書き直しの材料, 新しい方向の文)。
    """

    tried = []
    for call in calls:
        strategy = gs.Strategy.from_dict(call.get("strategy"))
        if strategy is not None and strategy not in tried and not str(
            call.get("failure_class") or ""
        ).startswith("provider_"):
            tried.append(strategy)
    last = calls[-1] if calls else None
    if last is None:
        strategy = gs.next_strategy(day, families=families, history=history, tried=[])
        return None if strategy is None else ("initial", strategy, None, None)
    previous = gs.Strategy.from_dict(last.get("strategy"))
    failure = last.get("failure_class")
    if failure == gs.PROVIDER_TRANSIENT and previous is not None:
        # 前の回は provider の一時的な失敗 (候補を見ていない): 同じ書き方でもう一度。
        return (last.get("purpose") or "initial", previous, None, last.get("retry_direction"))
    signature = previous.signature if previous is not None else None
    repairs = sum(1 for c in calls if c.get("purpose") == "repair"
                  and (c.get("strategy") or {}).get("signature") == signature)
    output = last.get("output") or {}
    items = output.get("proposals") if isinstance(output, dict) else None
    text = None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        text = json.dumps(output, ensure_ascii=False)
    reasons = (last.get("validation") or {}).get("reasons")
    if (failure in (gs.VALIDATION_FORMAT, gs.VALIDATION_FACT, gs.VALIDATION_HOOK)
            and previous is not None and text and reasons
            and repairs < gs.MAX_REPAIRS_PER_STRATEGY):  # fmt: skip
        last.setdefault("next_action", {"purpose": "repair", "strategy": previous.signature,
                                        "retry_reason": failure})  # fmt: skip
        return ("repair", previous, (text, reasons), None)
    strategy = gs.next_strategy(day, families=families, history=history, tried=tried)
    if strategy is None:
        last.setdefault("next_action", {"purpose": None, "retry_reason": gs.STRATEGY_EXHAUSTED})
        return None
    direction = _retry_direction(last, strategy)
    last.setdefault("next_action", {"purpose": "strategy_retry", "strategy": strategy.signature,
                                    "retry_reason": failure or "interrupted"})  # fmt: skip
    return ("strategy_retry", strategy, None, direction)


def _retry_direction(last: dict, strategy) -> str:
    """別の書き方で書き直すときの指示 (「似ないように」ではなく、新しい方向そのものを書く)。"""

    family = gs.FAMILIES[strategy.family]
    before = (last.get("strategy") or {}).get("family")
    sim = last.get("similarity") or {}
    if last.get("failure_class") == gs.VALIDATION_SIMILARITY:
        why = (f"前の案 ({before}) は、最近の Growth Post ({sim.get('compared')}) と似すぎていた "
               f"(3 文字の重なり {sim.get('max')}、上限 {sim.get('threshold')})。")
    else:
        why = f"前の案 ({before}) は検査に通らなかった。"
    return (
        why + "言い換えではなく、次の新しい方向で最初から書く: "
        f"{family.intent}。書き出しは「{gs.HOOKS[strategy.hook]}」、組み立ては"
        f"「{gs.STRUCTURES[strategy.structure]}」、結びは「{gs.CTAS[strategy.cta]}」。"
        "前の案と同じ書き出し・同じ言い回しは使わない。"
    )


def _totals(history: list[dict]) -> dict:
    totals = _call_totals(history)
    totals["generation_attempts"] = len({(c.get("strategy") or {}).get("signature")
                                         for c in history if c.get("strategy")}) or 1
    totals["strategy_retries"] = sum(1 for c in history if c.get("purpose") == "strategy_retry")
    totals.pop("history", None)
    return totals


def _growth_meta(row) -> dict | None:
    guidance = getattr(row, "learning_guidance_json", None)
    if not isinstance(guidance, dict) or guidance.get(CONTENT_KIND_KEY) != (
        CONTENT_KIND_ACCOUNT_GROWTH
    ):
        return None
    meta = guidance.get("growth")
    return meta if isinstance(meta, dict) else {}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = ["DEFAULT_DIRECTORY", "ThreadsGrowthService"]
