"""Growth Action のまとめ (digest) の計画と送信 (C9-A)。**既定は PLAN (書かない・送らない)。**

- 選び方は ``app.growth.digest`` (いま動ける・まだ扱われていない・同じ状態で知らせていない
  ものから、成分の順で 5 件まで。1 つの点数は作らない)。
- 間隔は週 1 回 (``growth_action_policy.json`` の ``cadence_days``)。承認の通知の窓
  (``threads_operations_policy.json`` の ``approval_notification_window``) の外では送らない
  (夜に候補が増えても、すぐにメールを送らない)。
- 通知の履歴は既存の ``notification_deliveries`` (メールの監査の表) の 1 行: 種類
  ``growth_action_digest``・まとめの ID (dedupe key)・送った時刻・結果・経路・``detail_json`` に
  候補の ID・版・指紋・通知の指紋。**同じ状態の候補は 2 回知らせない** (新しい証拠なら指紋が
  変わるので、もう一度知らせてよい)。新しい表は作らない。
- 本物の送信は ``sending_enabled`` (既定 false) と ``--execute`` の両方が要る。最初の本番の
  送信は人の判断 (この段階では有効にしない)。
- まとめの候補は **1 件ずつ** のレビューへ案内するだけ (一括の承認は無い)。承認は既存の
  ``manage_growth_actions.py review / approve`` の指紋の照合のまま。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.growth import digest as gd
from app.growth.policy import GrowthActionPolicy, load_policy
from app.models import DELIVERY_SENT, NOTIFICATION_GROWTH_ACTION_DIGEST, NotificationDelivery
from app.services.growth_action_service import build_inbox


def _iso(value) -> str | None:
    return ensure_aware(value).isoformat() if value is not None else None


class GrowthActionDigestService:
    def __init__(self, session: Session, *, settings=None,
                 policy: GrowthActionPolicy | None = None, inbox_builder=None,
                 threads_policy=None, operations_policy=None) -> None:  # fmt: skip
        self._session = session
        self._settings = settings
        self._policy = policy or load_policy()
        self._inbox_builder = inbox_builder or build_inbox
        if threads_policy is None:
            from app.social.threads.policy import get_operations_policy

            threads_policy = get_operations_policy()
        if operations_policy is None:
            from app.operations.policy import get_policy

            operations_policy = get_policy()
        self._window = threads_policy.approval_notification_window
        self._tz = operations_policy.timezone

    # -- 履歴 (読むだけ) --------------------------------------------------------------------
    def deliveries(self) -> list[NotificationDelivery]:
        return list(self._session.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.notification_type == NOTIFICATION_GROWTH_ACTION_DIGEST)
            .order_by(NotificationDelivery.id)))  # fmt: skip

    def notified(self) -> dict[str, int]:
        """知らせた候補の指紋 → 送ったまとめの配送の ID (送れたものだけ)。"""

        out: dict[str, int] = {}
        for row in self.deliveries():
            if row.outcome != DELIVERY_SENT:
                continue
            for member in (row.detail_json or {}).get("members") or []:
                fp = member.get("candidate_fingerprint")
                if fp:
                    out.setdefault(fp, row.id)
        return out

    def notification_state(self) -> dict[int, dict]:
        """候補の ID → 最後に知らせたまとめ (受け箱の表示のため)。"""

        out: dict[int, dict] = {}
        for row in self.deliveries():
            for member in (row.detail_json or {}).get("members") or []:
                cid = member.get("id")
                if cid is None:
                    continue
                out[cid] = {"delivery_id": row.id, "outcome": row.outcome,
                            "notified_at": _iso(row.attempted_at),
                            "candidate_fingerprint": member.get("candidate_fingerprint")}
        return out

    def last_sent_at(self) -> datetime | None:
        sent = [r for r in self.deliveries() if r.outcome == DELIVERY_SENT]
        return ensure_aware(sent[-1].attempted_at) if sent else None

    # -- 計画 ------------------------------------------------------------------------------
    def plan(self, *, now: datetime | None = None) -> dict:
        """まとめの計画。**書かない・送らない。**"""

        now = ensure_aware(now or datetime.now(UTC))
        box = self._inbox_builder(self._session, settings=self._settings, now=now)
        notified = self.notified()
        selection = gd.select(box["entries"], notified=notified, limit=self._policy.max_items)
        last = self.last_sent_at()
        next_due = (last + timedelta(days=self._policy.cadence_days)) if last else None
        due = next_due is None or now >= next_due
        local = now.astimezone(self._tz)
        in_window = (not self._policy.respect_window
                     or self._window.start <= local.time() < self._window.end)  # fmt: skip
        waiting = []
        if not self._policy.sending_enabled:
            waiting.append("sending_disabled (the first real production send needs a human "
                           "decision)")
        if not due:
            waiting.append(f"cadence: next digest after {next_due.isoformat()}")
        if not in_window:
            waiting.append(f"outside the approval notification window "
                           f"{self._window.start}-{self._window.end}")
        if not selection["selected"]:
            waiting.append("nothing to review (no eligible candidate)")
        self._session.rollback()
        return {
            "schema": gd.DIGEST_SCHEMA, "as_of": now.isoformat(),
            "policy_version": self._policy.policy_version,
            "history_source": box.get("history_source"),
            "sending_enabled": self._policy.sending_enabled,
            "cadence_days": self._policy.cadence_days, "last_sent_at": _iso(last),
            "next_due_at": _iso(next_due), "due": due, "in_window": in_window,
            "would_notify": not waiting, "waiting_for": waiting,
            "already_notified_candidates": len(notified),
            "selection": selection,
            "side_effects": {"db_writes": 0, "emails": 0, "external_calls": 0},
        }  # fmt: skip

    # -- 送信 ------------------------------------------------------------------------------
    def send(self, *, now: datetime | None = None, execute: bool = False,
             notifier=None) -> dict:  # fmt: skip
        """``execute`` で、送ってよいときだけ 1 通送る (履歴は notification_deliveries)。"""

        now = ensure_aware(now or datetime.now(UTC))
        plan = self.plan(now=now)
        if not execute or not plan["would_notify"]:
            return {**plan, "executed": False, "delivery": None}
        from app.services.operations_notification_service import (
            OperationsNotificationService,
        )

        selection = plan["selection"]
        members = [{"id": m["id"], "opportunity_key": m["opportunity_key"],
                    "revision": m["revision"], "action_type": m["action_type"],
                    "candidate_fingerprint": m["candidate_fingerprint"],
                    "notification_fingerprint": m["notification_fingerprint"]}
                   for m in selection["selected"]]  # fmt: skip
        detail = {"schema": gd.DIGEST_SCHEMA, "digest_identity": selection["digest_identity"],
                  "selected_at": now.isoformat(), "members": members,
                  "excluded_by_reason": selection["counts"]["excluded_by_reason"],
                  "policy_version": self._policy.policy_version,
                  "individual_review_only": True}  # fmt: skip
        body = gd.render_text(selection, tz_label=str(getattr(self._tz, "key", self._tz)))
        outcome = OperationsNotificationService(
            self._session, settings=self._settings, notifier=notifier,
        ).send_growth_action_digest(
            digest_identity=selection["digest_identity"],
            title=f"Growth Action のまとめ ({len(members)} 件)", body=body, detail=detail,
            now=now)  # fmt: skip
        return {**plan, "executed": True,
                "delivery": {"delivery_id": outcome.delivery_id, "sent": outcome.sent,
                             "reason": outcome.reason, "members": len(members)}}


__all__ = ["GrowthActionDigestService"]
