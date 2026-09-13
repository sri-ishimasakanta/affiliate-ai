"""D-D0.1 §5/§6 historical reproducibility — artifact_hash / tracked_html_hash は
生成時に凍結された引数のみの純関数であり、mapping/target/projection acknowledgement
の *現在* の状態が後でどう変化しても、常に同一に再現できなければならない。

このファイルは他の統合テストと区別して独立させる: 「artifact 生成後に周辺の
control-plane 状態を変化させても、既存 artifact の identity/検証結果が一切
影響を受けない」ことだけを検証する。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AffiliateLinkTarget, Article
from app.models.affiliate_link_target import ALT_DISABLED
from app.models.article_link_substitution_mapping import ALSM_REVOKED, ALSM_SUPERSEDED
from app.services.article_link_substitution_service import (
    ArticleLinkSubstitutionService,
)
from app.services.article_publication_artifact_service import (
    ArticlePublicationArtifactService,
)
from app.wordpress.publication_artifact import (
    compute_artifact_hash,
    compute_tracked_html_hash,
    expected_replacement_href,
)

_TOKEN = "DDDDDDDDDDDDDDDDDDDDDD"


def _seed_article(session: Session, slug: str = "p1") -> Article:
    art = Article(title="t", slug=slug, keyword_id=None, body="# b\n")
    session.add(art)
    session.commit()
    return art


def _seed_target(session: Session, *, article_id: int) -> AffiliateLinkTarget:
    target = AffiliateLinkTarget(
        token=_TOKEN,
        article_id=article_id,
        affiliate_program_id=1,
        destination_url="https://aff.example.test/x",
        destination_host="aff.example.test",
        status="active",
        link_identity_hash="1" * 64,
    )
    session.add(target)
    session.commit()
    return target


def _manifest_entries(mapping_id: int, target_id: int) -> list[dict]:
    return [
        {
            "occurrence_ordinal": 0,
            "occurrence_identity_hash": "a" * 64,
            "mapping_id": mapping_id,
            "affiliate_link_target_id": target_id,
            "token": _TOKEN,
            "target_projection_version": 1,
            "original_href": "https://official.example.test/x",
            "replacement_href": expected_replacement_href(_TOKEN),
            "rel_before": "nofollow",
            "rel_after": "sponsored nofollow",
        }
    ]


def test_artifact_hash_and_tracked_html_hash_survive_downstream_lifecycle_changes(
    session: Session,
) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    mapping_svc = ArticleLinkSubstitutionService(session)
    mapping = mapping_svc.create_mapping(
        article_id=art.id,
        occurrence_identity_hash="a" * 64,
        original_href="https://official.example.test/x",
        affiliate_link_target_id=target.id,
    )

    artifact_svc = ArticlePublicationArtifactService(session)
    tracked_html = (
        f"<p>see <a href='{expected_replacement_href(_TOKEN)}'>this</a></p>"
    )
    create_kwargs = {
        "article_id": art.id,
        "canonical_body_hash": "b" * 64,
        "renderer_version": "v1",
        "manifest_entries": _manifest_entries(mapping.id, target.id),
        "tracked_html": tracked_html,
    }
    artifact = artifact_svc.create_artifact(**create_kwargs)

    frozen_artifact_hash = artifact.artifact_hash
    frozen_tracked_html_hash = artifact.tracked_html_hash

    # --- 周辺 control-plane 状態を artifact 生成後に変化させる ------------
    # 1) target を disabled にする (供給側の状態変化)。
    target.status = ALT_DISABLED
    session.commit()

    # 2) mapping を同一 occurrence のまま新 target へ remap してから revoke する
    #    (mapping lifecycle の変化。D-D1.2: remap は old の occurrence を新
    #    replacement 行がそのまま継承する — 別 occurrence への supersede は
    #    もう構築できない)。
    other_target = AffiliateLinkTarget(
        token="EEEEEEEEEEEEEEEEEEEEEE",
        article_id=art.id,
        affiliate_program_id=1,
        destination_url="https://aff.example.test/y",
        destination_host="aff.example.test",
        status="active",
        link_identity_hash="2" * 64,
    )
    session.add(other_target)
    session.commit()
    remapped_mapping = mapping_svc.remap_occurrence(
        mapping.id, new_affiliate_link_target_id=other_target.id
    )
    assert mapping.status == ALSM_SUPERSEDED
    assert mapping.superseded_by_id == remapped_mapping.id

    revoked_mapping = mapping_svc.revoke_mapping(remapped_mapping.id)
    assert revoked_mapping.status == ALSM_REVOKED

    # --- artifact_hash / tracked_html_hash は元の凍結入力から再計算しても
    #     一切変化しない (mapping/target の現在状態を全く参照していない証明)。
    recomputed_artifact_hash = compute_artifact_hash(
        artifact_schema_version=artifact.artifact_schema_version,
        article_id=artifact.article_id,
        canonical_body_hash=artifact.canonical_body_hash,
        renderer_version=artifact.renderer_version,
        manifest=_manifest_entries(mapping.id, target.id),
    )
    assert recomputed_artifact_hash == frozen_artifact_hash

    recomputed_tracked_html_hash = compute_tracked_html_hash(tracked_html)
    assert recomputed_tracked_html_hash == frozen_tracked_html_hash

    # --- DB 上の既存 artifact 行自体も無変更のまま。
    refreshed = artifact_svc.get(artifact.id)
    assert refreshed.artifact_hash == frozen_artifact_hash
    assert refreshed.tracked_html_hash == frozen_tracked_html_hash
    assert refreshed.canonical_body_hash == "b" * 64
    assert refreshed.tracked_html == tracked_html


def test_creating_artifact_again_after_lifecycle_changes_still_dedupes_to_same_row(
    session: Session,
) -> None:
    """同一の凍結入力 (article_id/canonical_body_hash/renderer_version/manifest) で
    再度 create_artifact を呼んでも、周辺状態がどう変わっていようと同じ既存行が
    返る (idempotent — 現在状態を一切考慮しない)。"""

    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    mapping_svc = ArticleLinkSubstitutionService(session)
    mapping = mapping_svc.create_mapping(
        article_id=art.id,
        occurrence_identity_hash="a" * 64,
        original_href="https://official.example.test/x",
        affiliate_link_target_id=target.id,
    )

    artifact_svc = ArticlePublicationArtifactService(session)
    create_kwargs = {
        "article_id": art.id,
        "canonical_body_hash": "b" * 64,
        "renderer_version": "v1",
        "manifest_entries": _manifest_entries(mapping.id, target.id),
        "tracked_html": "<p>original</p>",
    }
    first = artifact_svc.create_artifact(**create_kwargs)
    artifact_svc.approve_artifact(first.id, expected_artifact_hash=first.artifact_hash)

    mapping_svc.revoke_mapping(mapping.id)
    target.status = ALT_DISABLED
    session.commit()

    second = artifact_svc.create_artifact(
        **{**create_kwargs, "tracked_html": "<p>irrelevant for dedupe</p>"}
    )
    assert second.id == first.id
    assert second.artifact_hash == first.artifact_hash
    # 承認済みの既存行がそのまま返る — dedupe が承認状態を巻き戻したりしない。
    assert second.approved_at is not None


def test_artifact_verification_never_consults_mapping_or_target_tables(
    session: Session,
) -> None:
    """compute_artifact_hash / compute_tracked_html_hash はセッションも DB も
    一切受け取らない純関数である — シグネチャそのものが「mapping/target/
    projection acknowledgement を参照できない」ことを保証する。"""

    import inspect

    artifact_hash_params = set(inspect.signature(compute_artifact_hash).parameters)
    assert artifact_hash_params == {
        "artifact_schema_version",
        "article_id",
        "canonical_body_hash",
        "renderer_version",
        "manifest",
    }

    tracked_html_hash_params = set(
        inspect.signature(compute_tracked_html_hash).parameters
    )
    assert tracked_html_hash_params == {"tracked_html"}


def test_mapping_revocation_does_not_affect_existing_artifact(
    session: Session,
) -> None:
    """mapping の revoke 後も、既存 artifact の内容/identity は完全に無変更のまま
    (artifact は mapping の現在 status を一切参照しないという独立性の確認)。"""

    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    mapping_svc = ArticleLinkSubstitutionService(session)
    mapping = mapping_svc.create_mapping(
        article_id=art.id,
        occurrence_identity_hash="a" * 64,
        original_href="https://official.example.test/x",
        affiliate_link_target_id=target.id,
    )
    artifact_svc = ArticlePublicationArtifactService(session)
    artifact = artifact_svc.create_artifact(
        article_id=art.id,
        canonical_body_hash="b" * 64,
        renderer_version="v1",
        manifest_entries=_manifest_entries(mapping.id, target.id),
        tracked_html="<p>x</p>",
    )

    mapping_svc.revoke_mapping(mapping.id)

    refreshed = artifact_svc.get(artifact.id)
    assert refreshed.artifact_hash == artifact.artifact_hash
    assert refreshed.tracked_html == artifact.tracked_html
    assert refreshed.approved_at is None
