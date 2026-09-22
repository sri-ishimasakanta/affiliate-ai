"""Content monetization mode (C2.5.8、pure・決定論的)。

「この記事を作って公開してよいか」と「この記事に今 affiliate の primary があるか」を分ける。

- ``affiliate``: affiliate の primary で収益化する記事。primary が必須で、primary は C2.5.7 の
  ``primary_eligible`` (strong、または weak かつ core) でなければならない。
- ``supporting``: SEO / topic authority / 内部リンク用の記事。affiliate の primary を要求しない
  (affiliate 0 件でもよい)。affiliate 以外の品質 / readiness の gate は全てそのまま通す必要がある。
  catalog の coverage が変われば後から affiliate に上げられる (primary の link を足す)。

保存 (``articles.monetization_mode``、明示)
-------------------------------------------
mode は **編集上の意図** であって link の状態ではない。したがって link から導出せず、
``articles.monetization_mode`` (nullable) に **明示で保存** する。

- 新規の approve は必ず明示値を保存する (request が省略したときの既定値も approve 時に確定する)。
- 承認後に affiliate link を足したり primary を外したりしても mode は変わらない。
  ``affiliate`` の article から primary を外しても ``supporting`` にはならず、
  readiness / freeze gate が「primary が無い」と報告する (mode 変更は明示操作のみ)。
- ``NULL`` = C2.5.8 より前に承認された legacy 行。**backfill しない**。読み取り時に
  :func:`resolve_effective_mode` が primary link から導出する (``legacy_derived``)。
  published の article #1 は primary=Make なので ``affiliate`` に解決される。

比較対象 (subject) の要件は affiliate とは別
-------------------------------------------
recommendation_roundup / comparison_listicle は比較する対象 (調査済みの subject) が 1 件以上
必要 (記事タイプ未確定も fail-closed で同じ扱い)。これは収益化ではなく内容の要件なので、mode に
関わらず残す (dummy の program は作らない)。
現在の fact model では subject = link した catalog program なので、supporting でも subject が
必要な型は、候補 (loose / unreviewed を含む) を secondary として link する必要がある。affiliate
でない比較対象 (catalog 外のツール) はまだ model に無い。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.article.planning import ArticleType

MODE_AFFILIATE = "affiliate"
MODE_SUPPORTING = "supporting"
MONETIZATION_MODES = (MODE_AFFILIATE, MODE_SUPPORTING)

#: 実効 mode の出どころ。``explicit`` = articles.monetization_mode に保存された編集上の意図、
#: ``legacy_derived`` = 明示値が無い legacy 行なので primary link から導出した。
SOURCE_EXPLICIT = "explicit"
SOURCE_LEGACY = "legacy_derived"
MODE_SOURCES = (SOURCE_EXPLICIT, SOURCE_LEGACY)


@dataclass(frozen=True)
class EffectiveMode:
    """``(mode, source)``。``stored`` は DB の生の値 (legacy 行は None)。"""

    mode: str
    source: str
    stored: str | None

    @property
    def is_affiliate(self) -> bool:
        return self.mode == MODE_AFFILIATE

    @property
    def is_explicit(self) -> bool:
        return self.source == SOURCE_EXPLICIT


#: 比較対象 (subject) が 1 件以上ないと本文が成り立たない記事タイプ (affiliate とは無関係)
SUBJECT_REQUIRED_ARTICLE_TYPES = frozenset(
    {ArticleType.RECOMMENDATION_ROUNDUP, ArticleType.COMPARISON_LISTICLE}
)

REASON_AFFILIATE = (
    "primary-eligible candidate(s) available: {n} (strong, or weak with core fit)"
)
REASON_SUPPORTING = (
    "no primary-eligible candidate (strong, or weak with core fit): produce as supporting "
    "content; this is not a blocker"
)

SUBJECTS_UNAVAILABLE = "comparison_subjects_unavailable"


def recommend_mode(primary_eligible_count: int) -> tuple[str, str]:
    """plan の推奨 mode と理由。primary_eligible な candidate があれば affiliate。"""

    if primary_eligible_count > 0:
        return MODE_AFFILIATE, REASON_AFFILIATE.format(n=primary_eligible_count)
    return MODE_SUPPORTING, REASON_SUPPORTING


def resolve_requested_mode(requested: str | None, primary_id: int | None) -> str:
    """approve request の mode。省略時は request の内容から安全に決める (後方互換)。

    primary を指定していれば affiliate、していなければ supporting。affiliate を自動で選ぶことは
    無い (推奨 mode が affiliate でも、primary の無い request は supporting として扱う)。
    """

    if requested is not None:
        return validated_mode(requested)
    return MODE_AFFILIATE if primary_id is not None else MODE_SUPPORTING


def legacy_mode_from_links(links: Iterable[object]) -> str:
    """**legacy fallback のみ**: 保存済み link から mode を導出する。

    ``articles.monetization_mode`` が NULL の行 (C2.5.8 より前の承認) にだけ使う。primary の
    link があれば affiliate、無ければ supporting。明示値がある行でこれを使ってはならない
    (link を変えただけで mode が変わってしまう) -- 必ず :func:`resolve_effective_mode` を通す。
    """

    has_primary = any(getattr(link, "is_primary", False) for link in links)
    return MODE_AFFILIATE if has_primary else MODE_SUPPORTING


def resolve_effective_mode(stored: str | None, links: Iterable[object]) -> EffectiveMode:
    """article の実効 mode。明示値があればそれ、無ければ link からの legacy fallback。

    ``stored`` に未知の値が入っていたら fail-closed で例外 (黙って affiliate 扱いにしない)。
    """

    if stored is None:
        return EffectiveMode(legacy_mode_from_links(links), SOURCE_LEGACY, None)
    return EffectiveMode(validated_mode(stored), SOURCE_EXPLICIT, stored)


def validated_mode(mode: str) -> str:
    """既知の mode ならそのまま返す。未知なら fail-closed で :class:`ValueError`。"""

    if mode not in MONETIZATION_MODES:
        raise ValueError(
            f"unknown monetization_mode {mode!r} (expected one of {list(MONETIZATION_MODES)})"
        )
    return mode


def requires_comparison_subjects(article_type: ArticleType | str | None) -> bool:
    """比較対象が 1 件以上要るか。記事タイプが未確定 / 不明なら fail-closed で True。"""

    if article_type is None:
        return True
    try:
        return ArticleType(article_type) in SUBJECT_REQUIRED_ARTICLE_TYPES
    except ValueError:
        return True
