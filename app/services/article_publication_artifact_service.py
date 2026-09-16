"""ArticlePublicationArtifactService — artifact 生成/承認オーケストレーション
(transaction owner)。

生成:
  未整列の substitution manifest エントリ群 -> canonical 化 (``build_substitution_manifest``)
  -> ``artifact_hash`` / ``tracked_html_hash`` を計算 -> 同一 hash が既にあれば
  その既存行を返す (idempotent — D-D0 §18) -> 無ければ新規 append。

承認:
  ``approved_at`` / ``approved_artifact_hash`` の NULL -> 値、ちょうど 1 回だけ。
  呼び出し側が明示した ``expected_artifact_hash`` が現在の ``artifact_hash`` と
  一致しない限り承認しない (D-D0.1 §8: 別の再生成 artifact を黙って承認できない)。
  D-D4.1: ``approve_artifact`` は書き込み (``approve`` -> ``commit``) の間に例外が
  起きたら **自分で** ``rollback`` してから re-raise する — 呼び出し側が rollback を
  知っている必要はない (transaction ownership はこの service が持つ)。
  D-D4.2: ``commit`` を trailing の fallible operation にする — ``commit`` 成功後に
  ``refresh`` のような別の DB round trip を行わない。``SessionLocal`` は
  ``expire_on_commit=False`` で構築されており (``app/config/database.py``)、かつ
  repository の ``add``/``approve`` は ``flush()`` 内で ``RETURNING`` により
  ``id``/``created_at`` を既に取得済みのため、commit 後の refresh は元々不要だった
  (commit 成功後に refresh だけが失敗すると、実際には承認/生成が durable に
  成功しているにもかかわらず呼び出し側が例外を受け取ってしまう危険があった)。

D-D1 は foundation のみ — 実際の HTML 置換や WordPress 更新は行わない。
``tracked_html`` は呼び出し側が既に生成した exact 文字列をそのまま凍結する。

D-D4.4: ``create_artifact`` 自身の idempotency 契約 (identity は
``artifact_hash`` のみで判定し、``tracked_html`` はそこに含まれないため異なる
``tracked_html`` でも同一 hash なら黙って dedupe する) は D-D1 で承認済みの
契約であり、ここでは変更しない (既存テスト
``test_create_artifact_different_tracked_html_same_hash_inputs_still_dedupes``
が pin している)。D-D4 persistence service が要求する、より厳しい
"existing row の凍結フィールドが候補と完全一致すること" という契約は、この
service の別メソッド :meth:`create_or_get_artifact` が provenance
(``created: bool``) を返すことで、呼び出し側 (persistence service) が
「既存行が返ってきた場合にだけ」自分で追加検証できるようにする。
``get_by_hash`` の呼び出しが 1 箇所(このメソッド内)に集約されるため、
"outer lookup -> None" の直後に "inner lookup -> existing" が見つかるという
TOCTOU window が構造的に発生しない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.exceptions import ArticlePublicationArtifactError, EntityNotFoundError
from app.models import ArticlePublicationArtifact
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.wordpress.publication_artifact import (
    ARTIFACT_SCHEMA_VERSION,
    PublicationArtifactError,
    build_substitution_manifest,
    compute_artifact_hash,
    compute_tracked_html_hash,
    serialize_manifest,
)


@dataclass(frozen=True)
class ArtifactCreationResult:
    """``create_or_get_artifact`` の戻り値。``created`` が ``False`` の場合のみ、
    呼び出し側は「この既存行が自分の候補と凍結フィールドが一致するか」を
    追加で検証する責務を持つ (D-D4.4)。``created`` が ``True`` の場合、
    ``artifact`` は呼び出し側が渡した値そのものから **たった今** 構築された
    行であり、by construction で self-consistent — 追加検証は不要。"""

    artifact: ArticlePublicationArtifact
    created: bool


class ArticlePublicationArtifactService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._artifacts = ArticlePublicationArtifactRepository(session)

    # -- create ------------------------------------------------------
    def create_artifact(
        self,
        *,
        article_id: int,
        canonical_body_hash: str,
        renderer_version: str,
        manifest_entries: Sequence[Mapping[str, Any]],
        tracked_html: str,
        artifact_schema_version: int = ARTIFACT_SCHEMA_VERSION,
        generated_at: datetime | None = None,
    ) -> ArticlePublicationArtifact:
        """既存 D-D1 の public API — 戻り値は artifact のみ (後方互換のため
        変更しない)。provenance (fresh か既存か) が必要な呼び出し側は
        :meth:`create_or_get_artifact` を使うこと。"""

        return self.create_or_get_artifact(
            article_id=article_id,
            canonical_body_hash=canonical_body_hash,
            renderer_version=renderer_version,
            manifest_entries=manifest_entries,
            tracked_html=tracked_html,
            artifact_schema_version=artifact_schema_version,
            generated_at=generated_at,
        ).artifact

    def create_or_get_artifact(
        self,
        *,
        article_id: int,
        canonical_body_hash: str,
        renderer_version: str,
        manifest_entries: Sequence[Mapping[str, Any]],
        tracked_html: str,
        artifact_schema_version: int = ARTIFACT_SCHEMA_VERSION,
        generated_at: datetime | None = None,
    ) -> ArtifactCreationResult:
        """``create_artifact`` と同じ idempotent-by-hash ロジックだが、
        ``created`` provenance を明示的に返す (D-D4.4)。``get_by_hash`` の
        呼び出しはこのメソッド内の 1 箇所だけなので、lookup と
        insert-or-return の判断の間に TOCTOU window が生まれない。"""

        generated_at = generated_at or datetime.now(UTC)

        try:
            manifest = build_substitution_manifest(manifest_entries)
        except PublicationArtifactError as exc:
            raise ArticlePublicationArtifactError(str(exc)) from exc

        artifact_hash = compute_artifact_hash(
            artifact_schema_version=artifact_schema_version,
            article_id=article_id,
            canonical_body_hash=canonical_body_hash,
            renderer_version=renderer_version,
            manifest=manifest,
        )

        existing = self._artifacts.get_by_hash(artifact_hash)
        if existing is not None:
            # 同一の凍結入力からの再生成 -> 同じ artifact (idempotent no-op)。
            # create_artifact() 自身の契約 (tracked_html は identity に含まない)
            # はここでは変更しない — 追加検証が必要な呼び出し側は
            # created=False を見て自分で行う。
            return ArtifactCreationResult(artifact=existing, created=False)

        artifact = self._artifacts.add(
            article_id=article_id,
            canonical_body_hash=canonical_body_hash,
            renderer_version=renderer_version,
            artifact_schema_version=artifact_schema_version,
            substitution_manifest_json=serialize_manifest(manifest),
            artifact_hash=artifact_hash,
            tracked_html=tracked_html,
            tracked_html_hash=compute_tracked_html_hash(tracked_html),
            substitution_count=len(manifest),
            approved_at=None,
            approved_artifact_hash=None,
            generated_at=to_storage_utc(generated_at),
        )
        self._session.commit()
        return ArtifactCreationResult(artifact=artifact, created=True)

    # -- approve (set-once) ---------------------------------------------
    def approve_artifact(
        self,
        artifact_id: int,
        *,
        expected_artifact_hash: str,
        approved_at: datetime | None = None,
    ) -> ArticlePublicationArtifact:
        approved_at = approved_at or datetime.now(UTC)
        artifact = self._require_artifact(artifact_id)
        try:
            self._artifacts.approve(
                artifact,
                approved_at=to_storage_utc(approved_at),
                approved_artifact_hash=expected_artifact_hash,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return artifact

    # -- reads ------------------------------------------------------
    def get(self, artifact_id: int) -> ArticlePublicationArtifact:
        return self._require_artifact(artifact_id)

    def get_by_hash(self, artifact_hash: str) -> ArticlePublicationArtifact | None:
        return self._artifacts.get_by_hash(artifact_hash)

    def list_for_article(self, article_id: int) -> list[ArticlePublicationArtifact]:
        return self._artifacts.list_for_article(article_id)

    def _require_artifact(self, artifact_id: int) -> ArticlePublicationArtifact:
        artifact = self._artifacts.get_by_id(artifact_id)
        if artifact is None:
            raise EntityNotFoundError("ArticlePublicationArtifact", artifact_id)
        return artifact
