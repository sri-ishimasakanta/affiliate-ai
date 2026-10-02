"""T6.3.3a: Growth Post の足し分の公開の枠 (偽の Threads API だけ。本番には出ない)。

記事の投稿は今まで通り (120 分の間隔・1 回 1 本・承認の順・公開窓・トピック "AI Threads")。
Growth Post は別の枠: 記事の間隔を待たず、出した後も記事の間隔の起点にならず、失敗しても記事の
queue を止めない。1 日 1 本・その日だけ・人の承認が要る・トピックなし・リンクなし。

偽の E2E:

- A: 13:00 に記事を出す → 同じ評価で承認済みの Growth を出す → 記事の次は 15:00 のまま
- B: 13:00 に記事 → 13:20 に Growth を承認 → 次の評価で出す (15:00 を待たない)
- C: Growth の公開が不確定 → 記録・Growth は止まる → 15:00 の記事は出せる
- D: 今日すでに Growth を出した → 別の承認済み Growth は出ない
- E: 夜に記事の枠が無くても、承認済みの Growth は自分の評価で出る
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    PUB_PUBLISHED,
    PUB_UNCERTAIN,
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    Article,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.services.threads_auto_publisher import ThreadsAutoPublisher
from app.services.threads_queue_service import ThreadsQueueService
from app.social.threads.client import ThreadsClient
from app.social.threads.growth import profile_hash
from app.social.threads.policy import get_measurement_policy, get_operations_policy
from app.social.threads.queue import MAX_PUBLICATIONS_PER_CYCLE
from app.social.threads.service import ThreadsService
from app.social.threads.worker import (
    MAX_GROWTH_PUBLICATIONS_PER_CYCLE,
    SubsystemResult,
    ThreadsWorker,
    WorkerSchedule,
)

JST = ZoneInfo("Asia/Tokyo")
TOKEN = "THAAAsecret-token-must-never-appear"
DAY = "2026-09-28"
GROWTH_TEXT = (
    "AIでメディア運営をどこまで自動化できるか検証中です。目標はフォロワー100人。フォロバします🙂"
)


def jst(hour, minute=0, day=28):
    return datetime(2026, 9, day, hour, minute, tzinfo=JST).astimezone(UTC)


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = "https://bizfluxlab.com"


class FakeThreadsAPI:
    """Threads API の代役。公開ごとに別の id を返す。Growth の公開だけを失敗させられる。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.clock: datetime = jst(13)
        self.fail_growth_publish = False
        self._texts: dict[str, str] = {}
        self._n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
        path = request.url.path
        self.requests.append({"method": request.method, "path": path, "form": form})
        if request.method == "POST" and path.endswith("/threads"):
            self._n += 1
            cid = f"c{self._n}"
            self._texts[cid] = form["text"]
            return httpx.Response(200, json={"id": cid})
        if request.method == "POST" and path.endswith("/threads_publish"):
            cid = form["creation_id"]
            if self.fail_growth_publish and self._texts[cid] == GROWTH_TEXT:
                return httpx.Response(500, json={"error": {"message": "temporarily down"}})
            mid = f"m{cid}"
            self._texts[mid] = self._texts[cid]
            return httpx.Response(200, json={"id": mid})
        if request.method == "GET" and path.endswith("/threads"):
            # manual-post coexistence: 自アカウントの投稿の一覧 (この代役が公開したものだけ)。
            stamp = self.clock.strftime("%Y-%m-%dT%H:%M:%S+0000")
            return httpx.Response(200, json={"data": [
                {"id": mid, "text": text, "timestamp": stamp, "media_type": "TEXT_POST"}
                for mid, text in self._texts.items() if mid.startswith("m")]})
        if request.method == "GET" and path.endswith("/9876543210"):
            return httpx.Response(200, json={"id": "9876543210", "username": "bizfluxlab"})
        if request.method == "GET":
            mid = path.rsplit("/", 1)[-1]
            stamp = self.clock.strftime("%Y-%m-%dT%H:%M:%S+0000")
            return httpx.Response(200, json={
                "id": mid, "text": self._texts.get(mid), "timestamp": stamp,
                "permalink": f"https://www.threads.net/@bizfluxlab/post/{mid}",
                "username": "bizfluxlab"})  # fmt: skip
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    def creates(self) -> list[dict]:
        return [r["form"] for r in self.requests
                if r["method"] == "POST" and r["path"].endswith("/threads")]


