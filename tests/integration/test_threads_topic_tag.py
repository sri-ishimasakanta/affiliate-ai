"""T6.3.2: 通常の投稿に固定のトピック "AI Threads" を付ける (偽の HTTP だけ。Meta には出ない)。

本物の ``ThreadsService`` / ``ThreadsClient`` を ``httpx.MockTransport`` につなぎ、実際に送る
フォームの中身を確かめる。

偽の E2E:

- A: 通常の投稿 → コンテナ作成に topic_tag="AI Threads" → 公開 → 記録にトピック
- B: 将来のアカウントを育てる投稿 (account_growth) → topic_tag を送らない → 公開
- C: 通常の投稿で API がトピックを断る → トピックなしで出し直さない → 公開されない → 記録
- D: link_mode=none の通常の投稿 → URL なし・topic_tag はある
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import func, select

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    PUB_FAILED,
    PUB_PUBLISHED,
    TP_APPROVED,
    Article,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.operations.report_format import (
    render_approval_digest_html,
    render_approval_digest_text,
)
from app.services.threads_approval_digest_service import _topic_text
from app.services.threads_publication_service import ThreadsPublicationService
from app.social.threads.client import ThreadsClient
from app.social.threads.models import TEXT_MAX_LENGTH
from app.social.threads.service import ThreadsService
from app.social.threads.topic import (
    CONTENT_KIND_ACCOUNT_GROWTH,
    CONTENT_KIND_ARTICLE,
    CONTENT_KINDS,
    THREADS_NORMAL_TOPIC_TAG,
    TopicPolicyError,
    content_kind,
    topic_tag_for,
    validate_topic_tag,
)

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
_TOKEN = "THAAAsecret-token-must-never-appear"
_TEXT = "体制を先に決めたほうが早い。\n参照先と版を残しておくほうが更新しやすい。"
_URL = f"{_BASE}/generative-ai-guidelines/?utm_source=threads&utm_medium=social"


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = _TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


class FakeMeta:
    """Threads API の代役。受け取ったフォームを記録する (外へは出ない)。"""

    def __init__(self, *, reject_topic=False, create_status=None, media_id="media-1") -> None:
        self.media_id = media_id
        self.requests: list[dict] = []
        self.reject_topic = reject_topic
        self.create_status = create_status
        self.published_text: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
        path = request.url.path
        self.requests.append({"method": request.method, "path": path, "form": form})
        if request.method == "POST" and path.endswith("/threads"):
            if self.create_status:
                return httpx.Response(self.create_status, json={"error": {"message": "down"}})
            if self.reject_topic and "topic_tag" in form:
                return httpx.Response(400, json={"error": {
                    "message": "Invalid parameter: topic_tag", "code": 100}})  # fmt: skip
            self._text = form.get("text")
            return httpx.Response(200, json={"id": "container-1"})
        if request.method == "POST" and path.endswith("/threads_publish"):
            self.published_text = self._text
            return httpx.Response(200, json={"id": self.media_id})
        if request.method == "GET":
            return httpx.Response(200, json={
                "id": self.media_id, "text": self.published_text,
                "permalink": "https://www.threads.net/@bizfluxlab/post/abc",
                "timestamp": "2026-09-28T03:00:30+0000", "username": "bizfluxlab"})  # fmt: skip
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    def creates(self) -> list[dict]:
        return [r["form"] for r in self.requests if r["path"].endswith("/threads")]

    def publishes(self) -> list[dict]:
        return [r for r in self.requests if r["path"].endswith("/threads_publish")]

    def sanitized(self) -> list[dict]:
        """報告に出してよい形 (token を伏せる)。"""

        return [
            {**r, "form": {k: ("***" if k == "access_token" else v) for k, v in r["form"].items()}}
            for r in self.requests
        ]


def _publisher(session, meta: FakeMeta) -> ThreadsPublicationService:
    settings = _Settings()
    client = ThreadsClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(
        meta.handler)))  # fmt: skip
    service = ThreadsService(settings, client=client, sleep=lambda _s: None)
    return ThreadsPublicationService(
        session, settings=settings, threads_service=service, sleep=lambda _s: None
    )


@pytest.fixture
def article(session) -> Article:
    row = Article(
        id=21, title="生成AIガイドライン｜要点と実務上の注意点", slug="generative-ai-guidelines",
        body="記事本文。", status="published", published_url=f"{_BASE}/generative-ai-guidelines/",
        published_at=_NOW - timedelta(days=10), article_type="informational",
        monetization_mode="supporting", wordpress_post_id="84",
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _proposal(session, article, *, text=_TEXT, link_mode="none", guidance=None, seed="a",
              policy_version="t2.1", angle="insight") -> ThreadsPostProposal:  # fmt: skip
    row = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle=angle,
        link_mode=link_mode,
        content_text=text,
        character_count=len(text),
        destination_url=_URL if link_mode == "article" else None,
        content_seed=seed * 64,
        proposal_hash=(chr(ord(seed) + 1)) * 64,
        policy_version=policy_version,
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
        learning_guidance_json=guidance,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _attempts(session, publication_id) -> list[ThreadsPublicationAttempt]:
    return session.scalars(
        select(ThreadsPublicationAttempt)
        .where(ThreadsPublicationAttempt.threads_publication_id == publication_id)
        .order_by(ThreadsPublicationAttempt.id)
    ).all()


def _no_secret(session, *objects) -> None:
    rows = session.scalars(select(ThreadsPublicationAttempt)).all()
    dumped = json.dumps(
        [[r.detail_json, r.error_message] for r in rows] + [o for o in objects],
        ensure_ascii=False, default=str,
    )  # fmt: skip
    pubs = session.scalars(select(ThreadsPublication)).all()
    dumped += json.dumps([[p.status_reason, p.error_message, p.reconciliation_note] for p in pubs])
    assert _TOKEN not in dumped


# --- the policy ---------------------------------------------------------------------------


def test_the_fixed_topic_is_ai_threads_and_is_valid() -> None:
    assert THREADS_NORMAL_TOPIC_TAG == "AI Threads"
    assert topic_tag_for(CONTENT_KIND_ARTICLE) == "AI Threads"
    assert topic_tag_for(CONTENT_KIND_ACCOUNT_GROWTH) is None
    assert set(CONTENT_KINDS) == {CONTENT_KIND_ARTICLE, CONTENT_KIND_ACCOUNT_GROWTH}
    assert validate_topic_tag("AI Threads") == "AI Threads"


@pytest.mark.parametrize("bad", ["", "x" * 51, "AI.Threads", "AI & Threads"])
def test_invalid_topic_tags_are_refused(bad) -> None:
    with pytest.raises(TopicPolicyError):
        validate_topic_tag(bad)


def test_unknown_content_kinds_are_refused() -> None:
    with pytest.raises(TopicPolicyError):
        topic_tag_for("digest")
    with pytest.raises(TopicPolicyError):
        content_kind(type("P", (), {"learning_guidance_json": {"content_kind": "digest"}})())


@pytest.mark.parametrize("angle", ["insight", "question", "comparison", "beginner_tip"])
@pytest.mark.parametrize("link_mode", ["none", "article"])
@pytest.mark.parametrize("hook", [None, "question", "choice", "none"])
def test_angle_link_mode_and_hook_never_change_the_topic(
    session, article, angle, link_mode, hook
) -> None:
    guidance = {"generation_brief": {"conversation_hook": hook}} if hook else None
    row = type("P", (), {"learning_guidance_json": guidance, "angle": angle,
                         "link_mode": link_mode})()  # fmt: skip
    assert topic_tag_for(content_kind(row)) == "AI Threads"


def test_a_proposal_without_a_marker_is_article_content(session, article) -> None:
    old = _proposal(session, article, guidance=None)
    assert content_kind(old) == CONTENT_KIND_ARTICLE
    growth = _proposal(session, article, seed="c",
                       guidance={"content_kind": CONTENT_KIND_ACCOUNT_GROWTH})  # fmt: skip
    assert content_kind(growth) == CONTENT_KIND_ACCOUNT_GROWTH


# --- fake E2E -----------------------------------------------------------------------------


def test_case_a_normal_post_sends_the_topic_and_records_it(session, article) -> None:
    proposal = _proposal(session, article)
    before = (proposal.content_text, proposal.proposal_hash, proposal.character_count)
    meta = FakeMeta()
    service = _publisher(session, meta)
    plan = service.plan(proposal_id=proposal.id, now=_NOW)
    assert plan.ok and plan.content_kind == "article" and plan.topic_tag == "AI Threads"
    assert "topic_tag='AI Threads'" in plan.would_call[0]
    out = service.publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert out.outcome == "published" and out.media_id == "media-1"
    (create,) = meta.creates()
    assert create["topic_tag"] == "AI Threads" and create["media_type"] == "TEXT"
    # トピックはメタデータ: 本文は承認された文字列そのまま (足さない)。
    assert create["text"] == _TEXT and "AI Threads" not in create["text"]
    assert "#" not in create["text"]
    row = session.get(ThreadsPublication, out.publication_id)
    assert row.status == PUB_PUBLISHED and row.exact_published_text == _TEXT
    steps = {a.step: a for a in _attempts(session, row.id)}
    assert steps["create_container"].detail_json == {
        "creation_id": "container-1", "content_kind": "article",
        "topic_tag": "AI Threads", "topic_tag_sent": True,
    }  # fmt: skip
    assert steps["publish_container"].detail_json["media_id"] == "media-1"
    assert steps["publish_container"].detail_json["topic_tag"] == "AI Threads"
    assert steps["readback"].detail_json["text_matches_approved"] is True
    session.refresh(proposal)
    assert (proposal.content_text, proposal.proposal_hash, proposal.character_count) == before
    _no_secret(session, out.as_dict(), meta.sanitized())
    assert all(r["form"].get("access_token") == "***" for r in meta.sanitized() if r["form"])


def test_case_b_account_growth_sends_no_topic(session, article) -> None:
    proposal = _proposal(session, article, guidance={"content_kind": "account_growth"})
    meta = FakeMeta()
    out = _publisher(session, meta).publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert out.outcome == "published"
    (create,) = meta.creates()
    assert "topic_tag" not in create  # 送らない (空の値も送らない)
    step = _attempts(session, out.publication_id)[0]
    assert step.detail_json["content_kind"] == "account_growth"
    assert step.detail_json["topic_tag"] is None and step.detail_json["topic_tag_sent"] is False


def test_case_c_topic_rejection_never_falls_back_to_an_untagged_post(session, article) -> None:
    proposal = _proposal(session, article)
    other = _proposal(session, article, seed="e", text="別の承認済みの投稿。")
    meta = FakeMeta(reject_topic=True)
    service = _publisher(session, meta)
    out = service.publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert out.outcome == "failed" and out.media_id is None
    # 作成は 1 回だけ。トピックなしの作成も、公開も無い。
    assert len(meta.creates()) == 1 and meta.creates()[0]["topic_tag"] == "AI Threads"
    assert meta.publishes() == []
    row = session.get(ThreadsPublication, out.publication_id)
    assert row.status == PUB_FAILED and row.threads_media_id is None
    assert row.reconciliation_required is True
    assert "no untagged fallback" in row.reconciliation_note
    (attempt,) = _attempts(session, row.id)
    assert attempt.step == "create_container" and attempt.outcome == "failed"
    assert attempt.detail_json == {
        "content_kind": "article", "topic_tag": "AI Threads", "topic_tag_sent": True,
        "status": 400, "api_code": "100",
    }  # fmt: skip
    assert "topic_tag" in (attempt.error_message or "")
    # 人が判断するまで、同じ提案も他の提案も出さない (自動の再試行も無い)。
    again = service.publish(proposal_id=proposal.id, execute=True, now=_NOW + timedelta(hours=3))
    assert again.outcome == "blocked"
    assert any("requires reconciliation" in r for r in again.blocked_reasons)
    blocked = service.plan(proposal_id=other.id, now=_NOW + timedelta(hours=3))
    assert not blocked.ok and any("requires reconciliation" in r for r in blocked.blocked_reasons)
    assert len(meta.creates()) == 1
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 1
    _no_secret(session, out.as_dict(), again.as_dict())


def test_case_d_link_none_has_no_url_and_still_has_the_topic(session, article) -> None:
    proposal = _proposal(session, article, link_mode="none")
    meta = FakeMeta()
    out = _publisher(session, meta).publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert out.outcome == "published"
    (create,) = meta.creates()
    assert create["topic_tag"] == "AI Threads"
    assert "http" not in create["text"] and _BASE not in create["text"]


def test_link_article_keeps_its_url_and_gets_the_topic(session, article) -> None:
    text = f"{_TEXT}\n{_URL}"
    proposal = _proposal(session, article, link_mode="article", text=text)
    meta = FakeMeta()
    out = _publisher(session, meta).publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert out.outcome == "published"
    (create,) = meta.creates()
    assert create["text"] == text and _URL in create["text"]  # UTM も同じ
    assert create["topic_tag"] == "AI Threads"


def test_the_500_character_limit_is_unchanged_by_the_topic(session, article) -> None:
    text = "あ" * TEXT_MAX_LENGTH
    proposal = _proposal(session, article, text=text)
    meta = FakeMeta()
    out = _publisher(session, meta).publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert out.outcome == "published"
    assert len(meta.creates()[0]["text"]) == TEXT_MAX_LENGTH  # トピックは数えない
    over = _proposal(session, article, text="い" * (TEXT_MAX_LENGTH + 1), seed="g")
    plan = _publisher(session, FakeMeta()).plan(proposal_id=over.id, now=_NOW)
    assert not plan.ok  # 501 字は今まで通り止まる


def test_a_pre_t632_approved_proposal_gets_the_topic_at_publication(session, article) -> None:
    """T6.3.2 より前に生成・承認された提案 (印なし・古い方針の版) も、公開の時点で付く。"""

    proposal = _proposal(session, article, guidance=None, policy_version="t2.1")
    approved_hash = proposal.proposal_hash
    meta = FakeMeta()
    out = _publisher(session, meta).publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert out.outcome == "published" and meta.creates()[0]["topic_tag"] == "AI Threads"
    row = session.get(ThreadsPublication, out.publication_id)
    # 承認の identity (hash) と送った本文は、承認されたものと同じ。
    assert row.proposal_hash == approved_hash and row.exact_published_text == _TEXT


def test_a_transient_create_failure_retries_with_the_topic_and_never_duplicates(
    session, article
) -> None:
    proposal = _proposal(session, article)
    meta = FakeMeta(create_status=503)
    service = _publisher(session, meta)
    first = service.publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert first.outcome == "failed" and first.reconciliation_required is False
    meta.create_status = None
    second = service.publish(proposal_id=proposal.id, execute=True, now=_NOW + timedelta(hours=1))
    assert second.outcome == "published"
    assert [c.get("topic_tag") for c in meta.creates()] == ["AI Threads", "AI Threads"]
    assert len(meta.publishes()) == 1
    third = service.publish(proposal_id=proposal.id, execute=True, now=_NOW + timedelta(hours=4))
    assert third.outcome == "blocked" and len(meta.publishes()) == 1
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 1


def test_an_unknown_content_kind_is_never_published(session, article) -> None:
    proposal = _proposal(session, article, guidance={"content_kind": "digest"})
    meta = FakeMeta()
    out = _publisher(session, meta).publish(proposal_id=proposal.id, execute=True, now=_NOW)
    assert out.outcome == "blocked" and meta.requests == []
    assert any("topic policy" in r for r in out.blocked_reasons)


# --- approval display (the digest email only; the review page is T6.4) ---------------------


def test_the_digest_email_shows_the_topic(session, article) -> None:
    normal = _proposal(session, article)
    growth = _proposal(session, article, seed="c", guidance={"content_kind": "account_growth"})
    assert _topic_text(normal) == "AI Threads" and _topic_text(growth) == "なし"
    item = {"proposal_id": 1, "article_title": "記事", "angle": "insight", "preview": "本文",
            "timing": None, "topic": _topic_text(normal), "review_url": "https://x/r"}  # fmt: skip
    assert "トピック: AI Threads" in render_approval_digest_text(items=[item], expires_at_local="-")
    assert "トピック: AI Threads" in render_approval_digest_html(items=[item], expires_at_local="-")
    legacy = {k: v for k, v in item.items() if k != "topic"}
    assert "トピック" not in render_approval_digest_text(items=[legacy], expires_at_local="-")


# --- Project State --------------------------------------------------------------------------


def test_project_state_reports_pending_until_a_tagged_container_is_accepted(
    session, article
) -> None:
    from app.project_state.db_state import topic_policy

    state = topic_policy(session.connection())
    assert state["normal_topic_tag"] == "AI Threads" and state["growth_post_excluded"] is True
    assert state["fail_closed"] is True and state["alters_body_hash_or_character_count"] is False
    assert state["production_acceptance"] == "pending_canary"
    rejected = _proposal(session, article, seed="k")
    _publisher(session, FakeMeta(reject_topic=True)).publish(
        proposal_id=rejected.id, execute=True, now=_NOW
    )
    state = topic_policy(session.connection())
    assert state["tagged_containers_rejected"] == 1
    assert state["production_acceptance"] == "pending_canary"  # 断られた作成は証拠にならない
    session.query(ThreadsPublication).update({"reconciliation_required": False})
    session.commit()
    accepted = _proposal(session, article, seed="m")
    _publisher(session, FakeMeta(media_id="media-9")).publish(
        proposal_id=accepted.id, execute=True, now=_NOW + timedelta(hours=3)
    )
    state = topic_policy(session.connection())
    assert state["tagged_containers_accepted"] == 1 and state["production_acceptance"] == "observed"
