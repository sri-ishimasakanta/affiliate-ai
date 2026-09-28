"""ThreadsGrowthService -- 毎日 1 本の Growth Post を用意する (T6.3.3)。

**用意するだけ。** 作った提案は ``awaiting_approval`` で保存され、通常の承認のまとめ送りで
人に依頼される。承認・却下・公開はしない (公開は既存の queue と T3 の経路が行う)。

1 日 1 本の守り方 (再起動しても増えない):

1. JST の日付ごとに、その日の Growth Post の提案が 1 つでもあれば作らない (状態を問わない)。
2. その日の生成の記録 (``<日付>.openai.json``) があれば、結果を問わず 2 度と呼ばない
   (呼ぶ前に記録を書く。途中で落ちても呼び直さない)。
3. 提案の公開の資格はその日の 07:00〜24:00 だけ (``not_before`` / ``expires_at``)。
   昨日の分は今日に持ち越さない (期限を過ぎた提案は公開されない。人の判断は書き換えない)。

生成は OpenAI (Luna) の既存の client (再送 2 回まで) を使い、書き直しは 1 回まで。
呼び出しごとの記録 (T6.3.1a と同じ形) を ``data/threads-growth`` に残す。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
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
    select_angle,
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
        elif self._record(day):
            reason = "today's growth generation was already attempted (no regeneration)"
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
        brief = self._brief(day, observation if observation and observation.fresh(now) else None)
        created, calls, failure = self._generate(brief, now)
        result.update(
            created=created,
            model_calls=calls,
            reason=failure,
            follower_observation=observation.as_dict() if observation else None,
            angle=brief.angle,
            uses_follower_count=brief.observation is not None,
            follower_read=self._follower_read,
        )
        self._write_status(result)
        return result

    # -- internals -----------------------------------------------------------------------
    def _brief(self, day, observation: FollowerObservation | None) -> GrowthBrief:
        recent = self.growth_proposals()[:RECENT_GROWTH_WINDOW]
        previous = (_growth_meta(recent[0]) or {}).get("angle") if recent else None
        return GrowthBrief(
            day=day,
            angle=select_angle(day, previous),
            follower_target=self._target,
            observation=observation,
            recent_bodies=tuple(r.content_text for r in recent),
        )

    def _recent_items(self) -> list[dict]:
        return [
            {"ref": f"proposal #{r.id}", "text": r.content_text}
            for r in self.growth_proposals()[:RECENT_GROWTH_WINDOW]
        ]

    def _generate(self, brief: GrowthBrief, now: datetime) -> tuple[int | None, int, str | None]:
        rid = brief.day.isoformat()
        prompt = build_prompt(brief)
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{rid}.prompt.txt").write_text(prompt, encoding="utf-8")
        record = {
            "request_id": f"growth-{rid}",
            "content_kind": CONTENT_KIND_ACCOUNT_GROWTH,
            "model": self._client.model,
            "brief": brief.as_dict(),
            "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "initial_call_made": True,
            "result": "in_progress",
            "updated_at": _now(),
        }
        self._write_record(rid, record)  # 呼ぶ前に残す (途中で落ちても 2 度呼ばない)
        history: list[dict] = []
        recent = self._recent_items()
        feedback = None
        failure = None
        for purpose in ("initial", "repair"):
            call = {
                "ordinal": len(history) + 1,
                "purpose": purpose,
                "at": _now(),
                "requested_model": self._client.model,
            }
            if feedback is not None:
                call["repair_reason_ids"] = growth_reason_ids(feedback[1])
                call["repair_reasons"] = redact(feedback[1])[:1500]
            try:
                result = self._client.generate(
                    prompt, angles=[brief.angle], feedback=feedback, link_mode="none"
                )
            except GenerationError as exc:
                call.update(result=f"failed:{exc.category}", usage={},
                            http_attempts=getattr(exc, "attempts", []))  # fmt: skip
                history.append(call)
                failure = f"generation failed ({exc.category})"
                break
            call.update(
                result="ok",
                returned_model=result.model,
                response_id=result.response_id,
                http_attempts=result.attempts,
                usage=result.usage,
                output=_sanitized_output(result.text),
            )
            check = self._check(result.text, brief, recent)
            call["validation"] = {
                "ok": check["ok"],
                "reason_ids": growth_reason_ids("; ".join(check["problems"])),
                "reasons": None if check["ok"] else "; ".join(check["problems"])[:1500],
                "audit": {"similarity": check.get("similarity"),
                          "prose_length": check.get("prose_length")},
            }  # fmt: skip
            history.append(call)
            if check["ok"]:
                created = self._persist(check["body"], brief, check, now, rid)
                record.update(result="stored" if purpose == "initial" else "repaired",
                              proposal_id=created, fallback=None, updated_at=_now(),
                              **_call_totals(history))  # fmt: skip
                self._write_record(rid, record)
                return created, len(history), None
            failure = "growth check failed: " + "; ".join(check["problems"])
            feedback = (result.text, "; ".join(check["problems"]))
        record.update(result="rejected_by_validation" if feedback else "failed",
                      reason=redact(failure or "")[:500], updated_at=_now(),
                      **_call_totals(history))  # fmt: skip
        self._write_record(rid, record)
        return None, len(history), failure

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

    def _persist(self, body: str, brief: GrowthBrief, check: dict, now: datetime, rid) -> int:
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
