"""ArticlePublicationPreparationService — D-D3 の READ-ONLY tracked HTML 準備
オーケストレーション。

DB write 0 (repository の書き込みメソッドを一切呼ばない・commit も一切行わない)。
HTTP request 0。``ArticlePublicationArtifactService.create_artifact`` など
D-D1 の persist 系サービスは一切呼ばない — この service は persist 前の
:class:`PreparedPublicationResult` を返すだけで、artifact 行の作成は別フェーズ
(D-D4 以降、Human 承認後) の責務。

occurrence discovery は D-D2 (:mod:`app.wordpress.link_occurrence`) を、mapping/
target/runtime acknowledgement eligibility は D-D2 の
:meth:`ArticleLinkOccurrencePreviewService.resolve_eligibility` を **そのまま**
再利用する (別実装を作らない)。1 occurrence につき eligibility 解決は 1 回だけ
行い、その呼び出しが返した ORM 参照 (``mapping`` / ``target``) だけを manifest
構築に使う — 判定後に target を再取得して別バージョンから manifest を組む、と
いった state drift を構造的に防止する (D-D3 §24)。

byte-preserving な HTML 変換そのものは :mod:`app.wordpress.publication_substitution`
(pure) が行う。生成した tracked_html / manifest は必ず ``validate_tracked_html``
(forward + strict reverse) を通してから返す — 検証に失敗すれば fail closed で
raise し、決して不正な準備結果を返さない。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.affiliate.projection import PROJECTION_VERSION_ACTIVE
from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import Settings, get_settings
from app.exceptions import EntityNotFoundError
from app.repositories.article_repository import ArticleRepository
from app.services.article_link_occurrence_preview_service import (
    ArticleLinkOccurrencePreviewService,
)
from app.wordpress.link_occurrence import (
    OCCURRENCE_SCHEMA_VERSION,
    extract_link_occurrences,
)
from app.wordpress.publication_artifact import (
    ARTIFACT_SCHEMA_VERSION,
    build_substitution_manifest,
    compute_artifact_hash,
    compute_tracked_html_hash,
    serialize_manifest,
)
from app.wordpress.publication_substitution import (
    SelectedSubstitution,
    build_tracked_html,
    validate_tracked_html,
)
from app.wordpress.renderer import render_wordpress_html


@dataclass(frozen=True)
class PreparedPublicationResult:
    """persist 前の pure な準備結果 (D-D3 §17)。production への書き込みは一切
    行わない — このオブジェクトを ``ArticlePublicationArtifactService.create_artifact``
    へ渡して初めて (別フェーズで、Human 承認を経て) 永続化される。"""

    article_id: int
    canonical_body_hash: str
    renderer_version: str
    artifact_schema_version: int
    occurrence_schema_version: int
    canonical_html: str
    tracked_html: str
    tracked_html_hash: str
    substitution_manifest: list[dict]
    substitution_manifest_json: str
    substitution_count: int
    artifact_hash: str


class ArticlePublicationPreparationService:
    """READ-ONLY。commit を一度も呼ばない。"""

    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._eligibility = ArticleLinkOccurrencePreviewService(
            session, settings=settings if settings is not None else get_settings()
        )

    def prepare(self, article_id: int) -> PreparedPublicationResult:
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

        selections: list[SelectedSubstitution] = []
        for occ in occurrences:
            elig = self._eligibility.resolve_eligibility(
                article_id=article_id,
                occurrence_identity_hash=occ.occurrence_identity_hash,
            )
            if not elig.eligible:
                continue
            # elig.mapping / elig.target は resolve_eligibility が今まさに読み込んだ
            # 同一 ORM 参照 -- ここで別途再取得しない (state drift 防止, D-D3 §24)。
            selections.append(
                SelectedSubstitution(
                    occurrence_ordinal=occ.occurrence_ordinal,
                    occurrence_identity_hash=occ.occurrence_identity_hash,
                    mapping_id=elig.mapping.id,
                    affiliate_link_target_id=elig.target.id,
                    token=elig.target.token,
                    target_projection_version=PROJECTION_VERSION_ACTIVE,
                )
            )

        built = build_tracked_html(
            canonical_html=rendered.html,
            external_links=rendered.external_links,
            canonical_body_hash=canonical_body_hash,
            renderer_version=rendered.renderer_version,
            selections=selections,
        )
        canonical_manifest = build_substitution_manifest(built.manifest)

        # forward + strict reverse validation。不正なら fail closed で raise する。
        validate_tracked_html(
            canonical_html=rendered.html,
            external_links=rendered.external_links,
            canonical_body_hash=canonical_body_hash,
            renderer_version=rendered.renderer_version,
            tracked_html=built.tracked_html,
            manifest=canonical_manifest,
        )

        artifact_hash = compute_artifact_hash(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            article_id=article_id,
            canonical_body_hash=canonical_body_hash,
            renderer_version=rendered.renderer_version,
            manifest=canonical_manifest,
        )
        tracked_html_hash = compute_tracked_html_hash(built.tracked_html)

        return PreparedPublicationResult(
            article_id=article_id,
            canonical_body_hash=canonical_body_hash,
            renderer_version=rendered.renderer_version,
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            occurrence_schema_version=OCCURRENCE_SCHEMA_VERSION,
            canonical_html=rendered.html,
            tracked_html=built.tracked_html,
            tracked_html_hash=tracked_html_hash,
            substitution_manifest=canonical_manifest,
            substitution_manifest_json=serialize_manifest(canonical_manifest),
            substitution_count=len(canonical_manifest),
            artifact_hash=artifact_hash,
        )
