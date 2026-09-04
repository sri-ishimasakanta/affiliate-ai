"""app/wordpress/publication_validator.py の pure テスト。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.article.draft_promotion_canonical import compute_text_hash
from app.wordpress.publication_validator import (
    validate_wordpress_publication_preview,
)
from app.wordpress.renderer import render_wordpress_html

_TOOLS = ["Make", "HubSpot", "ClickUp", "monday.com", "Pipedrive", "Reclaim.ai", "Todoist"]
_DOMAINS = {"www.make.com", "todoist.com"}

_TABLE = "| ツール | 料金 |\n|---|---|\n" + "".join(f"| {t} | 参照 |\n" for t in _TOOLS)
_BODY = (
    "本記事は広告（アフィリエイト）を含みます。\n\n"
    "## とは\n" + "解説。" * 300 + "\n\n"
    "## 選び方\n判断軸。\n\n"
    "## 比較\n" + " / ".join(_TOOLS) + " を比較。参照 https://www.make.com/ 。\n\n"
    f"{_TABLE}\n\n"
    "## 目的別\n用途別。\n\n"
    "## 注意点\n請求書払いは各社で異なり本記事では未確認です。\n\n"
    "## FAQ\nQ&A。\n\n"
    "## まとめ\n結論。\n\n"
    "### Make\nくわしく。\n\n### HubSpot\nくわしく。\n\n### ClickUp\nくわしく。\n\n"
    "### monday.com\nくわしく。\n\n### Pipedrive\nくわしく。\n\n"
    "### Reclaim.ai\nくわしく。\n\n### Todoist\nくわしく https://todoist.com/ 。\n"
)
_META = "業務効率化ツールのおすすめを目的別に比較して、選び方をわかりやすく解説する記事です。"


@dataclass
class _Promo:
    id: int = 1
    body_hash: str = field(default_factory=lambda: compute_text_hash(_BODY))
    meta_hash: str = field(default_factory=lambda: compute_text_hash(_META))
    validation_report: dict = field(default_factory=lambda: {"overall": "pass"})


def _run(**over):
    r = render_wordpress_html(over.get("body", _BODY))
    base = dict(
        article_status="review",
        article_title="タイトル",
        article_slug="gyomu-tool-roundup",
        article_body=_BODY,
        article_meta_description=_META,
        article_published_url=None,
        article_wordpress_post_id=None,
        article_published_at=None,
        promotion=_Promo(),
        rendered_html=r.html,
        rendered_h1_count=r.h1_count,
        rendered_h2_count=r.h2_count,
        rendered_h3_count=r.h3_count,
        rendered_table_count=r.table_count,
        rendered_external_links=r.external_links,
        expected_tool_names=_TOOLS,
        allowed_external_domains=_DOMAINS,
        affiliate_substitution_count=0,
        internal_link_substitution_count=0,
    )
    base.update({k: v for k, v in over.items() if k != "body"})
    return validate_wordpress_publication_preview(**base)


def _ids(rep, level):
    return {c["id"] for c in rep["checks"] if c["level"] == level}


def test_matching_promotion_passes() -> None:
    rep = _run()
    assert rep["overall"] == "pass"
    assert rep["publishable"] is True
    assert not _ids(rep, "fail")


def test_body_hash_mismatch_fails() -> None:
    rep = _run(promotion=_Promo(body_hash="0" * 64))
    assert rep["publishable"] is False
    assert "body_hash_matches_promotion" in _ids(rep, "fail")


def test_meta_hash_mismatch_fails() -> None:
    rep = _run(promotion=_Promo(meta_hash="0" * 64))
    assert "meta_hash_matches_promotion" in _ids(rep, "fail")


def test_no_promotion_fails() -> None:
    rep = _run(promotion=None)
    assert "promotion_exists" in _ids(rep, "fail")
    assert rep["publishable"] is False


def test_promotion_not_pass_fails() -> None:
    rep = _run(promotion=_Promo(validation_report={"overall": "warn"}))
    assert "promotion_validation_pass" in _ids(rep, "fail")


def test_wrong_status_fails() -> None:
    assert "article_status_review" in _ids(_run(article_status="approved"), "fail")


def test_existing_wordpress_post_id_fails() -> None:
    assert "wordpress_post_id_null" in _ids(_run(article_wordpress_post_id=123), "fail")


def test_existing_published_url_fails() -> None:
    assert "published_url_null" in _ids(
        _run(article_published_url="https://x/y"), "fail"
    )


def test_existing_published_at_fails() -> None:
    assert "published_at_null" in _ids(_run(article_published_at="2026-09-04"), "fail")


def test_h1_in_canonical_body_fails() -> None:
    body = "# 見出し1\n\n" + _BODY
    rep = _run(
        article_body=body,
        rendered_html=render_wordpress_html(body).html,
        rendered_h2_count=render_wordpress_html(body).h2_count,
        rendered_h3_count=render_wordpress_html(body).h3_count,
        rendered_table_count=render_wordpress_html(body).table_count,
    )
    assert "canonical_body_no_h1" in _ids(rep, "fail")


def test_missing_pr_disclosure_fails() -> None:
    body = _BODY.replace("本記事は広告（アフィリエイト）を含みます。", "はじめに。")
    assert "pr_disclosure_present" in _ids(_run(article_body=body), "fail")


def test_missing_tool_fails() -> None:
    body = _BODY.replace("Todoist", "XXX")
    assert "all_tools_present" in _ids(_run(article_body=body), "fail")


def test_placeholder_internal_link_fails() -> None:
    body = _BODY + "\n[関連](#)\n"
    assert "no_placeholder_internal_link" in _ids(_run(article_body=body), "fail")


def test_dangling_related_article_fails() -> None:
    body = _BODY + "\n関連記事：無料プラン比較（内部リンク）。\n"
    assert "no_dangling_related_article" in _ids(_run(article_body=body), "fail")


def test_economics_leakage_fails() -> None:
    body = _BODY + "\n提携報酬は35%です。\n"
    assert "commission_leakage" in _ids(_run(article_body=body), "fail")


def test_non_official_url_fails() -> None:
    body = _BODY + "\n参照 https://evil.example/x です。\n"
    r = render_wordpress_html(body)
    rep = _run(article_body=body, rendered_html=r.html,
               rendered_external_links=r.external_links)
    assert "external_links_official_domains" in _ids(rep, "fail")


def test_dangerous_rendered_html_fails() -> None:
    assert "rendered_html_safe" in _ids(
        _run(rendered_html="<p>ok</p><script>x</script>"), "fail"
    )


def test_affiliate_substitution_in_v1_fails() -> None:
    assert "affiliate_substitutions_zero_v1" in _ids(
        _run(affiliate_substitution_count=1), "fail"
    )


def test_suspicious_prose_question_mark_fails() -> None:
    body = _BODY.replace("結論。", "結論?672。")
    assert "no_suspicious_prose_question_mark" in _ids(_run(article_body=body), "fail")


def test_question_mark_in_url_query_does_not_fail() -> None:
    body = _BODY.replace("https://todoist.com/", "https://todoist.com/go?ref=abc&x=1")
    r = render_wordpress_html(body)
    rep = _run(
        article_body=body, rendered_html=r.html,
        rendered_external_links=r.external_links,
        allowed_external_domains={"www.make.com", "todoist.com"},
    )
    assert "no_suspicious_prose_question_mark" not in _ids(rep, "fail")


def test_fullwidth_question_mark_does_not_fail() -> None:
    body = _BODY.replace("結論。", "本当に？ 結論。")
    assert "no_suspicious_prose_question_mark" not in _ids(
        _run(article_body=body), "fail"
    )


def test_meta_too_long_fails() -> None:
    assert "meta_length_ok" in _ids(_run(article_meta_description="あ" * 161), "fail")