def _publisher(session, api: FakeThreadsAPI) -> ThreadsAutoPublisher:
    settings = _Settings()
    client = ThreadsClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(
        api.handler)))  # fmt: skip
    service = ThreadsService(settings, client=client, sleep=lambda _s: None)
    return ThreadsAutoPublisher(
        session, settings=settings, threads_service=service, policy=get_operations_policy(),
        measurement_policy=get_measurement_policy(), timezone=JST, flag_enabled=True,
        lock_held=True, sleep=lambda _s: None,
    )  # fmt: skip


def _queue(session, api) -> ThreadsQueueService:
    return _publisher(session, api)._queue


@pytest.fixture
def article(session) -> Article:
    row = Article(
        id=21, title="生成AI", slug="gen-ai", body="記事本文。", status="published",
        published_url="https://bizfluxlab.com/gen-ai/", published_at=jst(9, day=18),
        article_type="informational", monetization_mode="supporting", wordpress_post_id="84",
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _article_post(session, article, seed, *, approved_at, text=None):
    text = text or f"記事の投稿 {seed}。体制を先に決めたほうが早い。"
    row = ThreadsPostProposal(
        source_article_id=article.id, source_article_body_hash=compute_text_hash(article.body),
        angle="insight", link_mode="none", content_text=text, character_count=len(text),
        content_seed=seed * 64, proposal_hash=(seed * 64).upper(), policy_version="t2.1",
        generator_version="threads-proposal-1", status=TP_APPROVED, approved_at=approved_at,
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _growth_post(session, *, day=DAY, status=TP_APPROVED, approved_at=None, seed="g"):
    local = datetime.fromisoformat(day).replace(tzinfo=JST)
    row = ThreadsPostProposal(
        source_article_id=None, source_article_body_hash=profile_hash(), angle="account_growth",
        link_mode="none", content_text=GROWTH_TEXT, character_count=len(GROWTH_TEXT),
        content_seed=seed * 64, proposal_hash=(seed * 64).upper(), policy_version="t6.3.3",
        generator_version="threads-growth-1", status=status, approved_at=approved_at,
        not_before=(local + timedelta(hours=7)).astimezone(UTC),
        expires_at=(local + timedelta(days=1)).astimezone(UTC),
        learning_guidance_json={"content_kind": "account_growth",
                                "growth": {"date_jst": day, "follower_target": 100}},
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _published_article(session, article, seed, at):
    """過去に公開した記事の投稿 (間隔の起点)。"""

    row = _article_post(session, article, seed, approved_at=at - timedelta(hours=1))
    session.add(ThreadsPublication(
        proposal_id=row.id, proposal_hash=row.proposal_hash, source_article_id=article.id,
        angle="insight", exact_published_text=row.content_text, status=PUB_PUBLISHED,
        published_at=at, remote_timestamp=at.strftime("%Y-%m-%dT%H:%M:%S+0000"),
        threads_media_id=f"old-{seed}",
    ))  # fmt: skip
    session.commit()
    return row


def _run_cycle(session, api, now):
    """worker の公開の評価 1 回と同じ順: 記事の枠 → Growth の枠 (組の公開 id を渡す)。"""

    api.clock = now
    publisher = _publisher(session, api)
    article = publisher.publish_one(now=now)
    growth = publisher.publish_growth_one(
        now=now, companion_publication_id=article.publication_id if article.published else None
    )
    return article, growth


def _attempts(session, publication_id):
    return session.scalars(
        select(ThreadsPublicationAttempt)
        .where(ThreadsPublicationAttempt.threads_publication_id == publication_id)
        .order_by(ThreadsPublicationAttempt.id)
    ).all()


# --- fake E2E -----------------------------------------------------------------------------


def test_case_a_growth_publishes_with_the_article_and_the_gap_is_unchanged(
    session, article
) -> None:
    _published_article(session, article, "p", jst(11))
    art = _article_post(session, article, "a", approved_at=jst(10))
    growth = _growth_post(session, approved_at=jst(12, 45))
    api = FakeThreadsAPI()
    a, g = _run_cycle(session, api, jst(13))
    assert a.published and a.proposal_id == art.id
    assert g.published and g.proposal_id == growth.id and g.lane == "account_growth"
    assert g.growth_trigger == "article_companion"
    assert g.paired_article_publication_id == a.publication_id
    creates = api.creates()
    # T6.3.3b: 記事は AI Threads、Growth は インサイト祭り。
    assert creates[0]["topic_tag"] == "AI Threads" and creates[1]["topic_tag"] == "インサイト祭り"
    assert creates[1]["text"] == GROWTH_TEXT and "http" not in creates[1]["text"]
    step = _attempts(session, g.publication_id)[0]
    assert step.detail_json["lane"] == "account_growth"
    assert step.detail_json["growth_trigger"] == "article_companion"
    assert step.detail_json["paired_article_publication_id"] == a.publication_id
    assert step.detail_json["topic_tag"] == "インサイト祭り"
    assert step.detail_json["topic_tag_sent"] is True
    assert step.detail_json["article_gap_applies"] is False
    # 記事の次の枠は、記事の公開 (13:00) から 120 分のまま。Growth は起点にならない。
    queue = _queue(session, api)
    basis = queue.latest_gap_basis()
    assert basis.publication_id == a.publication_id
    later = queue.evaluate(now=jst(13, 30), publication_enabled=True)
    assert later.timing.earliest_at == jst(15)
    assert "gap_not_elapsed" in later.blockers


def test_case_b_growth_approved_after_the_article_publishes_on_the_next_heartbeat(
    session, article
) -> None:
    _published_article(session, article, "p", jst(11))
    _article_post(session, article, "a", approved_at=jst(10))
    _article_post(session, article, "b", approved_at=jst(10, 5))
    growth = _growth_post(session, status=TP_AWAITING_APPROVAL)
    api = FakeThreadsAPI()
    a, g = _run_cycle(session, api, jst(13))
    assert a.published and not g.published and g.outcome == "blocked"
    assert "no_eligible_growth_candidate" in g.blocked_reasons  # 承認前は出さない
    growth.status, growth.approved_at = TP_APPROVED, jst(13, 20)
    session.commit()
    a2, g2 = _run_cycle(session, api, jst(13, 22))
    assert not a2.published and "gap_not_elapsed" in a2.blocked_reasons  # 記事は 15:00 まで待つ
    assert g2.published and g2.growth_trigger == "post_approval_heartbeat"
    assert _queue(session, api).evaluate(now=jst(13, 30)).timing.earliest_at == jst(15)


def test_case_c_a_growth_failure_does_not_block_articles(session, article) -> None:
    _published_article(session, article, "p", jst(11))
    _article_post(session, article, "a", approved_at=jst(10))
    second = _article_post(session, article, "b", approved_at=jst(10, 5))
    growth = _growth_post(session, approved_at=jst(12))
    api = FakeThreadsAPI()
    api.fail_growth_publish = True
    a, g = _run_cycle(session, api, jst(13))
    assert a.published and g.outcome == "uncertain" and not g.published
    row = session.get(ThreadsPublication, g.publication_id)
    assert row.status == PUB_UNCERTAIN and row.source_article_id is None
    # Growth は不確定なので、照合までもう出さない (二重に出さない)。
    a2, g2 = _run_cycle(session, api, jst(13, 30))
    assert "uncertain_publication" in g2.blocked_reasons and not g2.attempted
    # 記事は Growth の不確定で止まらない: 15:00 に次の記事が出る。
    api.fail_growth_publish = False
    a3, g3 = _run_cycle(session, api, jst(15, 1))
    assert a3.published and a3.proposal_id == second.id
    assert not g3.published
    growth_rows = session.scalars(select(ThreadsPublication).where(
        ThreadsPublication.proposal_id == growth.id)).all()  # fmt: skip
    assert len(growth_rows) == 1  # 送り直していない


def test_case_d_only_one_growth_post_per_jst_day(session, article) -> None:
    first = _growth_post(session, approved_at=jst(8), seed="g")
    api = FakeThreadsAPI()
    _a, g = _run_cycle(session, api, jst(9))
    assert g.published and g.proposal_id == first.id
    extra = _growth_post(session, approved_at=jst(9, 30), seed="h")  # 同じ日の別の承認済み
    _a2, g2 = _run_cycle(session, api, jst(10))
    assert not g2.attempted and "growth_daily_limit" in g2.blocked_reasons
    plan = _publisher(session, api)._publications.plan(proposal_id=extra.id, now=jst(10))
    assert any("already published today" in r for r in plan.blocked_reasons)  # 書き込み経路も
    assert session.scalar(select(func.count()).select_from(ThreadsPublication).where(
        ThreadsPublication.source_article_id.is_(None))) == 1  # fmt: skip


def test_case_e_growth_publishes_late_without_an_article_slot(session, article) -> None:
    _published_article(session, article, "p", jst(22))
    _article_post(session, article, "a", approved_at=jst(21))
    growth = _growth_post(session, approved_at=jst(21, 30))
    api = FakeThreadsAPI()
    a, g = _run_cycle(session, api, jst(22, 30))
    assert not a.published and "gap_not_elapsed" in a.blocked_reasons  # 記事の枠は無い
    assert g.published and g.proposal_id == growth.id and g.growth_trigger == "normal_heartbeat"


# --- focused tests ------------------------------------------------------------------------


def test_growth_needs_human_approval(session, article) -> None:
    _growth_post(session, status=TP_AWAITING_APPROVAL)
    _a, g = _run_cycle(session, FakeThreadsAPI(), jst(9))
    assert not g.attempted and "no_eligible_growth_candidate" in g.blocked_reasons


def test_yesterdays_growth_is_never_published_today(session, article) -> None:
    old = _growth_post(session, day="2026-09-27", approved_at=jst(20, day=27))
    api = FakeThreadsAPI()
    _a, g = _run_cycle(session, api, jst(9))
    assert not g.attempted
    lane = _queue(session, api).evaluate_growth(now=jst(9), publication_enabled=True)
    assert {c.proposal_id: c for c in lane.candidates}[old.id].reason == "expired"
    plan = _publisher(session, api)._publications.plan(proposal_id=old.id, now=jst(9))
    assert any("never caught up" in r for r in plan.blocked_reasons)


def test_growth_after_its_day_expires(session, article) -> None:
    _growth_post(session, approved_at=jst(8))
    _a, g = _run_cycle(session, FakeThreadsAPI(), jst(7, 30, day=29))
    assert not g.attempted


def test_a_restart_never_duplicates_a_growth_post(session, article) -> None:
    _growth_post(session, approved_at=jst(8))
    api = FakeThreadsAPI()
    _run_cycle(session, api, jst(9))
    for minutes in (5, 60, 240):
        _a, g = _run_cycle(session, api, jst(9) + timedelta(minutes=minutes))  # 新しい publisher
        assert not g.attempted
    assert len([c for c in api.creates() if c["text"] == GROWTH_TEXT]) == 1


def test_a_growth_post_cannot_starve_behind_repeated_article_posts(session, article) -> None:
    """#25 のように、承認の早い記事が並んでいても Growth はその日のうちに出る。"""

    _published_article(session, article, "p", jst(11))
    for i, seed in enumerate("abcdef"):
        _article_post(session, article, seed, approved_at=jst(9, i))
    growth = _growth_post(session, approved_at=jst(12, 45))
    api = FakeThreadsAPI()
    _a, g = _run_cycle(session, api, jst(13))
    assert g.published and g.proposal_id == growth.id


def test_growth_does_not_count_toward_the_article_advisory(session, article) -> None:
    _published_article(session, article, "p", jst(11))
    _article_post(session, article, "a", approved_at=jst(10))
    _growth_post(session, approved_at=jst(12))
    api = FakeThreadsAPI()
    _run_cycle(session, api, jst(13))
    queue = _queue(session, api)
    assert queue.published_today(jst(13, 5)) == 2  # 記事だけ (11:00 と 13:00)
    assert queue.growth_published_today(jst(13, 5)) == 1


def test_the_article_lane_is_unchanged(session, article) -> None:
    """記事の 120 分・1 回 1 本・承認の順・トピックは同じ。"""

    policy = get_operations_policy()
    assert policy.soft_min_gap_minutes == 120 and MAX_PUBLICATIONS_PER_CYCLE == 1
    _published_article(session, article, "p", jst(11))
    first = _article_post(session, article, "a", approved_at=jst(9))
    _article_post(session, article, "b", approved_at=jst(9, 30))
    api = FakeThreadsAPI()
    a, _g = _run_cycle(session, api, jst(12, 30))
    assert not a.published and "gap_not_elapsed" in a.blocked_reasons
    a, _g = _run_cycle(session, api, jst(13))
    assert a.published and a.proposal_id == first.id  # 承認の古い順
    a, _g = _run_cycle(session, api, jst(13, 1))
    assert not a.published  # 1 本出したら次の 120 分まで出さない
    assert api.creates()[0]["topic_tag"] == "AI Threads"


def test_an_article_uncertain_publication_still_stops_articles_and_growth(
    session, article
) -> None:
    """記事の失敗の扱いは今まで通り (記事の queue 全体を止める)。Growth も出さない。"""

    art = _article_post(session, article, "a", approved_at=jst(9))
    session.add(ThreadsPublication(
        proposal_id=art.id, proposal_hash=art.proposal_hash, source_article_id=article.id,
        angle="insight", exact_published_text=art.content_text, status=PUB_UNCERTAIN,
    ))  # fmt: skip
    session.commit()
    _article_post(session, article, "b", approved_at=jst(9, 30))
    _growth_post(session, approved_at=jst(9))
    a, g = _run_cycle(session, FakeThreadsAPI(), jst(13))
    assert not a.published and "uncertain_publication" in a.blocked_reasons
    assert not g.attempted and "uncertain_publication" in g.blocked_reasons


def test_the_worker_caps_growth_separately_from_articles() -> None:
    assert MAX_GROWTH_PUBLICATIONS_PER_CYCLE == 1
    now = jst(13)

    def both(_now):
        return SubsystemResult(next_run_at=None, publications=1, growth_publications=1)

    worker = ThreadsWorker(handlers={"publication_evaluation": both},
                           schedule=_schedule(now), heartbeat=timedelta(minutes=5),
                           clock=lambda: now)  # fmt: skip
    report = worker.run_cycle()
    assert (report.publications, report.growth_publications) == (1, 1)

    def two_growth(_now):
        return SubsystemResult(next_run_at=None, growth_publications=2)

    worker = ThreadsWorker(handlers={"publication_evaluation": two_growth},
                           schedule=_schedule(now), heartbeat=timedelta(minutes=5),
                           clock=lambda: now)  # fmt: skip
    with pytest.raises(RuntimeError, match="more than one growth publication"):
        worker.run_cycle()


def _schedule(now) -> WorkerSchedule:
    schedule = WorkerSchedule()
    schedule.register("publication_evaluation", first_run_at=now)
    return schedule


def test_the_log_line_explains_the_growth_lane() -> None:
    from app.services.threads_worker_log import WorkerLogFormatter

    fields, level = WorkerLogFormatter._describe_publication_evaluation({
        "next_candidate_id": 23, "blockers": ["gap_not_elapsed"],
        "auto_publish": {"outcome": "blocked", "threads_writes": 0},
        "growth": {"proposal_id": 25, "outcome": "published", "attempted": True,
                   "growth_trigger": "post_approval_heartbeat", "publication_id": 20},
    })  # fmt: skip
    assert fields["growth_candidate"] == 25 and fields["growth"] == "published"
    assert fields["growth_trigger"] == "post_approval_heartbeat"
    assert fields["growth_publication"] == 20 and level == "INFO"
    blocked, _ = WorkerLogFormatter._describe_publication_evaluation({
        "blockers": [], "growth": {"proposal_id": None, "outcome": "blocked",
                                   "blocked_reasons": ["no_eligible_growth_candidate"]},
    })  # fmt: skip
    assert blocked["growth_blockers"] == "no_eligible_growth_candidate"


def test_no_secret_reaches_the_growth_audit(session, article) -> None:
    _growth_post(session, approved_at=jst(8))
    _a, g = _run_cycle(session, FakeThreadsAPI(), jst(9))
    dumped = json.dumps([a.detail_json for a in _attempts(session, g.publication_id)])
    assert TOKEN not in dumped and TOKEN not in json.dumps(g.as_dict(), default=str)
