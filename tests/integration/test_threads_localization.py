"""T6.4: 人に見せる Threads の画面・メール・報告を日本語にする (表示だけ。振る舞いは変えない)。

内部の値 (状態・理由の ID・enum) は英語のまま。表示層 (``labels_ja``) が日本語にする。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.approval.review_snapshot import build_snapshot
from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    PUB_PUBLISHED,
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    Article,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.operations.report_format import (
    approval_digest_subject,
    render_approval_digest_html,
    render_approval_digest_text,
    render_threads_daily_summary,
    render_weekly_report,
)
from app.services.threads_approval_digest_service import _display, _kind_lines, _topic_text
from app.services.threads_report_service import ThreadsReportService
from app.social.threads import labels_ja as ja
from app.social.threads.conversation import conversation_errors
from app.social.threads.growth import GrowthBrief, profile_hash
from app.social.threads.growth import validate as growth_validate
from app.social.threads.quality import quality_findings

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=JST).astimezone(UTC)
TOKEN = "THAAAsecret-token-must-never-appear"
ARTICLE_TEXT = (
    "ChatGPTの法人プランでありがちな間違いは、1人分だけでBusinessの見積もりを出すこと。"
    "Businessは2ユーザーから利用できます。導入のとき、最初に何人で試しましたか？"
)
GROWTH_TEXT = (
    "AIを使って、メディア運営をどこまで自動化できるかを、実際に作りながら検証・記録しています。"
    "まずはフォロワー100人を目標にしています。フォローいただけたら、こちらからもフォローします🙂"
)
WARNINGS = [
    "1 sentence(s) exceed 60 characters; prefer shorter ones",
    "the experience hook does not clearly ask about the reader's own use",
]


@pytest.fixture
def article(session) -> Article:
    row = Article(
        id=23, title="ChatGPT 法人プランの選び方", slug="chatgpt-business", body="記事本文。",
        status="published", published_url="https://bizfluxlab.com/chatgpt-business/",
        published_at=NOW - timedelta(days=5), article_type="informational",
        monetization_mode="supporting", wordpress_post_id="90",
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _article_proposal(session, article, *, status=TP_AWAITING_APPROVAL, warnings=None):
    row = ThreadsPostProposal(
        source_article_id=article.id, source_article_body_hash=compute_text_hash(article.body),
        angle="common_mistake", link_mode="none", content_text=ARTICLE_TEXT,
        character_count=len(ARTICLE_TEXT), content_seed="a" * 64, proposal_hash="b" * 64,
        policy_version="t2.1", generator_version="threads-proposal-1", status=status,
        warnings_json=list(warnings if warnings is not None else WARNINGS),
        learning_guidance_json={"generation_brief": {"conversation_hook": "experience",
                                                     "brief_version": "t6.3"}},
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _growth_proposal(session, *, status=TP_AWAITING_APPROVAL):
    row = ThreadsPostProposal(
        source_article_id=None, source_article_body_hash=profile_hash(), angle="account_growth",
        link_mode="none", content_text=GROWTH_TEXT, character_count=len(GROWTH_TEXT),
        content_seed="g" * 64, proposal_hash="h" * 64, policy_version="t6.3.3",
        generator_version="threads-growth-1", status=status, warnings_json=[],
        learning_guidance_json={"content_kind": "account_growth", "growth": {
            "date_jst": "2026-09-28", "angle": "account_identity", "follower_target": 100}},
    )  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _digest_item(row, url="https://bizfluxlab.com/bfl-approval/s1#cap"):
    return {
        "proposal_id": row.id,
        "article_title": "ChatGPT 法人プランの選び方" if row.source_article_id else "Growth Post",
        "angle": row.angle, "preview": row.content_text[:80], "timing": None,
        "topic": _topic_text(row), **_kind_lines(row), **_display(row), "review_url": url,
    }  # fmt: skip


# --- labels ----------------------------------------------------------------------------------


def test_status_labels_keep_internal_values() -> None:
    assert ja.status_label("awaiting_approval") == "承認待ち"
    assert ja.status_label("approved") == "承認済み"
    assert ja.status_label("rejected") == "却下"
    assert ja.status_label("published") == "公開済み"
    assert ja.status_label("failed") == "失敗"
    assert ja.status_label("blocked") == "保留"
    assert ja.status_label("expired") == "期限切れ"
    assert ja.status_label("uncertain") == "結果確認中"
    assert ja.status_label("needs_human_check", raw=True) == "要確認（needs_human_check）"
    assert ja.status_label("mystery") == "不明な状態（mystery）"


def test_kind_hook_angle_link_topic_and_metric_labels() -> None:
    assert ja.kind_label("article") == "通常記事"
    assert ja.kind_label("account_growth") == "Growth Post"
    assert ja.hook_label("experience") == "経験（experience）"
    assert [ja.HOOK_LABELS[h] for h in ("question", "choice", "opinion", "none")] == [
        "質問", "選択", "意見", "なし"]  # fmt: skip
    assert ja.angle_label("common_mistake") == "よくある間違い（common_mistake）"
    assert [ja.GROWTH_ANGLE_LABELS[a] for a in ("account_identity", "goal_progress",
            "build_in_public", "community", "mutual_growth")] == [
        "アカウント紹介", "目標・進捗", "制作過程", "交流", "相互成長"]  # fmt: skip
    assert ja.angle_label("account_growth", growth_angle="community") == "交流"
    assert (ja.link_label("none"), ja.link_label("article")) == ("なし", "元記事へのリンクあり")
    assert (ja.topic_label("AI Threads"), ja.topic_label(None)) == ("AI Threads", "なし")
    assert [ja.metric_label(m) for m in ("views", "likes", "replies", "reposts", "quotes",
                                          "shares", "followers_count")] == [
        "表示数", "いいね", "返信", "再投稿", "引用", "シェア", "フォロワー数"]  # fmt: skip


def test_jst_rendering_does_not_change_stored_values() -> None:
    assert ja.format_jst(datetime(2026, 9, 28, 4, 55, 39)) == "2026-09-28 13:55 JST"  # naive=UTC
    assert ja.format_jst("2026-09-28T04:55:39+00:00", seconds=True) == "2026-09-28 13:55:39 JST"
    assert ja.format_jst(None) == "-"


# --- warnings --------------------------------------------------------------------------------


def test_the_two_production_warnings_are_japanese_with_numbers() -> None:
    first, second = ja.localize_warnings(WARNINGS)
    assert (first.reason_id, first.label) == ("sentence_too_long", "長い文があります")
    assert first.detail == "60文字を超える文が1文あります。短くすると読みやすくなります。"
    assert first.raw == WARNINGS[0]  # 元の文 (内部) は残る
    assert second.reason_id == "hook_experience_weak"
    assert second.label == "会話フックを確認してください"
    assert "「経験」型ですが" in second.detail


def test_every_generator_template_is_in_the_catalog() -> None:
    """生成のコードが実際に書く警告は、すべて ID に対応づく (定型が変わったら落ちる)。"""

    produced: list[str] = []
    _soft, warns = quality_findings(
        "Notion AIとClickUpとKrispとFireflies.aiの料金・機能・導入・サポート・セキュリティの違い。"
        "2026年9月時点で月$8、$10、$18、2ユーザー、7日間、3人、5件、12時間。"
        "あなたはどう使っていますか？" * 3, "experience")  # fmt: skip
    produced += warns + [f"quality: {s}" for s in _soft]
    produced += quality_findings("料金を比べる。どちらを優先しますか？", "opinion")[1]
    produced += [f"quality: {s}" for s in quality_findings("AとBのどちらですか？", "question")[0]]
    produced += [f"quality: {s}" for s in quality_findings("一つだけ。", "choice")[0]]
    produced += conversation_errors("まとめです。どう思いますか？", "none")[1]
    produced += conversation_errors("皆さんはどう思いますか？", "question")[1]
    brief = GrowthBrief(day=datetime(2026, 9, 28).date(), angle="community", follower_target=100)
    produced += growth_validate(GROWTH_TEXT + "あ" * 150, brief)["warnings"]
    produced += _style_warnings()
    assert produced, "the generators produced no warnings"
    unknown = [w for w in produced if ja.localize_warning(w).reason_id == "unknown"]
    assert unknown == []


def _style_warnings() -> list[str]:
    from app.social.threads.policy import load_policy
    from app.social.threads.validators import ValidationResult, _check_style

    policy = load_policy()
    result = ValidationResult()
    text = ("まず導入の目的を明確にし、次に比較の観点を整理し、最後に費用対効果を見極めることが"
            "重要です。" + "確認します。" * 6 + "どう？" * 3 + "🙂" * 5)  # fmt: skip
    _check_style(text, policy, result)
    return list(result.warnings)


def test_an_unknown_warning_is_kept_as_a_system_warning() -> None:
    message = ja.localize_warning("brand new internal check failed: x=3")
    assert message.reason_id == "unknown" and message.label == "システム警告"
    assert "brand new internal check failed: x=3" in message.detail  # 黙って落とさない


# --- review page / snapshot ------------------------------------------------------------------


def test_case_a_article_review_snapshot(session, article) -> None:
    row = _article_proposal(session, article)
    before = (row.content_text, row.proposal_hash, row.character_count)
    snap = build_snapshot(subject_type="threads_post", subject=row, article=article)
    assert snap["post_kind_label"] == "通常記事" and snap["status_label"] == "承認待ち"
    assert snap["source_article_label"] == "ChatGPT 法人プランの選び方"
    assert snap["angle"] == "よくある間違い（common_mistake）"
    assert snap["angle_raw"] == "common_mistake"
    assert snap["hook_label"] == "経験（experience）" and snap["topic_label"] == "AI Threads"
    assert snap["link_mode"] == "なし" and snap["link_mode_raw"] == "none"
    assert snap["warnings"][0] == (
        "長い文があります: 60文字を超える文が1文あります。短くすると読みやすくなります。"
    )
    assert snap["warnings_raw"] == WARNINGS
    assert snap["publish_text"] == ARTICLE_TEXT  # 本文は変えない
    session.refresh(row)
    assert (row.content_text, row.proposal_hash, row.character_count) == before


def test_case_b_growth_review_snapshot(session) -> None:
    row = _growth_proposal(session)
    snap = build_snapshot(subject_type="threads_post", subject=row, article=None)
    assert snap["post_kind_label"] == "Growth Post" and snap["goal_label"] == "フォロワー100人"
    assert snap["topic_label"] == "インサイト祭り" and snap["link_mode"] == "なし"
    assert snap["source_article_label"] == "なし（Growth Post）"
    assert snap["angle"] == "アカウント紹介" and snap["publish_text"] == GROWTH_TEXT


# --- approval email ---------------------------------------------------------------------------


def test_the_approval_digest_is_japanese(session, article) -> None:
    items = [
        _digest_item(_article_proposal(session, article)),
        _digest_item(_growth_proposal(session)),
    ]
    assert approval_digest_subject(2) == "【Threads承認】投稿案2件の確認をお願いします"
    text = render_approval_digest_text(items=items, expires_at_local="2026-09-29 14:00 JST")
    for fragment in ("[1] 投稿案 #", "投稿種別: 通常記事", "状態: 承認待ち",
                     "元記事: ChatGPT 法人プランの選び方",
                     "切り口: よくある間違い（common_mistake）", "会話フック: 経験（experience）",
                     "リンク: なし", "トピック: AI Threads", "文字数: ", "本文:", "注意:",
                     "⚠ 長い文があります", "投稿種別: Growth Post", "目標: フォロワー100人",
                     "トピック: インサイト祭り", "元記事: なし（Growth Post）",
                     "切り口: アカウント紹介"):  # fmt: skip
        assert fragment in text, fragment
    assert ARTICLE_TEXT in text.replace("      ", "") and "exceed 60" not in text
    html = render_approval_digest_html(items=items, expires_at_local="-")
    assert "投稿種別: Growth Post" in html and "⚠ 長い文があります" in html


def test_the_digest_html_escapes_values(session, article) -> None:
    item = _digest_item(_article_proposal(session, article))
    item["body"] = '<script>alert("x")</script>'
    html = render_approval_digest_html(items=[item], expires_at_local="-")
    assert "<script>" not in html and "&lt;script&gt;" in html


# --- failures --------------------------------------------------------------------------------


def test_case_d_failure_explanations() -> None:
    topic = ja.explain_publication_failure(status="failed", error_category="threads_response",
                                           api_code="100", http_status=400, publication_id=21,
                                           topic_rejected=True)  # fmt: skip
    assert topic.lines() == [
        "Threads 側で Topic が受け付けられませんでした。", "この投稿は公開されていません。",
        "自動的に Topic なしで再投稿はしません。", "確認が必要です（次の公開は止まっています。）",
    ]  # fmt: skip
    assert topic.technical == {"publication_id": 21, "status": "failed",
                               "error_category": "threads_response", "api_code": "100",
                               "http_status": 400}  # fmt: skip
    uncertain = ja.explain_publication_failure(status="uncertain", growth=True, publication_id=20)
    assert "安全のため自動再送は停止しています。" in uncertain.lines()
    assert "Growth Post だけを止め、通常投稿は続けます。" in uncertain.action


def test_the_failure_alert_is_not_labelled_as_a_preflight_failure() -> None:
    from app.operations.threads_health import build_autopublish_failure_draft

    draft = build_autopublish_failure_draft(
        {"category": "threads_response", "reason": "Invalid parameter: topic_tag", "status": 400,
         "api_code": "100"}, publication_id=21)  # fmt: skip
    assert draft.title == "Threads 側で Topic が受け付けられませんでした"
    assert "事前確認" not in draft.title
    assert "自動的に Topic なしで再投稿はしません。" in draft.summary
    assert TOKEN not in json.dumps(draft.evidence)


def test_blocker_labels() -> None:
    assert ja.blocker_label("gap_not_elapsed").startswith("次の通常投稿まで待機中")
    assert ja.blocker_label("outside_publication_window").startswith("現在は公開時間外")
    assert ja.blocker_label("not_approved") == "あなたの承認を待っています"
    assert ja.blocker_label("growth_daily_limit") == "本日の Growth Post はすでに公開済みです"
    assert ja.blocker_label("expired") == "公開期限を過ぎました"
    assert ja.blocker_label("uncertain_publication").startswith("公開結果を確認できないため")
    assert ja.blocker_label("weird") == "その他の理由（weird）"


def test_the_alert_email_body_is_japanese_with_jst() -> None:
    from app.operations.email import render_alert_body
    from app.operations.notifications import NotificationMessage

    body = render_alert_body(NotificationMessage(
        severity="error", title="Threads への公開に失敗しました",
        summary="この投稿は公開されていません。",
        alert_type="AUTOMATION_HEALTH", fingerprint="threads_autopublish_failed:x",
        operations_run_id=None, occurred_at=datetime(2026, 9, 28, 4, 55, tzinfo=UTC),
        evidence={"publication_id": 21},
    ))  # fmt: skip
    assert "重要度          : エラー（error）" in body
    assert "発生            : 2026-09-28 13:55 JST" in body
    assert "技術的な詳細 (evidence):" in body


# --- reports ---------------------------------------------------------------------------------


def _published(session, proposal, *, at, views):
    pub = ThreadsPublication(
        proposal_id=proposal.id, proposal_hash=proposal.proposal_hash,
        source_article_id=proposal.source_article_id, angle=proposal.angle,
        exact_published_text=proposal.content_text, status=PUB_PUBLISHED, published_at=at,
        threads_media_id=f"m{proposal.id}", trigger="automatic",
    )  # fmt: skip
    session.add(pub)
    session.commit()
    session.add(ThreadsInsightSnapshot(
        threads_publication_id=pub.id, threads_media_id=pub.threads_media_id, observed_at=NOW,
        views=views, likes=1, replies=0, reposts=0, quotes=0, shares=0, outcome="observed",
    ))  # fmt: skip
    session.commit()
    return pub


def test_case_e_daily_and_weekly_reports(session, article, tmp_path) -> None:
    art = _article_proposal(session, article, status=TP_APPROVED)
    growth = _growth_proposal(session, status=TP_APPROVED)
    _published(session, art, at=NOW - timedelta(hours=3), views=120)
    _published(session, growth, at=NOW - timedelta(hours=1), views=15)
    service = ThreadsReportService(session, timezone=JST, growth_directory=tmp_path)
    day = NOW.astimezone(JST).date()
    daily = service.summary(start=day, end=day, now=NOW)
    assert (daily["article_posts"], daily["growth_posts"], daily["total_posts"]) == (1, 1, 2)
    text = render_threads_daily_summary(daily)
    for fragment in ("今日の Threads 運用", "通常投稿      : 1件", "Growth Post   : 1件",
                     "合計          : 2件", "表示数", "135", "いいね", "フォロワーの目標: 100人",
                     "フォロワー数  : 取得できませんでした", "対応が必要:", "  なし",
                     "Growth Post  提案 #"):  # fmt: skip
        assert fragment in text, fragment
    from app.services.operations_weekly_report_service import WeeklyReport

    weekly = WeeklyReport(generated_at=NOW.isoformat(), reporting_timezone="Asia/Tokyo",
                          period_start="2026-09-22", period_end="2026-09-28", complete=True,
                          threads=service.summary(start=day - timedelta(days=6), end=day,
                                                  now=NOW))  # fmt: skip
    body = render_weekly_report(weekly)
    for fragment in ("BizFluxLab 週次運用レポート", "Threads (THREADS)", "通常投稿      : 1件",
                     "今週公開した投稿の最新の指標", "来週も自動で続くこと", "システム (SYSTEM)",
                     "アラート (ALERTS)"):  # fmt: skip
        assert fragment in body, fragment
    assert TOKEN not in body and TOKEN not in text


def test_reports_surface_items_that_need_the_human(session, article, tmp_path) -> None:
    _article_proposal(session, article)  # 承認待ち
    service = ThreadsReportService(session, timezone=JST, growth_directory=tmp_path)
    day = NOW.astimezone(JST).date()
    text = render_threads_daily_summary(service.summary(start=day, end=day, now=NOW))
    assert "承認待ちの投稿案が 1 件あります" in text


# --- follower read reason ------------------------------------------------------------------


def test_the_follower_read_reason_is_recorded_without_extra_calls(session, tmp_path) -> None:
    from app.services.threads_growth_service import ThreadsGrowthService
    from app.social.threads.errors import ThreadsPermissionError
    from app.social.threads.models import ThreadsInsights
    from tests.integration.test_threads_growth_posts import BODY_A, BODY_C, GrowthLuna, _client

    class Threads:
        def __init__(self, behaviour):
            self.behaviour, self.reads = behaviour, 0

        def user_insights(self, metrics, **_kw):
            self.reads += 1
            if self.behaviour == "denied":
                raise ThreadsPermissionError("no permission", status=403, api_code="10")
            return ThreadsInsights(subject="user", values={}, missing=("followers_count",))

    for behaviour, outcome in (("denied", "permission_denied"), ("empty", "metric_unavailable")):
        threads = Threads(behaviour)
        fake = GrowthLuna([BODY_A if behaviour == "denied" else BODY_C])
        out = ThreadsGrowthService(session, timezone=JST, client=_client(fake),
                                   threads_service=threads, directory=tmp_path / behaviour,
                                   collect_followers=True).maintain(
            now=NOW + timedelta(days=0 if behaviour == "denied" else 1), execute=True)  # fmt: skip
        assert threads.reads == 1 and fake.calls == 1  # 1 回だけ読み、生成は目標だけで進む
        assert out["follower_read"]["outcome"] == outcome and out["created"]
        status = json.loads((tmp_path / behaviour / "status.json").read_text("utf-8"))
        assert status["follower_read"]["label"]
    off = ThreadsGrowthService(session, timezone=JST, client=None, directory=tmp_path / "off")
    assert off.plan(now=NOW)["due"] is False  # 呼ばない (すでに今日の分がある・client なし)
