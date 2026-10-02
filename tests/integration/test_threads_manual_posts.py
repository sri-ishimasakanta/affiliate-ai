"""Threads manual-post coexistence (2026-10-02)。偽の Threads API (HTTP の層) だけを使う。

人が Threads のアプリ・ブラウザから投稿しても:

- 自動公開の数え方 (記事の本数・Growth の 1 日 1 本・承認・公開の成功) は変わらない
- 直後の自動公開は待つ (間隔・密度)。予定は取り消さず、提案は承認済みのまま
- 実質的に同じ提案は ``superseded`` (公開の成功ではない)
- 自動公開の直前に一覧を必ず読み直し、読めなければ公開しない
- 観測・分析には origin つきで入る。同一性は投稿 ID だけ

外への書き込みは偽の API だけ (本番には出ない)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    PUB_PUBLISHED,
    PUB_PUBLISHING,
    TP_APPROVED,
    TP_SUPERSEDED,
    Article,
    ThreadsAccountPost,
    ThreadsAccountPostEvent,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_account_post_service import (
    AccountPostError,
    ThreadsAccountPostService,
)
from app.services.threads_auto_publisher import ThreadsAutoPublisher
from app.services.threads_queue_service import ThreadsQueueService
from app.social.threads.client import ThreadsClient
from app.social.threads.growth import profile_hash
from app.social.threads.policy import get_measurement_policy, get_operations_policy
from app.social.threads.queue import BLOCKER_RECENT_ACCOUNT_POST
from app.social.threads.service import ThreadsService

JST = ZoneInfo("Asia/Tokyo")
DAY = "2026-09-28"
GROWTH_TEXT = (
    "AIでメディア運営をどこまで自動化できるか検証中です。目標はフォロワー100人。フォロバします🙂"
)
MANUAL_TEXT = "今日は手で書いた投稿です。AI記事の運営で気づいたことを短くまとめます。"


def jst(hour, minute=0, day=28):
    return datetime(2026, 9, day, hour, minute, tzinfo=JST).astimezone(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = "THAAAsecret-token-must-never-appear"
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = "https://bizfluxlab.com"


class FakeThreadsAPI:
    """偽の Threads API。自アカウントの一覧には、この代役が公開したものと人の投稿を出す。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.clock = jst(13)
        self.manual: list[dict] = []
        self.listing_status = 200
        self.insights_status = 200
        self._texts: dict[str, str] = {}
        self._times: dict[str, str] = {}
        self._n = 0

    def post_manually(self, media_id: str, text: str, at: datetime) -> None:
        self.manual.insert(0, {"id": media_id, "text": text, "timestamp": stamp(at),
                               "media_type": "TEXT_POST",
                               "permalink": f"https://www.threads.net/@bizfluxlab/post/{media_id}"})

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
            mid = f"m{form['creation_id']}"
            self._texts[mid] = self._texts[form["creation_id"]]
            self._times[mid] = stamp(self.clock)
            return httpx.Response(200, json={"id": mid})
        if request.method == "GET" and path.endswith("/9876543210/threads"):
            if self.listing_status != 200:
                return httpx.Response(self.listing_status, json={"error": {"message": "down"}})
            system = [{"id": m, "text": t, "timestamp": self._times[m], "media_type": "TEXT_POST"}
                      for m, t in self._texts.items() if m in self._times]
            items = sorted(self.manual + system, key=lambda i: i["timestamp"], reverse=True)
            return httpx.Response(200, json={"data": items})
        if request.method == "GET" and path.endswith("/9876543210"):
            return httpx.Response(200, json={"id": "9876543210", "username": "bizfluxlab"})
        if request.method == "GET" and path.endswith("/insights"):
            if self.insights_status != 200:
                return httpx.Response(self.insights_status,
                                      json={"error": {"message": "not found", "code": 100}})
            return httpx.Response(200, json={"data": [
                {"name": "views", "values": [{"value": 120}]},
                {"name": "likes", "values": [{"value": 3}]}]})
        if request.method == "GET":
            mid = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={
                "id": mid, "text": self._texts.get(mid), "timestamp": stamp(self.clock),
                "permalink": f"https://www.threads.net/@bizfluxlab/post/{mid}",
                "username": "bizfluxlab"})  # fmt: skip
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    def writes(self) -> list[str]:
        return [r["path"] for r in self.requests if r["method"] == "POST"]

    def listings(self) -> int:
        return sum(1 for r in self.requests if r["method"] == "GET"
                   and r["path"].endswith("/9876543210/threads"))


