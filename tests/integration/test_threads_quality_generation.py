"""T6.3.1: 質の検査つきの生成 (偽の Luna だけ。本物の API・本番の依頼は使わない)。

偽の E2E (本番の #12 で見えた問題を再現する):

- A: #12 のような詰め込んだ本文 (金額 4 つ・二択の終わり方) → 書き直し → 1 要点の短い案
- B: 最近の案と同じ Krisp / Fireflies の料金 ($8・$18・$10) を言い換え → 重なり → 別の要点
  (書き直しでも重なれば、保存せずに manual の依頼として残す)
- C: question を求めたのに二択 → 書き直しで開いた問い (きっかけは同じ)
- D: 同じ記事で本当に違う要点 → そのまま保存

どれも awaiting_approval のまま。公開・承認・通知は増えない。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.models import (
    TP_AWAITING_APPROVAL,
    MobileApprovalSession,
    NotificationDelivery,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_generation_provider import ManualFileProvider
from app.social.threads.quality import RECENT_WINDOW, hook_forms, money_figures
from tests.integration.test_threads_openai_generation import FakeLuna, _provider, seed_articles
from tests.integration.test_threads_proposal_stock_service import _NOW, _service

FACTS = (
    "AI議事録の比較。Fireflies.aiはFree $0でFree forever。Krispは7日間のFree Trial。"
    "KrispのCoreは月$8/ユーザー。Fireflies.aiのProは月払い$18、年払い$10。2026年9月時点。"
    "Krispはノイズキャンセリングも用途に含む。会議の聞き取りにくさを減らせる。"
)
DENSE = (
    "AI議事録を無料で続けたいなら、まず確認するのは無料の期間。Fireflies.aiはFree $0でFree "
    "forever、Krispは7日間のFree Trial。最初の一歩は同じ会議の音声を両方で試すこと。料金まで比べる"
    "なら、KrispのCoreは月$8/ユーザー、Fireflies.aiのProは月払い$18、年払い$10（2026年9月時点）。"
    "無料継続と音声品質、先に見るのはどちらですか？"
)
ONE_POINT_QUESTION = (
    "AI議事録を無料で続けるなら、先に見るのは無料の期間。Fireflies.aiは期限のないFree、"
    "Krispは7日間のFree Trial。同じ無料でも、続け方がまるで違う。今の会議だと、無料期間の"
    "どこがいちばん気になる？"
)
PRICE_REWORD = (
    "料金で迷ったら、支払い方をそろえて見る。Fireflies.aiのProは年払いなら$10、月払いだと$18。"
    "KrispのCoreは$8。数字だけ並べると、比べ方を誤る。今の選び方で、どこを基準にしている？"
)
NOISE_POINT = (
    "会議の聞き取りにくさが悩みなら、文字起こしより先に音声を見る。Krispはノイズキャンセリングも"
    "用途に含む。雑音が減ると、議事録の手直しも減る。いまの会議で、いちばん聞き取りにくいのは"
    "どんな場面？"
)
EXISTING_PRICE = (
    "AI議事録選びでありがちな間違いは、月額の数字だけで比べること。Fireflies.aiは月払い$18、"
    "年払い$10、KrispのCoreは$8。支払い条件をそろえないと比較を誤りやすい。"
)


@pytest.fixture
def articles(session):
    rows = seed_articles(session)
    for article in rows:
        article.body = FACTS + f" 記事{article.id}。"
    session.commit()
    return rows


def _request(session, tmp_path, *, article_id=21, hook="question", link_mode="none",
             angle="beginner_tip"):  # fmt: skip
    """計画を通さずに、決めた形の依頼を 1 件作る (manual の形で置く)。"""

    service = _service(session, tmp_path, provider=ManualFileProvider(tmp_path / "gen"))
    planned = {"article_id": article_id, "article_title": f"記事{article_id}", "angle": angle,
               "link_mode": link_mode, "reasons": ["test"], "conversation_hook": hook}  # fmt: skip
    request = service._build_request(planned, _NOW, "unused")
    service.provider.submit(request)
    return request


def _existing_price_proposal(session, tmp_path):
    service = _service(session, tmp_path)
    rows = service._proposals.persist(
        article_id=21,
        generated_output=json.dumps(
            {
                "proposals": [
                    {"angle": "common_mistake", "link_mode": "none", "body": EXISTING_PRICE}
                ]
            },
            ensure_ascii=False,
        ),  # fmt: skip
        now=_NOW,
        learning_as_of=_NOW,
    )
    return rows[0]


def _generate(session, tmp_path, request, fake):
    provider = _provider(tmp_path, fake)
    return _service(session, tmp_path, provider=provider).generate_pending(
        request.request_id, now=_NOW
    )


def _body(session, pid) -> str:
    return session.get(ThreadsPostProposal, pid).content_text


def _no_side_effects(session):
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0
    assert session.scalar(select(func.count()).select_from(MobileApprovalSession)) == 0
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
    rows = session.scalars(select(ThreadsPostProposal)).all()
    assert all(r.status == TP_AWAITING_APPROVAL and r.approved_at is None for r in rows)


def test_case_a_a_dense_12_style_post_is_repaired_into_one_point(
    session, articles, tmp_path
) -> None:
    request = _request(session, tmp_path, hook="question")
    fake = FakeLuna([DENSE, ONE_POINT_QUESTION])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    repair = fake.payload(1)["input"][2]["content"]
    assert "prices" in repair and "A/B choice" in repair
    body = _body(session, out["created"][0])
    assert body == ONE_POINT_QUESTION and len(money_figures(body)) <= 2
    brief = session.get(ThreadsPostProposal, out["created"][0]).learning_guidance_json[
        "generation_brief"
    ]
    assert brief["conversation_hook"] == "question" and brief["quality"]["hook_forms"] == [
        "question"
    ]
    assert brief["topic_signature"]["entities"] == ["fireflies.ai", "krisp"]
    _no_side_effects(session)


def test_case_b_same_price_topic_is_caught_and_a_different_point_is_used(
    session, articles, tmp_path
) -> None:
    _existing_price_proposal(session, tmp_path)
    request = _request(session, tmp_path, article_id=22, hook="question")
    assert "$18" in request.prompt and "最近の話題" in request.prompt  # 避ける話題を渡している
    fake = FakeLuna([PRICE_REWORD, NOISE_POINT])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    repair = fake.payload(1)["input"][2]["content"]
    assert "recent topic overlap" in repair and "different main point" in repair
    assert _body(session, out["created"][0]) == NOISE_POINT
    _no_side_effects(session)


def test_case_b_prime_no_different_point_fails_safely(session, articles, tmp_path) -> None:
    _existing_price_proposal(session, tmp_path)
    request = _request(session, tmp_path, article_id=22, hook="question")
    fake = FakeLuna([PRICE_REWORD, PRICE_REWORD])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and out["created"] == []  # 書き直し 1 回まで。繰り返さない
    assert any("recent topic overlap" in f["reason"] for f in out["failures"])
    assert (tmp_path / "gen/pending" / f"{request.request_id}.request.json").exists()
    again = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and again["created"] == []  # 同じ依頼を送り直さない
    assert session.scalar(select(func.count()).select_from(ThreadsPostProposal)) == 1


def test_case_c_a_choice_ending_for_question_is_repaired_with_the_same_hook(
    session, articles, tmp_path
) -> None:
    choice_like = (
        "AI議事録を無料で続けるなら、先に見るのは無料の期間。Fireflies.aiは期限のないFree、"
        "Krispは7日間のFree Trial。無料期間と音声品質、先に見るのはどちらですか？"
    )
    request = _request(session, tmp_path, hook="question")
    fake = FakeLuna([choice_like, ONE_POINT_QUESTION])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    assert "conversation_hook" in fake.payload(1)["text"]["format"]["schema"]["properties"][
        "proposals"]["items"]["properties"]  # fmt: skip
    hooks = [
        p["text"]["format"]["schema"]["properties"]["proposals"]["items"]["properties"][
            "conversation_hook"]["enum"] for p in (fake.payload(0), fake.payload(1))
    ]  # fmt: skip
    assert hooks == [["question"], ["question"]]  # 書き直しでもきっかけは同じ
    assert hook_forms(_body(session, out["created"][0])) == {"question"}
    _no_side_effects(session)


def test_case_d_same_article_different_point_is_accepted(session, articles, tmp_path) -> None:
    _existing_price_proposal(session, tmp_path)  # 記事 21 の料金の案
    request = _request(session, tmp_path, article_id=21, hook="question")
    fake = FakeLuna([NOISE_POINT])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 1 and len(out["created"]) == 1  # 同じ記事でも要点が違えば通る
    _no_side_effects(session)


def test_the_recent_window_is_bounded(session, articles, tmp_path) -> None:
    service = _service(session, tmp_path)
    for i in range(RECENT_WINDOW + 4):
        service._proposals.persist(
            article_id=21 + i % 5,
            generated_output=json.dumps(
                {
                    "proposals": [
                        {
                            "angle": "insight",
                            "link_mode": "none",
                            "body": f"別々の話 その{i}。手順を{i}回見直す。",
                        }
                    ]
                },
                ensure_ascii=False,
            ),  # fmt: skip
            now=_NOW,
            learning_as_of=_NOW,
        )
    assert len(service._recent_items()) == RECENT_WINDOW


def test_manual_answers_keep_quality_as_warnings(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question")
    (tmp_path / "gen/pending" / f"{request.request_id}.response.json").write_text(
        json.dumps({"proposals": [{"angle": "beginner_tip", "conversation_hook": "question",
                    "link_mode": "none", "body": DENSE}]}, ensure_ascii=False),
        encoding="utf-8",
    )  # fmt: skip
    out = _service(session, tmp_path, provider=ManualFileProvider(tmp_path / "gen")).maintain(
        now=_NOW, execute=True, collect_only=True
    )
    assert len(out["created"]) == 1  # 人の答えは質の問題で落とさない (警告として残す)
    row = session.get(ThreadsPostProposal, out["created"][0])
    assert any(w.startswith("quality:") for w in row.warnings_json)
