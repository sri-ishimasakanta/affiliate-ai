"""T6.3.3c: Growth Post の多様さと確かさ + 日付のトピックの方針 (偽の Luna・偽の Threads だけ)。

- 1 日に **検査を通った提案 1 本** を目指す (呼び出しは多くても 4 回、記録から数える)
- 似すぎたら別の書き方 (family) で新しく書く。形の失敗は同じ書き方で 1 回だけ書き直す
- Growth のトピック: 公開の JST の日付が 2026-10-04 まで "インサイト祭り"、10-05 からなし
- 記事の投稿 (Luna・トピック・枠) は変えない

OpenAI・Threads・WordPress の本物には触れない。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.approval.review_snapshot import build_snapshot
from app.models import ThreadsPostProposal, ThreadsPublicationAttempt
from app.services.threads_approval_digest_service import _topic_text
from app.social.threads import growth_strategy as gs
from app.social.threads.growth import (
    GROWTH_SIMILARITY_MAX,
    GrowthBrief,
    day_window,
    profile_hash,
    validate,
)
from app.social.threads.topic import (
    GROWTH_TOPIC_AUTO_REPLACEMENT,
    GROWTH_TOPIC_LAST_DAY,
    TopicPolicyError,
    topic_tag_for,
    topic_tag_for_proposal,
)
from tests.integration.test_threads_growth_posts import (
    BODY_A,
    BODY_C,
    DAY,
    MORNING,
    FakeThreads,
    GrowthLuna,
    _client,
    _growth,
    _growth_rows,
    _record,
)
from tests.integration.test_threads_topic_tag import FakeMeta, _proposal, _publisher

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[2]


class FailingLuna(GrowthLuna):
    """Responses API の代役: 決まった HTTP の失敗を返す (台本が尽きたら本文を返す)。"""

    def __init__(self, statuses, bodies=()) -> None:
        super().__init__(bodies)
        self.statuses = list(statuses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.statuses:
            self.requests.append(request)
            return httpx.Response(self.statuses.pop(0), json={"error": {"message": "x"}})
        return super().handler(request)


def _yesterday(session, tmp_path, body=BODY_A) -> None:
    _growth(session, tmp_path, GrowthLuna([body])).maintain(
        now=MORNING - timedelta(days=1), execute=True
    )


# -- 方針 ---------------------------------------------------------------------------------------


def test_the_strategy_policy_is_versioned_and_bounded() -> None:
    assert gs.GROWTH_STRATEGY_POLICY_VERSION == "threads-growth-strategy-1"
    assert gs.MAX_GROWTH_MODEL_CALLS_PER_DAY == 4
    assert set(gs.FAMILY_NAMES) == {
        "account_identity", "goal_progress", "build_in_public", "behind_the_scenes",
        "lesson_learned", "failure_improvement", "experiment", "community_question", "principle",
        "next_step", "milestone", "mutual_growth"}  # fmt: skip
    assert set(gs.HOOKS) == {"question", "experience", "progress", "observation", "opinion",
                             "lesson", "challenge", "direct_statement"}  # fmt: skip
    assert set(gs.CTAS) == {"follow_connect", "mutual_growth", "question", "experience_share",
                            "soft_connection", "none"}  # fmt: skip
    assert set(gs.STRUCTURES) == {"single_short_point", "two_paragraph", "progress_then_invite",
                                  "lesson_then_question", "observation_then_connection",
                                  "question_then_context"}  # fmt: skip
    for family in gs.FAMILIES.values():
        assert set(family.hooks) <= set(gs.HOOKS) and set(family.ctas) <= set(gs.CTAS)
        assert set(family.structures) <= set(gs.STRUCTURES)


def test_the_similarity_threshold_is_unchanged() -> None:
    assert GROWTH_SIMILARITY_MAX == 0.5


def test_fact_families_need_real_facts() -> None:
    without = set(gs.eligible_families(()))
    assert without == {"account_identity", "build_in_public", "community_question", "principle",
                       "mutual_growth"}  # fmt: skip
    assert "goal_progress" not in without  # フォロワー数の観測が無いと進み具合は書かない
    assert "milestone" not in without and "experiment" not in without
    assert "goal_progress" in gs.eligible_families({gs.FACT_FOLLOWER_COUNT})
    assert "experiment" in gs.eligible_families({"experiment"})


def test_the_facts_file_is_public_safe_and_dated(tmp_path) -> None:
    facts = gs.load_facts()
    assert {f.kind for f in facts} <= set(gs.FACT_KINDS)
    assert "milestone" not in {f.kind for f in facts}  # 本物の節目が無いので使わない
    assert gs.active_facts(facts, date(2026, 9, 28)) == {}  # 書かれた日より前は使わない
    assert set(gs.active_facts(facts, date(2026, 10, 1))) == {f.kind for f in facts}
    for bad in ("詳しくは https://example.invalid", "D:\\Projects\\x", "token を更新した",
                "data/threads-growth に保存"):  # fmt: skip
        path = tmp_path / "facts.json"
        path.write_text(json.dumps({"facts": [{"id": "x", "kind": "lesson", "text": bad,
                                               "valid_from": "2026-01-01",
                                               "valid_until": "2026-12-31"}]}), "utf-8")
        with pytest.raises(gs.GrowthStrategyError):
            gs.load_facts(path)


def test_the_order_is_deterministic_and_avoids_repeats() -> None:
    families = gs.eligible_families(())
    first = gs.next_strategy(DAY, families=families, history=[], tried=[])
    assert first == gs.next_strategy(DAY, families=families, history=[], tried=[])
    # 前の日と同じ family は、ほかがあれば選ばない。
    yesterday = [gs.HistoryItem("2026-09-27", first.family, first.hook, first.cta,
                                first.structure)]  # fmt: skip
    after = gs.next_strategy(DAY, families=families, history=yesterday, tried=[])
    assert after.family != first.family
    # 同じ signature は、同じ family の中でも後ろに回る。
    order = gs.signature_order(DAY, first.family, yesterday)
    assert order[0].signature != first.signature and order[-1] != order[0]


def test_retries_change_family_then_two_dimensions_then_stop() -> None:
    only = ("community_question",)
    tried = [gs.next_strategy(DAY, families=only, history=[], tried=[])]
    second = gs.next_strategy(DAY, families=only, history=[], tried=tried)
    assert second is not None and second.family == "community_question"
    assert second.differs_in(tried[0]) >= 2  # 同じ family なら hook/CTA/structure の 2 つ以上
    tried.append(second)
    assert gs.next_strategy(DAY, families=only, history=[], tried=tried) is None


def test_failure_classification() -> None:
    assert gs.classify_validation(["growth_duplicate", "growth_length"]) == "validation_similarity"
    assert gs.classify_validation(["growth_money_claim", "growth_length"]) == "validation_fact"
    assert gs.classify_validation(["growth_hook_mismatch"]) == "validation_hook"
    assert gs.classify_validation(["growth_length", "growth_cta_missing"]) == "validation_format"
    assert gs.classify_provider("auth") == gs.classify_provider("bad_request") == "provider_auth"
    assert gs.classify_provider("server_error") == gs.classify_provider("timeout") == (
        "provider_transient")  # fmt: skip
    assert gs.classify_provider("malformed") == "validation_format"
    assert set(gs.FAILURE_CLASSES) >= {"follower_target_reached", "strategy_exhausted",
                                       "model_call_budget_exhausted"}  # fmt: skip


def test_validation_follows_the_strategy_without_weakening_the_fact_guard() -> None:
    lesson = gs.Strategy("lesson_learned", "lesson", "question", "lesson_then_question")
    brief = GrowthBrief(day=DAY, angle="lesson_learned", follower_target=100, strategy=lesson)
    body = ("AIでメディア運営を自動化していて分かったのは、画面と照らさないとズレに気づけない"
            "ことでした。記録の仕組みも、少しずつ直しながら育てています。\n\n"
            "同じように自動化を試している方は、どうやって確かめていますか？")  # fmt: skip
    assert validate(body, brief)["ok"] is True  # 目標の人数は、この書き方では書かなくてよい
    identity = GrowthBrief(day=DAY, angle="account_identity", follower_target=100,
                           strategy=gs.Strategy("account_identity", "question",
                                                "soft_connection", "two_paragraph"))  # fmt: skip
    assert "state the current goal (100 followers)" in validate(body, identity)["problems"]
    no_question = body.replace("いますか？", "います。")
    assert any("closing" in p for p in validate(no_question, brief)["problems"])
    invented = body.replace("分かったのは", "フォロワーが現在48人になって分かったのは")
    assert any("unsupported follower number" in p or "state progress" in p
               for p in validate(invented, brief)["problems"])  # fmt: skip
    hook = GrowthBrief(day=DAY, angle="community_question", follower_target=100,
                       strategy=gs.Strategy("community_question", "question", "question",
                                            "question_then_context"))  # fmt: skip
    statement = "AIで自動化しています。\n\n皆さんはどうしていますか？"
    assert "the requested question hook is missing from the opening" in validate(
        statement, hook)["problems"]  # fmt: skip


def test_growth_strategy_does_not_read_trend_intelligence() -> None:
    for module in ("app/social/threads/growth_strategy.py", "app/social/threads/growth.py",
                   "app/services/threads_growth_service.py"):  # fmt: skip
        source = (ROOT / module).read_text("utf-8")
        for word in ("threads_trend_analysis", "observer", "external_post", "trends import"):
            assert word not in source, (module, word)


# -- Fake E2E A〜H (生成) -----------------------------------------------------------------------


def test_e2e_a_the_first_candidate_passes_with_one_call(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_A])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 1 and out["model_calls_today"] == 1 and len(_growth_rows(session)) == 1
    record = _record(tmp_path)
    assert record["outcome"] == "stored" and record["strategy_policy_version"] == (
        "threads-growth-strategy-1")  # fmt: skip
    call = record["history"][0]
    assert call["purpose"] == "initial" and call["strategy"]["signature"].count("+") == 3
    row = _growth_rows(session)[0]
    assert row.learning_guidance_json["growth"]["strategy"] == call["strategy"]
    prompt = fake.payload(0)["input"][0]["content"]
    assert f"書き方の種類: {call['strategy']['family']}" in prompt
    assert "インサイト祭り" not in prompt and "トピックの言葉は書かない" in prompt


def test_e2e_b_similarity_then_success_with_a_different_strategy(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    fake = GrowthLuna([BODY_A, BODY_C])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 2 and out["created"] and len(_growth_rows(session)) == 2
    first, second = _record(tmp_path)["history"]
    assert first["failure_class"] == "validation_similarity"
    assert second["strategy"]["family"] != first["strategy"]["family"]
    prompt = fake.payload(1)["input"][0]["content"]
    assert "今回は新しい方向で書く" in prompt and "言い換えではなく" in prompt
    assert gs.FAMILIES[second["strategy"]["family"]].intent in prompt
    assert "feedback" not in json.dumps(fake.payload(1)["input"][:1])  # 書き直しの材料を渡さない


def test_e2e_c_several_strategy_retries_then_success(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    fake = GrowthLuna([BODY_A, BODY_A, BODY_C])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 3 <= gs.MAX_GROWTH_MODEL_CALLS_PER_DAY and out["created"]
    history = _record(tmp_path)["history"]
    assert [c["purpose"] for c in history] == ["initial", "strategy_retry", "strategy_retry"]
    assert len({c["strategy"]["family"] for c in history}) == 3
    assert [c["attempt_index"] for c in history] == [1, 2, 3]
    meta = session.get(ThreadsPostProposal, out["created"]).learning_guidance_json["growth"]
    assert (meta["model_call_index"], meta["attempt_index"]) == (3, 3)


def test_e2e_d_exhaustion_makes_no_proposal_and_no_fifth_call(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    fake = GrowthLuna([BODY_A] * 8)
    service = _growth(session, tmp_path, fake)
    out = service.maintain(now=MORNING, execute=True)
    assert fake.calls == 4 and out["created"] is None and out["outcome"] == (
        "growth_generation_exhausted")  # fmt: skip
    for hours in (1, 2, 5):
        _growth(session, tmp_path, fake).maintain(now=MORNING + timedelta(hours=hours),
                                                  execute=True)  # fmt: skip
    assert fake.calls == 4 and len(_growth_rows(session)) == 1  # 昨日の分だけ


def test_e2e_e_a_restart_keeps_the_used_calls_and_strategies(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    fake = FailingLuna([], [BODY_A, BODY_A])
    service = _growth(session, tmp_path, fake)
    original = service._generate_day

    def two_calls(day, observation, now):  # 2 回呼んだところで worker が止まった
        budget = gs.MAX_GROWTH_MODEL_CALLS_PER_DAY
        gs.MAX_GROWTH_MODEL_CALLS_PER_DAY = 2
        try:
            return original(day, observation, now)
        finally:
            gs.MAX_GROWTH_MODEL_CALLS_PER_DAY = budget

    service._generate_day = two_calls
    service.maintain(now=MORNING, execute=True)
    before = _record(tmp_path)
    assert fake.calls == 2 and len(before["history"]) == 2
    before["outcome"] = before["result"] = None  # 止まった時点の記録 (結果はまだ無い)
    before.pop("exhaustion_reason", None)
    (tmp_path / "growth" / f"{DAY.isoformat()}.openai.json").write_text(
        json.dumps(before, ensure_ascii=False), "utf-8")  # fmt: skip
    plan = _growth(session, tmp_path, fake).plan(now=MORNING + timedelta(hours=1))
    assert plan["due"] is True and plan["model_calls_today"] == 2
    again_fake = GrowthLuna([BODY_A, BODY_A, BODY_A])
    again = _growth(session, tmp_path, again_fake).maintain(now=MORNING + timedelta(hours=1),
                                                            execute=True)  # fmt: skip
    assert again_fake.calls == 2  # 残りは 2 回 (0 からやり直さない)
    history = _record(tmp_path)["history"]
    assert len(history) == 4 and [c["call_index"] for c in history] == [1, 2, 3, 4]
    families = [c["strategy"]["family"] for c in history]
    assert len(set(families)) == 4  # 使った書き方は使ったまま
    assert again["outcome"] == "growth_generation_exhausted"


def test_e2e_e2_a_transient_provider_failure_pauses_and_resumes(session, tmp_path) -> None:
    fake = FailingLuna([503, 503, 503], [BODY_A])  # 1 回の呼び出しの中で再送 2 回まで
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert out["created"] is None and "will resume" in out["reason"]
    record = _record(tmp_path)
    assert record["result"] == "paused" and record["history"][0]["failure_class"] == (
        "provider_transient")  # fmt: skip
    later = _growth(session, tmp_path, fake).maintain(now=MORNING + timedelta(hours=1),
                                                      execute=True)  # fmt: skip
    assert later["created"] and later["model_calls_today"] == 2
    first, second = _record(tmp_path)["history"]
    assert first["strategy"] == second["strategy"]  # 候補を見ていないので同じ書き方で


def test_e2e_e3_an_auth_failure_stops_the_day_without_burning_calls(session, tmp_path) -> None:
    fake = FailingLuna([401])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 1 and out["outcome"] == "provider_auth"
    again = _growth(session, tmp_path, fake).maintain(now=MORNING + timedelta(hours=1),
                                                      execute=True)  # fmt: skip
    assert fake.calls == 1 and "finished (provider_auth)" in again["reason"]


def test_e2e_e4_a_format_failure_is_repaired_once_with_the_same_strategy(
    session, tmp_path
) -> None:
    short = "AIで自動化しています？フォローしてね"  # 短すぎる → 同じ書き方で書き直す
    fake = GrowthLuna([short, BODY_A])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 2 and out["created"]
    first, second = _record(tmp_path)["history"]
    assert first["failure_class"] == "validation_format"
    assert second["purpose"] == "repair" and second["strategy"] == first["strategy"]
    assert "growth_length" in second["repair_reason_ids"]


def test_e2e_f_the_follower_target_stops_generation(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_A])
    service = _growth(session, tmp_path, fake, followers=100, collect=True)
    out = service.maintain(now=MORNING, execute=True)
    assert fake.calls == 0 and out["created"] is None and out["follower_target_reached"] is True
    assert out["follower_target"] == 100  # 次の目標を自動で決めない
    assert not (tmp_path / "growth" / f"{DAY.isoformat()}.openai.json").exists()


@pytest.fixture
def article(session):
    from app.models import Article

    row = Article(
        id=21, title="生成AI", slug="gen-ai", body="記事本文。", status="published",
        published_url="https://bizfluxlab.com/gen-ai/", published_at=MORNING - timedelta(days=10),
        article_type="informational", monetization_mode="supporting", wordpress_post_id="84",
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def test_e2e_g_growth_exhaustion_leaves_the_article_lane_alone(session, tmp_path, article):
    _yesterday(session, tmp_path)
    _growth(session, tmp_path, GrowthLuna([BODY_A] * 8)).maintain(now=MORNING, execute=True)
    assert _record(tmp_path)["outcome"] == "growth_generation_exhausted"
    meta = FakeMeta()
    art = _proposal(session, article)
    out = _publisher(session, meta).publish(proposal_id=art.id, execute=True, now=MORNING)
    assert out.outcome == "published" and meta.creates()[0]["topic_tag"] == "AI Threads"


def test_e2e_h_the_2026_09_29_history_is_preserved(session, tmp_path) -> None:
    legacy = {"request_id": "growth-2026-09-28", "content_kind": "account_growth",
              "result": "rejected_by_validation", "model_calls": 2, "repair_calls": 1,
              "reason": "growth check failed: too similar to the recent growth post proposal "
                        "#25 (0.543); vary the wording and angle",
              "history": [{"ordinal": 1, "purpose": "initial"},
                          {"ordinal": 2, "purpose": "repair"}]}  # fmt: skip
    folder = tmp_path / "growth"
    folder.mkdir()
    path = folder / f"{DAY.isoformat()}.openai.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False), "utf-8")
    before = path.read_bytes()
    fake = GrowthLuna([BODY_A])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING + timedelta(hours=3),
                                                    execute=True)  # fmt: skip
    assert fake.calls == 0 and out["created"] is None
    assert "already attempted (no regeneration)" in out["reason"]
    assert path.read_bytes() == before and _growth_rows(session) == []


def test_reliability_and_diversity_are_reported_read_only(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    _growth(session, tmp_path, GrowthLuna([BODY_A, BODY_C])).maintain(now=MORNING, execute=True)
    service = _growth(session, tmp_path, GrowthLuna([]))
    report = service.reliability(now=MORNING + timedelta(hours=1), days=7)
    r = report["reliability"]
    assert (r["eligible_growth_days"], r["valid_proposal_days"]) == (2, 2)
    assert (r["total_model_calls"], r["similarity_rejections"], r["strategy_retries"]) == (3, 1, 1)
    assert r["average_calls_per_valid_proposal"] == 1.5 and r["proposal_success_rate"] == 1.0
    d = report["diversity"]
    assert d["with_strategy"] == 2 and sum(d["family"].values()) == 2
    assert d["repeated_signatures"] == [] and d["calls_per_valid_proposal"] == [2, 1]
    assert any("not fed back" in n for n in report["notes"])
    from sqlalchemy.orm import sessionmaker

    from scripts import report_threads_growth_reliability as cli

    code = cli.main(["--json"], session_factory=sessionmaker(bind=session.get_bind()),
                    now=MORNING + timedelta(hours=1), directory=tmp_path / "growth")  # fmt: skip
    assert code == cli.EXIT_OK


# -- トピック (日付の方針) ------------------------------------------------------------------------

LAST = datetime(2026, 10, 4, 23, 59, 59, tzinfo=JST)
FIRST = datetime(2026, 10, 5, 0, 0, 0, tzinfo=JST)


def test_the_growth_topic_cutoff_is_the_jst_date() -> None:
    assert GROWTH_TOPIC_LAST_DAY == date(2026, 10, 4) and GROWTH_TOPIC_AUTO_REPLACEMENT is False
    assert topic_tag_for("account_growth", at=LAST) == "インサイト祭り"
    assert topic_tag_for("account_growth", at=FIRST) is None
    # UTC の日付では決めない (10/5 00:00 JST = 10/4 15:00 UTC)。
    assert topic_tag_for("account_growth", at=FIRST.astimezone(UTC)) is None
    assert topic_tag_for("account_growth", at=LAST.astimezone(UTC)) == "インサイト祭り"
    for moment in (LAST, FIRST, FIRST + timedelta(days=30)):
        assert topic_tag_for("article", at=moment) == "AI Threads"
    with pytest.raises(TopicPolicyError):
        topic_tag_for("account_growth")  # 公開の時刻が無ければ決めない
    with pytest.raises(TopicPolicyError):
        topic_tag_for("account_growth", at=datetime(2026, 10, 4, 12, 0))  # 時差なし
    with pytest.raises(TopicPolicyError):
        topic_tag_for("digest", at=LAST)


def _growth_for(session, day: date, *, not_before: datetime | None = None, text=BODY_A):
    start, end = day_window(day, JST)
    row = ThreadsPostProposal(
        source_article_id=None, source_article_body_hash=profile_hash(), angle="account_growth",
        link_mode="none", content_text=text, character_count=len(text),
        content_seed=f"{day}" * 6, proposal_hash=f"{day}".replace("-", "") * 8,
        policy_version="t6.3.3", generator_version="threads-growth-2", status="approved",
        approved_at=start, not_before=not_before or start, expires_at=end,
        learning_guidance_json={"content_kind": "account_growth",
                                "growth": {"date_jst": day.isoformat(), "follower_target": 100}},
    )  # fmt: skip
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_the_publication_plan_uses_the_exact_boundary(session) -> None:
    before = _growth_for(session, date(2026, 10, 4))
    after = _growth_for(session, date(2026, 10, 5), not_before=FIRST)
    plan_last = _publisher(session, FakeMeta()).plan(proposal_id=before.id, now=LAST)
    plan_first = _publisher(session, FakeMeta()).plan(proposal_id=after.id, now=FIRST)
    assert (plan_last.topic_tag, plan_last.topic_decision) == ("インサイト祭り", "policy_topic")
    assert (plan_first.topic_tag, plan_first.topic_decision) == (None, "policy_no_topic")
    # 23:00 からは公開の窓の外なので、実際にはこの時刻には出ない (トピックの決め方は同じ)。
    assert any("publication window" in r for r in plan_last.blocked_reasons)


def test_e2e_i_growth_before_the_cutoff_sends_the_topic(session) -> None:
    row = _growth_for(session, date(2026, 10, 4))
    before = (row.content_text, row.proposal_hash, row.character_count)
    meta = FakeMeta()
    now = datetime(2026, 10, 4, 22, 59, tzinfo=JST)  # 公開の窓の最後の分
    out = _publisher(session, meta).publish(proposal_id=row.id, execute=True, now=now)
    assert out.outcome == "published"
    (create,) = meta.creates()
    assert (create["media_type"], create["text"], create["topic_tag"]) == (
        "TEXT", BODY_A, "インサイト祭り")  # fmt: skip
    assert "インサイト祭り" not in create["text"]
    session.refresh(row)
    assert (row.content_text, row.proposal_hash, row.character_count) == before
    step = session.scalars(select(ThreadsPublicationAttempt)).first()
    assert step.detail_json["topic_decision"] == "policy_topic"


def test_e2e_j_growth_after_the_cutoff_sends_no_topic_by_policy(session) -> None:
    row = _growth_for(session, date(2026, 10, 5))
    meta = FakeMeta(reject_topic=True)  # トピックを送れば断る Threads (送らないので通る)
    now = datetime(2026, 10, 5, 7, 5, tzinfo=JST)
    out = _publisher(session, meta).publish(proposal_id=row.id, execute=True, now=now)
    assert out.outcome == "published"
    (create,) = meta.creates()  # 1 回だけ。トピック付きで試してから外す、はしない
    assert "topic_tag" not in create and create["text"] == BODY_A
    assert create["media_type"] == "TEXT"
    step = session.scalars(select(ThreadsPublicationAttempt)).first()
    assert step.detail_json["topic_tag_sent"] is False
    assert step.detail_json["topic_decision"] == "policy_no_topic"  # 方針でなし (失敗ではない)
    assert step.outcome == "succeeded"


def test_before_the_cutoff_a_rejected_topic_has_no_untagged_fallback(session) -> None:
    row = _growth_for(session, date(2026, 10, 4))
    meta = FakeMeta(reject_topic=True)
    now = datetime(2026, 10, 4, 12, 0, tzinfo=JST)
    out = _publisher(session, meta).publish(proposal_id=row.id, execute=True, now=now)
    assert out.outcome == "failed" and len(meta.creates()) == 1
    assert meta.creates()[0]["topic_tag"] == "インサイト祭り" and meta.publishes() == []


def test_e2e_k_articles_keep_ai_threads_across_the_cutoff(session, article) -> None:
    tags = []
    for seed, now in (("a", datetime(2026, 10, 4, 22, 0, tzinfo=JST)),
                      ("b", datetime(2026, 10, 5, 8, 0, tzinfo=JST))):  # fmt: skip
        meta = FakeMeta(media_id=f"media-{seed}")
        row = _proposal(session, article, seed=seed)
        out = _publisher(session, meta).publish(proposal_id=row.id, execute=True, now=now,
                                                gap_override="test")  # fmt: skip
        assert out.outcome == "published", out.blocked_reasons
        tags.append(meta.creates()[0]["topic_tag"])
    assert tags == ["AI Threads", "AI Threads"]


def test_the_approval_display_follows_the_publication_day(session) -> None:
    before = _growth_for(session, date(2026, 10, 4))
    after = _growth_for(session, date(2026, 10, 5))
    for row, topic in ((before, "インサイト祭り"), (after, "なし")):
        snap = build_snapshot(subject_type="threads_post", subject=row, article=None)
        assert snap["post_kind_label"] == "Growth Post" and snap["goal_label"] == "フォロワー100人"
        assert snap["link_mode"] == "なし" and snap["topic_label"] == topic
        assert _topic_text(row) == topic
    assert topic_tag_for_proposal(after) is None


def test_historical_rows_are_not_rewritten(session, tmp_path) -> None:
    old = _growth_for(session, date(2026, 9, 28), text=BODY_C)
    frozen = (old.content_text, old.proposal_hash, dict(old.learning_guidance_json))
    _growth(session, tmp_path, GrowthLuna([BODY_A])).maintain(now=MORNING + timedelta(days=7),
                                                              execute=True)  # fmt: skip
    session.refresh(old)
    assert (old.content_text, old.proposal_hash, old.learning_guidance_json) == frozen
    assert "topic" not in json.dumps(old.learning_guidance_json)  # トピックを書き足さない


def test_the_article_generation_path_does_not_use_growth_strategy() -> None:
    for module in ("app/services/threads_proposal_service.py", "app/social/threads/prompt.py",
                   "app/social/threads/quality.py", "app/social/threads/conversation.py"):
        source = (ROOT / module).read_text("utf-8")
        assert "growth_strategy" not in source and "GROWTH_TOPIC_LAST_DAY" not in source


def test_the_client_is_bounded_even_through_the_worker(session, tmp_path) -> None:
    from app.services.threads_worker_service import ThreadsWorkerService
    from app.social.threads.worker import SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE
    from tests.integration.test_threads_worker_service import _factory

    _yesterday(session, tmp_path)
    fake = GrowthLuna([BODY_A] * 10)
    worker = ThreadsWorkerService(_factory(session), settings=type("S", (), {})(),
                                  threads_service=FakeThreads(), timezone=JST,
                                  maintain_growth_posts=True, growth_client=_client(fake),
                                  growth_directory=tmp_path / "growth")  # fmt: skip
    for hours in range(0, 6):
        worker.handlers()[SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE](MORNING + timedelta(hours=hours))
    assert fake.calls == gs.MAX_GROWTH_MODEL_CALLS_PER_DAY
