"""ArticlePublicationArtifactPersistenceService — D-D3 の
``PreparedPublicationResult`` を D-D1 の ``ArticlePublicationArtifact`` として
安全に persist する、狭いオーケストレーション (D-D4)。

このサービス自体は artifact/hash/manifest ロジックを一切再実装しない —
``build_substitution_manifest`` / ``serialize_manifest`` / ``compute_artifact_hash`` /
``compute_tracked_html_hash`` / ``validate_tracked_html`` / D-D1 の
``ArticlePublicationArtifactService.create_or_get_artifact`` を **そのまま** 使う。

persist の前に必ず以下を fail closed で検証する (D-D4 §6-9):

1. canonical drift guard — 現在の ``Article.body`` の hash が
   ``prepared.canonical_body_hash`` と一致すること (古い prepared 結果を
   黙って再利用しない)。
2. renderer/schema guard — ``prepared.renderer_version`` /
   ``prepared.artifact_schema_version`` / ``prepared.occurrence_schema_version``
   が現在の承認済み定数と一致すること (NEW artifact のみに適用 — 既存の
   historical artifact 行は対象外)。
3. self-consistency re-verification — tracked_html_hash / artifact_hash /
   manifest の canonical 化結果 / substitution_count / 直列化 JSON を
   独立に再計算し、prepared の値と完全一致することを確認する。加えて
   ``validate_tracked_html`` (forward + strict reverse) を通す。
4. artifact_hash による idempotency — 既存行があれば凍結フィールドが
   完全に一致することを確認してから返す (一致しなければ integrity drift として
   fail closed — 上書きしない)。

D-D4.3: 上記 4 の既存行判定は **必ず新規 insert より前** に確定する。既存行が
見つかった場合はそこで確定する (0 write / 0 commit) — insert 経路には一切
進まない。新規 insert に進むのは「既存行が無いと確認済み」の場合のみであり、
その insert は validated ``prepared`` の値そのものから構築されるため
by construction で self-consistent — commit 成功後に追加の検証・DB read は
一切行わない (commit が最後の fallible DB operation)。

D-D4.4: D-D4.3 は「既存行 lookup」と「insert-or-return の判断」を別々の
``get_by_hash`` 呼び出し (このサービスの outer lookup + ``create_artifact``
内部の inner lookup) に分けていたため、outer が None を返した直後に別の
書き込みが同じ ``artifact_hash`` で inner lookup 前に現れる TOCTOU window が
理論上存在した (D-D4.3 のコードでは inner lookup が既存行を見つけて
consistency check を経ずに返してしまう)。``ArticlePublicationArtifactService.
create_or_get_artifact`` を使うことでこの 2 段階の lookup を 1 箇所に統合し、
``created: bool`` provenance を受け取る — ``created=False`` の場合にのみ
(＝このメソッド呼び出しの中で write が一切起きていない場合にのみ)
``_require_consistent_persisted`` を実行する。これにより race window が
構造的に消滅し、post-commit validation (D-D4.3 が禁止した形) を復活させずに
済む。``create_artifact`` 自身の identity 契約 (``tracked_html`` は
``artifact_hash`` に含まれないため異なっていても dedupe される、D-D1 で
承認済み) はここでは一切変更していない。

承認 (approve) はこのサービスの責務ではない — D-D1 の
``ArticlePublicationArtifactService.approve_artifact`` を別の明示的な操作として
そのまま呼ぶこと (D-D4 §3: prepare / persist / inspect / approve を混在させない)。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.exceptions import ArticlePublicationArtifactError, EntityNotFoundError
from app.models import ArticlePublicationArtifact
from app.repositories.article_repository import ArticleRepository
from app.services.article_publication_artifact_service import (
    ArticlePublicationArtifactService,
)
from app.services.article_publication_preparation_service import (
    PreparedPublicationResult,
)
from app.wordpress.link_occurrence import OCCURRENCE_SCHEMA_VERSION
from app.wordpress.publication_artifact import (
    ARTIFACT_SCHEMA_VERSION,
    build_substitution_manifest,
    compute_artifact_hash,
    compute_tracked_html_hash,
    serialize_manifest,
)
from app.wordpress.publication_substitution import validate_tracked_html
from app.wordpress.renderer import RENDERER_VERSION, render_wordpress_html


class ArticlePublicationArtifactPersistenceService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._artifacts = ArticlePublicationArtifactService(session)

    def persist(self, prepared: PreparedPublicationResult) -> ArticlePublicationArtifact:
        article = self._articles.get_by_id(prepared.article_id)
        if article is None:
            raise EntityNotFoundError("Article", prepared.article_id)

        # -- 1. canonical drift guard ---------------------------------
        current_body_hash = compute_text_hash(article.body or "")
        if current_body_hash != prepared.canonical_body_hash:
            raise ArticlePublicationArtifactError(
                "prepared result is stale: current Article.body hash does not "
                "match prepared.canonical_body_hash; re-prepare instead of "
                "persisting a stale candidate"
            )

        # -- 2. renderer/schema guard (NEW artifact のみ) ---------------
        if prepared.renderer_version != RENDERER_VERSION:
            raise ArticlePublicationArtifactError(
                "prepared.renderer_version does not match the current approved "
                "renderer_version; re-prepare instead of persisting a stale "
                "candidate"
            )
        if prepared.artifact_schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ArticlePublicationArtifactError(
                "prepared.artifact_schema_version does not match the current "
                "approved artifact_schema_version; re-prepare instead of "
                "persisting a stale candidate"
            )
        if prepared.occurrence_schema_version != OCCURRENCE_SCHEMA_VERSION:
            raise ArticlePublicationArtifactError(
                "prepared.occurrence_schema_version does not match the current "
                "approved occurrence_schema_version; re-prepare instead of "
                "persisting a stale candidate"
            )

        # -- 3. self-consistency re-verification (fail closed on tampering) --
        rendered = render_wordpress_html(article.body or "")
        if rendered.html != prepared.canonical_html:
            raise ArticlePublicationArtifactError(
                "prepared.canonical_html does not match the HTML re-rendered "
                "from the current Article.body"
            )

        try:
            recomputed_manifest = build_substitution_manifest(prepared.substitution_manifest)
        except Exception as exc:
            raise ArticlePublicationArtifactError(
                f"prepared.substitution_manifest failed canonicalization: {exc}"
            ) from exc
        if recomputed_manifest != prepared.substitution_manifest:
            raise ArticlePublicationArtifactError(
                "prepared.substitution_manifest is not already canonical"
            )
        if len(recomputed_manifest) != prepared.substitution_count:
            raise ArticlePublicationArtifactError(
                "prepared.substitution_count does not match the manifest length"
            )
        if serialize_manifest(recomputed_manifest) != prepared.substitution_manifest_json:
            raise ArticlePublicationArtifactError(
                "prepared.substitution_manifest_json does not match the "
                "serialized canonical manifest"
            )

        recomputed_artifact_hash = compute_artifact_hash(
            artifact_schema_version=prepared.artifact_schema_version,
            article_id=prepared.article_id,
            canonical_body_hash=prepared.canonical_body_hash,
            renderer_version=prepared.renderer_version,
            manifest=recomputed_manifest,
        )
        if recomputed_artifact_hash != prepared.artifact_hash:
            raise ArticlePublicationArtifactError(
                "prepared.artifact_hash does not match the recomputed hash of "
                "its own inputs"
            )

        recomputed_tracked_hash = compute_tracked_html_hash(prepared.tracked_html)
        if recomputed_tracked_hash != prepared.tracked_html_hash:
            raise ArticlePublicationArtifactError(
                "prepared.tracked_html_hash does not match the recomputed hash "
                "of prepared.tracked_html"
            )

        validate_tracked_html(
            canonical_html=prepared.canonical_html,
            external_links=rendered.external_links,
            canonical_body_hash=prepared.canonical_body_hash,
            renderer_version=prepared.renderer_version,
            tracked_html=prepared.tracked_html,
            manifest=recomputed_manifest,
        )

        # -- 4. single atomic lookup-or-create (D-D4.4: no TOCTOU window) ----
        # ``create_or_get_artifact`` が lookup と insert-or-return の判断を
        # 1 箇所で行うため、"outer lookup -> None" の直後に "inner lookup ->
        # existing" が見つかるという race window が構造的に発生しない。
        try:
            result = self._artifacts.create_or_get_artifact(
                article_id=prepared.article_id,
                canonical_body_hash=prepared.canonical_body_hash,
                renderer_version=prepared.renderer_version,
                manifest_entries=recomputed_manifest,
                tracked_html=prepared.tracked_html,
                artifact_schema_version=prepared.artifact_schema_version,
            )
        except Exception:
            self._session.rollback()
            raise

        if not result.created:
            # 既存行が返ってきた場合 (このメソッド呼び出しの中で write は一切
            # 起きていない) だけ、凍結フィールドが候補と一致するか検証する。
            self._require_consistent_persisted(result.artifact, prepared)
            return result.artifact

        # 新規 insert の場合: commit は create_or_get_artifact 内で既に完了
        # している。validated prepared の値そのものから構築したため
        # by construction で self-consistent — 追加の検証・DB read は
        # 一切行わない (commit が最後の fallible DB operation のまま)。
        return result.artifact

    @staticmethod
    def _require_consistent_persisted(
        artifact: ArticlePublicationArtifact, prepared: PreparedPublicationResult
    ) -> None:
        """同じ ``artifact_hash`` の既存行が (理論上あり得ないはずの) 異なる
        凍結内容を持っていないことを確認する — hash 衝突ではなく実装バグに
        よる drift を検出するための defense-in-depth (D-D4 §9, D-D4.3 §7)。

        ``artifact_hash`` 自体は比較しない (``get_by_hash`` で検索した時点で
        ``existing.artifact_hash == prepared.artifact_hash`` は自明に真)。
        ``generated_at`` も比較しない — ``PreparedPublicationResult`` は
        ``generated_at`` を持たない (insert 時にその場で確定する値であり、
        idempotent match の場合は最初に作られた行の値を意図的にそのまま
        保持する — これは仕様どおりで drift ではない)。"""

        mismatches = []
        if artifact.article_id != prepared.article_id:
            mismatches.append("article_id")
        if artifact.canonical_body_hash != prepared.canonical_body_hash:
            mismatches.append("canonical_body_hash")
        if artifact.renderer_version != prepared.renderer_version:
            mismatches.append("renderer_version")
        if artifact.artifact_schema_version != prepared.artifact_schema_version:
            mismatches.append("artifact_schema_version")
        if artifact.substitution_manifest_json != prepared.substitution_manifest_json:
            mismatches.append("substitution_manifest_json")
        if artifact.tracked_html != prepared.tracked_html:
            mismatches.append("tracked_html")
        if artifact.tracked_html_hash != prepared.tracked_html_hash:
            mismatches.append("tracked_html_hash")
        if artifact.substitution_count != prepared.substitution_count:
            mismatches.append("substitution_count")
        if mismatches:
            raise ArticlePublicationArtifactError(
                f"artifact_hash {artifact.artifact_hash!r} matches an existing "
                f"row but frozen fields differ ({', '.join(mismatches)}) -- "
                "integrity drift, refusing to reuse"
            )
