"""WordPress dry-run preview の **pre-publication editorial/safety validation** (pure)。

DB / network 非依存。呼び出し側 (service) が Article / matching promotion / rendered
HTML / source run 由来のメタ (7 tool 名・許可ドメイン) を渡す。結果は Human review 用の
``validation_report`` であり、``publishable`` は「fail が 0 件」を意味する。
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlparse

from app.article.draft_output_validators import _commission_leakage_check
from app.article.draft_promotion_canonical import compute_text_hash

_META_MAX = 160
_PR_HEAD = 700
_PR_MARKERS = ("PR", "広告", "アフィリエイト", "プロモーション", "スポンサー")
_H1_LINE_RE = re.compile(r"^#\s", re.MULTILINE)
_H1_SETEXT_RE = re.compile(r"^\S.*\n=+\s*$", re.MULTILINE)
_URL_TOKEN_RE = re.compile(r"https?://[^\s<>\"'`)\]}（）「」『』【】、。，．；：・…　]+")
_DANGEROUS_HTML = (
    "<script",
    "<iframe",
    "<style",
    "<object",
    "<embed",
    "javascript:",
    "vbscript:",
)
_EVENT_ATTR_RE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
_PLACEHOLDER_LINK_RE = re.compile(r"\]\(#[^)]*\)|\(#\)|（#）")


def _check(cid: str, level: str, detail: str) -> dict:
    return {"id": cid, "level": level, "detail": detail}


def validate_wordpress_publication_preview(
    *,
    article_status: str,
    article_title: str,
    article_slug: str,
    article_body: str,
    article_meta_description: str,
    article_published_url: str | None,
    article_wordpress_post_id: int | None,
    article_published_at,
    promotion: object | None,
    rendered_html: str,
    rendered_h1_count: int,
    rendered_h2_count: int,
    rendered_h3_count: int,
    rendered_table_count: int,
    rendered_external_links: list[str],
    expected_tool_names: list[str],
    allowed_external_domains: set[str],
    affiliate_substitution_count: int,
    internal_link_substitution_count: int,
) -> dict:
    checks: list[dict] = []

    # -- state / identity ------------------------------------------------
    checks.append(
        _check(
            "article_status_review",
            "pass" if article_status == "review" else "fail",
            f"status={article_status!r}",
        )
    )
    if promotion is None:
        checks.append(
            _check("promotion_exists", "fail", "matching ArticleDraftPromotion なし")
        )
    else:
        checks.append(_check("promotion_exists", "pass", f"promotion #{promotion.id}"))
        report = getattr(promotion, "validation_report", None) or {}
        checks.append(
            _check(
                "promotion_validation_pass",
                "pass" if report.get("overall") == "pass" else "fail",
                f"promotion.validation_report.overall={report.get('overall')!r}",
            )
        )
        body_h = compute_text_hash(article_body)
        meta_h = compute_text_hash(article_meta_description)
        checks.append(
            _check(
                "body_hash_matches_promotion",
                "pass" if body_h == promotion.body_hash else "fail",
                "canonical body hash != promotion.body_hash"
                if body_h != promotion.body_hash
                else "ok",
            )
        )
        checks.append(
            _check(
                "meta_hash_matches_promotion",
                "pass" if meta_h == promotion.meta_hash else "fail",
                "canonical meta hash != promotion.meta_hash"
                if meta_h != promotion.meta_hash
                else "ok",
            )
        )

    checks.append(
        _check(
            "title_nonblank",
            "pass" if article_title and article_title.strip() else "fail",
            "",
        )
    )
    checks.append(
        _check(
            "slug_nonblank",
            "pass" if article_slug and article_slug.strip() else "fail",
            "",
        )
    )
    checks.append(
        _check(
            "published_url_null",
            "pass" if article_published_url is None else "fail",
            f"published_url={article_published_url!r}",
        )
    )
    checks.append(
        _check(
            "wordpress_post_id_null",
            "pass" if article_wordpress_post_id is None else "fail",
            f"wordpress_post_id={article_wordpress_post_id!r}",
        )
    )
    checks.append(
        _check(
            "published_at_null",
            "pass" if article_published_at is None else "fail",
            f"published_at={article_published_at!r}",
        )
    )

    # -- canonical body structure -------------------------------------
    canonical_h1 = len(_H1_LINE_RE.findall(article_body)) + len(
        _H1_SETEXT_RE.findall(article_body)
    )
    checks.append(
        _check(
            "canonical_body_no_h1",
            "pass" if canonical_h1 == 0 else "fail",
            f"canonical H1={canonical_h1}",
        )
    )
    checks.append(
        _check(
            "rendered_no_h1",
            "pass" if rendered_h1_count == 0 else "fail",
            f"rendered <h1>={rendered_h1_count}",
        )
    )
    checks.append(
        _check(
            "rendered_structure_h2_h3",
            "pass"
            if rendered_h2_count == 7 and rendered_h3_count == 7
            else "fail",
            f"rendered h2={rendered_h2_count} h3={rendered_h3_count} (期待 7/7)",
        )
    )
    checks.append(
        _check(
            "rendered_table_count_one",
            "pass" if rendered_table_count == 1 else "fail",
            f"rendered <table>={rendered_table_count} (期待 1)",
        )
    )

    # -- disclosure / meta ------------------------------------------
    head = article_body[:_PR_HEAD]
    checks.append(
        _check(
            "pr_disclosure_present",
            "pass" if any(m in head for m in _PR_MARKERS) else "fail",
            "冒頭に PR/広告表記あり" if any(m in head for m in _PR_MARKERS) else "なし",
        )
    )
    ml = len(article_meta_description)
    checks.append(
        _check(
            "meta_length_ok",
            "pass" if ml <= _META_MAX else "fail",
            f"meta 長さ {ml} (<= {_META_MAX})",
        )
    )

    # -- tools / links / placeholders -----------------------------
    missing = [t for t in expected_tool_names if t not in article_body]
    checks.append(
        _check(
            "all_tools_present",
            "pass" if not missing else "fail",
            f"本文に未登場: {missing}" if missing else "全ツール登場",
        )
    )
    checks.append(
        _check(
            "no_placeholder_internal_link",
            "pass" if not _PLACEHOLDER_LINK_RE.search(article_body) else "fail",
            "プレースホルダ内部リンクあり"
            if _PLACEHOLDER_LINK_RE.search(article_body)
            else "なし",
        )
    )
    checks.append(
        _check(
            "no_dangling_related_article",
            "pass" if "関連記事：" not in article_body else "fail",
            "『関連記事：』の宙吊り参照あり"
            if "関連記事：" in article_body
            else "なし",
        )
    )

    # -- commission / economics leakage ---------------------------
    leak = _commission_leakage_check(f"{article_meta_description}\n{article_body}")
    checks.extend(leak)

    # -- external links ------------------------------------------
    non_https = [u for u in rendered_external_links if not u.startswith("https://")]
    checks.append(
        _check(
            "external_links_all_https",
            "pass" if not non_https else "fail",
            f"non-HTTPS: {non_https}" if non_https else "all HTTPS",
        )
    )
    bad_domains = sorted(
        {
            urlparse(u).netloc
            for u in rendered_external_links
            if urlparse(u).netloc not in allowed_external_domains
        }
    )
    checks.append(
        _check(
            "external_links_official_domains",
            "pass" if not bad_domains else "fail",
            f"想定外ドメイン: {bad_domains}" if bad_domains else "all official",
        )
    )

    # -- V1 substitution invariants ----------------------------
    checks.append(
        _check(
            "affiliate_substitutions_zero_v1",
            "pass" if affiliate_substitution_count == 0 else "fail",
            f"affiliate substitutions={affiliate_substitution_count} (V1 期待 0)",
        )
    )
    checks.append(
        _check(
            "internal_link_substitutions_zero_v1",
            "pass" if internal_link_substitution_count == 0 else "fail",
            f"internal link substitutions={internal_link_substitution_count} (V1 期待 0)",
        )
    )

    # -- rendered HTML safety --------------------------------
    low = rendered_html.lower()
    hits = [tok for tok in _DANGEROUS_HTML if tok in low]
    if _EVENT_ATTR_RE.search(rendered_html):
        hits.append("on*=")
    checks.append(
        _check(
            "rendered_html_safe",
            "pass" if not hits else "fail",
            f"危険な HTML: {hits}" if hits else "危険な HTML なし",
        )
    )

    # -- suspicious prose "?" (URL/query は除外, 全角 ？ は許可) (§16) -----
    prose = _URL_TOKEN_RE.sub("", article_body)
    prose = unescape(prose)
    suspicious_q = prose.count("?")  # ASCII のみ; 全角 ？ (U+FF1F) は数えない
    checks.append(
        _check(
            "no_suspicious_prose_question_mark",
            "pass" if suspicious_q == 0 else "fail",
            f"prose 中の ASCII '?' = {suspicious_q}",
        )
    )

    has_fail = any(c["level"] == "fail" for c in checks)
    has_warn = any(c["level"] == "warn" for c in checks)
    overall = "fail" if has_fail else ("warn" if has_warn else "pass")
    return {
        "overall": overall,
        "publishable": not has_fail,
        "checks": checks,
    }