def _service(api: FakeThreadsAPI) -> ThreadsService:
    settings = _Settings()
    client = ThreadsClient(settings, http_client=httpx.Client(
        transport=httpx.MockTransport(api.handler)))  # fmt: skip
    return ThreadsService(settings, client=client, sleep=lambda _s: None)


def _publisher(session, api) -> ThreadsAutoPublisher:
    return ThreadsAutoPublisher(
        session, settings=_Settings(), threads_service=_service(api),
        policy=get_operations_policy(), measurement_policy=get_measurement_policy(),
        timezone=JST, flag_enabled=True, lock_held=True, sleep=lambda _s: None)  # fmt: skip


def _ledger(session, api) -> ThreadsAccountPostService:
    return ThreadsAccountPostService(session, threads_service=_service(api),
                                     measurement_policy=get_measurement_policy())


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


def _article_post(session, article, seed, *, text=None, status=TP_APPROVED):
    text = text or f"記事の投稿 {seed}。体制を先に決めたほうが早い。"
    row = ThreadsPostProposal(
        source_article_id=article.id, source_article_body_hash=compute_text_hash(article.body),
        angle="insight", link_mode="none", content_text=text, character_count=len(text),
        content_seed=seed * 64, proposal_hash=(seed * 64).upper(), policy_version="t2.1",
        generator_version="threads-proposal-1", status=status, approved_at=jst(8),
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _growth_post(session, *, seed="g", text=GROWTH_TEXT):
    local = datetime.fromisoformat(DAY).replace(tzinfo=JST)
    row = ThreadsPostProposal(
        source_article_id=None, source_article_body_hash=profile_hash(), angle="account_growth",
        link_mode="none", content_text=text, character_count=len(text),
        content_seed=seed * 64, proposal_hash=(seed * 64).upper(), policy_version="t6.3.3",
        generator_version="threads-growth-1", status=TP_APPROVED, approved_at=jst(8),
        not_before=(local + timedelta(hours=7)).astimezone(UTC),
        expires_at=(local + timedelta(days=1)).astimezone(UTC),
        learning_guidance_json={"content_kind": "account_growth",
                                "growth": {"date_jst": DAY, "follower_target": 100}},
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _published(session, proposal, media, at):
    row = ThreadsPublication(
        proposal_id=proposal.id, proposal_hash=proposal.proposal_hash,
        source_article_id=proposal.source_article_id, angle=proposal.angle,
        exact_published_text=proposal.content_text, status=PUB_PUBLISHED, published_at=at,
        remote_timestamp=stamp(at), threads_media_id=media)  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _post(session, media) -> ThreadsAccountPost | None:
    return session.scalars(select(ThreadsAccountPost)
                           .where(ThreadsAccountPost.threads_media_id == media)).first()


def _events(session, post) -> list[str]:
    return [e.event_type for e in session.scalars(
        select(ThreadsAccountPostEvent)
        .where(ThreadsAccountPostEvent.threads_account_post_id == post.id)
        .order_by(ThreadsAccountPostEvent.id))]  # fmt: skip


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# -- discovery / reconciliation -------------------------------------------------------------
def test_a_manual_post_is_discovered_but_never_counted_as_a_system_publication(
        session, article) -> None:  # fmt: skip
    api = FakeThreadsAPI()
    api.post_manually("manual-1", MANUAL_TEXT, jst(12, 50))
    queue = _queue(session, api)
    before = (queue.published_today(jst(13)), queue.growth_published_today(jst(13)))
    summary = _ledger(session, api).refresh(now=jst(13), source="periodic")
    post = _post(session, "manual-1")
    assert (post.origin, post.origin_evidence, post.post_kind) == (
        "manual", "no_publication_record", "unknown")
    assert post.threads_publication_id is None and post.latest_text == MANUAL_TEXT
    assert summary["discovered"] == [{"media_id": "manual-1", "origin": "manual"}]
    assert _events(session, post) == ["discovered"]
    # 公開・提案・承認の記録は増えない。本数の数え方も同じ
    assert _count(session, ThreadsPublication) == 0 and _count(session, ThreadsPostProposal) == 0
    assert (queue.published_today(jst(13)), queue.growth_published_today(jst(13))) == before
    assert queue.counts()["approved"] == 0
    assert api.writes() == []


def test_rediscovering_a_system_post_never_makes_it_manual(session, article) -> None:
    api = FakeThreadsAPI()
    proposal = _article_post(session, article, "a")
    publication = _published(session, proposal, "sys-1", jst(11))
    api._texts["sys-1"], api._times["sys-1"] = proposal.content_text, stamp(jst(11))
    ledger = _ledger(session, api)
    for _ in range(3):  # 同じ投稿が何度来ても 1 行
        ledger.refresh(now=jst(13), source="periodic")
    rows = session.scalars(select(ThreadsAccountPost)).all()
    assert [(r.threads_media_id, r.origin, r.threads_publication_id, r.post_kind)
            for r in rows] == [("sys-1", "system", publication.id, "normal")]
    # 公開の記録が無くなったように見えても system から manual には戻さない
    publication.status = "failed"
    session.commit()
    ledger.refresh(now=jst(13, 15), source="periodic")
    assert _post(session, "sys-1").origin == "system"


def test_an_in_flight_publication_makes_the_origin_unknown_until_reconciled(
        session, article) -> None:  # fmt: skip
    api = FakeThreadsAPI()
    proposal = _article_post(session, article, "a")
    inflight = ThreadsPublication(
        proposal_id=proposal.id, proposal_hash=proposal.proposal_hash,
        source_article_id=article.id, angle="insight", exact_published_text=proposal.content_text,
        status=PUB_PUBLISHING, publish_started_at=jst(12, 58))  # fmt: skip
    session.add(inflight)
    session.commit()
    api.post_manually("late-1", proposal.content_text, jst(12, 59))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    post = _post(session, "late-1")
    assert (post.origin, post.origin_evidence) == ("unknown", "publication_in_flight")
    # 照合で同じ投稿 ID が公開の記録に入った → 同じ行を system に (別の行を作らない)
    inflight.status, inflight.threads_media_id, inflight.published_at = (
        PUB_PUBLISHED, "late-1", jst(12, 59))
    session.commit()
    ledger.refresh(now=jst(13, 15), source="periodic")
    rows = session.scalars(select(ThreadsAccountPost)).all()
    assert len(rows) == 1 and rows[0].id == post.id
    assert (rows[0].origin, rows[0].threads_publication_id) == ("system", inflight.id)
    assert _events(session, rows[0]) == ["discovered", "origin_changed"]


def test_unknown_becomes_manual_once_no_publication_is_in_flight(session, article) -> None:
    api = FakeThreadsAPI()
    proposal = _article_post(session, article, "a")
    inflight = ThreadsPublication(
        proposal_id=proposal.id, proposal_hash=proposal.proposal_hash,
        source_article_id=article.id, angle="insight", exact_published_text="x",
        status=PUB_PUBLISHING, publish_started_at=jst(12, 58))  # fmt: skip
    session.add(inflight)
    session.commit()
    api.post_manually("m-1", MANUAL_TEXT, jst(12, 59))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    assert _post(session, "m-1").origin == "unknown"  # 無理に manual / system に決めない
    inflight.status = "failed"
    session.commit()
    ledger.refresh(now=jst(13, 15), source="periodic")
    assert _post(session, "m-1").origin == "manual"


def test_a_manual_post_is_reconciled_to_system_only_with_publication_evidence(
        session, article) -> None:  # fmt: skip
    api = FakeThreadsAPI()
    api.post_manually("x-1", "本文", jst(12))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    post_id = _post(session, "x-1").id
    proposal = _article_post(session, article, "b", status=TP_APPROVED)
    _published(session, proposal, "x-1", jst(12))  # 同じ投稿 ID の公開の記録が見つかった
    ledger.refresh(now=jst(13, 15), source="periodic")
    post = _post(session, "x-1")
    assert post.id == post_id and post.origin == "system"
    assert _count(session, ThreadsAccountPost) == 1


def test_same_text_with_different_post_ids_are_different_posts(session) -> None:
    api = FakeThreadsAPI()
    api.post_manually("p-1", MANUAL_TEXT, jst(11))
    api.post_manually("p-2", MANUAL_TEXT, jst(12))
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    assert sorted(p.threads_media_id for p in session.scalars(select(ThreadsAccountPost))) == [
        "p-1", "p-2"]


def test_edits_missing_posts_and_reappearance_do_not_break_anything(session) -> None:
    api = FakeThreadsAPI()
    api.post_manually("e-0", "古い投稿", jst(9))
    api.post_manually("e-1", MANUAL_TEXT, jst(11))
    api.post_manually("e-2", "別の投稿", jst(12))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    by_id = {i["id"]: i for i in api.manual}
    by_id["e-1"]["text"] = MANUAL_TEXT + "（追記）"  # e-1 を人が編集
    # e-2 が一覧から消えた (削除かもしれないし一時的かもしれない)。e-0 は一覧の範囲の外 (古い)。
    api.manual = [by_id["e-1"]]
    summary = ledger.refresh(now=jst(13, 15), source="periodic")
    e0, e1, e2 = _post(session, "e-0"), _post(session, "e-1"), _post(session, "e-2")
    assert e1.latest_text.endswith("（追記）") and "text_changed" in _events(session, e1)
    # 一覧の範囲 (いちばん古い項目 11:00 以降) にあるはずの e-2 だけを「出なかった」とする
    assert summary["missing"] == ["e-2"] and e0.missing_since is None
    assert e2.missing_since is not None and _post(session, "e-2") is not None  # 消さない
    api.manual = [by_id["e-2"], by_id["e-1"]]  # 再び出た
    summary = ledger.refresh(now=jst(13, 30), source="periodic")
    assert summary["reappeared"] == ["e-2"] and _post(session, "e-2").missing_since is None
    assert _events(session, e2) == ["discovered", "missing_from_listing", "reappeared"]


def test_events_are_append_only(session) -> None:
    api = FakeThreadsAPI()
    api.post_manually("a-1", MANUAL_TEXT, jst(12))
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    event = session.scalars(select(ThreadsAccountPostEvent)).first()
    event.actor = "someone else"
    with pytest.raises(RuntimeError, match="append-only"):
        session.commit()
    session.rollback()
    session.delete(session.scalars(select(ThreadsAccountPostEvent)).first())
    with pytest.raises(RuntimeError, match="append-only"):
        session.commit()
    session.rollback()


# -- quota / cooldown --------------------------------------------------------------------
def test_a_manual_post_delays_the_article_lane_without_consuming_it(session, article) -> None:
    api = FakeThreadsAPI()
    proposal = _article_post(session, article, "a")
    api.post_manually("manual-1", MANUAL_TEXT, jst(12, 55))  # 人が 12:55 に投稿
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    queue = _queue(session, api)
    evaluation = queue.evaluate(now=jst(13), publication_enabled=True)
    assert BLOCKER_RECENT_ACCOUNT_POST in evaluation.blockers
    assert evaluation.next_evaluation_at == jst(13, 55)  # 待ちが明けたら見直す (取り消さない)
    result = _publisher(session, api).publish_one(now=jst(13))
    assert not result.attempted and api.writes() == []
    session.refresh(proposal)
    assert proposal.status == TP_APPROVED  # 公開済みにも却下にもしない
    assert queue.published_today(jst(13)) == 0
    # 待ちが明ければ、ふつうに 1 本出る (manual 投稿は記事の本数を消費していない)
    later = _publisher(session, api).publish_one(now=jst(13, 56))
    assert later.published and queue.published_today(jst(13, 56)) == 1


def test_a_manual_post_delays_the_growth_lane_and_keeps_the_daily_growth_slot(
        session, article) -> None:  # fmt: skip
    api = FakeThreadsAPI()
    growth = _growth_post(session)
    thanks = "フォローしてくれた方ありがとうございます。今日も検証を続けます。"
    api.post_manually("manual-g", thanks, jst(12, 55))
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    queue = _queue(session, api)
    blocked = queue.evaluate_growth(now=jst(13), publication_enabled=True)
    assert BLOCKER_RECENT_ACCOUNT_POST in blocked.blockers
    assert queue.growth_published_today(jst(13)) == 0  # manual は Growth の 1 日 1 本を使わない
    assert not _publisher(session, api).publish_growth_one(now=jst(13)).attempted
    session.refresh(growth)
    assert growth.status == TP_APPROVED
    later = _publisher(session, api).publish_growth_one(now=jst(13, 56))
    assert later.published and queue.growth_published_today(jst(13, 56)) == 1


def test_the_growth_day_record_and_retry_are_untouched(session, article, tmp_path) -> None:
    from app.services.threads_growth_service import ThreadsGrowthService

    api = FakeThreadsAPI()
    api.post_manually("manual-g", GROWTH_TEXT, jst(12))
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    growth = ThreadsGrowthService(session, timezone=JST, directory=tmp_path)
    assert growth.published_on(datetime.fromisoformat(DAY).date()) == []
    assert list(tmp_path.iterdir()) == []  # その日の記録 (再試行・置き換え) には触れない


# -- pre-publication refresh -------------------------------------------------------------
def test_the_list_is_always_read_right_before_publishing(session, article) -> None:
    api = FakeThreadsAPI()
    _article_post(session, article, "a")
    result = _publisher(session, api).publish_one(now=jst(13))
    assert result.published
    order = [("list" if r["path"].endswith("/9876543210/threads") and r["method"] == "GET"
              else r["method"]) for r in api.requests]  # fmt: skip
    assert order.index("list") < order.index("POST")  # 書く前に読む
    assert result.account_post_refresh["ok"] is True


def test_a_manual_post_seen_only_by_the_pre_publication_read_defers_the_post(
        session, article) -> None:  # fmt: skip
    api = FakeThreadsAPI()
    proposal = _article_post(session, article, "a")
    _ledger(session, api).refresh(now=jst(12, 50), source="periodic")  # 12:50 の定期の読み
    api.post_manually("manual-1", MANUAL_TEXT, jst(12, 58))  # 12:58 に人が投稿
    result = _publisher(session, api).publish_one(now=jst(13))  # 13:00 の評価
    assert not result.attempted and api.writes() == []
    assert BLOCKER_RECENT_ACCOUNT_POST in result.blocked_reasons
    session.refresh(proposal)
    assert proposal.status == TP_APPROVED


@pytest.mark.parametrize("lane", ["article", "growth"])
def test_a_failed_pre_publication_read_never_publishes(session, article, lane) -> None:
    api = FakeThreadsAPI()
    proposal = _article_post(session, article, "a") if lane == "article" else _growth_post(session)
    api.listing_status = 500
    publisher = _publisher(session, api)
    result = (publisher.publish_one(now=jst(13)) if lane == "article"
              else publisher.publish_growth_one(now=jst(13)))
    assert result.outcome == "account_post_refresh_failed" and not result.attempted
    assert api.writes() == []  # fail closed
    assert any("fail closed" in r for r in result.blocked_reasons)
    assert result.next_evaluation_at is not None
    session.refresh(proposal)
    assert proposal.status == TP_APPROVED  # 次の評価へ回す (却下・公開済みにしない)
    assert _count(session, ThreadsPublication) == 0


def test_the_worker_discovers_every_15_minutes_only_when_it_may_read(session) -> None:
    from app.services.threads_worker_service import ThreadsWorkerService
    from app.social.threads.worker import SUBSYSTEM_ACCOUNT_POST_DISCOVERY
    from tests.integration.test_threads_worker_service import _factory

    api = FakeThreadsAPI()
    api.post_manually("w-1", MANUAL_TEXT, jst(12))
    quiet = ThreadsWorkerService(_factory(session), settings=_Settings(),
                                 threads_service=_service(api))
    assert quiet.build_schedule(jst(13)).state(SUBSYSTEM_ACCOUNT_POST_DISCOVERY).enabled is False
    reading = ThreadsWorkerService(_factory(session), settings=_Settings(),
                                   threads_service=_service(api), collect_insights=True)
    assert reading.build_schedule(jst(13)).state(SUBSYSTEM_ACCOUNT_POST_DISCOVERY).enabled
    result = reading.handlers()[SUBSYSTEM_ACCOUNT_POST_DISCOVERY](jst(13))
    assert result.next_run_at == jst(13, 15)
    assert result.summary["discovered_count"] == 1 and result.summary["threads_writes"] == 0
    assert "publication_evaluation" in result.wake  # 新しい manual 投稿: 公開の評価を前倒し


def test_a_failing_periodic_read_does_not_stop_the_worker(session) -> None:
    from app.services.threads_worker_service import ThreadsWorkerService
    from app.social.threads.worker import SUBSYSTEM_ACCOUNT_POST_DISCOVERY
    from tests.integration.test_threads_worker_service import _factory

    api = FakeThreadsAPI()
    api.listing_status = 503
    worker = ThreadsWorkerService(_factory(session), settings=_Settings(),
                                  threads_service=_service(api), collect_insights=True)
    result = worker.handlers()[SUBSYSTEM_ACCOUNT_POST_DISCOVERY](jst(13))
    assert result.summary["discovered"] is False and result.next_run_at == jst(13, 15)


# -- proposal collision ---------------------------------------------------------------------
def test_a_manual_post_identical_to_an_approved_proposal_supersedes_it(session, article) -> None:
    api = FakeThreadsAPI()
    same = _article_post(session, article, "a", text=MANUAL_TEXT)
    other = _article_post(session, article, "b", text="まったく別の話題の投稿。体制の話。")
    api.post_manually("manual-1", MANUAL_TEXT, jst(10))
    summary = _ledger(session, api).refresh(now=jst(13), source="periodic")
    session.refresh(same)
    session.refresh(other)
    assert same.status == TP_SUPERSEDED
    assert "manual Threads post manual-1 made proposal unnecessary (identical)" in (
        same.status_reason)
    assert "not a system publication" in same.status_reason
    assert other.status == TP_APPROVED
    assert summary["superseded_proposals"][0]["proposal_id"] == same.id
    # 公開の成功としては記録しない (公開の行を作らない)
    assert _count(session, ThreadsPublication) == 0
    post = _post(session, "manual-1")
    assert _events(session, post) == ["discovered", "proposal_superseded"]
    # system が後から同じ内容を出さない
    result = _publisher(session, api).publish_one(now=jst(14))
    assert result.proposal_id in (None, other.id)


def test_an_old_manual_post_found_on_the_first_read_does_not_supersede_proposals(
        session, article) -> None:  # fmt: skip
    api = FakeThreadsAPI()
    same = _article_post(session, article, "a", text=MANUAL_TEXT)
    api.post_manually("old-1", MANUAL_TEXT, jst(10, day=1))  # 4 週間前の手の投稿
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    session.refresh(same)
    assert same.status == TP_APPROVED  # 古い投稿では消さない
    assert _post(session, "old-1").origin == "manual"  # 台帳・分析には入る


def test_a_manual_growth_like_post_supersedes_the_similar_growth_proposal(session) -> None:
    api = FakeThreadsAPI()
    growth = _growth_post(session)
    api.post_manually("manual-g", GROWTH_TEXT, jst(10))
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    session.refresh(growth)
    assert growth.status == TP_SUPERSEDED and "growth:" in growth.status_reason
    assert _queue(session, api).growth_published_today(jst(13)) == 0


def test_manual_posts_feed_the_existing_duplicate_and_wording_checks(session, article) -> None:
    from app.services.threads_proposal_stock_service import manual_recent_items
    from app.social.threads.validators import normalized_identity

    api = FakeThreadsAPI()
    api.post_manually("manual-1", MANUAL_TEXT, jst(10))
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    items = manual_recent_items(session, limit=12)
    assert items[0]["ref"] == "manual Threads post manual-1" and items[0]["text"] == MANUAL_TEXT
    from app.services.threads_proposal_stock_service import ThreadsProposalStockService

    stock = ThreadsProposalStockService.__new__(ThreadsProposalStockService)
    stock._session = session
    assert stock._live_identities()[normalized_identity(MANUAL_TEXT)] == (
        "manual Threads post manual-1")
    assert any(i["ref"] == "manual Threads post manual-1" for i in stock._recent_items())


# -- classification -------------------------------------------------------------------------
def test_a_human_classifies_a_manual_post_with_a_plan_first(session) -> None:
    api = FakeThreadsAPI()
    api.post_manually("c-1", MANUAL_TEXT, jst(12))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    post = _post(session, "c-1")
    plan = ledger.classify(post.id, kind="growth", by="human", reason="follow post from the app",
                           now=jst(13))  # fmt: skip
    assert plan["executed"] is False and _post(session, "c-1").post_kind == "unknown"
    ledger.classify(post.id, kind="growth", by="human", reason="follow post from the app",
                    execute=True, now=jst(13))  # fmt: skip
    ledger.classify(post.id, kind="normal", by="human", reason="actually an article topic",
                    execute=True, now=jst(13, 5))  # fmt: skip
    post = _post(session, "c-1")
    assert post.post_kind == "normal"
    history = [(e.event_type, (e.detail_json or {}).get("from"), (e.detail_json or {}).get("to"),
                e.actor) for e in ledger.events(post.id)]  # fmt: skip
    assert history[1:] == [("classification_changed", "unknown", "growth", "human"),
                           ("classification_changed", "growth", "normal", "human")]


@pytest.mark.parametrize("kw,match", [
    ({"kind": "other"}, "normal or growth"), ({"by": "a@b.c"}, "short name"),
    ({"reason": "x"}, "3-200"), ({"reason": "see https://x"}, "without a URL"),
])  # fmt: skip
def test_classification_refuses_bad_input(session, kw, match) -> None:
    api = FakeThreadsAPI()
    api.post_manually("c-1", MANUAL_TEXT, jst(12))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    args = {"kind": "growth", "by": "human", "reason": "follow post", **kw}
    with pytest.raises(AccountPostError, match=match):
        ledger.classify(_post(session, "c-1").id, execute=True, now=jst(13), **args)


def test_a_system_post_is_not_classified_by_hand(session, article) -> None:
    api = FakeThreadsAPI()
    proposal = _article_post(session, article, "a")
    _published(session, proposal, "sys-1", jst(11))
    api._texts["sys-1"], api._times["sys-1"] = proposal.content_text, stamp(jst(11))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    with pytest.raises(AccountPostError, match="comes from its proposal"):
        ledger.classify(_post(session, "sys-1").id, kind="growth", by="human",
                        reason="no", execute=True, now=jst(13))  # fmt: skip


def test_the_cli_plans_by_default(engine, capsys) -> None:
    from sqlalchemy.orm import sessionmaker

    from scripts.manage_threads_account_posts import main

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    api = FakeThreadsAPI()
    api.post_manually("c-1", MANUAL_TEXT, jst(12))
    with factory() as s:
        _ledger(s, api).refresh(now=jst(13), source="periodic")
        pid = _post(s, "c-1").id
    args = ["classify", str(pid), "--kind", "growth", "--by", "human", "--reason", "app post"]
    assert main(args, session_factory=factory, now=jst(13)) == 0
    with factory() as s:
        assert _post(s, "c-1").post_kind == "unknown"
    assert main([*args, "--execute"], session_factory=factory, now=jst(13)) == 0
    assert main(["discover"], session_factory=factory, now=jst(13)) == 0  # PLAN: 読まない
    assert api.listings() == 1
    out = capsys.readouterr().out
    assert "Threads writes: 0" in out and "PLAN only" in out
    with factory() as s:
        assert _post(s, "c-1").post_kind == "growth"


# -- insights / analysis --------------------------------------------------------------------
def test_manual_insights_are_stored_once_per_observation_and_analysed_with_origin(
        session, article) -> None:  # fmt: skip
    api = FakeThreadsAPI()
    proposal = _article_post(session, article, "a")
    _published(session, proposal, "sys-1", jst(11))
    api._texts["sys-1"], api._times["sys-1"] = proposal.content_text, stamp(jst(11))
    api.post_manually("manual-1", MANUAL_TEXT, jst(12))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    manual = _post(session, "manual-1")
    due = ledger.due_for_insights(jst(13), by_maturity={}, stop_after_hours=336)
    assert due["due"] == [manual.id]  # system は公開の記録の経路で観測する
    ledger.collect_insights(due["due"], now=jst(13))
    ledger.collect_insights(due["due"], now=jst(13))  # 同じ観測は 2 回積まない
    snaps = session.scalars(select(ThreadsInsightSnapshot)
                            .where(ThreadsInsightSnapshot.threads_account_post_id == manual.id)
                            ).all()  # fmt: skip
    assert len(snaps) == 1 and snaps[0].threads_publication_id is None
    assert (snaps[0].views, snaps[0].likes, snaps[0].shares) == (120, 3, None)  # 欠測は NULL
    rows = {r["threads_media_id"]: r for r in ledger.dataset(now=jst(13), tz=JST)}
    assert (rows["manual-1"]["origin"], rows["manual-1"]["post_kind"], rows["manual-1"]["angle"]
            ) == ("manual", "unknown", "unknown")
    assert (rows["sys-1"]["origin"], rows["sys-1"]["post_kind"], rows["sys-1"]["angle"]) == (
        "system", "normal", "insight")
    obs = rows["manual-1"]["observations"][0]
    assert obs["post_age_hours"] == 1.0 and obs["metrics"]["views"] == 120
    assert rows["manual-1"]["published_local_hour"] == 12  # 時間帯の比較の材料 (T6.6)


def test_unavailable_metrics_are_recorded_once_and_do_not_raise(session) -> None:
    api = FakeThreadsAPI()
    api.post_manually("gone-1", MANUAL_TEXT, jst(12))
    ledger = _ledger(session, api)
    ledger.refresh(now=jst(13), source="periodic")
    api.insights_status = 404
    post = _post(session, "gone-1")
    first = ledger.collect_insights([post.id], now=jst(13))
    ledger.collect_insights([post.id], now=jst(14))
    assert first["details"][0]["result"] == "failed"
    assert _events(session, post).count("metrics_unavailable") == 1


def test_system_snapshots_link_to_their_account_post_automatically(session, article) -> None:
    proposal = _article_post(session, article, "a")
    publication = _published(session, proposal, "sys-1", jst(11))
    session.add(ThreadsInsightSnapshot(threads_publication_id=publication.id,
                                       threads_media_id="sys-1", observed_at=jst(12), views=5,
                                       outcome="observed"))  # fmt: skip
    session.commit()
    snap = session.scalars(select(ThreadsInsightSnapshot)).one()
    post = session.get(ThreadsAccountPost, snap.threads_account_post_id)
    assert (post.origin, post.threads_publication_id, post.threads_media_id) == (
        "system", publication.id, "sys-1")


def test_the_performance_diagnostic_lists_manual_posts_as_untracked(session) -> None:
    from app.services.threads_performance_service import untracked_posts

    api = FakeThreadsAPI()
    api.post_manually("manual-1", MANUAL_TEXT, jst(12))
    _ledger(session, api).refresh(now=jst(13), source="periodic")
    report = untracked_posts(session)
    assert report["status"] == "available"
    assert [r["media_id"] for r in report["remote_not_tracked"]] == ["manual-1"]


# -- regression: no manual posts = unchanged behaviour ----------------------------------------
def test_without_manual_posts_the_article_and_growth_lanes_publish_as_before(
        session, article) -> None:  # fmt: skip
    api = FakeThreadsAPI()
    _article_post(session, article, "a")
    _growth_post(session)
    publisher = _publisher(session, api)
    article_result = publisher.publish_one(now=jst(13))
    growth_result = publisher.publish_growth_one(
        now=jst(13), companion_publication_id=article_result.publication_id)
    assert article_result.published and growth_result.published
    assert growth_result.growth_trigger == "article_companion"
    assert api.writes().count(f"/v1.0/{_Settings.threads_user_id}/threads_publish") == 2
    queue = _queue(session, api)
    assert (queue.published_today(jst(13)), queue.growth_published_today(jst(13))) == (1, 1)
    # 出した後の一覧の読みで、自分の公開は system として 1 行ずつ (manual にならない)
    _ledger(session, api).refresh(now=jst(13, 15), source="periodic")
    assert sorted(p.origin for p in session.scalars(select(ThreadsAccountPost))) == [
        "system", "system"]
