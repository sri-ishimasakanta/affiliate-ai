"""DraftInputSnapshot builder / service テスト用の Article #1 相当シナリオ生成。

Article #1 の形をなぞる:
- status=planned / body=None の Article
- keyword ("...おすすめ" -> recommendation_roundup)
- N 個の active AffiliateProgram + N 本の link (先頭 = primary)
- 各 tool: 6 required fact を verified+fresh、任意で 1 件 unknown、残りは行なし
  (= not_researched) -> claim partition が 17 で成立
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import (
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    ArticleFact,
    Keyword,
    Source,
)
from app.models.enums import ArticleStatus

KEYWORD_TEXT = "widget tools おすすめ"

# (fact_key, value, is_list)
_REQUIRED_VERIFIED: tuple[tuple[str, object], ...] = (
    ("official_product_name", "Tool"),
    ("official_url", "https://example.com/"),
    ("primary_use_cases", ["use a", "use b"]),
    ("key_features", ["feat x", "feat y"]),
    ("pricing_summary", "free plan available; paid from $9"),
    ("free_plan_available", True),
)


@dataclass
class Scenario:
    article_id: int
    keyword_id: int
    program_ids: list[int]
    primary_program_id: int
    link_ids: list[int]
    source_ids: list[int]
    now: datetime


def _mk_source(session: Session, article_id: int, kind: str, url: str,
               checked_at: datetime) -> Source:
    s = Source(
        article_id=article_id, source_type=kind, source_url=url,
        title=f"{kind} page", checked_at=checked_at,
    )
    session.add(s)
    session.flush()
    return s


def build_scenario(
    session: Session,
    *,
    n_tools: int = 7,
    with_unknown: bool = True,
    now: datetime | None = None,
    article_status: str = ArticleStatus.PLANNED.value,
    article_body: str | None = None,
    suffix: str = "",
) -> Scenario:
    """同一 session で複数シナリオを作る場合は ``suffix`` を変えて一意にする。"""

    # デフォルトは実時刻。API 経由 (endpoint が datetime.now(UTC) を使う) でも
    # fact が fresh になるよう、checked_at は now より確実に過去へ置く。
    now = now or datetime.now(UTC)
    fresh = now - timedelta(days=1)
    keyword_text = f"{KEYWORD_TEXT}{(' ' + suffix) if suffix else ''}"
    slug_suffix = f"-{suffix}" if suffix else ""

    kw = Keyword(
        keyword=keyword_text, category="productivity", opportunity_score=68.81
    )
    session.add(kw)
    session.flush()

    art = Article(
        title="Widget Tools — 決定版比較",
        slug=f"widget-tools-roundup{slug_suffix}",
        keyword_id=kw.id,
        status=article_status,
        body=article_body,
    )
    session.add(art)
    session.flush()

    program_ids: list[int] = []
    link_ids: list[int] = []
    source_ids: list[int] = []
    names = [
        "Make", "HubSpot", "ClickUp", "monday.com", "Pipedrive",
        "Reclaim.ai", "Todoist", "Zapier", "Airtable", "Notion",
    ]
    for i in range(n_tools):
        name = f"{names[i]}{slug_suffix}"
        prog = AffiliateProgram(
            name=name,
            provider="direct" if i % 2 == 0 else "PartnerStack",
            category="automation",
            commission_type="percentage",
            commission_value=35.0 - i,
            currency=None,
            match_terms=[keyword_text, name],
            status="active",
        )
        session.add(prog)
        session.flush()
        program_ids.append(prog.id)

        link = ArticleAffiliateProgram(
            article_id=art.id,
            affiliate_program_id=prog.id,
            is_primary=(i == 0),
        )
        session.add(link)
        session.flush()
        link_ids.append(link.id)

        prod = _mk_source(
            session, art.id, "official_product",
            f"https://example.com/{name.lower()}/", fresh,
        )
        price = _mk_source(
            session, art.id, "official_pricing",
            f"https://example.com/{name.lower()}/pricing", fresh,
        )
        source_ids += [prod.id, price.id]

        for fk, value in _REQUIRED_VERIFIED:
            src_id = price.id if fk in {"pricing_summary", "free_plan_available"} else prod.id
            session.add(
                ArticleFact(
                    article_id=art.id,
                    subject_ref=name,
                    affiliate_program_id=prog.id,
                    fact_key=fk,
                    fact_value=value,
                    value_status="verified",
                    unknown_reason=None,
                    source_id=src_id,
                    checked_at=fresh,
                )
            )
        if with_unknown:
            session.add(
                ArticleFact(
                    article_id=art.id,
                    subject_ref=name,
                    affiliate_program_id=prog.id,
                    fact_key="ai_features",
                    fact_value=None,
                    value_status="unknown",
                    unknown_reason="not stated on official pages",
                    source_id=price.id,
                    checked_at=fresh,
                )
            )

    session.commit()
    return Scenario(
        article_id=art.id,
        keyword_id=kw.id,
        program_ids=program_ids,
        primary_program_id=program_ids[0],
        link_ids=link_ids,
        source_ids=source_ids,
        now=now,
    )
