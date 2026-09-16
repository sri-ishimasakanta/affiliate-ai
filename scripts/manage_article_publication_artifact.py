"""管理用 CLI: D-D3 tracked HTML 準備 -> D-D1 ArticlePublicationArtifact の
persist / inspect / Human 承認 (D-D4)。

4 つの明示的なサブコマンドに分離する (読み取り専用と書き込みを絶対に混在させない):

    plan     読み取り専用。prepare() の安全な要約のみ表示 (DB write 0)。
    persist  書き込み。``--execute`` を明示しない限り実行しない。
    inspect  読み取り専用。既存 artifact 行を独立に再検証して安全に表示。
    approve  書き込み。``--artifact-id`` と ``--expected-artifact-hash`` の両方
             を明示しない限り実行しない。「最新を承認」のような暗黙操作は無い。

    uv run python -m scripts.manage_article_publication_artifact plan --article-id 1
    uv run python -m scripts.manage_article_publication_artifact persist --article-id 1 --execute
    uv run python -m scripts.manage_article_publication_artifact inspect --artifact-id 1
    uv run python -m scripts.manage_article_publication_artifact approve \
        --artifact-id 1 --expected-artifact-hash <hash> --approve

出力は安全な要約のみ: full token / destination_url / runtime secret / HMAC は
一切出力しない。tracked HTML 全文・manifest JSON 全文もデフォルトでは出力しない
(manifest JSON には full token が含まれるため)。

D-E1: ``approve`` は同一の呼び出しの中で **必ず** 最新の
``ArticlePublicationArtifactInspectionService.inspect()`` を実行し、FROZEN
artifact 証跡 + CURRENT mapping/target/program 証跡の安全なプレビューを表示して
から (``--approve`` の有無に関わらず)、frozen 整合性ゲート + CURRENT evidence
resolvability ゲートの両方を通った場合に限り、明示された ``--approve`` を条件に
既存の ``approve_artifact()`` を呼ぶ。inspect を経由しない承認経路は存在しない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.affiliate.projection_push_acknowledgement import token_fingerprint  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.exceptions import (  # noqa: E402
    ArticlePublicationArtifactError,
    EntityNotFoundError,
)
from app.services.article_publication_artifact_inspection_service import (  # noqa: E402
    CURRENT_CANONICAL_MATCH,
    ArticlePublicationArtifactInspectionService,
)
from app.services.article_publication_artifact_persistence_service import (  # noqa: E402
    ArticlePublicationArtifactPersistenceService,
)
from app.services.article_publication_artifact_service import (  # noqa: E402
    ArticlePublicationArtifactService,
)
from app.services.article_publication_preparation_service import (  # noqa: E402
    ArticlePublicationPreparationService,
)

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_REJECTED = 2
EXIT_UNEXPECTED = 3
EXIT_BAD_INPUT = 4


def _fp(value: str | None, chars: int = 12) -> str:
    return "-" if value is None else f"{value[:chars]}…"


# ==================== plan (read-only) =======================================
def cmd_plan(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        prep = ArticlePublicationPreparationService(session, settings=get_settings())
        prepared = prep.prepare(args.article_id)

    print("=== Publication Prepare Plan (READ-ONLY) ===")
    print(f"article_id                  = {prepared.article_id}")
    print(f"canonical_body_hash         = {prepared.canonical_body_hash}")
    print(f"renderer_version            = {prepared.renderer_version}")
    print(f"artifact_schema_version     = {prepared.artifact_schema_version}")
    print(f"occurrence_schema_version   = {prepared.occurrence_schema_version}")
    print(f"substitution_count          = {prepared.substitution_count}")
    print(f"canonical_html_len          = {len(prepared.canonical_html)}")
    print(f"tracked_html_len            = {len(prepared.tracked_html)}")
    print(f"tracked_html_hash           = {prepared.tracked_html_hash}")
    print(f"canonical_equals_tracked    = {prepared.canonical_html == prepared.tracked_html}")
    print(f"artifact_hash               = {prepared.artifact_hash}")
    for e in prepared.substitution_manifest:
        print(
            f"  ordinal={e['occurrence_ordinal']} mapping_id={e['mapping_id']} "
            f"target_id={e['affiliate_link_target_id']} "
            f"token_fp={_fp(token_fingerprint(e['token']))}"
        )
    print()
    print("dry-run: no DB write, no WordPress request, no artifact persisted.")
    return EXIT_OK


# ==================== persist (write, gated) =================================
def cmd_persist(args: argparse.Namespace) -> int:
    if not args.execute:
        print("REFUSED: persist requires --execute (no default write).")
        return EXIT_BAD_INPUT

    with SessionLocal() as session:
        prep = ArticlePublicationPreparationService(session, settings=get_settings())
        prepared = prep.prepare(args.article_id)
        artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
        artifact_id = artifact.id
        artifact_hash = artifact.artifact_hash
        substitution_count = artifact.substitution_count
        approved = artifact.approved_at is not None

    print("=== Publication Artifact Persisted ===")
    print(f"artifact_id         = {artifact_id}")
    print(f"artifact_hash       = {artifact_hash}")
    print(f"substitution_count  = {substitution_count}")
    print(f"approved            = {approved}")
    return EXIT_OK


# ==================== shared safe renderer (inspect + approve) ================
def _print_inspection(insp) -> None:
    """FROZEN artifact 証跡 + CURRENT control-plane 証跡を明示的に分けて表示する。

    ``inspect`` と ``approve`` の両方がこの **1 つ** の renderer を再利用する
    (D-E1 §21: フォーマット実装を 2 つ作らない -- drift 防止)。"""

    print("=== Publication Artifact Inspection (READ-ONLY) ===")
    print(f"artifact_id                     = {insp.artifact_id}")
    print(f"article_id                      = {insp.article_id}")
    print(f"article_title                   = {insp.article_title}")
    print(f"canonical_body_hash             = {insp.canonical_body_hash}")
    print(f"renderer_version                = {insp.renderer_version}")
    print(f"artifact_schema_version         = {insp.artifact_schema_version}")
    print(f"substitution_count              = {insp.substitution_count}")
    print(f"artifact_hash                   = {insp.artifact_hash}")
    print(f"tracked_html_hash               = {insp.tracked_html_hash}")
    print(f"approved                        = {insp.approved}")
    print(f"approved_at                     = {insp.approved_at}")
    print(f"approved_artifact_hash          = {_fp(insp.approved_artifact_hash, 64)}")
    print(f"generated_at                    = {insp.generated_at}")
    print(f"created_at                      = {insp.created_at}")
    print()
    print(f"artifact_hash_valid             = {insp.artifact_hash_valid}")
    print(f"tracked_html_hash_valid         = {insp.tracked_html_hash_valid}")
    print(f"manifest_valid                  = {insp.manifest_valid}")
    print(f"strict_html_validation_valid    = {insp.strict_html_validation_valid}")
    print(f"current_canonical_status        = {insp.current_canonical_status}")
    print(f"all_current_evidence_resolvable = {insp.all_current_evidence_resolvable}")
    print()
    print(
        "occurrences (FROZEN artifact evidence vs. CURRENT control-plane "
        "evidence; full token never shown):"
    )
    for e in insp.manifest_summary:
        print(
            f"  Occurrence {e.occurrence_ordinal} "
            f"(is_affiliate_substitution={e.is_affiliate_substitution})"
        )
        print(
            f"    [FROZEN]  original_host={e.original_host} mapping_id={e.mapping_id} "
            f"target_id={e.affiliate_link_target_id} "
            f"target_projection_version={e.target_projection_version} "
            f"replacement={e.replacement_href_masked} "
            f"token_fp={_fp(e.token_fingerprint)} "
            f"rel_before={e.rel_before!r} rel_after={e.rel_after!r}"
        )
        c = e.current
        if not c.resolvable:
            print(f"    [CURRENT] UNRESOLVABLE fail_reason={c.fail_reason}")
            continue
        print(
            f"    [CURRENT] mapping_status={c.mapping_status} "
            f"target_status={c.target_status} program_name={c.program_name} "
            f"program_provider={c.program_provider} program_status={c.program_status} "
            f"destination_host={c.destination_host} "
            f"current_projection_version={c.current_projection_version} "
            f"projection_eligible={c.projection_eligible} "
            f"host_policy_eligible={c.host_policy_eligible}"
        )


def _approval_gate_failures(insp, expected_artifact_hash: str) -> list[str]:
    """D-E1 §18: approve_artifact() を呼ぶ前に必須の frozen 整合性 + CURRENT
    evidence resolvability ゲート。1 つでも欠けたら書き込み 0。"""

    failures: list[str] = []
    if insp.artifact_hash != expected_artifact_hash:
        failures.append(
            "--expected-artifact-hash does not match the inspected artifact_hash"
        )
    if not insp.artifact_hash_valid:
        failures.append("artifact_hash_valid is False")
    if not insp.tracked_html_hash_valid:
        failures.append("tracked_html_hash_valid is False")
    if not insp.manifest_valid:
        failures.append("manifest_valid is False")
    if not insp.strict_html_validation_valid:
        failures.append("strict_html_validation_valid is False")
    if insp.current_canonical_status != CURRENT_CANONICAL_MATCH:
        failures.append(
            f"current_canonical_status is {insp.current_canonical_status!r}, "
            f"not {CURRENT_CANONICAL_MATCH!r}"
        )
    if not insp.all_current_evidence_resolvable:
        failures.append(
            "one or more substituted occurrences have unresolvable CURRENT "
            "mapping/target/program evidence"
        )
    return failures


# ==================== inspect (read-only) =====================================
def cmd_inspect(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        insp = ArticlePublicationArtifactInspectionService(session).inspect(args.artifact_id)

    _print_inspection(insp)
    return EXIT_OK


# ==================== approve (write, gated) ==================================
def cmd_approve(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        # D-E1: 承認は必ず同じ呼び出しの中で fresh な inspection を経由する。
        # inspect を経由しない承認経路は存在しない (--approve の有無に関わらず
        # プレビューは常に表示する)。
        insp = ArticlePublicationArtifactInspectionService(session).inspect(args.artifact_id)
        _print_inspection(insp)

        failures = _approval_gate_failures(insp, args.expected_artifact_hash)
        if failures:
            print()
            print("REJECTED: approval preconditions not met:")
            for f in failures:
                print(f"  - {f}")
            return EXIT_REJECTED

        if not args.approve:
            print()
            print("REFUSED: approval requires --approve (no default write).")
            return EXIT_BAD_INPUT

        svc = ArticlePublicationArtifactService(session)
        artifact = svc.approve_artifact(
            args.artifact_id, expected_artifact_hash=args.expected_artifact_hash
        )
        artifact_id = artifact.id
        approved_at = artifact.approved_at

    print()
    print("=== Publication Artifact Approved ===")
    print(f"artifact_id  = {artifact_id}")
    print(f"approved_at  = {approved_at}")
    return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="manage_article_publication_artifact",
        description=(
            "D-D4: prepare -> persist -> inspect -> approve "
            "(write actions require explicit flags)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="read-only: prepare candidate summary")
    p_plan.add_argument("--article-id", type=int, required=True)
    p_plan.set_defaults(func=cmd_plan)

    p_persist = sub.add_parser("persist", help="write: persist a freshly prepared artifact")
    p_persist.add_argument("--article-id", type=int, required=True)
    p_persist.add_argument("--execute", action="store_true", help="required to actually write")
    p_persist.set_defaults(func=cmd_persist)

    p_inspect = sub.add_parser("inspect", help="read-only: inspect a persisted artifact")
    p_inspect.add_argument("--artifact-id", type=int, required=True)
    p_inspect.set_defaults(func=cmd_inspect)

    p_approve = sub.add_parser("approve", help="write: Human-approve one exact artifact hash")
    p_approve.add_argument("--artifact-id", type=int, required=True)
    p_approve.add_argument("--expected-artifact-hash", type=str, required=True)
    p_approve.add_argument("--approve", action="store_true", help="required to actually write")
    p_approve.set_defaults(func=cmd_approve)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return args.func(args)
    except EntityNotFoundError as exc:
        print(f"NOT FOUND: {exc}")
        return EXIT_NOT_FOUND
    except ArticlePublicationArtifactError as exc:
        print(f"REJECTED: {exc}")
        return EXIT_REJECTED
    except Exception as exc:  # noqa: BLE001 - 管理用 CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}: {exc}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
