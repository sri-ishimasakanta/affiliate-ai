"""T6.3: 会話のきっかけつきの生成 (偽の Luna だけ。本物の API・本番の依頼は使わない)。

pin する契約:

- 在庫の計画は依頼ごとにきっかけを決める (決定的)。依頼・prompt・schema・保存した来歴に
  同じきっかけが入る。再試行・書き直しでもきっかけは変わらない。
- 返ったきっかけが違えば検査落ち (書き直し 1 回)。コメント依頼・2 つの問い・none での呼びかけは
  保存しない。好みの問題は警告として提案に残る。
- 保存は必ず awaiting_approval。公開・承認・通知は増えない。
- T6.3 より前の依頼 (きっかけなし) は前と同じように取り込め、来歴は legacy。
- 公開ごとの表は、きっかけ・切り口・リンク・提案 ID・公開 ID をつなぐ。views=0 でも落ちない。
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.models import (
    TP_AWAITING_APPROVAL,
    MobileApprovalSession,
    NotificationDelivery,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_conversation_report import by_hook, publication_rows
from app.services.threads_generation_provider import GenerationRequest, ManualFileProvider
from app.services.threads_openai_provider import OpenAIGenerationProvider, OpenAIResponsesClient
from app.social.threads.conversation import HOOKS, hook_from_provenance
from tests.integration.test_threads_openai_generation import (
    API_KEY,
    FakeLuna,
    _provider,
    seed_articles,
)
from tests.integration.test_threads_proposal_stock_service import _NOW, _service

BODIES = {
    "none": "記事{aid}の要点は、体制を先に決めるほうが早いこと。範囲を小さく決めると迷わない。",
    "question": (
        "記事{aid}の要点。体制を先に決めるほうが早い。今の現場で先に決まっていないのはどこ？"
    ),
    "choice": "記事{aid}の要点。体制を先に決めるか、道具を先に決めるか。ここで進み方が変わる。",
    "experience": "記事{aid}の要点。体制を先に決めるほうが早い。導入のとき、どこで止まった？",
    "opinion": (
        "記事{aid}の要点。体制を先に決めるほうが早い、と考えている。道具が先という見方もある。"
    ),
}


@pytest.fixture
def articles(session):
    return seed_articles(session)


def _hook_of(payload: dict) -> str | None:
    props = payload["text"]["format"]["schema"]["properties"]["proposals"]["items"]["properties"]
    return props.get("conversation_hook", {}).get("enum", [None])[0]


class HookLuna(FakeLuna):
    """求められたきっかけに合う本文を返す偽の Luna。"""

    def __init__(self, *, override=None, **kwargs):
        super().__init__(**kwargs)
        self.override = override or {}
        self.hooks: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        hook = _hook_of(payload)
        self.hooks.append(hook)
        aid = int(payload["input"][0]["content"].split("## 記事 (id=")[1].split(")")[0])
        body = self.override.get(len(self.hooks), BODIES.get(hook, BODIES["none"])).format(aid=aid)
        self.body_for = lambda *_args, _body=body: _body
        return super().handler(request)


def _no_side_effects(session):
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0
    assert session.scalar(select(func.count()).select_from(MobileApprovalSession)) == 0
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
    rows = session.scalars(select(ThreadsPostProposal)).all()
    assert all(r.status == TP_AWAITING_APPROVAL and r.approved_at is None for r in rows)


# == planning ========================================================================
def test_the_plan_gives_every_request_a_deterministic_hook(session, articles, tmp_path) -> None:
    first = _service(session, tmp_path).plan(now=_NOW)["stock"]["requests"]
    again = _service(session, tmp_path).plan(now=_NOW)["stock"]["requests"]
    hooks = [r["conversation_hook"] for r in first]
    assert hooks == [r["conversation_hook"] for r in again]  # 同じ計画は同じきっかけ
    assert all(h in HOOKS for h in hooks)
    assert all(a != b for a, b in zip(hooks, hooks[1:], strict=False))
    assert all(any("conversation_hook" in reason for reason in r["reasons"]) for r in first)


def test_manual_requests_carry_the_same_hook_in_request_prompt_and_schema(
    session, articles, tmp_path
) -> None:
    service = _service(session, tmp_path)
    service.maintain(now=_NOW, execute=True)
    for request in service.provider.pending():
        data = json.loads(
            (tmp_path / "gen" / "pending" / f"{request.request_id}.request.json").read_text("utf-8")
        )
        prompt = (tmp_path / "gen" / "pending" / f"{request.request_id}.prompt.txt").read_text(
            "utf-8"
        )
        assert data["conversation_hook"] == request.conversation_hook in HOOKS
        assert f"conversation_hook={request.conversation_hook}" in prompt


# == generation =====================================================================
def test_fake_luna_generates_one_proposal_per_hook_awaiting_approval(
    session, articles, tmp_path
) -> None:
    fake = HookLuna()
    produced = {}
    for day in range(20):  # 在庫が尽きるたびに計画 → 生成。5 つの型が出るまで回す
        _service(session, tmp_path, provider=_provider(tmp_path, fake)).maintain(
            now=_NOW + timedelta(days=30 * day), execute=True
        )
        for row in session.scalars(select(ThreadsPostProposal)).all():
            produced.setdefault(hook_from_provenance(row.learning_guidance_json), row)
            row.status = "published"  # 在庫から外して次の計画を促す (テストの DB だけ)
        session.commit()
        if set(produced) >= set(HOOKS):
            break
    assert set(produced) == set(HOOKS), sorted(produced)  # 5 つの型すべて
    assert set(fake.hooks) - {None} <= set(HOOKS)
    for hook, row in produced.items():
        brief = row.learning_guidance_json["generation_brief"]
        assert brief["conversation_hook"] == hook and brief["brief_version"] == "t6.3"
        assert len(row.content_text) <= 500


def test_a_wrong_hook_is_repaired_once_with_the_same_requested_hook(
    session, articles, tmp_path
) -> None:
    fake = HookLuna()

    def wrong_first(request):
        response = fake.handler(request)
        if len(fake.hooks) == 1:
            data = response.json()
            text = json.loads(data["output"][0]["content"][0]["text"])
            text["proposals"][0]["conversation_hook"] = "banter"
            data["output"][0]["content"][0]["text"] = json.dumps(text, ensure_ascii=False)
            return httpx.Response(200, json=data)
        return response

    client = OpenAIResponsesClient(api_key=API_KEY, transport=httpx.MockTransport(wrong_first),
                                   sleep=lambda s: None)  # fmt: skip
    provider = OpenAIGenerationProvider(tmp_path / "gen", client=client)
    out = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert len(out["created"]) == 3 and len(fake.hooks) == 4  # 1 件だけ書き直し
    assert fake.hooks[0] == fake.hooks[1]  # 書き直しでもきっかけは同じ
    _no_side_effects(session)


@pytest.mark.parametrize(
    "bad",
    [
        "記事{aid}の要点。体制を先に決める。役に立ったらいいねしてください。",
        "記事{aid}の要点。体制は先？道具は先？",
        "記事{aid}の要点。私は体制を先に決めて早くなった。",
        "記事{aid}の要点。こう書くとおすすめに載りやすい。",
    ],
)
def test_bait_and_invention_are_not_saved(session, articles, tmp_path, bad) -> None:
    fake = HookLuna(override={i: bad for i in range(1, 20)})
    out = _service(session, tmp_path, provider=_provider(tmp_path, fake)).maintain(
        now=_NOW, execute=True
    )
    assert out["created"] == []
    assert all("check failed" in f["reason"] for f in out["failures"])
    assert len(list((tmp_path / "gen" / "pending").glob("*.request.json"))) == 3  # manual に残る


def test_subjective_style_issues_become_warnings_not_rejections(
    session, articles, tmp_path
) -> None:
    manual = ManualFileProvider(tmp_path / "gen")
    service = _service(session, tmp_path, provider=manual)
    service.maintain(now=_NOW, execute=True)
    for request in manual.pending():
        body = f"記事{request.article_id}の要点。体制を先に決めるほうが早い。どう思いますか？"
        if request.conversation_hook == "none":
            body = f"記事{request.article_id}の要点。体制を先に決めるほうが早い。道具から入る？"
        (tmp_path / "gen" / "pending" / f"{request.request_id}.response.json").write_text(
            json.dumps({"proposals": [{"angle": request.angles[0], "link_mode": "none",
                        "conversation_hook": request.conversation_hook, "body": body}]},
                       ensure_ascii=False), encoding="utf-8")  # fmt: skip
    out = _service(session, tmp_path, provider=manual).maintain(
        now=_NOW + timedelta(hours=1), execute=True
    )
    assert len(out["created"]) == 3
    for row in session.scalars(select(ThreadsPostProposal)).all():
        assert any("conversation" in w or "ends with a question" in w for w in row.warnings_json)
    _no_side_effects(session)


def test_a_legacy_request_without_a_hook_still_ingests(session, articles, tmp_path) -> None:
    manual = ManualFileProvider(tmp_path / "gen")
    service = _service(session, tmp_path, provider=manual)
    package = service._proposals.build_prompt(
        article_id=21, angles=["insight"], learning_as_of=_NOW
    )
    legacy = GenerationRequest(
        request_id="legacyrequest0000001", article_id=21, article_title="記事21",
        angles=("insight",), link_mode="none", learning_as_of=_NOW.isoformat(),
        guidance_fingerprint=package.guidance.fingerprint, guidance_mode=package.guidance.mode,
        prompt_hash=package.prompt_hash, created_at=_NOW.isoformat(),
        prompt=package.rendered_prompt,
    )  # fmt: skip
    manual.submit(legacy)
    assert "conversation_hook" not in json.loads(
        (tmp_path / "gen/pending/legacyrequest0000001.request.json").read_text("utf-8")
    )
    (tmp_path / "gen/pending/legacyrequest0000001.response.json").write_text(
        json.dumps({"proposals": [{"angle": "insight", "link_mode": "none",
                    "body": "記事21の要点。体制を先に決めるほうが早い。"}]}, ensure_ascii=False),
        encoding="utf-8",
    )  # fmt: skip
    out = _service(session, tmp_path, provider=manual).maintain(
        now=_NOW + timedelta(minutes=1), execute=True, collect_only=True
    )
    assert len(out["created"]) == 1
    row = session.get(ThreadsPostProposal, out["created"][0])
    assert "generation_brief" not in row.learning_guidance_json
    assert hook_from_provenance(row.learning_guidance_json) == "legacy"


# == measurement ===================================================================
def test_publication_rows_carry_hook_angle_link_and_ids(session, articles, tmp_path) -> None:
    fake = HookLuna()
    _service(session, tmp_path, provider=_provider(tmp_path, fake)).maintain(now=_NOW, execute=True)
    proposal = session.scalars(select(ThreadsPostProposal)).first()
    pub = ThreadsPublication(
        proposal_id=proposal.id, proposal_hash=proposal.proposal_hash,
        source_article_id=proposal.source_article_id, angle=proposal.angle,
        exact_published_text=proposal.content_text, status="published",
    )  # fmt: skip
    session.add(pub)
    session.flush()
    session.add(ThreadsInsightSnapshot(
        threads_publication_id=pub.id, threads_media_id="m1", observed_at=_NOW, views=0,
        likes=0, replies=0, reposts=0, quotes=0, shares=0, outcome="observed",
    ))  # fmt: skip
    session.commit()
    rows = publication_rows(session)
    assert rows[0]["publication_id"] == pub.id and rows[0]["proposal_id"] == proposal.id
    assert rows[0]["conversation_hook"] == hook_from_provenance(proposal.learning_guidance_json)
    assert rows[0]["angle"] == proposal.angle and rows[0]["link_mode"] == proposal.link_mode
    assert rows[0]["reply_rate"] is None  # views=0 でも落ちない
    assert by_hook(rows)[rows[0]["conversation_hook"]]["publications"] == 1
