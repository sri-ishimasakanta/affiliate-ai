"""ArticleLinkOccurrencePreviewService — D-D2 の READ-ONLY occurrence discovery +
mapping/target eligibility preview オーケストレーション。

DB write 0。HTTP request 0 (WordPress にも一切繋がない)。commit は一度も行わない
(repository の書き込みメソッドを一切呼ばない)。

occurrence 抽出は :mod:`app.wordpress.link_occurrence` (pure) を使い、canonical
``Article.body`` -> ``render_wordpress_html`` から始める。各 occurrence について
既存の active mapping / target / D-D1A acknowledgement resolver を **読むだけ**
使い、現在の substitution eligibility を fail-closed reason 付きで判定する。

occurrence discovery (pure・常に valid) と eligibility 判定 (current DB state 依存・
lifecycle で変化しうる) を明確に区別する — このファイルは後者を「今この瞬間」の
値として返すだけで、どちらも一切変更しない。

D-D3: :meth:`resolve_eligibility` は元々 ``preview()`` 内部だけの private ロジック
だったものを public 化したもの。1 occurrence につき mapping/target ORM を
**1 回だけ** 読み、その同じオブジェクト参照を eligibility 判定・DTO 構築・(D-D3
preparation service の) manifest 構築のどれにも使い回す — "eligibility を v1 で
判定してから manifest を v2 から組む" ような state drift を構造的に防止する
(D-D3 §24)。D-D3 の preparation service はこのメソッドをそのまま呼んで再利用し、
mapping/target/acknowledgement ロジックを再実装しない。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.affiliate.projection import (
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_VERSION_ACTIVE,
    AffiliateProjectionError,
    projection_from_target,
)
from app.affiliate.projection_push_acknowledgement import (
    is_target_eligible,
    resolve_latest_acknowledgement,
    token_fingerprint,
)
from app.affiliate.runtime_http import require_https_origin
from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import Settings, get_settings
from app.exceptions import EntityNotFoundError
from app.models.affiliate_link_target import ALT_ACTIVE
from app.models.affiliate_target_projection_push_run import (
    ATPP_OUTCOME_UNKNOWN,
    ATPP_RUNNING,
)
from app.repositories.affiliate_link_target_repository import (
    AffiliateLinkTargetRepository,
)
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)
from app.repositories.article_link_substitution_mapping_repository import (
    ArticleLinkSubstitutionMappingRepository,
)
from app.repositories.article_repository import ArticleRepository
from app.wordpress.link_occurrence import (
    OCCURRENCE_SCHEMA_VERSION,
    extract_link_occurrences,
)
from app.wordpress.renderer import render_wordpress_html

# -- fail-closed reason codes (D-D2 §24) -------------------------------------
REASON_NO_ACTIVE_MAPPING = "NO_ACTIVE_MAPPING"
REASON_MAPPING_TARGET_MISSING = "MAPPING_TARGET_MISSING"
REASON_TARGET_ARTICLE_MISMATCH = "TARGET_ARTICLE_MISMATCH"
REASON_TARGET_NOT_ACTIVE = "TARGET_NOT_ACTIVE"
REASON_NO_RUNTIME_ACKNOWLEDGEMENT = "NO_RUNTIME_ACKNOWLEDGEMENT"
REASON_RUNTIME_ACK_AMBIGUOUS = "RUNTIME_ACK_AMBIGUOUS"
REASON_TARGET_ABSENT_FROM_LATEST_FULL_SNAPSHOT = (
    "TARGET_ABSENT_FROM_LATEST_FULL_SNAPSHOT"
)
REASON_RUNTIME_ACK_TARGET_MISMATCH = "RUNTIME_ACK_TARGET_MISMATCH"
REASON_ELIGIBLE = "ELIGIBLE"

_REASON_TEXT = {
    REASON_NO_ACTIVE_MAPPING: "この occurrence に active な mapping がない。",
    REASON_MAPPING_TARGET_MISSING: (
        "mapping の affiliate_link_target_id が指す target が存在しない。"
    ),
    REASON_TARGET_ARTICLE_MISMATCH: (
        "target.article_id が occurrence の article と一致しない。"
    ),
    REASON_TARGET_NOT_ACTIVE: "target の status が active ではない。",
    REASON_NO_RUNTIME_ACKNOWLEDGEMENT: (
        "この runtime_origin に対する acknowledgement 履歴がない。"
    ),
    REASON_RUNTIME_ACK_AMBIGUOUS: (
        "最新の acknowledgement run が running/outcome_unknown で結果不明。"
    ),
    REASON_TARGET_ABSENT_FROM_LATEST_FULL_SNAPSHOT: (
        "最新の authoritative manifest にこの target が含まれていない。"
    ),
    REASON_RUNTIME_ACK_TARGET_MISMATCH: (
        "manifest 上のエントリが現在の target の値と一致しない。"
    ),
    REASON_ELIGIBLE: "現在 substitution 対象として適格。",
}


@dataclass(frozen=True)
class OccurrenceEligibility:
    """1 occurrence の eligibility 判定結果 + それに使った frozen ORM 参照。

    ``mapping`` / ``target`` はこの解決の間に **1 回だけ** 読み込まれたオブジェクト
    参照そのもの — 呼び出し側 (D-D3 preparation service 含む) はこれをそのまま
    再利用し、別途再取得しないこと (state drift 防止)。"""

    mapping: object | None
    target: object | None
    eligible: bool
    reason: str


@dataclass(frozen=True)
class OccurrencePreview:
    occurrence_ordinal: int
    occurrence_identity_hash: str
    original_href: str
    original_host: str | None

    mapped: bool
    mapping_id: int | None
    mapping_status: str | None
    target_id: int | None
    target_status: str | None
    # 生の token は絶対に含めない — fingerprint (sha256) のみ。
    target_token_fingerprint: str | None

    eligible: bool
    reason: str
    reason_text: str


@dataclass(frozen=True)
class ArticleLinkOccurrencePreview:
    article_id: int
    article_title: str
    canonical_body_hash: str
    renderer_version: str
    occurrence_schema_version: int

    external_occurrence_count: int
    active_mapping_count: int
    eligible_substitution_count: int

    occurrences: list[OccurrencePreview]


class ArticleLinkOccurrencePreviewService:
    """READ-ONLY。commit を一度も呼ばない — session はここでは読み取り専用に扱う。"""

    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings if settings is not None else get_settings()
        self._articles = ArticleRepository(session)
        self._mappings = ArticleLinkSubstitutionMappingRepository(session)
        self._targets = AffiliateLinkTargetRepository(session)
        self._push_runs = AffiliateTargetProjectionPushRunRepository(session)

    def preview(self, article_id: int) -> ArticleLinkOccurrencePreview:
        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError("Article", article_id)

        canonical_body_hash = compute_text_hash(article.body or "")
        rendered = render_wordpress_html(article.body or "")
        occurrences = extract_link_occurrences(
            external_links=rendered.external_links,
            canonical_body_hash=canonical_body_hash,
            renderer_version=rendered.renderer_version,
        )

        previews: list[OccurrencePreview] = []
        active_mapping_count = 0
        eligible_count = 0
        for occ in occurrences:
            elig = self.resolve_eligibility(
                article_id=article_id,
                occurrence_identity_hash=occ.occurrence_identity_hash,
            )
            preview = self._result(
                occurrence_ordinal=occ.occurrence_ordinal,
                occurrence_identity_hash=occ.occurrence_identity_hash,
                original_href=occ.original_href,
                original_host=occ.original_host,
                mapping=elig.mapping,
                target=elig.target,
                reason=elig.reason,
            )
            previews.append(preview)
            if preview.mapped:
                active_mapping_count += 1
            if preview.eligible:
                eligible_count += 1

        return ArticleLinkOccurrencePreview(
            article_id=article_id,
            article_title=article.title or "",
            canonical_body_hash=canonical_body_hash,
            renderer_version=rendered.renderer_version,
            occurrence_schema_version=OCCURRENCE_SCHEMA_VERSION,
            external_occurrence_count=len(occurrences),
            active_mapping_count=active_mapping_count,
            eligible_substitution_count=eligible_count,
            occurrences=previews,
        )

    # -- shared eligibility resolution (reused by D-D3) -------------------
    def resolve_eligibility(
        self, *, article_id: int, occurrence_identity_hash: str
    ) -> OccurrenceEligibility:
        """1 occurrence の mapping/target/runtime acknowledgement eligibility を
        1 回だけ読み込んで判定する。mapping/target の ORM 参照はこの呼び出し内で
        読み込んだものをそのまま返す (再取得しない — D-D3 §24 の drift guard)。"""

        mapping = self._mappings.get_active_for_occurrence(
            article_id, occurrence_identity_hash
        )
        if mapping is None:
            return OccurrenceEligibility(
                mapping=None, target=None, eligible=False,
                reason=REASON_NO_ACTIVE_MAPPING,
            )

        target = self._targets.get_by_id(mapping.affiliate_link_target_id)
        if target is None:
            return OccurrenceEligibility(
                mapping=mapping, target=None, eligible=False,
                reason=REASON_MAPPING_TARGET_MISSING,
            )

        if target.article_id != article_id:
            return OccurrenceEligibility(
                mapping=mapping, target=target, eligible=False,
                reason=REASON_TARGET_ARTICLE_MISMATCH,
            )

        if target.status != ALT_ACTIVE:
            return OccurrenceEligibility(
                mapping=mapping, target=target, eligible=False,
                reason=REASON_TARGET_NOT_ACTIVE,
            )

        runtime_origin = self._resolve_runtime_origin()
        reason = self._resolve_runtime_eligibility(
            target=target, runtime_origin=runtime_origin
        )
        return OccurrenceEligibility(
            mapping=mapping, target=target,
            eligible=(reason == REASON_ELIGIBLE), reason=reason,
        )

    def _resolve_runtime_origin(self) -> str | None:
        base_url = self._settings.wordpress_base_url
        try:
            return require_https_origin(base_url, error_cls=ValueError)
        except ValueError:
            return None

    def _resolve_runtime_eligibility(self, *, target, runtime_origin: str | None) -> str:
        if runtime_origin is None:
            return REASON_NO_RUNTIME_ACKNOWLEDGEMENT

        runs = self._push_runs.latest_for_origin(runtime_origin)
        if not runs:
            return REASON_NO_RUNTIME_ACKNOWLEDGEMENT

        resolution = resolve_latest_acknowledgement(runs)
        if resolution.manifest is None:
            if runs[0].status in (ATPP_RUNNING, ATPP_OUTCOME_UNKNOWN):
                return REASON_RUNTIME_ACK_AMBIGUOUS
            return REASON_NO_RUNTIME_ACKNOWLEDGEMENT

        entry = next(
            (
                e
                for e in resolution.manifest
                if e["affiliate_link_target_id"] == target.id
            ),
            None,
        )
        if entry is None:
            return REASON_TARGET_ABSENT_FROM_LATEST_FULL_SNAPSHOT

        try:
            expected_entry_hash = projection_from_target(target).projection_entry_hash
        except AffiliateProjectionError:
            return REASON_RUNTIME_ACK_TARGET_MISMATCH

        eligible = is_target_eligible(
            resolution,
            affiliate_link_target_id=target.id,
            expected_token_fingerprint=token_fingerprint(target.token),
            expected_link_identity_hash=target.link_identity_hash,
            expected_status=PROJECTION_STATUS_ACTIVE,
            expected_projection_version=PROJECTION_VERSION_ACTIVE,
            expected_entry_hash=expected_entry_hash,
        )
        return REASON_ELIGIBLE if eligible else REASON_RUNTIME_ACK_TARGET_MISMATCH

    # -- result builder ---------------------------------------------------
    @staticmethod
    def _result(
        *,
        occurrence_ordinal: int,
        occurrence_identity_hash: str,
        original_href: str,
        original_host: str | None,
        mapping,
        target,
        reason: str,
    ) -> OccurrencePreview:
        return OccurrencePreview(
            occurrence_ordinal=occurrence_ordinal,
            occurrence_identity_hash=occurrence_identity_hash,
            original_href=original_href,
            original_host=original_host,
            mapped=mapping is not None,
            mapping_id=(mapping.id if mapping is not None else None),
            mapping_status=(mapping.status if mapping is not None else None),
            target_id=(target.id if target is not None else None),
            target_status=(target.status if target is not None else None),
            target_token_fingerprint=(
                token_fingerprint(target.token) if target is not None else None
            ),
            eligible=(reason == REASON_ELIGIBLE),
            reason=reason,
            reason_text=_REASON_TEXT[reason],
        )
