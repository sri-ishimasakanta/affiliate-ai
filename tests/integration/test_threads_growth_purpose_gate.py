"""Growth Post の目的の検査を、生成の経路で通す (偽の Luna・偽の Threads だけ)。

pin する契約:

- ``account_growth_maintenance`` → Growth の生成 → 目的の検査 → 落ちたら同じ書き方で 1 回、
  主役を入れ替える指示つきで書き直し → 通ったものだけ ``awaiting_approval`` で保存。
- 上限 (1 日 4 回) まで通らなければ保存しない (#36 のような提案は残らない)。
- Luna の自己評価 (``growth_assessment``) は Growth の依頼の schema にだけあり、否決にだけ使う。
- 通常の投稿の schema・prompt は変わらない。Growth の prompt に成績の参考 (T6.5) は入らない。
- 公開・承認・通知は 0 件。
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import func, select

from app.models import TP_AWAITING_APPROVAL, ThreadsPostProposal
from app.services.threads_openai_provider import proposal_schema
from app.social.threads import growth_purpose as gp
from app.social.threads.growth import GROWTH_GENERATOR_VERSION
from tests.integration.test_threads_growth_posts import (
    BODY_A,
    MORNING,
    GrowthLuna,
    _growth,
    _growth_rows,
    _no_side_effects,
    _record,
)
from tests.unit.test_threads_growth_purpose import NG_36, NG_ARTICLE, NG_DIARY, OK_FUTURE


class AssessingLuna(GrowthLuna):
    """本文と一緒に自己評価を返す偽の Luna (台本: (本文, 自己評価) の並び)。"""

    def __init__(self, script) -> None:
        super().__init__([body for body, _a in script])
        self.assessments = [a for _b, a in script]

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        assessment = self.assessments.pop(0) if self.assessments else None
        if assessment is None:
            return response
        data = response.json()
        text = data["output"][0]["content"][0]["text"]
        payload = json.loads(text)
        payload["proposals"][0]["growth_assessment"] = assessment
        data["output"][0]["content"][0]["text"] = json.dumps(payload, ensure_ascii=False)
        return httpx.Response(200, json=data)


_HONEST = dict.fromkeys(gp.ASSESSMENT_FIELDS, False)


def test_the_36_shape_is_rewritten_once_and_only_the_passing_post_is_saved(
    session, tmp_path
) -> None:
    fake = GrowthLuna([NG_36, BODY_A])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 2 and out["created"]
    first, second = _record(tmp_path)["history"]
    assert "growth_development_diary_only" in first["validation"]["reason_ids"]
    assert "growth_follow_reason_missing" in first["validation"]["reason_ids"]
    # その日の書き方は問いかけの書き出し: 分類は hook (目的の理由も残り、書き直しの指示が付く)。
    assert first["failure_class"] in ("validation_hook", "validation_purpose")
    assert first["validation"]["purpose"]["signals"]["development_diary_only"] is True
    assert second["purpose"] == "repair" and second["strategy"] == first["strategy"]
    repair = fake.payload(1)["input"][-1]["content"]
    assert "開発の出来事を主役から外す" in repair and "元の事実以上のこと" in repair
    rows = _growth_rows(session)
    assert [r.content_text for r in rows] == [BODY_A]
    row = rows[0]
    assert row.status == TP_AWAITING_APPROVAL and row.generator_version == GROWTH_GENERATOR_VERSION
    meta = row.learning_guidance_json["growth"]
    assert meta["purpose"]["policy_version"] == gp.GROWTH_PURPOSE_POLICY_VERSION
    assert meta["purpose"]["signals"]["identity_signal"] is True
    assert meta["framing"]["axis"] in gp.FRAMING_AXES
    _no_side_effects(session)


def test_when_every_candidate_fails_the_purpose_nothing_is_saved(session, tmp_path) -> None:
    fake = GrowthLuna([NG_36, NG_DIARY, NG_ARTICLE, NG_36, NG_DIARY])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert out["created"] is None
    assert fake.calls == 4  # 1 日の上限
    record = _record(tmp_path)
    assert record["outcome"] == "growth_generation_exhausted"
    assert all(not c["validation"]["ok"] for c in record["history"])
    assert session.scalar(select(func.count()).select_from(ThreadsPostProposal)) == 0
    # 同じ日にもう一度保守しても呼ばない。
    again = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 4 and again["created"] is None


def test_the_self_assessment_vetoes_and_is_recorded(session, tmp_path) -> None:
    flagged = {**_HONEST, "identity_signal": True, "development_diary_only": True}
    honest = {**_HONEST, "identity_signal": True, "future_value_signal": True,
              "follow_invitation_signal": True, "account_purpose_signal": True}  # fmt: skip
    fake = AssessingLuna([(BODY_A, flagged), (BODY_A, honest)])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 2 and out["created"]
    first, second = _record(tmp_path)["history"]
    assert "growth_self_assessment_flag" in first["validation"]["reason_ids"]
    assert first["output"]["proposals"][0]["growth_assessment"] == flagged
    assert second["validation"]["ok"] is True
    purpose = second["validation"]["purpose"]
    assert purpose["source"] == "rules+self_assessment_veto"
    meta = session.get(ThreadsPostProposal, out["created"]).learning_guidance_json["growth"]
    assert meta["purpose"]["self_assessment_used"] is True


def test_only_growth_requests_carry_the_assessment_schema(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_A])
    _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    payload = fake.payload(0)
    item = payload["text"]["format"]["schema"]["properties"]["proposals"]["items"]
    assert "growth_assessment" in item["required"]
    assert item["properties"]["growth_assessment"] == gp.assessment_schema()
    prompt = payload["input"][0]["content"]
    assert "## この投稿の目的 (Growth Post。いちばん優先する)" in prompt
    assert "過去の成績からの補助の参考" not in prompt
    regular = proposal_schema(["insight"], "question", "none")["properties"]["proposals"]["items"]
    assert "growth_assessment" not in regular["properties"]
    assert "growth_assessment" not in regular["required"]


def test_the_next_day_sees_the_previous_framing(session, tmp_path) -> None:
    from datetime import timedelta

    from tests.integration.test_threads_growth_posts import DAY

    _growth(session, tmp_path, GrowthLuna([BODY_A])).maintain(now=MORNING, execute=True)
    first = _growth_rows(session)[0].learning_guidance_json["growth"]
    observed = first["purpose"]["observed_framing"]
    service = _growth(session, tmp_path, GrowthLuna([OK_FUTURE]))
    assert service.recent_framings(DAY + timedelta(days=1)) == [observed]
    assert service.recent_framings(DAY) == []  # その日より前だけ
    brief = service._brief(DAY + timedelta(days=1), None,
                           gp_strategy("account_identity", "follow_connect"), (), None)
    # 前の日と同じ軸・結びは後ろへ (合うものが残っていれば、それを選ぶ)。
    if observed["axis"] in gp.FAMILY_AXES["account_identity"]:
        assert brief.framing.axis != observed["axis"]
    assert brief.framing.cta_kind != observed["cta_kind"]
    assert brief.recent_framings == (observed,)


def gp_strategy(family, cta):
    from app.social.threads.growth_strategy import FAMILIES, Strategy

    spec = FAMILIES[family]
    return Strategy(family, spec.hooks[0], cta, spec.structures[0])
