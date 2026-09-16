"""管理用 CLI: D-F4 -- Article の canonical body 内にある external link occurrence
(ordinal 指定) と、既存の ``AffiliateLinkTarget`` を bind する
``ArticleLinkSubstitutionMapping`` を安全に作成する。

    plan (デフォルト。読み取り専用、DB へ一切書き込まない):
        uv run python -m scripts.manage_article_link_mapping \
            --article-id 1 --occurrence-ordinal 1 --target-id 1

    execute (書き込み。``--execute`` を明示しない限り実行しない):
        uv run python -m scripts.manage_article_link_mapping \
            --article-id 1 --occurrence-ordinal 1 --target-id 1 --execute

``--original-href`` / ``--occurrence-identity-hash`` のような、Human が任意の
値を authoritative input として渡せる引数は一切存在しない。occurrence の
identity は常に ``ArticleLinkOccurrencePreviewService`` (D-D2 の既存 read-only
occurrence discovery) を **今この瞬間** に article から再計算し、
``--occurrence-ordinal`` でその結果から 1 件を選ぶだけ -- CLI 引数の hash /
href を signature 検証なしに信用することは構造的にできない。

``ArticleLinkSubstitutionService.create_mapping()`` 自身は occurrence_identity_hash
を「呼び出し側が既に検証済みの値」として凍結するだけで、canonical article
との再検証はしない (既存 docstring で明示) -- そのため stale な occurrence
を書き込まないという保証は、この CLI が毎回 fresh discovery から
occurrence_identity_hash/original_href を再計算する構造そのものによって
成立している。

Mapping の作成には ``ArticleLinkSubstitutionService.create_mapping()`` のみを
使う -- ``remap_occurrence()`` / ``revoke_mapping()`` はこの CLI からは一切
呼ばない (destination の入れ替え・失効は別の専用ワークフローの責務)。

出力は安全な要約のみ: target の full token / affiliate destination URL /
pc 値は一切出力しない (``original_href`` は記事本文に既に公開されている
非アフィリエイトの URL であり、この CLI の対象外なので通常どおり表示する)。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app.affiliate.projection import (  # noqa: E402
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_VERSION_ACTIVE,
    AffiliateProjectionError,
    projection_from_target,
)
from app.affiliate.projection_push_acknowledgement import (  # noqa: E402
    is_target_eligible,
    resolve_latest_acknowledgement,
    token_fingerprint,
)
from app.affiliate.runtime_http import require_https_origin  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.exceptions import ApplicationError  # noqa: E402
from app.models.affiliate_link_target import ALT_ACTIVE, AffiliateLinkTarget  # noqa: E402
from app.repositories.affiliate_target_projection_push_run_repository import (  # noqa: E402
    AffiliateTargetProjectionPushRunRepository,
)
from app.repositories.article_link_substitution_mapping_repository import (  # noqa: E402
    ArticleLinkSubstitutionMappingRepository,
)
from app.services.affiliate_link_target_service import AffiliateLinkTargetService  # noqa: E402
from app.services.article_link_occurrence_preview_service import (  # noqa: E402
    ArticleLinkOccurrencePreviewService,
    OccurrencePreview,
)
from app.services.article_link_substitution_service import (  # noqa: E402
    ArticleLinkSubstitutionService,
)

EXIT_OK = 0
EXIT_FAILED = 1

_IDEMPOTENCY_PREFIX = "article-link-mapping"

_ACTION_CREATE = "would_create_mapping"
_ACTION_REPLAY = "would_replay_existing_mapping"
_ACTION_CONFLICT = "conflict_active_mapping_exists"


class MappingPreflightError(Exception):
    """この CLI 固有の fail-closed 前提条件違反。"""


_SAFE_EXCEPTION_TYPES = (ApplicationError, MappingPreflightError)


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, _SAFE_EXCEPTION_TYPES):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__} (message withheld: may reference sensitive input)"


def _compute_idempotency_key(
    *, article_id: int, occurrence_identity_hash: str, affiliate_link_target_id: int
) -> str:
    """決定論的 idempotency key。token / destination URL は一切含めない。
    同じ (article_id, occurrence, target) の再実行は常に同じ key -- target が
    違えば同じ occurrence でも常に異なる key になる (silent replay を防ぐ)。
    """

    fingerprint_input = (
        f"{_IDEMPOTENCY_PREFIX}:{article_id}:{occurrence_identity_hash}:"
        f"{affiliate_link_target_id}"
    )
    digest = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
    return f"{_IDEMPOTENCY_PREFIX}:{digest}"


def _resolve_occurrence(
    preview, occurrence_ordinal: int
) -> OccurrencePreview:
    for occ in preview.occurrences:
        if occ.occurrence_ordinal == occurrence_ordinal:
            return occ
    raise MappingPreflightError(
        f"occurrence ordinal {occurrence_ordinal} not found among "
        f"{len(preview.occurrences)} current external link occurrences for "
        f"article {preview.article_id}"
    )


def _validate_target(
    session: Session, *, article_id: int, target_id: int
) -> AffiliateLinkTarget:
    target = AffiliateLinkTargetService(session).get(target_id)
    if target.article_id != article_id:
        raise MappingPreflightError(
            f"target {target_id} belongs to article {target.article_id}, not "
            f"the requested article {article_id}"
        )
    if target.status != ALT_ACTIVE:
        raise MappingPreflightError(
            f"target {target_id} status {target.status!r} is not active"
        )
    return target


def _resolve_target_projection_available(
    session: Session, target: AffiliateLinkTarget
) -> bool:
    """target が最新の successful projection push acknowledgement に、現在の
    値のまま含まれているか (D-D2 の ``resolve_eligibility`` と同じ pure
    判定ロジックを再利用するだけで、独自に再実装はしない)。mapping 作成の
    gate ではなく、Human への情報表示専用。"""

    settings = get_settings()
    try:
        runtime_origin = require_https_origin(
            settings.wordpress_base_url or "", error_cls=ValueError
        )
    except ValueError:
        return False

    runs = AffiliateTargetProjectionPushRunRepository(session).latest_for_origin(
        runtime_origin
    )
    if not runs:
        return False

    resolution = resolve_latest_acknowledgement(runs)
    if resolution.manifest is None:
        return False

    try:
        expected_entry_hash = projection_from_target(target).projection_entry_hash
    except AffiliateProjectionError:
        return False

    return is_target_eligible(
        resolution,
        affiliate_link_target_id=target.id,
        expected_token_fingerprint=token_fingerprint(target.token),
        expected_link_identity_hash=target.link_identity_hash,
        expected_status=PROJECTION_STATUS_ACTIVE,
        expected_projection_version=PROJECTION_VERSION_ACTIVE,
        expected_entry_hash=expected_entry_hash,
    )


@dataclass(frozen=True)
class PlanResult:
    article_id: int
    canonical_body_hash: str
    renderer_version: str
    occurrence_ordinal: int
    occurrence_identity_hash: str
    original_href: str
    anchor_text: str | None
    target_id: int
    target_status: str
    target_link_identity_hash: str
    target_projection_available: bool
    mapping_exists: bool
    existing_mapping_id: int | None
    existing_mapping_status: str | None
    action: str
    idempotency_fingerprint: str
    would_execute: bool = False


def _preflight(
    session: Session, *, article_id: int, occurrence_ordinal: int, target_id: int
) -> PlanResult:
    """読み取り専用の前提条件チェック + 安全な metadata 計算。DB を一切
    mutate しない。occurrence_identity_hash / original_href は必ず今この瞬間の
    discovery から導出する (CLI 引数からは一切受け取らない)。"""

    preview = ArticleLinkOccurrencePreviewService(session).preview(article_id)
    occ = _resolve_occurrence(preview, occurrence_ordinal)
    target = _validate_target(session, article_id=article_id, target_id=target_id)

    idempotency_key = _compute_idempotency_key(
        article_id=article_id,
        occurrence_identity_hash=occ.occurrence_identity_hash,
        affiliate_link_target_id=target_id,
    )
    idempotency_fingerprint = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[
        :12
    ]

    mappings = ArticleLinkSubstitutionMappingRepository(session)
    existing_by_key = mappings.get_by_idempotency_key(idempotency_key)
    active_for_occurrence = mappings.get_active_for_occurrence(
        article_id, occ.occurrence_identity_hash
    )

    if existing_by_key is not None:
        if (
            existing_by_key.article_id == article_id
            and existing_by_key.occurrence_identity_hash == occ.occurrence_identity_hash
            and existing_by_key.original_href == occ.original_href
            and existing_by_key.affiliate_link_target_id == target_id
        ):
            # create_mapping() 自身が同じ 4 フィールドの完全一致だけで replay
            # 判定する -- ここではその実際の契約を予測しているだけで、
            # 再実装はしていない。
            action = _ACTION_REPLAY
        else:
            # 同じ決定論的 key を指す行の identity が一致しない -- 矛盾した
            # historical state。fail closed。
            action = _ACTION_CONFLICT
        existing_mapping = existing_by_key
    elif active_for_occurrence is not None:
        # この occurrence に active mapping が既にあるが、我々の key では
        # 見つからなかった (= 違う target、または別経路で作られたもの)。
        # create_mapping() のアクティブ一意性チェックで必ず conflict になる。
        action = _ACTION_CONFLICT
        existing_mapping = active_for_occurrence
    else:
        action = _ACTION_CREATE
        existing_mapping = None

    return PlanResult(
        article_id=article_id,
        canonical_body_hash=preview.canonical_body_hash,
        renderer_version=preview.renderer_version,
        occurrence_ordinal=occ.occurrence_ordinal,
        occurrence_identity_hash=occ.occurrence_identity_hash,
        original_href=occ.original_href,
        # D-D2 の occurrence 抽出は anchor text を一切追跡しない (renderer の
        # 最終 HTML から正規表現で再抽出するのは不確実なため設計上除外されて
        # いる) -- ここでは正直に None を返す。意味的なラベル付けは Human が
        # 記事本文を読んで判断する (この CLI が推測して埋め込むことはしない)。
        anchor_text=None,
        target_id=target_id,
        target_status=target.status,
        target_link_identity_hash=target.link_identity_hash,
        target_projection_available=_resolve_target_projection_available(
            session, target
        ),
        mapping_exists=(existing_mapping is not None),
        existing_mapping_id=(
            existing_mapping.id if existing_mapping is not None else None
        ),
        existing_mapping_status=(
            existing_mapping.status if existing_mapping is not None else None
        ),
        action=action,
        idempotency_fingerprint=idempotency_fingerprint,
    )


@dataclass(frozen=True)
class ExecuteResult:
    mapping_id: int
    article_id: int
    occurrence_ordinal: int
    occurrence_identity_hash: str
    target_id: int
    mapping_status: str
    idempotency_fingerprint: str


def execute_mapping(
    session: Session, *, article_id: int, occurrence_ordinal: int, target_id: int
) -> ExecuteResult:
    plan = _preflight(
        session,
        article_id=article_id,
        occurrence_ordinal=occurrence_ordinal,
        target_id=target_id,
    )

    if plan.action == _ACTION_CONFLICT:
        raise MappingPreflightError(
            "an existing active mapping for this exact occurrence does not "
            "match the requested target (fail closed; this CLI never remaps "
            "or revokes -- use the dedicated remap workflow for that)"
        )

    idempotency_key = _compute_idempotency_key(
        article_id=article_id,
        occurrence_identity_hash=plan.occurrence_identity_hash,
        affiliate_link_target_id=target_id,
    )
    mapping = ArticleLinkSubstitutionService(session).create_mapping(
        article_id=article_id,
        occurrence_identity_hash=plan.occurrence_identity_hash,
        original_href=plan.original_href,
        affiliate_link_target_id=target_id,
        idempotency_key=idempotency_key,
    )

    return ExecuteResult(
        mapping_id=mapping.id,
        article_id=article_id,
        occurrence_ordinal=occurrence_ordinal,
        occurrence_identity_hash=mapping.occurrence_identity_hash,
        target_id=target_id,
        mapping_status=mapping.status,
        idempotency_fingerprint=plan.idempotency_fingerprint,
    )


def _print_plan(result: PlanResult) -> None:
    print("=== Article Link Mapping Plan (READ-ONLY, no DB mutation) ===")
    print(f"article_id                    = {result.article_id}")
    print(f"canonical_body_hash           = {result.canonical_body_hash}")
    print(f"renderer_version              = {result.renderer_version}")
    print(f"occurrence_ordinal            = {result.occurrence_ordinal}")
    print(f"occurrence_identity_hash      = {result.occurrence_identity_hash}")
    print(f"original_href                 = {result.original_href}")
    print(f"anchor_text                   = {result.anchor_text}")
    print(f"target_id                     = {result.target_id}")
    print(f"target_status                 = {result.target_status}")
    print(f"target_link_identity_hash     = {result.target_link_identity_hash}")
    print(f"target_projection_available   = {result.target_projection_available}")
    print(f"mapping_exists                = {result.mapping_exists}")
    print(f"existing_mapping_id           = {result.existing_mapping_id}")
    print(f"existing_mapping_status       = {result.existing_mapping_status}")
    print(f"action                        = {result.action}")
    print(f"idempotency_fingerprint       = {result.idempotency_fingerprint}")
    print(f"would_execute                 = {result.would_execute}")
    print()
    if result.action == _ACTION_CONFLICT:
        print(
            "CONFLICT: an existing active mapping for this occurrence does not "
            "match the requested target. --execute would fail closed; this "
            "CLI never remaps or revokes."
        )
    print("no DB write performed. Pass --execute to perform the intended write.")


def _print_execute(result: ExecuteResult) -> None:
    print("=== Article Link Mapping Execution ===")
    print(f"mapping_id                    = {result.mapping_id}")
    print(f"article_id                    = {result.article_id}")
    print(f"occurrence_ordinal            = {result.occurrence_ordinal}")
    print(f"occurrence_identity_hash      = {result.occurrence_identity_hash}")
    print(f"target_id                     = {result.target_id}")
    print(f"mapping_status                = {result.mapping_status}")
    print(f"idempotency_fingerprint       = {result.idempotency_fingerprint}")


def cmd_manage(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        if not args.execute:
            result = _preflight(
                session,
                article_id=args.article_id,
                occurrence_ordinal=args.occurrence_ordinal,
                target_id=args.target_id,
            )
            _print_plan(result)
            return EXIT_OK

        plan = _preflight(
            session,
            article_id=args.article_id,
            occurrence_ordinal=args.occurrence_ordinal,
            target_id=args.target_id,
        )
        print("=== Pre-write safe preflight summary (about to execute) ===")
        _print_plan(plan)
        print()
        result = execute_mapping(
            session,
            article_id=args.article_id,
            occurrence_ordinal=args.occurrence_ordinal,
            target_id=args.target_id,
        )
        _print_execute(result)
        return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="manage_article_link_mapping",
        description=(
            "D-F4: safely create an ArticleLinkSubstitutionMapping binding one "
            "current external-link occurrence (selected by ordinal, from fresh "
            "read-only discovery) to an existing AffiliateLinkTarget. PLAN by "
            "default (read-only, zero DB mutation); pass --execute to perform "
            "the intended mapping creation. occurrence_identity_hash and "
            "original_href are never accepted as CLI input -- they are always "
            "derived from the article's current canonical body/renderer."
        ),
    )
    parser.add_argument("--article-id", type=int, required=True)
    parser.add_argument("--occurrence-ordinal", type=int, required=True)
    parser.add_argument("--target-id", type=int, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required to perform the intended mapping creation; PLAN "
        "(read-only) is the default",
    )
    parser.set_defaults(func=cmd_manage)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI 境界は安全側に倒す
        print(f"FAILED: {_safe_error_message(exc)}")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
