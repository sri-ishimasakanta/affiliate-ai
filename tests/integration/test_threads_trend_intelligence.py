"""T6.5A-B: 自分の投稿の特徴・外の観察の保存・傾向の記述・CLI、と偽の E2E (A〜E)。

すべて手元の DB (メモリの SQLite) と偽のページだけ。Threads・OpenAI・WordPress には触れない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    PUB_FAILED,
    PUB_PUBLISHED,
    TP_APPROVED,
    Article,
    ThreadsExternalObservation,
    ThreadsExternalPost,
    ThreadsInsightSnapshot,
    ThreadsObserverRun,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
    ThreadsTrendingTopic,
)
from app.models.threads_observer import RUN_DOM_UNRECOGNIZED, RUN_LOGIN_REQUIRED, RUN_SUCCEEDED
from app.services.threads_feature_store import own_baselines, own_feature_rows
from app.services.threads_observer_service import (
    EXTRACTION_COMPLETE,
    EXTRACTION_PARTIAL,
    record_run,
)
from app.services.threads_trend_analysis_service import build_report, observer_tables_present
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, collect
from app.social.threads.trends import BREAKOUT_CANDIDATE
from scripts import analyze_threads_trends, observe_threads
from tests.support.threads_observer_pages import (
    FakePage,
    card,
    drifted_page,
    login_page,
    page,
    trends_page,
)

T0 = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)


def _clock(start: datetime):
    moments = iter(start + timedelta(seconds=i) for i in range(1000))
    return lambda: next(moments)


# -- 自分の投稿 --------------------------------------------------------------------------


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(id=21, title="記事", slug="a", body="本文。", status="published",
                  published_url="https://bizfluxlab.com/a/", published_at=T0 - timedelta(days=9),
                  article_type="informational", monetization_mode="supporting",
                  wordpress_post_id="84")  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _own(session: Session, article: Article | None, seed: str, body: str, *,
         topic="unrecorded", status=PUB_PUBLISHED, metrics: dict | None = None,
         hook: str | None = "question") -> ThreadsPublication:  # fmt: skip
    guidance = {"generation_brief": {"conversation_hook": hook}} if hook else {}
    if article is None:
        guidance = {"content_kind": "account_growth"}
    proposal = ThreadsPostProposal(
        source_article_id=article.id if article else None,
        source_article_body_hash=compute_text_hash("x"),
        angle=f"angle-{seed}", link_mode="none", content_text=body, character_count=len(body),
        content_seed=(seed * 64)[:64], proposal_hash=(seed.upper() * 64)[:64],
        policy_version="t2.1", generator_version="g", status=TP_APPROVED,
        learning_guidance_json=guidance,
    )  # fmt: skip
    session.add(proposal)
    session.commit()
    pub = ThreadsPublication(
        proposal_id=proposal.id, proposal_hash=proposal.proposal_hash,
        source_article_id=proposal.source_article_id, angle=proposal.angle,
        exact_published_text=body, threads_media_id=f"m-{seed}", status=status,
        trigger="automatic", published_at=T0, remote_timestamp="2026-09-28T01:00:00+0000",
    )  # fmt: skip
    session.add(pub)
    session.commit()
    detail = {"creation_id": "c"} if topic == "unrecorded" else {"topic_tag": topic}
    session.add(ThreadsPublicationAttempt(threads_publication_id=pub.id, step="create_container",
                                          outcome="succeeded", detail_json=detail,
                                          started_at=T0, finished_at=T0))  # fmt: skip
    if metrics is not None:
        session.add(ThreadsInsightSnapshot(threads_publication_id=pub.id,
                                           threads_media_id=pub.threads_media_id,
                                           observed_at=T0 + timedelta(hours=5),
                                           outcome="observed", **metrics))  # fmt: skip
    session.commit()
    return pub


def test_a_own_post_features_are_separate_from_raw_metrics(
    session: Session, article: Article
) -> None:
    """E2E A: 自分の投稿の特徴 (本文から) と生の指標 (観測から) を分けて持つ。"""

    _own(session, article, "a", "AIの記事を読んだ感想。\n\nみなさんはどう思いますか？",
         topic="AI Threads", metrics={"views": 40, "likes": 2, "replies": 1})  # fmt: skip
    _own(session, article, "b", "昔の投稿。", metrics={"views": 10, "likes": 0})
    _own(session, None, "g", "今日の気づきを一つ。", topic="インサイト祭り", hook=None)
    _own(session, article, "f", "失敗した投稿。", status=PUB_FAILED)
    rows = own_feature_rows(session)
    assert [r["content_kind"] for r in rows] == ["article", "article", "account_growth"]
    first, legacy, growth = rows
    assert first["features"]["topic"] == "AI Threads"
    assert first["features"]["cta_class"] == "opinion_request"
    assert first["features"]["hour"] == 10  # JST
    assert first["metrics"]["views"] == 40
    assert "views" not in first["features"] and "likes" not in first["features"]
    assert first["conversation_hook"] == "question"
    # 記録の無い古い公開のトピックは推測しない。
    assert legacy["topic_recorded"] is False and legacy["features"]["topic"] is None
    # 指標が未観測なら None (0 にしない)。
    assert growth["metrics"] == {m: None for m in growth["metrics"]}
    assert growth["conversation_hook"] == "account_growth"
    baselines = own_baselines(rows)
    assert baselines["publications"] == 3
    assert baselines["publications_with_metrics"] == 2
    assert baselines["small_sample"] is True
    topics = baselines["dimensions"]["topic"]
    assert set(topics) == {"AI Threads", "インサイト祭り", "unknown"}
    assert topics["AI Threads"]["median"]["views"] == 40.0
    assert all(group["small_sample"] for group in topics.values())
    kinds = baselines["dimensions"]["content_kind"]
    assert kinds["account_growth"]["median"]["views"] is None
    assert any("not a cause" in note for note in baselines["limitations"])


def test_a_recorded_untagged_post_is_none_not_unknown(session: Session, article: Article) -> None:
    _own(session, article, "n", "トピックなしで出した投稿。", topic=None)
    baselines = own_baselines(own_feature_rows(session))
    assert set(baselines["dimensions"]["topic"]) == {"none"}


# -- 外の観察 --------------------------------------------------------------------------


def test_b_breakout_against_the_author_median(session: Session) -> None:
    """E2E B: 過去の投稿 8 本 (いいねの中央値 9)、新しい投稿のいいね 71 → 7.89 倍 (伸びた候補)。"""

    history = [5, 6, 8, 9, 9, 10, 12, 14]
    usual = [card("alice", f"A{i}", f"いつもの投稿 {i}", likes=str(v), replies="1")
             for i, v in enumerate(history)]  # fmt: skip
    new = card("alice", "NEW", "伸びた投稿。\n\n・理由1\n・理由2", likes="71", replies="4")
    fake = FakePage({sel.account_url("alice"): page(*usual, new)})
    assert len(usual) + 1 <= sel.LIMITS["known_account"]
    result = collect(fake, CollectionPlan(known_accounts=("alice",)), clock=_clock(T0))
    record_run(session, result)
    report = build_report(session, now=T0)
    [candidate] = report["external"]["candidate_breakouts"]
    assert candidate["external_post_key"] == "threads:NEW"
    assert candidate["author_likes_baseline"]["median"] == 9.0
    assert candidate["author_likes_baseline"]["n"] == 8
    assert candidate["likes_breakout_ratio"] == 7.89
    assert candidate["likes_breakout"] == BREAKOUT_CANDIDATE
    assert report["external"]["high_likes"][0]["likes"] == 71
    patterns = report["external"]["patterns"]["structure_class"]
    assert patterns["candidate_breakouts"] == {"checklist_like": 1}
    assert report["read_only"] is True and report["fed_back_to_generation"] is False
    assert any("not a cause" in note for note in report["limitations"])
    # 過去の投稿は基準どおり (伸びた候補ではない)。
    assert report["external"]["authors_with_baseline"] == 1


def test_c_missing_metrics_are_stored_as_null(session: Session) -> None:
    """E2E C: 見えない指標は NULL。表示回数の列は無い。"""

    fake = FakePage({sel.for_you_url(): page(
        card("bob", "B1", "本文", likes="3", replies=None, reposts=None, shares=None),
        card("bob", "B2", "本文2", likes="4", replies="1", reposts="0", shares="0"),
    )})  # fmt: skip
    record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    observations = {o.post_id: o for o in session.scalars(select(ThreadsExternalObservation))}
    posts = {p.external_post_key: p.id for p in session.scalars(select(ThreadsExternalPost))}
    partial, complete = observations[posts["threads:B1"]], observations[posts["threads:B2"]]
    assert (partial.likes, partial.replies, partial.reposts, partial.shares) == (3, None, None,
                                                                                 None)  # fmt: skip
    # 再投稿・共有は画面に数があっても読まない (意味を確かめていない)。
    assert (complete.reposts, complete.shares) == (None, None)
    assert partial.quotes is None
    assert partial.extraction_status == EXTRACTION_PARTIAL
    assert complete.extraction_status == EXTRACTION_COMPLETE
    columns = {c["name"] for c in inspect(session.get_bind()).get_columns(
        "threads_external_observations")}  # fmt: skip
    assert not {"views", "impressions", "reach"} & columns


def test_d_repeated_observation_appends_snapshots(session: Session) -> None:
    """E2E D: 同じ投稿を 3 回見る → 観測が 3 行 (上書きしない)。最初の本文を残す。"""

    for i, (likes, body) in enumerate([("5", "最初の本文"), ("20", "最初の本文"),
                                       ("64", "書き換えた本文")]):  # fmt: skip
        fake = FakePage({sel.search_url("AI"): page(card("carol", "C1", body, likes=likes))})
        result = collect(fake, CollectionPlan(search_queries=("AI",)),
                         clock=_clock(T0 + timedelta(hours=i)))  # fmt: skip
        record_run(session, result)
    [post] = session.scalars(select(ThreadsExternalPost)).all()
    history = session.scalars(select(ThreadsExternalObservation).order_by(
        ThreadsExternalObservation.observed_at)).all()  # fmt: skip
    assert [o.likes for o in history] == [5, 20, 64]
    assert len({o.run_id for o in history}) == 3
    assert post.body_text == "最初の本文"
    assert post.first_seen_at < post.last_seen_at
    report = build_report(session, now=T0)
    [row] = [r for r in report["external"]["high_likes"]]
    assert row["snapshots"] == 3 and row["likes"] == 64
    assert report["external"]["posts_with_repeated_snapshots"] == 1
    assert report["external"]["observations"] == 3


def test_e_dom_drift_fails_closed_and_stores_no_posts(session: Session) -> None:
    """E2E E: 画面の形が違う → 実行の行だけ (理由つき)。投稿は保存しない。"""

    fake = FakePage({sel.for_you_url(): page(card("d", "D1", "本文")),
                     sel.search_url("x"): drifted_page()})  # fmt: skip
    result = collect(fake, CollectionPlan(for_you=True, search_queries=("x",)),
                     clock=_clock(T0))  # fmt: skip
    run = record_run(session, result)
    assert run.status == RUN_DOM_UNRECOGNIZED
    assert sel.SELECTOR_VERSION in run.failure_reason
    assert run.items_collected == 0
    assert session.scalars(select(ThreadsExternalPost)).all() == []
    assert session.scalars(select(ThreadsExternalObservation)).all() == []


def test_login_required_is_recorded_without_posts(session: Session) -> None:
    fake = FakePage({sel.for_you_url(): login_page()})
    run = record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    assert run.status == RUN_LOGIN_REQUIRED
    assert session.scalars(select(ThreadsExternalPost)).all() == []


def test_run_row_and_trending_topics(session: Session) -> None:
    pages = {sel.trends_url(): trends_page("AI", "副業")}
    pages[sel.topic_url("AI")] = page(card("e", "E1", "AIの話", topic="AI"))
    pages[sel.topic_url("副業")] = page(card("f", "F1", "副業の話", topic="副業"),
                                       card("f", "F2", "副業の話2", topic="副業"))  # fmt: skip
    for hour in (0, 1):
        result = collect(FakePage(pages), CollectionPlan(trending=True, screenshots=False),
                         clock=_clock(T0 + timedelta(hours=hour)))  # fmt: skip
        run = record_run(session, result)
    assert run.status == RUN_SUCCEEDED
    assert run.collector_version == sel.COLLECTOR_VERSION
    assert run.selector_version == sel.SELECTOR_VERSION
    assert run.source_types_json == ["trending_topic"]
    assert run.item_limit == sel.LIMITS["run_total"]
    topics = {t.topic_name: t for t in session.scalars(select(ThreadsTrendingTopic))}
    assert topics["副業"].observations == 2 and topics["副業"].sample_post_count == 4
    report = build_report(session, now=T0)
    assert {t["topic"] for t in report["external"]["repeated_trending_topics"]} == {"AI", "副業"}
    assert report["external"]["runs_by_status"] == {"succeeded": 2}


def test_the_report_without_observer_tables(session: Session, article: Article) -> None:
    for table in ("threads_external_observations", "threads_trending_topics",
                  "threads_external_posts", "threads_observer_runs"):  # fmt: skip
        session.execute(text(f"DROP TABLE {table}"))
    session.commit()
    assert observer_tables_present(session) is False
    report = build_report(session, now=T0)
    assert report["external"]["available"] is False
    assert "2cfa0ccb2059" in report["external"]["reason"]


def test_observation_tables_are_separate_from_publication_tables(session: Session) -> None:
    fake = FakePage({sel.for_you_url(): page(card("g", "G1", "本文"))})
    record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    assert session.scalars(select(ThreadsPublication)).all() == []
    assert session.scalars(select(ThreadsPostProposal)).all() == []
    assert len(session.scalars(select(ThreadsObserverRun)).all()) == 1


# -- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _page_factory(fake: FakePage):
    def build(**kwargs):
        build.kwargs = kwargs
        return fake

    return build


def test_observe_cli_stores_one_run(factory, capsys) -> None:
    fake = FakePage({sel.for_you_url(): page(card("h", "H1", "本文"))})
    code = observe_threads.main(["--for-you"], session_factory=factory,
                                page_factory=_page_factory(fake), now=T0)  # fmt: skip
    out = capsys.readouterr().out
    assert code == observe_threads.EXIT_OK
    summary = json.loads(out)
    assert summary["stored"] is True and summary["items_collected"] == 1
    assert fake.closed is True
    with factory() as session:
        assert len(session.scalars(select(ThreadsExternalObservation)).all()) == 1


def test_observe_cli_dry_run_stores_nothing(factory, capsys) -> None:
    fake = FakePage({sel.for_you_url(): page(card("h", "H1", "本文"))})
    code = observe_threads.main(["--for-you", "--dry-run"], session_factory=factory,
                                page_factory=_page_factory(fake), now=T0)  # fmt: skip
    assert code == observe_threads.EXIT_OK
    assert json.loads(capsys.readouterr().out)["stored"] is False
    with factory() as session:
        assert session.scalars(select(ThreadsObserverRun)).all() == []


def test_observe_cli_login_required_exit(factory, capsys) -> None:
    fake = FakePage({sel.for_you_url(): login_page()})
    code = observe_threads.main(["--for-you"], session_factory=factory,
                                page_factory=_page_factory(fake), now=T0)  # fmt: skip
    assert code == observe_threads.EXIT_LOGIN_REQUIRED
    assert "--login" in capsys.readouterr().out


def test_observe_cli_needs_a_source_and_cannot_raise_limits(factory, capsys) -> None:
    fake = FakePage({}, default=page(*[card("i", f"I{n}", f"本文{n}") for n in range(30)]))
    with pytest.raises(SystemExit):
        observe_threads.main([], session_factory=factory, page_factory=_page_factory(fake))
    code = observe_threads.main(["--for-you", "--limit-total", "999", "--dry-run"],
                                session_factory=factory, page_factory=_page_factory(fake),
                                now=T0)  # fmt: skip
    assert code == observe_threads.EXIT_OK
    assert json.loads(capsys.readouterr().out)["items_collected"] == sel.LIMITS["for_you"]
    observe_threads.main(["--for-you", "--limit-total", "3", "--dry-run"],
                         session_factory=factory, page_factory=_page_factory(fake),
                         now=T0)  # fmt: skip
    assert json.loads(capsys.readouterr().out)["items_collected"] == 3


def test_observe_cli_stops_before_opening_when_tables_are_missing(factory, capsys) -> None:
    with factory() as session:
        session.execute(text("DROP TABLE threads_external_observations"))
        session.commit()
    build = _page_factory(FakePage({}))
    code = observe_threads.main(["--for-you"], session_factory=factory, page_factory=build)
    assert code == observe_threads.EXIT_UNAVAILABLE
    assert not hasattr(build, "kwargs")  # ブラウザを開いていない
    assert "2cfa0ccb2059" in capsys.readouterr().out


def test_observe_cli_uses_the_dedicated_profile_and_headless_by_default(factory, capsys) -> None:
    build = _page_factory(FakePage({sel.for_you_url(): page(card("j", "J1", "本文"))}))
    observe_threads.main(["--for-you", "--dry-run"], page_factory=build, now=T0)
    assert str(build.kwargs["profile_dir"]).replace("\\", "/") == (
        "data/threads-observer/browser-profile"
    )
    assert build.kwargs["headless"] is True


def test_analyze_cli_is_read_only(factory, capsys) -> None:
    fake = FakePage({sel.for_you_url(): page(card("k", "K1", "本文", likes="2"))})
    with factory() as session:
        record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
        before = len(session.scalars(select(ThreadsExternalObservation)).all())
    assert analyze_threads_trends.main([], session_factory=factory, now=T0) == 0
    text_out = capsys.readouterr().out
    assert "外の観察" in text_out and "自分の投稿" in text_out
    assert analyze_threads_trends.main(["--json"], session_factory=factory, now=T0) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["external"]["posts"] == 1
    with factory() as session:
        assert len(session.scalars(select(ThreadsExternalObservation)).all()) == before


# -- 追加の性質 ------------------------------------------------------------------------


def test_own_link_and_growth_separation_and_extremes(session: Session, article: Article) -> None:
    for i in range(5):
        _own(session, article, f"l{i}", f"記事の紹介 {i}。\nhttps://bizfluxlab.com/a/",
             topic="AI Threads", metrics={"views": 10 + i, "likes": 0, "replies": 0,
                                          "reposts": 0, "quotes": 0, "shares": 0})  # fmt: skip
    for i in range(5):
        _own(session, article, f"n{i}", f"リンクなしの投稿 {i}。", topic="AI Threads",
             metrics={"views": 30 + i, "likes": 1, "replies": 1, "reposts": 0, "quotes": 0,
                      "shares": 0})  # fmt: skip
    _own(session, None, "gr", "Growth の投稿。", topic="インサイト祭り", hook=None,
         metrics={"views": 99, "likes": 9})  # fmt: skip
    rows = own_feature_rows(session)
    assert rows[0]["derived"]["engagement"] == 0
    assert rows[-1]["derived"]["engagement"] is None  # 指標の一部が欠けていれば作らない
    assert rows[0]["published_at_jst"].endswith("+09:00")
    assert rows[0]["source_article_id"] == article.id and rows[-1]["source_article_id"] is None
    baselines = own_baselines(rows)
    links = baselines["dimensions"]["has_url"]
    assert (links["True"]["n"], links["False"]["n"]) == (5, 6)
    assert links["True"]["median"]["views"] == 12.0
    kinds = baselines["dimensions"]["content_kind"]
    assert kinds["article"]["n"] == 10 and kinds["account_growth"]["n"] == 1
    assert kinds["account_growth"]["small_sample"] is True
    extremes = baselines["observed_extremes"]["has_url"]["views"]
    assert extremes["observed higher median"]["group"] == "False"
    assert extremes["observed lower median"]["group"] == "True"
    # Growth は 1 本だけ → 種類の比較は insufficient sample。
    assert baselines["observed_extremes"]["content_kind"]["views"]["status"] == (
        "insufficient sample"
    )


def test_own_and_external_features_share_one_schema(session: Session, article: Article) -> None:
    _own(session, article, "s", "同じ本文？", topic="AI Threads")
    fake = FakePage({sel.for_you_url(): page(card("x", "X1", "同じ本文？"))})
    record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    own = own_feature_rows(session)[0]["features"]
    external = session.scalars(select(ThreadsExternalPost)).one().features_json
    assert set(own) == set(external)
    for key in ("feature_version", "cta_class", "structure_class", "char_bucket",
                "body_length", "has_question", "has_url", "source"):  # fmt: skip
        assert own[key] == external[key], key


def test_no_hidden_views_are_reported_for_external_posts(session: Session) -> None:
    fake = FakePage({sel.for_you_url(): page(card("v", "V1", "本文", likes="5"))})
    record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    report = build_report(session, now=T0)
    dumped = json.dumps(report["external"], ensure_ascii=False)
    assert '"views"' not in dumped and "impressions" not in dumped
    assert any("never estimated" in note for note in report["limitations"])


def test_the_output_does_not_use_winner_words(session: Session, article: Article,
                                              capsys) -> None:  # fmt: skip
    for i in range(6):
        _own(session, article, f"w{i}", f"投稿 {i}。", metrics={"views": i, "likes": i})
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    analyze_threads_trends.main([], session_factory=factory, now=T0)
    analyze_threads_trends.main(["--json"], session_factory=factory, now=T0)
    out = capsys.readouterr().out.lower()
    for word in ("best", "winning", "winner", "guaranteed", "最強", "必ず", "勝ち"):
        assert word not in out, word


class _StrictPage(FakePage):
    """読む以外の操作に触れたら落ちる。"""

    ALLOWED = {"goto", "scroll", "content", "screenshot", "close", "pages", "default",
               "visited", "scrolls", "screenshots", "closed", "_url", "_index"}  # fmt: skip

    def __getattribute__(self, name):
        if not name.startswith("__") and name not in _StrictPage.ALLOWED:
            raise AssertionError(f"the collector touched {name!r}")
        return super().__getattribute__(name)


def test_the_collector_only_reads(session: Session) -> None:
    pages = {sel.for_you_url(): page(card("r", "R1", "本文")),
             sel.trends_url(): trends_page("AI"),
             sel.topic_url("AI"): page(card("r", "R2", "本文2")),
             sel.search_url("q"): page(card("r", "R3", "本文3")),
             sel.custom_feed_url("f1"): page(card("r", "R4", "本文4")),
             sel.account_url("r"): page(card("r", "R5", "本文5"))}  # fmt: skip
    fake = _StrictPage(pages)
    result = collect(fake, CollectionPlan(for_you=True, trending=True, search_queries=("q",),
                                          custom_feeds=("f1",), known_accounts=("r",)),
                     clock=_clock(T0))  # fmt: skip
    assert result.status == RUN_SUCCEEDED
    assert len(result.posts) == 5
    assert all(url.startswith(sel.BASE_URL) for url in fake.visited)
    assert not any(word in url for url in fake.visited
                   for word in ("/intent", "/compose", "/login"))  # fmt: skip


def test_the_browser_profile_and_screenshots_are_excluded_from_git() -> None:
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    paths = ["data/threads-observer/browser-profile/Default/Cookies",
             "artifacts/threads-observer/2026-09-28/010000/for_you.png"]  # fmt: skip
    out = subprocess.run(["git", "check-ignore", *paths], cwd=root, capture_output=True,
                         text=True, check=False)  # fmt: skip
    assert out.stdout.split() == paths


def test_project_state_reports_the_observer_facts(session: Session) -> None:
    from app.project_state.db_state import observer_state
    from app.project_state.local_state import PENDING_PRODUCTION_MIGRATIONS

    assert "2cfa0ccb2059" not in PENDING_PRODUCTION_MIGRATIONS  # 2026-09-28 に本番へ適用済み
    fake = FakePage({sel.for_you_url(): page(card("p", "P1", "本文"))})
    record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    state = observer_state(session.connection())
    assert state["schema_ready"] is True
    assert state["runs_by_status"] == {"succeeded": 1}
    assert (state["posts"], state["observations"]) == (1, 1)
    assert state["read_only"] is True and state["social_actions"] is False
    assert state["scheduled"] is False and state["fed_back_to_generation"] is False
    assert state["selector_verified"] is True
    assert state["selector_version"] == "threads-web-verified-2026-09-28-v1"
    session.execute(text("DROP TABLE threads_external_observations"))
    session.commit()
    missing = observer_state(session.connection())
    assert missing["schema_ready"] is False and "posts" not in missing
