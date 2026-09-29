"""T6.3.3: 毎日 1 本の Growth Post (偽の Luna・偽の Threads だけ。本番の API は使わない)。

偽の E2E:

- A: その日の Growth Post が無い・フォロワー数が分からない → 1 本 (awaiting_approval・リンクなし・
  トピックなし・記事なし)
- B: 新しい観測 (63 人) → 63 / 100・あと 37 人を書いてよい → 観測の記録を残す
- C: 観測が 100 人以上 → 生成 0 回・提案なし・目標到達 (人が次の目標を決める)
- D: 同じ日に何度も保守・再起動 → 提案 1 つ・呼び出し 1 回
- E: 記事の投稿は T6.3.1 の検査のまま・トピック "AI Threads"・フォローのお願いは許さない
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    PUB_PUBLISHED,
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    Article,
    MobileApprovalSession,
    NotificationDelivery,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_growth_service import ThreadsGrowthService
from app.services.threads_openai_provider import OpenAIResponsesClient
from app.services.threads_publication_service import ThreadsPublicationService
from app.social.threads.conversation import conversation_errors
from app.social.threads.growth import (
    GROWTH_ANGLES,
    GROWTH_FOLLOWER_TARGET,
    GROWTH_POST_TARGET_PER_JST_DAY,
    FollowerObservation,
    GrowthBrief,
    day_window,
    profile_hash,
    select_angle,
    validate,
)
from app.social.threads.growth_strategy import FAMILY_NAMES
from app.social.threads.models import ThreadsInsights
from app.social.threads.policy import get_operations_policy
from app.social.threads.queue import (
    KIND_ACCOUNT_GROWTH,
    REASON_EXPIRED,
    REASON_GROWTH_DAILY_LIMIT,
    CandidateFacts,
    QueueFacts,
    evaluate_queue,
)
from app.social.threads.schedule import daily_activity
from app.social.threads.topic import (
    TopicPolicyError,
    content_kind,
    topic_tag_for,
    topic_tag_for_proposal,
)

JST = ZoneInfo("Asia/Tokyo")
API_KEY = "sk-test-NEVER-A-REAL-KEY-growth-0123456789"
DAY = date(2026, 9, 28)
MORNING = datetime(2026, 9, 28, 7, 5, tzinfo=JST).astimezone(UTC)

BODY_A = (
    "AIを使って、収益メディアをどこまで自動化できると思いますか？WordPressの記事づくりから"
    "Threadsへの投稿、計測まで、実際に作りながら検証して記録しています🛠️\n\n"
    "まずはフォロワー100人が目標です。AI活用やブログ運営、自動化に取り組んでいる方、気軽に"
    "つながってください。フォローいただけたら、こちらからもフォローします！"
)
BODY_B = (
    "AIでメディア運営をどこまで自動化できるか、気になりませんか？作りながら公開しているアカウントです。"
    "記事の下書きも、Threadsの投稿も、数字の振り返りも仕組みにしています。\n\n"
    "いまのフォロワーは現在63人。目標の100人まで、あと37人です。同じようにAIや自動化を"
    "試している方、フォロバしますので一緒に伸ばしていきましょう🙌"
)
BODY_C = (
    "メディア運営は、AIでどこまで自動化できるのでしょう？作りながら公開しています。記事の下書き、"
    "Threadsの投稿、数字の振り返りまで、仕組みごと少しずつ育てています。\n\n"
    "目標はフォロワー100人。同じようにAIや自動化を試している方と、一緒に伸ばしていけたら"
    "うれしいです。フォロバします🙌"
)


class GrowthLuna:
    """Responses API の代役 (MockTransport)。台本の本文を、求められた切り口で返す。"""

    def __init__(self, bodies) -> None:
        self.bodies = list(bodies)
        self.requests: list[httpx.Request] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def payload(self, index=-1) -> dict:
        return json.loads(self.requests[index].content)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = json.loads(request.content)
        props = payload["text"]["format"]["schema"]["properties"]["proposals"]["items"][
            "properties"
        ]
        body = self.bodies.pop(0) if self.bodies else BODY_C
        item = {"angle": props["angle"]["enum"][0], "link_mode": "none", "body": body}
        return httpx.Response(200, json={
            "id": f"resp_g{self.calls}", "model": "gpt-5.6-luna", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text",
                        "text": json.dumps({"proposals": [item]}, ensure_ascii=False)}]}],
            "usage": {"input_tokens": 900, "output_tokens": 120, "total_tokens": 1020,
                      "secret": API_KEY},
        })  # fmt: skip


class FakeThreads:
    """アカウントの指標を返すだけの Threads (読むだけ)。書き込みは失敗させる。"""

    def __init__(self, followers=None) -> None:
        self.followers = followers
        self.reads = 0

    def user_insights(self, metrics, **_kw):
        self.reads += 1
        values = {} if self.followers is None else {"followers_count": self.followers}
        return ThreadsInsights(subject="user", values=values, missing=())

    def __getattr__(self, name):
        raise AssertionError(f"growth maintenance must not call Threads.{name}")


def _client(fake: GrowthLuna) -> OpenAIResponsesClient:
    return OpenAIResponsesClient(
        api_key=API_KEY, transport=httpx.MockTransport(fake.handler), sleep=lambda _s: None
    )


def _growth(session, tmp_path, fake=None, *, followers=None, collect=False, client=True):
    return ThreadsGrowthService(
        session,
        timezone=JST,
        client=_client(fake or GrowthLuna([BODY_A])) if client else None,
        threads_service=FakeThreads(followers),
        directory=tmp_path / "growth",
        collect_followers=collect,
    )


def _growth_rows(session):
    return session.scalars(
        select(ThreadsPostProposal).where(ThreadsPostProposal.source_article_id.is_(None))
    ).all()


def _record(tmp_path, day=DAY) -> dict:
    return json.loads((tmp_path / "growth" / f"{day.isoformat()}.openai.json").read_text("utf-8"))


def _no_side_effects(session) -> None:
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0
    assert session.scalar(select(func.count()).select_from(MobileApprovalSession)) == 0
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
    assert all(r.approved_at is None for r in _growth_rows(session))


@pytest.fixture
def article(session) -> Article:
    row = Article(
        id=21, title="生成AIガイドライン", slug="generative-ai-guidelines", body="記事本文。",
        status="published", published_url="https://bizfluxlab.com/generative-ai-guidelines/",
        published_at=MORNING - timedelta(days=10), article_type="informational",
        monetization_mode="supporting", wordpress_post_id="84",
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _article_proposal(session, article, *, status=TP_APPROVED, seed="a", text="記事の投稿。"):
    row = ThreadsPostProposal(
        source_article_id=article.id, source_article_body_hash=compute_text_hash(article.body),
        angle="insight", link_mode="none", content_text=text, character_count=len(text),
        destination_url=None, content_seed=seed * 64, proposal_hash=chr(ord(seed) + 1) * 64,
        policy_version="t2.1", generator_version="threads-proposal-1", status=status,
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


# --- the data model / content kind -------------------------------------------------------------


def test_a_legacy_article_proposal_stays_article_with_the_fixed_topic(session, article) -> None:
    row = _article_proposal(session, article)
    assert row.learning_guidance_json is None
    assert content_kind(row) == "article" and topic_tag_for(content_kind(row)) == "AI Threads"


def test_article_content_requires_a_source_article() -> None:
    orphan = type("P", (), {"learning_guidance_json": None, "source_article_id": None})()
    with pytest.raises(TopicPolicyError, match="must declare content_kind=account_growth"):
        content_kind(orphan)
    marked = type("P", (), {"learning_guidance_json": {"content_kind": "article"},
                            "source_article_id": None})()  # fmt: skip
    with pytest.raises(TopicPolicyError):
        content_kind(marked)


def test_account_growth_requires_a_null_source_article() -> None:
    growth = {"content_kind": "account_growth"}
    ok = type("P", (), {"learning_guidance_json": growth, "source_article_id": None})()
    bad = type("P", (), {"learning_guidance_json": growth, "source_article_id": 21})()
    assert content_kind(ok) == "account_growth"
    assert topic_tag_for("account_growth", at=MORNING) == "インサイト祭り"  # 10/4 まで
    with pytest.raises(TopicPolicyError, match="must not have a source article"):
        content_kind(bad)


def test_unknown_null_source_content_is_never_published(session) -> None:
    row = ThreadsPostProposal(
        source_article_id=None, source_article_body_hash="0" * 64, angle="x", link_mode="none",
        content_text="記事のない投稿。", character_count=8, content_seed="z" * 64,
        proposal_hash="y" * 64, policy_version="t", generator_version="g", status=TP_APPROVED,
    )  # fmt: skip
    session.add(row)
    session.commit()
    plan = ThreadsPublicationService(
        session, settings=type("S", (), {})(), threads_service=_ReadyThreads(),
        sleep=lambda _s: None, gap_minutes=120,
    ).plan(proposal_id=row.id, now=MORNING)  # fmt: skip
    assert not plan.ok
    assert any("topic policy" in r for r in plan.blocked_reasons)
    assert any("account_growth" in r for r in plan.blocked_reasons + plan.stale_reasons)


class _ReadyThreads:
    def describe(self):
        from app.social.threads.service import ThreadsConnectionStatus

        return ThreadsConnectionStatus(enabled=True, configured=True, api_version="v1.0",
                                       user_id_configured=True, access_token_configured=True)

    @property
    def client(self):
        raise AssertionError("planning must not call Threads")


# --- fake E2E ----------------------------------------------------------------------------------


def test_case_a_one_growth_post_is_prepared_for_the_day(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_A])
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 1 and out["model_calls"] == 1 and out["created"]
    row = session.get(ThreadsPostProposal, out["created"])
    assert row.status == TP_AWAITING_APPROVAL and row.approved_at is None
    assert row.source_article_id is None and row.link_mode == "none"
    assert row.destination_url is None and row.content_text == BODY_A
    assert row.angle == "account_growth"
    assert content_kind(row) == "account_growth"
    # T6.3.3b/c: 公開のときに付ける (公開の日 2026-09-28 JST は 10/4 より前)。
    assert topic_tag_for_proposal(row) == "インサイト祭り"
    meta = row.learning_guidance_json["growth"]
    assert meta["date_jst"] == "2026-09-28" and meta["follower_target"] == 100
    assert meta["follower_observation"] is None and meta["uses_follower_count"] is False
    assert meta["angle"] in FAMILY_NAMES and meta["strategy"]["family"] == meta["angle"]
    assert meta["strategy_policy_version"] == "threads-growth-strategy-1"
    assert (meta["model_call_index"], meta["attempt_index"]) == (1, 1)
    start, end = day_window(DAY, JST)
    assert row.not_before.replace(tzinfo=UTC) == start
    assert row.expires_at.replace(tzinfo=UTC) == end
    prompt = fake.payload(0)["input"][0]["content"]
    assert "今の人数・残りの人数は書かない" in prompt and "記事 (id=" not in prompt
    schema = fake.payload(0)["text"]["format"]["schema"]["properties"]["proposals"]["items"]
    assert schema["properties"]["link_mode"]["enum"] == ["none"]
    assert "topic" not in json.dumps(schema)
    _no_side_effects(session)


def test_case_b_a_fresh_follower_count_may_be_used_and_is_audited(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_B])
    service = _growth(session, tmp_path, fake, followers=63, collect=True)
    out = service.maintain(now=MORNING, execute=True)
    assert out["created"] and service._threads.reads == 1  # 生成の直前に 1 回だけ読む
    row = session.get(ThreadsPostProposal, out["created"])
    assert "63人" in row.content_text and "37人" in row.content_text
    meta = row.learning_guidance_json["growth"]
    assert meta["uses_follower_count"] is True
    assert meta["follower_observation"]["followers_count"] == 63
    assert meta["follower_observation"]["source"] == "threads_user_insights:followers_count"
    assert meta["follower_observation"]["observed_at"].startswith("2026-09-27T22:05")
    prompt = fake.payload(0)["input"][0]["content"]
    assert "現在のフォロワー: 63 人" in prompt and "あと 37 人" in prompt


def test_a_stale_follower_count_is_not_used(session, tmp_path) -> None:
    (tmp_path / "growth").mkdir()
    old = FollowerObservation(63, MORNING - timedelta(hours=7))
    (tmp_path / "growth/followers.json").write_text(json.dumps(old.as_dict()), "utf-8")
    fake = GrowthLuna([BODY_B, BODY_A])  # 1 回目は人数を書く → 書き直し
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 2 and out["created"]
    assert session.get(ThreadsPostProposal, out["created"]).content_text == BODY_A
    first = _record(tmp_path)["history"][0]
    assert "growth_unsupported_follower_count" in first["validation"]["reason_ids"]
    assert "今の人数・残りの人数は書かない" in fake.payload(0)["input"][0]["content"]


def test_case_c_the_target_reached_pauses_generation(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_A])
    service = _growth(session, tmp_path, fake, followers=104, collect=True)
    out = service.maintain(now=MORNING, execute=True)
    assert fake.calls == 0 and out["created"] is None and _growth_rows(session) == []
    assert out["follower_target_reached"] is True
    assert "a human chooses the next target" in out["reason"]
    status = json.loads((tmp_path / "growth/status.json").read_text("utf-8"))
    assert status["follower_target_reached"] is True
    # 次の日も (記録は古くなっても) 止まったまま。目標は自動で変えない。
    later = _growth(session, tmp_path, fake).plan(now=MORNING + timedelta(days=1))
    assert later["due"] is False and later["follower_target_reached"] is True
    assert later["follower_target"] == GROWTH_FOLLOWER_TARGET == 100


def test_case_d_repeated_runs_and_restarts_prepare_exactly_one(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_A, BODY_C, BODY_C])
    for minutes in (0, 5, 60, 180):
        _growth(session, tmp_path, fake).maintain(
            now=MORNING + timedelta(minutes=minutes), execute=True
        )  # 毎回新しい service = 再起動
    assert fake.calls == 1 and len(_growth_rows(session)) == 1


def test_a_crash_after_the_call_keeps_the_call_counted(session, tmp_path) -> None:
    # T6.3.3c: 呼ぶ前に記録を書くので、落ちても呼び出しの数は戻らない。再起動の後は、試した
    # 書き方を使ったことにして、残りの上限 (4 回) の中で別の書き方で続ける。
    fake = GrowthLuna([BODY_A, BODY_C])
    service = _growth(session, tmp_path, fake)
    service._persist = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        service.maintain(now=MORNING, execute=True)
    assert len(_record(tmp_path)["history"]) == 1 and _growth_rows(session) == []
    again = _growth(session, tmp_path, fake).maintain(now=MORNING + timedelta(hours=1),
                                                      execute=True)  # fmt: skip
    assert fake.calls == 2 and again["created"] and again["model_calls_today"] == 2
    first, second = _record(tmp_path)["history"]
    assert second["purpose"] == "strategy_retry"
    assert second["strategy"]["family"] != first["strategy"]["family"]


def test_case_e_article_posts_keep_their_rules_and_topic(session, article) -> None:
    row = _article_proposal(session, article)
    assert topic_tag_for(content_kind(row)) == "AI Threads"
    errors, _warnings = conversation_errors(
        "記事の要点。役に立ったらフォローお願いします！", "question"
    )
    assert any("follows" in e for e in errors)  # 記事の投稿ではフォローのお願いを許さない
    # Growth の検査はフォローのお願いを許す (この種類だけ)。
    assert validate(BODY_A, GrowthBrief(day=DAY, angle="community", follower_target=100))["ok"]


# --- no catch-up / queue / cadence ----------------------------------------------------------


def test_yesterdays_missed_growth_post_is_not_caught_up(session, tmp_path) -> None:
    yesterday = MORNING - timedelta(days=1)
    first = _growth(session, tmp_path, GrowthLuna([BODY_A])).maintain(now=yesterday,
                                                                      execute=True)  # fmt: skip
    old = session.get(ThreadsPostProposal, first["created"])
    old.status, old.approved_at = TP_APPROVED, yesterday
    session.commit()
    fake = GrowthLuna([BODY_C])
    today = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 1 and today["created"]  # 今日の分は 1 本だけ
    assert len(_growth_rows(session)) == 2
    from app.services.threads_queue_service import ThreadsQueueService

    queue = ThreadsQueueService(session, settings=type("S", (), {})(),
                                threads_service=_ReadyThreads(), timezone=JST)  # fmt: skip
    lane = queue.evaluate_growth(now=MORNING, publication_enabled=True)
    verdicts = {c.proposal_id: c for c in lane.candidates}
    assert verdicts[old.id].reason == REASON_EXPIRED  # 昨日の分は期限切れ (消さない)
    assert lane.candidate is None  # 今日の分は承認待ち、昨日の分は取り戻さない


def test_at_most_one_growth_publication_per_jst_day() -> None:
    from app.social.threads.queue import evaluate_growth_lane

    policy = get_operations_policy()
    now = MORNING + timedelta(hours=4)
    candidates = (
        CandidateFacts(proposal_id=5, status="approved", angle="account_growth",
                       source_article_id=None, link_mode="none", approved_at=MORNING,
                       content_kind=KIND_ACCOUNT_GROWTH, growth_date="2026-09-28"),
        CandidateFacts(proposal_id=6, status="approved", angle="insight", source_article_id=21,
                       link_mode="none", approved_at=MORNING),
    )  # fmt: skip
    facts = QueueFacts(now=now, threads_state="ready", candidates=candidates,
                       growth_published_today=GROWTH_POST_TARGET_PER_JST_DAY)  # fmt: skip
    lane = evaluate_growth_lane(facts, policy, JST, publication_enabled=True)
    assert "growth_daily_limit" in lane.blockers and not lane.would_publish_now
    assert {c.proposal_id: c for c in lane.candidates}[5].reason == REASON_GROWTH_DAILY_LIMIT
    evaluation = evaluate_queue(facts, policy, JST, publication_enabled=True)
    assert evaluation.next_candidate.proposal_id == 6  # 記事の投稿は止めない
    assert 5 not in {c.proposal_id for c in evaluation.candidates}  # 記事の枠に Growth は無い


def test_growth_ignores_the_article_gap_but_keeps_the_window_and_advisory() -> None:
    """T6.3.3a: Growth は記事の 120 分の間隔を使わない。公開窓と目安 (記事だけ) は同じ。"""

    from app.social.threads.queue import evaluate_growth_lane

    policy = get_operations_policy()
    assert policy.soft_min_gap_minutes == 120
    assert (policy.publication_window.start, policy.publication_window.end) == (time(7), time(23))
    assert (policy.daily_target_low, policy.daily_target_high) == (3, 5)
    candidates = (
        CandidateFacts(proposal_id=5, status="approved", angle="account_growth",
                       source_article_id=None, link_mode="none", approved_at=MORNING,
                       content_kind=KIND_ACCOUNT_GROWTH, growth_date="2026-09-28"),
    )  # fmt: skip
    recent = QueueFacts(now=MORNING + timedelta(minutes=30), threads_state="ready",
                        candidates=candidates, last_published_at=MORNING)  # fmt: skip
    lane = evaluate_growth_lane(recent, policy, JST, publication_enabled=True)
    assert lane.would_publish_now and "gap_not_elapsed" not in lane.blockers
    night = QueueFacts(now=datetime(2026, 9, 28, 23, 30, tzinfo=JST), threads_state="ready",
                       candidates=candidates)  # fmt: skip
    assert "outside_publication_window" in evaluate_growth_lane(
        night, policy, JST, publication_enabled=True
    ).blockers
    assert daily_activity(3, policy)["band"] == "within_target"  # 記事 3 本で目安の中


def test_growth_publications_are_counted_separately(session, tmp_path, article) -> None:
    from app.services.threads_queue_service import ThreadsQueueService

    out = _growth(session, tmp_path).maintain(now=MORNING, execute=True)
    art = _article_proposal(session, article)
    for pid, source in ((out["created"], None), (art.id, article.id)):
        session.add(ThreadsPublication(
            proposal_id=pid, proposal_hash=f"{pid}" * 8, source_article_id=source,
            angle="x", exact_published_text="t", status=PUB_PUBLISHED,
            published_at=MORNING + timedelta(hours=1), threads_media_id=f"m{pid}",
        ))  # fmt: skip
    session.commit()
    queue = ThreadsQueueService(session, settings=type("S", (), {})(),
                                threads_service=_ReadyThreads(), timezone=JST)  # fmt: skip
    now = MORNING + timedelta(hours=2)
    assert queue.published_today(now) == 1  # 記事の投稿だけ (3〜5 本の目安)
    assert queue.growth_published_today(now) == 1


# --- validation / duplicate / angles ------------------------------------------------------------


def test_follow_and_follow_back_wording_is_allowed_for_growth() -> None:
    brief = GrowthBrief(day=DAY, angle="mutual_growth", follower_target=100)
    for body in (BODY_A, BODY_C):
        assert validate(body, brief)["ok"], validate(body, brief)["problems"]


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ("\nhttps://bizfluxlab.com/", "URL"),
        ("\n{link}", "URL"),
        ("先月は収益3万円でした。", "revenue"),
        ("ついに50人を突破しました。", "follower number"),
        ("家族にも手伝ってもらっています。", "personal"),
        ("🎉🎉🎉🎉", "emoji"),
    ],
)
def test_growth_validation_rejects_unsupported_claims(extra, reason) -> None:
    brief = GrowthBrief(day=DAY, angle="community", follower_target=100)
    verdict = validate(BODY_A + extra, brief)
    assert not verdict["ok"] and any(reason in p for p in verdict["problems"]), verdict


def test_growth_body_limit_is_500() -> None:
    brief = GrowthBrief(day=DAY, angle="community", follower_target=100)
    verdict = validate(BODY_A + "あ" * 400, brief)
    assert any("limit is 500" in p for p in verdict["problems"])


def test_a_near_duplicate_switches_to_a_different_strategy(session, tmp_path) -> None:
    _growth(session, tmp_path, GrowthLuna([BODY_A])).maintain(
        now=MORNING - timedelta(days=1), execute=True
    )
    fake = GrowthLuna([BODY_A, BODY_C])  # 昨日と同じ言い回し → 別の書き方で新しく
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 2 and out["created"]
    assert session.get(ThreadsPostProposal, out["created"]).content_text == BODY_C
    record = _record(tmp_path)
    assert (record["generation_attempts"], record["model_calls"], record["repair_calls"],
            record["strategy_retries"]) == (2, 2, 0, 1)  # fmt: skip
    first, second = record["history"]
    assert first["purpose"] == "initial" and first["output"]["proposals"][0]["body"] == BODY_A
    assert first["validation"]["reason_ids"] == ["growth_duplicate"]
    assert first["failure_class"] == "validation_similarity"
    assert first["similarity"]["max"] >= 0.5 and first["similarity"]["compared"]
    assert first["next_action"]["purpose"] == "strategy_retry"
    # 言い換え (書き直し) ではなく、別の family で新しく書く。新しい方向を指示に書く。
    assert second["purpose"] == "strategy_retry" and "repair_reason_ids" not in second
    assert second["strategy"]["family"] != first["strategy"]["family"]
    assert "言い換えではなく" in second["retry_direction"]
    assert second["validation"]["ok"] is True and record["outcome"] == "stored"
    assert record["usage"]["input_tokens"] == 1800 and record["content_kind"] == "account_growth"
    assert API_KEY not in json.dumps(record)


def test_a_persistent_duplicate_exhausts_safely(session, tmp_path) -> None:
    _growth(session, tmp_path, GrowthLuna([BODY_A])).maintain(
        now=MORNING - timedelta(days=1), execute=True
    )
    fake = GrowthLuna([BODY_A] * 6)
    out = _growth(session, tmp_path, fake).maintain(now=MORNING, execute=True)
    assert fake.calls == 4 and out["created"] is None and len(_growth_rows(session)) == 1
    record = _record(tmp_path)
    assert record["outcome"] == "growth_generation_exhausted"
    assert record["exhaustion_reason"] == "model_call_budget_exhausted"
    assert len({c["strategy"]["family"] for c in record["history"]}) == 4  # 毎回 別の書き方
    again = _growth(session, tmp_path, fake).maintain(now=MORNING + timedelta(hours=2),
                                                      execute=True)  # fmt: skip
    assert fake.calls == 4 and again["created"] is None  # 5 回目は呼ばない
    assert "finished (growth_generation_exhausted)" in again["reason"]


def test_angles_rotate_deterministically_without_adjacent_repeats() -> None:
    previous = None
    seen = set()
    for offset in range(60):
        day = DAY + timedelta(days=offset)
        angle = select_angle(day, previous)
        assert angle != previous and angle == select_angle(day, previous)
        seen.add(angle)
        previous = angle
    assert seen == set(GROWTH_ANGLES)


# --- approval / publication -------------------------------------------------------------------


def test_a_growth_post_needs_human_approval_and_publishes_with_the_growth_topic(
    session, tmp_path
) -> None:
    out = _growth(session, tmp_path).maintain(now=MORNING, execute=True)
    service = ThreadsPublicationService(session, settings=type("S", (), {})(),
                                        threads_service=_ReadyThreads(), sleep=lambda _s: None,
                                        gap_minutes=120)  # fmt: skip
    plan = service.plan(proposal_id=out["created"], now=MORNING + timedelta(hours=1))
    assert not plan.ok and any("only an approved proposal" in r for r in plan.blocked_reasons)
    row = session.get(ThreadsPostProposal, out["created"])
    row.status, row.approved_at = TP_APPROVED, MORNING + timedelta(minutes=30)
    session.commit()
    plan = service.plan(proposal_id=row.id, now=MORNING + timedelta(hours=1))
    assert plan.ok, plan.blocked_reasons
    assert plan.content_kind == "account_growth" and plan.topic_tag == "インサイト祭り"
    assert plan.source_article_title == "Growth Post" and plan.destination_url is None
    assert "topic_tag='インサイト祭り'" in plan.would_call[0]


def test_the_approval_snapshot_and_email_show_the_growth_post(session, tmp_path) -> None:
    from app.approval.review_snapshot import build_snapshot
    from app.operations.report_format import render_approval_digest_text
    from app.services.threads_approval_digest_service import _kind_lines, _topic_text

    out = _growth(session, tmp_path).maintain(now=MORNING, execute=True)
    row = session.get(ThreadsPostProposal, out["created"])
    snap = build_snapshot(subject_type="threads_post", subject=row, article=None)
    assert snap["article_title"] == "投稿種別: Growth Post / 目標: フォロワー100人"
    assert snap["publish_text"] == row.content_text and snap["link_mode"] == "なし"
    assert snap["link_mode_raw"] == "none" and snap["topic_label"] == "インサイト祭り"
    assert snap["post_kind_label"] == "Growth Post" and snap["goal_label"] == "フォロワー100人"
    item = {"proposal_id": row.id, "article_title": "Growth Post", "angle": row.angle,
            "preview": "…", "timing": None, "topic": _topic_text(row), **_kind_lines(row),
            "review_url": "https://x/r"}  # fmt: skip
    text = render_approval_digest_text(items=[item], expires_at_local="-")
    assert "投稿種別: Growth Post" in text and "目標: フォロワー100人" in text
    assert "トピック: インサイト祭り" in text


def test_the_digest_does_not_defer_a_growth_post_for_article_stock() -> None:
    from app.social.threads.digest import DigestCandidate, plan_digest

    policy = get_operations_policy()
    growth = DigestCandidate(proposal_id=9, source_article_id=None, article_title="Growth Post",
                             angle="account_growth", ready_at=MORNING, identity="g",
                             preview="…", expires_at=MORNING + timedelta(hours=16),
                             growth=True)  # fmt: skip
    article = DigestCandidate(proposal_id=8, source_article_id=21, article_title="記事",
                              angle="insight", ready_at=MORNING, identity="a",
                              preview="…")  # fmt: skip
    plan = plan_digest([growth, article], now=MORNING + timedelta(hours=2), last_sent_at=None,
                       queued_identities=set(), approved_unpublished=99, policy=policy,
                       tz=JST)  # fmt: skip
    assert [i.candidate.proposal_id for i in plan.selected] == [9]  # 記事は在庫で先延ばし
    assert [i.candidate.proposal_id for i in plan.deferred] == [8]


def test_profile_hash_is_stable() -> None:
    assert profile_hash() == profile_hash() and len(profile_hash()) == 64


# --- worker ---------------------------------------------------------------------------------


def test_the_worker_does_not_run_growth_posts_unless_started_with_the_flag(
    session, tmp_path
) -> None:
    from app.services.threads_worker_log import WorkerLogFormatter
    from app.services.threads_worker_service import ThreadsWorkerService
    from app.social.threads.worker import SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE
    from tests.integration.test_threads_worker_service import _factory

    off = ThreadsWorkerService(_factory(session), settings=type("S", (), {})(),
                               threads_service=FakeThreads(), timezone=JST)  # fmt: skip
    state = {s.name: s for s in off.build_schedule(MORNING).states()}
    assert state[SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE].enabled is False
    assert off.capabilities["maintain_growth_posts"] is False
    fake = GrowthLuna([BODY_A])
    on = ThreadsWorkerService(_factory(session), settings=type("S", (), {})(),
                              threads_service=FakeThreads(), timezone=JST,
                              maintain_growth_posts=True, growth_client=_client(fake),
                              growth_directory=tmp_path / "growth")  # fmt: skip
    result = on.handlers()[SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE](MORNING)
    assert result.summary["created"] and result.summary["model_calls"] == 1
    again = on.handlers()[SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE](MORNING + timedelta(hours=1))
    assert again.summary["created"] is None and fake.calls == 1
    assert result.next_run_at <= MORNING + timedelta(hours=1)
    fields, level = WorkerLogFormatter._describe_account_growth_maintenance(again.summary)
    assert fields["date"] == "2026-09-28" and fields["active_proposal"] == result.summary["created"]
    assert "already exists" in fields["reason"] and level == "INFO"


def test_before_seven_nothing_is_called(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_A])
    out = _growth(session, tmp_path, fake).maintain(
        now=datetime(2026, 9, 28, 6, 55, tzinfo=JST), execute=True
    )
    assert fake.calls == 0 and out["reason"] == "before 07:00 JST"


def test_without_a_client_nothing_is_called_and_no_manual_fallback(session, tmp_path) -> None:
    out = _growth(session, tmp_path, client=False).maintain(now=MORNING, execute=True)
    assert out["created"] is None and "not configured" in out["reason"]
    assert not (tmp_path / "growth" / f"{DAY.isoformat()}.openai.json").exists()


# --- migration ------------------------------------------------------------------------------


def test_the_migration_round_trips_and_refuses_to_drop_growth_rows(tmp_path, monkeypatch):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    from app.config.settings import get_settings

    db = tmp_path / "migrate.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    get_settings.cache_clear()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "afc2f36bb3ca")
        conn = sqlite3.connect(db)
        conn.execute("insert into articles (id, title, slug, status) values (1, 't', 's', 'x')")
        conn.execute(
            "insert into threads_post_proposals (id, source_article_id, source_article_body_hash,"
            " angle, link_mode, content_text, character_count, content_seed, proposal_hash,"
            " policy_version, generator_version, status) values (1, 1, 'h', 'insight', 'none',"
            " 'x', 1, 's', 'p', 'v', 'g', 'approved')"
        )
        conn.commit()
        before = conn.execute("select * from threads_post_proposals").fetchall()
        conn.close()
        command.upgrade(config, "head")
        conn = sqlite3.connect(db)
        assert conn.execute("select * from threads_post_proposals").fetchall() == before
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert conn.execute("pragma foreign_key_check").fetchall() == []
        conn.execute(
            "insert into threads_post_proposals (id, source_article_id, source_article_body_hash,"
            " angle, link_mode, content_text, character_count, content_seed, proposal_hash,"
            " policy_version, generator_version, status) values (2, null, 'h', 'account_growth',"
            " 'none', 'y', 1, 's2', 'p2', 'v', 'g', 'awaiting_approval')"
        )
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError, match="refusing"):
            command.downgrade(config, "afc2f36bb3ca")
        conn = sqlite3.connect(db)
        conn.execute("delete from threads_post_proposals where id = 2")
        conn.commit()
        conn.close()
        command.downgrade(config, "afc2f36bb3ca")
        conn = sqlite3.connect(db)
        assert conn.execute("select * from threads_post_proposals").fetchall() == before
        conn.close()
    finally:
        monkeypatch.delenv("DATABASE_URL")
        get_settings.cache_clear()


# --- Project State --------------------------------------------------------------------------


def test_project_state_distinguishes_the_pending_production_migration(
    tmp_path, monkeypatch
) -> None:
    import sqlite3

    from app.project_state import local_state, strict
    from app.project_state.generator import build_report
    from tests.unit.test_project_state import _context

    # 本番は 2026-09-28 に適用済み。適用前の状態 (宣言あり・DB は afc2f36bb3ca) を作って確かめる。
    # T6.5B: code head は 2cfa0ccb2059 (観察の表、本番は未適用の宣言あり) に進んだ。
    # C9 Batch 2: code head は 74bfaf6c9c9f (Growth Action の履歴、本番は未適用の宣言あり)。
    monkeypatch.setattr(local_state, "PENDING_PRODUCTION_MIGRATIONS",
                        {"c4d2e8f1a9b3": "T6.3.3 growth posts",
                         "2cfa0ccb2059": "T6.5B observer tables",
                         "74bfaf6c9c9f": "C9 growth action history",
                         "74dbecaa4bb2": "C9 growth action conversions",
                         "4fe83827d695": "C9-B growth handoff requests"})  # fmt: skip
    ctx, _, db_path = _context(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("update alembic_version set version_num = 'afc2f36bb3ca'")
    conn.commit()
    conn.close()
    report = build_report(ctx)
    db = report["database"]
    assert db["db_at_code_head"] is False
    assert sorted(db["pending_migrations"]) == ["2cfa0ccb2059", "4fe83827d695", "74bfaf6c9c9f",
                                                "74dbecaa4bb2", "c4d2e8f1a9b3"]
    assert db["pending_declared_for_production"] is True
    inv = next(i for i in report["invariants"]["results"] if i["id"] == "db-at-code-head")
    assert inv["result"] == "pass" and str(inv["observed"]).startswith("pending production")
    warning = next(w for w in report["warnings"]
                   if w["id"] == "db-production-migration-pending-c4d2e8f1a9b3")  # fmt: skip
    assert warning["blocking"] is False and "human applies it" in warning["action_required"]
    assert not any(f.startswith("invariant db-at-code-head") for f in strict.failures(report))
    growth = report["threads"]["growth"]
    assert growth["schema_ready"] is False and growth["policy"]["follower_target"] == 100
    assert growth["policy"]["supplemental_to_article_cadence"] is True
    # alembic check が「DB が head でない」だけで落ちたら、宣言済みなら失敗にしない。
    quality = {"alembic_check": {"ok": False, "summary": "FAILED: Target database is not up to "
                                 "date."}}  # fmt: skip
    fixed = local_state.reconcile_alembic_check(quality, db)["alembic_check"]
    assert fixed["ok"] is None and fixed["pending_production"] is True
    other = {"alembic_check": {"ok": False, "summary": "FAILED: New upgrade operations"}}
    assert local_state.reconcile_alembic_check(other, db)["alembic_check"]["ok"] is False
    undeclared = {**db, "pending_declared_for_production": False}
    assert local_state.reconcile_alembic_check(quality, undeclared)["alembic_check"]["ok"] is False


def test_the_cli_plan_writes_and_calls_nothing(session, tmp_path, capsys) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "growth_cli", "scripts/maintain_threads_growth_post.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    from tests.integration.test_threads_worker_service import _factory

    fake = GrowthLuna([BODY_A])
    code = cli.main([], session_factory=_factory(session), settings=type("S", (), {})(),
                    overrides={"client": _client(fake), "directory": tmp_path / "growth"},
                    now=MORNING)  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "due                 = True" in out and "plan only" in out
    assert fake.calls == 0 and _growth_rows(session) == []
    assert not (tmp_path / "growth").exists()


def test_project_state_flags_a_worker_started_without_the_growth_flag() -> None:
    from app.project_state.drift import _stock

    def state(caps):
        return {"threads": {"worker": {
            "stock_maintenance_enabled": True, "growth_maintenance_enabled": True,
            "running": True, "runtime_start": {"found": True, "pid": 1, "capabilities": caps},
        }}}  # fmt: skip

    stale = _stock(state(["maintain_proposal_stock"]))
    assert [f["id"] for f in stale] == ["threads-worker-growth-restart-pending"]
    assert _stock(state(["maintain_proposal_stock", "maintain_growth_posts"])) == []
