"""C3: 選定済みポートフォリオの **構造的な** production readiness (pure)。

``app/config/content_portfolio.json`` の各記事について、

- 記事タイプが確定できるか
- その記事タイプの prompt template があるか
- affiliate の primary が要るか (C2.5.8 の monetization mode)
- 比較対象 (content subject) が要るか / 用意できるか

を判定する。ここで見るのは **構造** だけ。実際の source 調査・fact の鮮度・human review は
通常の production 作業であり、ここでの blocker には数えない。

DB / network に触れない (config だけで決まる)。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.article.cluster_plan import template_readiness
from app.article.content_subjects import ContentSubjectCatalog, load_content_subject_config
from app.article.monetization import (
    MODE_AFFILIATE,
    requires_comparison_subjects,
)
from app.article.planning import ArticleType

DEFAULT_PORTFOLIO_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "content_portfolio.json"
)

BLOCKER_TYPE_UNRESOLVED = "article_type_unresolved"
BLOCKER_NO_TEMPLATE = "no_prompt_template"
BLOCKER_NO_PRIMARY = "affiliate_primary_unavailable"
BLOCKER_NO_SUBJECTS = "comparison_subjects_unavailable"


@dataclass(frozen=True)
class PortfolioEntryReadiness:
    keyword: str
    cluster: str
    wave: int
    priority: int
    article_type: ArticleType | None
    template_version: str | None
    monetization_mode: str
    affiliate_primary_required: bool
    affiliate_primary_available: bool
    comparison_subjects_required: bool
    comparison_subject_count: int
    non_affiliate_subject_count: int
    blockers: tuple[str, ...]

    @property
    def article_type_resolved(self) -> bool:
        return self.article_type is not None

    @property
    def template_resolved(self) -> bool:
        return self.template_version is not None

    @property
    def structurally_producible(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class PortfolioReadiness:
    entries: tuple[PortfolioEntryReadiness, ...]

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def types_resolved(self) -> int:
        return sum(1 for e in self.entries if e.article_type_resolved)

    @property
    def templates_resolved(self) -> int:
        return sum(1 for e in self.entries if e.template_resolved)

    @property
    def producible(self) -> int:
        return sum(1 for e in self.entries if e.structurally_producible)

    @property
    def blocked(self) -> list[PortfolioEntryReadiness]:
        return [e for e in self.entries if e.blockers]

    def blocker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            for blocker in entry.blockers:
                counts[blocker] = counts.get(blocker, 0) + 1
        return counts


def _entry_readiness(
    row: dict, catalog: ContentSubjectCatalog
) -> PortfolioEntryReadiness:
    blockers: list[str] = []

    raw_type = row.get("article_type")
    try:
        article_type = ArticleType(raw_type) if raw_type else None
    except ValueError:
        article_type = None
    if article_type is None:
        blockers.append(BLOCKER_TYPE_UNRESOLVED)

    template = template_readiness(article_type)
    template_version = template.template_version if template.ready else None
    if article_type is not None and template_version is None:
        blockers.append(f"{BLOCKER_NO_TEMPLATE}:{article_type.value}")

    mode = row["monetization_mode"]
    primary_required = mode == MODE_AFFILIATE
    primary_available = bool(row.get("primary_candidates"))
    if primary_required and not primary_available:
        blockers.append(BLOCKER_NO_PRIMARY)

    # 比較対象: affiliate 裏付けの候補 + 編集 subject の合計で満たす
    editorial_keys = list(row.get("content_subject_keys", []))
    unknown = [k for k in editorial_keys if catalog.by_key(k) is None]
    affiliate_subjects = list(row.get("primary_candidates", []))
    subject_count = len(affiliate_subjects) + len([k for k in editorial_keys if k not in unknown])
    subjects_required = requires_comparison_subjects(article_type)
    if subjects_required and subject_count == 0:
        blockers.append(BLOCKER_NO_SUBJECTS)
    if unknown:
        blockers.append(f"unknown_content_subject:{','.join(sorted(unknown))}")

    return PortfolioEntryReadiness(
        keyword=row["keyword"],
        cluster=row["cluster"],
        wave=row["wave"],
        priority=row["priority"],
        article_type=article_type,
        template_version=template_version,
        monetization_mode=mode,
        affiliate_primary_required=primary_required,
        affiliate_primary_available=primary_available,
        comparison_subjects_required=subjects_required,
        comparison_subject_count=subject_count,
        non_affiliate_subject_count=len([k for k in editorial_keys if k not in unknown]),
        blockers=tuple(blockers),
    )


def evaluate_portfolio(
    portfolio_path: str | Path = DEFAULT_PORTFOLIO_PATH,
    *,
    catalog: ContentSubjectCatalog | None = None,
    rows: Sequence[dict] | None = None,
) -> PortfolioReadiness:
    """ポートフォリオ全件の構造的 readiness を返す。"""

    if rows is None:
        raw = json.loads(Path(portfolio_path).read_text(encoding="utf-8"))
        rows = raw["articles"]
    subject_catalog = catalog if catalog is not None else load_content_subject_config()
    return PortfolioReadiness(
        tuple(_entry_readiness(row, subject_catalog) for row in rows)
    )
