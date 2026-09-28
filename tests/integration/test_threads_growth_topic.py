"""T6.3.3b: Growth Post のトピック "インサイト祭り" (偽の Threads API だけ)。

- 記事: いつも "AI Threads" (変えない)。Growth: いつも "インサイト祭り" (公開のときに付ける)。
- トピックはメタデータ: 本文・hash・文字数・リンクは変えない。
- 断られたら: トピックなしで出し直さない・別のトピックに変えない。Growth の枠だけを止める
  (照合待ち + アラート)。記事は続く。一時的な失敗の再試行は同じトピックで送る。
- 公開済みの Growth Post (#25 のような行) は変えない (前へ進むだけ)。
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs

import httpx
from sqlalchemy import select

from app.approval.review_snapshot import build_snapshot
from app.models import PUB_FAILED, PUB_PUBLISHED, ThreadsPublication, ThreadsPublicationAttempt
from app.operations.threads_health import build_autopublish_failure_draft
from app.social.threads.topic import (
    THREADS_GROWTH_TOPIC_TAG,
    THREADS_NORMAL_TOPIC_TAG,
    topic_tag_for,
)
from tests.integration.test_threads_growth_lane import (
    GROWTH_TEXT,
    FakeThreadsAPI,
    _article_post,
    _growth_post,
    _published_article,
    _run_cycle,
    article,  # noqa: F401 (fixture)
    jst,
)

G = "インサイト祭り"


class TopicAPI(FakeThreadsAPI):
    """Growth のトピックを断る / 一時的に失敗させることができる代役。"""

    def __init__(self, *, reject_growth_topic=False, transient_growth=0) -> None:
        super().__init__()
        self.reject_growth_topic = reject_growth_topic
        self.transient_growth = transient_growth

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
        if request.method == "POST" and request.url.path.endswith("/threads"):
            if form.get("topic_tag") == G and self.reject_growth_topic:
                self.requests.append({"method": "POST", "path": request.url.path, "form": form})
                return httpx.Response(400, json={"error": {
                    "message": "Invalid parameter: topic_tag", "code": 100}})  # fmt: skip
            if form.get("topic_tag") == G and self.transient_growth > 0:
                self.transient_growth -= 1
                self.requests.append({"method": "POST", "path": request.url.path, "form": form})
                return httpx.Response(503, json={"error": {"message": "down"}})
        return super().handler(request)


def _growth_creates(api) -> list[dict]:
    return [f for f in api.creates() if f.get("text") == GROWTH_TEXT]


def test_the_topic_policy() -> None:
    assert THREADS_NORMAL_TOPIC_TAG == "AI Threads" and THREADS_GROWTH_TOPIC_TAG == G
    assert topic_tag_for("article") == "AI Threads" and topic_tag_for("account_growth") == G


def test_case_a_and_b_growth_and_article_payloads(session, article) -> None:  # noqa: F811
    _published_article(session, article, "p", jst(11))
    art = _article_post(session, article, "a", approved_at=jst(10))
    growth = _growth_post(session, approved_at=jst(12))
    before = (growth.content_text, growth.proposal_hash, growth.character_count, growth.link_mode)
    api = TopicAPI()
    a, g = _run_cycle(session, api, jst(13))
    assert a.published and a.proposal_id == art.id and g.published
    article_create, growth_create = api.creates()
    assert article_create["topic_tag"] == "AI Threads"
    assert growth_create["topic_tag"] == G
    assert growth_create["text"] == GROWTH_TEXT and G not in growth_create["text"]
    assert "http" not in growth_create["text"]
    assert set(article_create) == set(growth_create) == {"media_type", "text", "topic_tag",
                                                          "access_token"}  # fmt: skip
    session.refresh(growth)
    assert (growth.content_text, growth.proposal_hash, growth.character_count,
            growth.link_mode) == before  # fmt: skip
    step = session.scalars(select(ThreadsPublicationAttempt).where(
        ThreadsPublicationAttempt.threads_publication_id == g.publication_id)).first()  # fmt: skip
    assert step.detail_json["content_kind"] == "account_growth"
    assert step.detail_json["topic_tag"] == G and step.detail_json["topic_tag_sent"] is True
    row = session.get(ThreadsPublication, g.publication_id)
    assert row.threads_media_id and row.status == PUB_PUBLISHED


def test_case_c_a_rejected_growth_topic_never_goes_untagged_and_articles_continue(
    session, article  # noqa: F811
) -> None:
    _published_article(session, article, "p", jst(11))
    _article_post(session, article, "a", approved_at=jst(10))
    second = _article_post(session, article, "b", approved_at=jst(10, 5))
    growth = _growth_post(session, approved_at=jst(12))
    api = TopicAPI(reject_growth_topic=True)
    a, g = _run_cycle(session, api, jst(13))
    assert a.published and g.outcome == "failed" and not g.published
    assert [c["topic_tag"] for c in _growth_creates(api)] == [G]  # 1 回だけ・トピック付き
    row = session.get(ThreadsPublication, g.publication_id)
    assert row.status == PUB_FAILED and row.reconciliation_required is True
    assert "no untagged fallback" in row.reconciliation_note
    alert = build_autopublish_failure_draft(g.error, growth=True, publication_id=row.id)
    assert alert.title == "Threads 側で Topic が受け付けられませんでした"
    assert "Growth Post だけを止め、通常投稿は続けます。" in alert.summary
    # Growth の枠は止まる (人が判断するまで)。トピックなし・別のトピックの作成は無い。
    for minutes in (30, 90):  # 記事の次の枠 (15:00) より前
        _a, g2 = _run_cycle(session, api, jst(13) + timedelta(minutes=minutes))
        assert not g2.attempted and "uncertain_publication" in g2.blocked_reasons
    assert [c.get("topic_tag") for c in _growth_creates(api)] == [G]
    # 記事は続く。
    a3, _g3 = _run_cycle(session, api, jst(15, 1))
    assert a3.published and a3.proposal_id == second.id
    assert session.get(type(growth), growth.id).content_text == GROWTH_TEXT


def test_a_transient_growth_failure_resends_the_same_topic(session, article) -> None:  # noqa: F811
    _growth_post(session, approved_at=jst(8))
    api = TopicAPI(transient_growth=1)
    _a, g = _run_cycle(session, api, jst(9))
    assert g.outcome == "failed" and not g.published
    _a, g2 = _run_cycle(session, api, jst(9, 30))
    assert g2.published
    assert [c["topic_tag"] for c in _growth_creates(api)] == [G, G]


def test_historical_published_growth_rows_are_not_rewritten(session, article) -> None:  # noqa: F811
    """#25 のように、トピックなしで出した Growth Post の行と記録は変えない。"""

    old = _growth_post(session, day="2026-09-28", approved_at=jst(12))
    pub = ThreadsPublication(
        proposal_id=old.id, proposal_hash=old.proposal_hash, source_article_id=None,
        angle="account_growth", exact_published_text=old.content_text, status=PUB_PUBLISHED,
        published_at=jst(13, 55), threads_media_id="m-old",
    )  # fmt: skip
    session.add(pub)
    session.commit()
    session.add(ThreadsPublicationAttempt(
        threads_publication_id=pub.id, step="create_container", outcome="succeeded",
        detail_json={"content_kind": "account_growth", "topic_tag": None,
                     "topic_tag_sent": False}, started_at=jst(13, 55)))  # fmt: skip
    session.commit()
    api = TopicAPI()
    _run_cycle(session, api, jst(15))  # 今日の Growth はもう出た → 何もしない
    step = session.scalars(select(ThreadsPublicationAttempt).where(
        ThreadsPublicationAttempt.threads_publication_id == pub.id)).one()  # fmt: skip
    assert step.detail_json["topic_tag"] is None and step.detail_json["topic_tag_sent"] is False
    assert _growth_creates(api) == []
    from app.project_state.db_state import topic_policy

    state = topic_policy(session.connection())
    assert state["growth_topic_tag"] == G
    assert state["growth_topic_production_acceptance"] == "pending_canary"  # 古い行は数えない


