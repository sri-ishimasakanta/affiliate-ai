"""Growth Action のまとめの計画と送信 (C9-A)。偽のメールだけ (本物は送らない)。

pin する契約:

- PLAN は書かない・送らない。方針の ``sending_enabled`` が false なら、execute でも送らない
  (CLI は理由つきで断る)。
- 送るのは、有効・期日 (週 1 回)・承認の通知の窓の中・選ぶ候補がある、がそろったときだけ。
  1 通に 5 件まで。通知の履歴は notification_deliveries の 1 行 (候補の指紋つき)。
- 同じ状態の候補は 2 回知らせない。同じまとめは 2 回送らない。証拠が変われば知らせてよい。
- まとめで見た指紋が古ければ、レビューを断る (fail closed)。一括の承認は無い。
- worker はまとめを送らない。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.growth import analysis as ga
from app.growth import inbox as gi
from app.growth.policy import load_policy
from app.models import NotificationDelivery
from app.operations.email import NotificationResult
from app.services import growth_action_service as gas
from app.services.growth_action_digest_service import GrowthActionDigestService

#: 2026-10-01 10:00 JST (承認の通知の窓 08:00-21:00 の中)。
_NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
_NIGHT = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)  # 23:00 JST


class _Settings:
    operations_email_enabled = True
    operations_email_smtp_host = "smtp.example.com"
    operations_email_smtp_port = 587
    operations_email_username = "ops@example.com"
    operations_email_password = "smtp-password-never-stored"
    operations_email_from = "ops@example.com"
    operations_email_use_starttls = True
    operations_email_subject_prefix = "BizFluxLab"
    operations_email_recipients = ("owner@example.com",)


class _Notifier:
    name = "email"

    def __init__(self):
        self.sent = []

    def send_report(self, *, subject, body, html_body=None):
        self.sent.append({"subject": subject, "body": body})
        return NotificationResult(self.name, True, None)


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    import httpx

    def refuse(*_a, **_k):
        raise AssertionError("the digest must not call anything external")

    monkeypatch.setattr(httpx.Client, "send", refuse)


def _src(source, state=ga.USABLE, **metrics):
    return ga.SourceEvidence(source, state, "test", "test", metrics=metrics)


def _report(clean=(3, 5, 8)) -> dict:
    candidates = []
    for aid, clicks in enumerate(clean, 1):
        sources = {s: _src(s, ga.UNAVAILABLE) for s in ga.SOURCES}
        sources["affiliate"] = _src("affiliate", ga.INSUFFICIENT, clean_clicks=clicks)
        sources["threads"] = _src("threads", ga.USABLE, publications=1, latest_age_hours=10,
                                  oldest_age_hours=10, angles_tried=["insight"],
                                  open_proposals=[])
        evidence = ga.GrowthEvidence(subject_type="article", subject_id=f"article:{aid}",
                                     article_id=aid, article={"status": "published",
                                                              "age_days": 20,
                                                              "monetization_mode": "affiliate",
                                                              "monetized": True},
                                     sources=sources)  # fmt: skip
        candidates += ga.build_candidates(evidence, ga.classify(evidence))
    return {"candidates": [c.as_dict() for c in ga.sort_candidates(candidates)],
            "growth_plan": None}


def _builder(report_fn):
    def build(session, **kw):
        report = report_fn()
        history = gas.GrowthActionHistory(session)
        plan = history.plan_refresh(report, gi.CoverageContext(), now=kw["now"])
        rows = {r.id: r for r in history.rows()}
        return {"entries": gas.plan_entries(report, plan, rows), "history_source": "history"}

    return build


def _policy(**digest):
    base = load_policy()
    return replace(base, raw={**base.raw, "digest": {**base.digest, **digest}})


def _service(session, report_fn=_report, **digest):
    return GrowthActionDigestService(session, settings=_Settings(), policy=_policy(**digest),
                                     inbox_builder=_builder(report_fn))


def _seed(session, report=None):
    gas.GrowthActionHistory(session).refresh(report or _report(), gi.CoverageContext(),
                                             now=_NOW - timedelta(hours=1), execute=True)


def _tables(session) -> dict:
    names = [r[0] for r in session.execute(text(
        "select name from sqlite_master where type='table'"))]  # fmt: skip
    return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names}


def _deliveries(session):
    return list(session.scalars(select(NotificationDelivery)))


def test_the_plan_writes_and_sends_nothing(session) -> None:
    _seed(session)
    before = _tables(session)
    notifier = _Notifier()
    plan = _service(session).plan(now=_NOW)
    assert _tables(session) == before and notifier.sent == []
    assert plan["sending_enabled"] is False and plan["would_notify"] is False
    assert any("sending_disabled" in w for w in plan["waiting_for"])
    assert 0 < plan["selection"]["counts"]["selected"] <= 5
    assert plan["side_effects"] == {"db_writes": 0, "emails": 0, "external_calls": 0}


def test_execute_with_sending_disabled_sends_nothing(session) -> None:
    _seed(session)
    notifier = _Notifier()
    result = _service(session).send(now=_NOW, execute=True, notifier=notifier)
    assert result["executed"] is False and notifier.sent == [] and _deliveries(session) == []


def test_an_enabled_digest_sends_once_and_records_the_members(session) -> None:
    _seed(session)
    notifier = _Notifier()
    service = _service(session, sending_enabled=True)
    result = service.send(now=_NOW, execute=True, notifier=notifier)
    assert result["delivery"]["sent"] is True and len(notifier.sent) == 1
    [row] = _deliveries(session)
    assert row.notification_type == "growth_action_digest" and row.outcome == "sent"
    members = row.detail_json["members"]
    assert 0 < len(members) <= 5 and row.detail_json["individual_review_only"] is True
    assert {m["candidate_fingerprint"] for m in members} <= set(service.notified())
    assert "--fingerprint" in notifier.sent[0]["body"]
    assert "smtp-password" not in str(row.detail_json)
    # 同じ状態で、週の期日の前: 送らない (履歴も増えない)。
    again = service.send(now=_NOW + timedelta(hours=2), execute=True, notifier=notifier)
    assert again["executed"] is False and len(_deliveries(session)) == 1
    # 期日が来ても、同じ状態の候補はもう知らせない (選ぶものが無い)。
    later = service.send(now=_NOW + timedelta(days=8), execute=True, notifier=notifier)
    assert later["selection"]["selected"] == []
    assert later["executed"] is False and len(notifier.sent) == 1


def test_changed_evidence_may_be_notified_again(session) -> None:
    _seed(session)
    notifier = _Notifier()
    _service(session, sending_enabled=True).send(now=_NOW, execute=True, notifier=notifier)
    changed = _report(clean=(4, 5, 8))  # 記事 1 に信頼できるクリックが増えた
    _seed(session, changed)
    result = _service(session, lambda: changed, sending_enabled=True).send(
        now=_NOW + timedelta(days=8), execute=True, notifier=notifier)
    assert result["delivery"]["sent"] is True
    [again] = result["selection"]["selected"]
    assert again["subject_id"] == "article:1"


def test_outside_the_window_nothing_is_sent(session) -> None:
    _seed(session)
    notifier = _Notifier()
    result = _service(session, sending_enabled=True).send(now=_NIGHT, execute=True,
                                                          notifier=notifier)
    assert result["in_window"] is False and notifier.sent == []
    assert any("outside the approval notification window" in w for w in result["waiting_for"])


def test_a_stale_digest_fingerprint_cannot_open_a_review(session) -> None:
    _seed(session)
    plan = _service(session).plan(now=_NOW)
    item = plan["selection"]["selected"][0]
    reviews = gas.GrowthActionReviewService(session)
    with pytest.raises(gas.GrowthActionError, match="fingerprint mismatch"):
        reviews.request_review(item["id"], expected_candidate_fingerprint="0" * 64, now=_NOW)
    review = reviews.request_review(item["id"],
                                    expected_candidate_fingerprint=item["candidate_fingerprint"],
                                    now=_NOW)
    assert review.candidate_id == item["id"]


def test_the_cli_refuses_to_send_while_disabled(session, capsys) -> None:
    from scripts import manage_growth_actions as cli

    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    _seed(session)
    code = cli.main(["digest-send", "--execute"], session_factory=_Scoped(), settings=_Settings())
    assert code == 2 and "sending is disabled" in capsys.readouterr().out
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0


def test_the_committed_policy_keeps_sending_disabled() -> None:
    policy = load_policy()
    assert policy.sending_enabled is False and policy.max_items == 5 and policy.cadence_days == 7


def test_the_worker_does_not_send_growth_digests() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2]
              / "app/services/threads_worker_service.py").read_text(encoding="utf-8")
    assert "GrowthActionDigestService" not in source
    assert "send_growth_action_digest" not in source