def test_case_d_approval_rendering_and_no_relay_change(session, article) -> None:  # noqa: F811
    from app.services.threads_approval_digest_service import _display, _kind_lines, _topic_text

    growth = _growth_post(session, approved_at=None)
    snap = build_snapshot(subject_type="threads_post", subject=growth, article=None)
    assert snap["topic_label"] == G and snap["post_kind_label"] == "Growth Post"
    assert snap["goal_label"] == "フォロワー100人" and snap["link_mode"] == "なし"
    from app.operations.report_format import render_approval_digest_text

    item = {"proposal_id": growth.id, "article_title": "Growth Post", "angle": growth.angle,
            "preview": "…", "timing": None, "topic": _topic_text(growth),
            **_kind_lines(growth), **_display(growth), "review_url": "https://x/r"}  # fmt: skip
    text = render_approval_digest_text(items=[item], expires_at_local="-")
    for fragment in ("投稿種別: Growth Post", "目標: フォロワー100人", "リンク: なし",
                     f"トピック: {G}"):  # fmt: skip
        assert fragment in text, fragment
    # 中継の許可リストはすでに topic_label を持つ (WordPress の配備は要らない)。
    from pathlib import Path

    lib = Path("wordpress/mu-plugins/bizfluxlab-approval-relay/lib-core.php").read_text("utf-8")
    assert "'topic_label'" in lib and '["トピック",s.topic_label]' in lib
