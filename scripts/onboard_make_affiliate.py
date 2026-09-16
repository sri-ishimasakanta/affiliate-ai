"""管理用 CLI: D-F2C/D -- 既存 production の Make ``AffiliateProgram`` (id=1,
provider="direct" -> "make") と、その最初の ``AffiliateLinkTarget`` を安全に
onboarding する。

    plan (デフォルト。読み取り専用、DB へ一切書き込まない):
        uv run python -m scripts.onboard_make_affiliate \
            --article-id 1 --program-id 1

    execute (書き込み。``--execute`` を明示しない限り実行しない):
        uv run python -m scripts.onboard_make_affiliate \
            --article-id 1 --program-id 1 --execute

真の Make affiliate URL は ``--url`` / ``--tracking-url`` のような CLI 引数
としては一切受け付けない。実行時に ``getpass.getpass()`` で非表示入力として
対話的に取得する (echo しない、shell history にも残らない)。

出力は安全な要約のみ: 実 URL 全体・query 値・``pc`` の値・token 本体は
一切出力しない。scheme / destination_host / query パラメータ **名** /
fragment の有無 / URL の SHA256 / link_identity_hash / idempotency
fingerprint のみを安全な metadata として表示する。

Program の変更は ``AffiliateProgramService.update_program()``、target の作成は
``AffiliateLinkTargetService.create_target()`` を通じてのみ行う -- どちらも
既存のビジネスロジック (host policy / destination 検証 / idempotency) を
そのまま利用し、この CLI では再実装しない。

Target 作成には article_id / affiliate_program_id / link_identity_hash から
決定論的に導出した idempotency_key を渡す -- 同じ隠し URL での再実行は必ず
同じ key になり、途中で target 作成だけが失敗した場合も安全に再開できる
(program 更新をこの CLI が raw SQL で rollback することはしない -- 各
service が自分の commit 境界を持つ)。

既に (article_id, affiliate_program_id) に active target が存在し、それが
このリクエストの idempotency_key に一致しない場合は常に fail closed で
停止する -- destination の変更はこの CLI の対象外で、専用の supersede
workflow に委ねる。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app.affiliate.destination_safety import (  # noqa: E402
    AffiliateDestinationError,
    validate_destination_url,
)
from app.affiliate.link_identity import compute_link_identity_hash  # noqa: E402
from app.affiliate.schemas import AffiliateProgramUpdate  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.exceptions import ApplicationError  # noqa: E402
from app.models import AffiliateProgram, Article  # noqa: E402
from app.models.affiliate_link_target import ALT_ACTIVE  # noqa: E402
from app.models.enums import AffiliateProgramStatus  # noqa: E402
from app.repositories.affiliate_link_target_repository import (  # noqa: E402
    AffiliateLinkTargetRepository,
)
from app.repositories.article_affiliate_program_repository import (  # noqa: E402
    ArticleAffiliateProgramRepository,
)
from app.services.affiliate_link_target_service import AffiliateLinkTargetService  # noqa: E402
from app.services.affiliate_program_service import AffiliateProgramService  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1

_EXPECTED_PROGRAM_NAME = "Make"
_EXPECTED_MAKE_HOST = "www.make.com"
_TARGET_PROVIDER = "make"
_ALLOWED_CURRENT_PROVIDERS = frozenset({"direct", _TARGET_PROVIDER})
_REQUIRED_QUERY_PARAM = "pc"
_IDEMPOTENCY_PREFIX = "make-onboard"

_ACTION_CREATE_ONLY = "would_create_target_only"
_ACTION_UPDATE_AND_CREATE = "would_update_program_and_create_target"
_ACTION_REPLAY = "would_replay_existing_target"
_ACTION_CONFLICT = "conflict_active_target_exists"


class OnboardingPreflightError(Exception):
    """この CLI 固有の fail-closed 前提条件違反。メッセージに URL 本体は含めない。"""


# 例外メッセージをそのまま印字してよい既知の安全な型のみを許可する (allowlist)。
# それ以外 (生の SQLAlchemyError を含む) は自動的に redact する -- SQLAlchemy の
# IntegrityError 等はデフォルトで bound parameters (= 実 URL を含みうる) を
# メッセージに含めるため。
_SAFE_EXCEPTION_TYPES = (ApplicationError, AffiliateDestinationError, OnboardingPreflightError)


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, _SAFE_EXCEPTION_TYPES):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__} (message withheld: may reference sensitive input)"


@dataclass(frozen=True)
class _MakeUrlFacts:
    destination_url: str
    destination_host: str
    query_param_names: frozenset[str]
    fragment_present: bool


def _validate_make_affiliate_url(raw_url: str) -> _MakeUrlFacts:
    """汎用の :func:`validate_destination_url` に加え、Make onboarding 固有の
    追加検証 (host 完全一致 / fragment 不在 / ``pc`` パラメータ名の存在) を行う。
    値は一切出力しない -- 呼び出し側もこの関数もメッセージに URL 本体を含めない。
    """

    facts = validate_destination_url(raw_url)
    if facts.destination_host != _EXPECTED_MAKE_HOST:
        raise AffiliateDestinationError("destination host is not the approved Make host")

    parts = urlsplit(raw_url)
    if parts.fragment:
        raise AffiliateDestinationError("destination URL must not contain a fragment")

    query_param_names = frozenset(
        name for name, _value in parse_qsl(parts.query, keep_blank_values=True)
    )
    if _REQUIRED_QUERY_PARAM not in query_param_names:
        raise AffiliateDestinationError(
            f"destination URL is missing the required {_REQUIRED_QUERY_PARAM!r} query "
            "parameter"
        )

    return _MakeUrlFacts(
        destination_url=facts.destination_url,
        destination_host=facts.destination_host,
        query_param_names=query_param_names,
        fragment_present=bool(parts.fragment),
    )


def _compute_idempotency_key(
    *, article_id: int, affiliate_program_id: int, link_identity_hash: str
) -> str:
    """決定論的・非可逆な idempotency key。実 URL / pc 値 / affiliate code は
    平文で埋め込まない -- ``link_identity_hash`` (既に SHA-256) を経由するのみ。
    同じ隠し URL での再実行は常に同じ key、異なる URL は常に異なる key になる。
    """

    fingerprint_input = (
        f"{_IDEMPOTENCY_PREFIX}:{article_id}:{affiliate_program_id}:{link_identity_hash}"
    )
    digest = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
    return f"{_IDEMPOTENCY_PREFIX}:{digest}"


@dataclass(frozen=True)
class PlanResult:
    article_id: int
    program_id: int
    program_name: str
    current_provider: str | None
    desired_provider: str
    provider_update_would_occur: bool
    tracking_url_would_change: bool
    destination_scheme: str
    destination_host: str
    destination_query_param_names: tuple[str, ...]
    fragment_present: bool
    destination_url_sha256: str
    link_identity_hash: str
    active_target_exists: bool
    existing_target_id: int | None
    existing_target_status: str | None
    action: str
    idempotency_fingerprint: str
    would_execute: bool = False


def _validate_identity(
    session: Session, *, article_id: int, program_id: int
) -> AffiliateProgram:
    """article/program/relationship の前提条件のみを検証する (URL 非依存)。

    ``cmd_onboard`` はこれを hidden URL の prompt より **先** に呼ぶ --
    identity が既に不正なら、Human に機微な URL の入力を求める前に fail
    closed する (§10 の要求順序)。
    """

    article = session.get(Article, article_id)
    if article is None:
        raise OnboardingPreflightError(f"Article not found: {article_id!r}")

    program = session.get(AffiliateProgram, program_id)
    if program is None:
        raise OnboardingPreflightError(f"AffiliateProgram not found: {program_id!r}")
    if program.id != program_id:
        raise OnboardingPreflightError("program identity mismatch")
    if program.name != _EXPECTED_PROGRAM_NAME:
        raise OnboardingPreflightError(
            f"program {program_id} name {program.name!r} does not match expected "
            f"{_EXPECTED_PROGRAM_NAME!r}"
        )
    if str(program.status) != AffiliateProgramStatus.ACTIVE.value:
        raise OnboardingPreflightError(
            f"program {program_id} status {program.status!r} is not active"
        )
    if program.provider not in _ALLOWED_CURRENT_PROVIDERS:
        raise OnboardingPreflightError(
            f"program {program_id} provider {program.provider!r} is not an expected "
            "onboarding provider ('direct' or 'make')"
        )

    relation = ArticleAffiliateProgramRepository(session).get_by_article_and_program(
        article_id, program_id
    )
    if relation is None:
        raise OnboardingPreflightError(
            f"no ArticleAffiliateProgram relationship exists for article {article_id} "
            f"and program {program_id}"
        )
    return program


def _preflight(
    session: Session, *, article_id: int, program_id: int, hidden_url: str
) -> PlanResult:
    """読み取り専用の前提条件チェック + 安全な metadata 計算。DB を一切
    mutate しない (flush/commit を引き起こす操作は行わない)。"""

    program = _validate_identity(session, article_id=article_id, program_id=program_id)
    url_facts = _validate_make_affiliate_url(hidden_url)

    link_hash = compute_link_identity_hash(
        article_id=article_id,
        affiliate_program_id=program_id,
        destination_url=url_facts.destination_url,
    )
    idempotency_key = _compute_idempotency_key(
        article_id=article_id,
        affiliate_program_id=program_id,
        link_identity_hash=link_hash,
    )
    idempotency_fingerprint = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]

    existing_by_key = AffiliateLinkTargetRepository(session).get_by_idempotency_key(
        idempotency_key
    )
    active_targets = [
        t
        for t in AffiliateLinkTargetService(session).list_active_for_article(article_id)
        if t.affiliate_program_id == program_id
    ]
    active_target = active_targets[0] if active_targets else None

    provider_update_would_occur = program.provider != _TARGET_PROVIDER
    tracking_url_would_change = program.tracking_url != url_facts.destination_url

    if existing_by_key is not None:
        existing_target_id = existing_by_key.id
        existing_target_status = existing_by_key.status
        if (
            existing_by_key.article_id == article_id
            and existing_by_key.affiliate_program_id == program_id
            and existing_by_key.link_identity_hash == link_hash
            and not tracking_url_would_change
        ):
            # create_target() は destination を常に「現在の」
            # program.tracking_url から再計算する (この CLI の hidden_url を
            # 直接は見ない)。よって真の replay が成立するのは
            # program.tracking_url が既に hidden_url と一致しているときだけ
            # -- 一致していなければ create_target() 自身が (hash が過去に
            # 一致していても) "different target identity" で conflict を
            # 投げる。ここで再実装するのではなく、実際の service 契約を
            # 正しく予測するために tracking_url_would_change も確認している。
            action = _ACTION_REPLAY
            active_target_exists = existing_by_key.status == ALT_ACTIVE
        else:
            # 同じ決定論的 key を指す行の identity が一致しないか、
            # program.tracking_url が乖離していて真の replay が成立しない --
            # いずれも矛盾した historical state。fail closed。
            action = _ACTION_CONFLICT
            active_target_exists = active_target is not None
    elif active_target is not None:
        # この key では見つからない active target が既に存在する -- hash が
        # たまたま一致していても、create_target() の idempotency 経路では
        # replay とは判定されない (existing.idempotency_key が異なるため
        # active-uniqueness エラーになる)。fail closed。
        action = _ACTION_CONFLICT
        existing_target_id = active_target.id
        existing_target_status = active_target.status
        active_target_exists = True
    else:
        existing_target_id = None
        existing_target_status = None
        active_target_exists = False
        if provider_update_would_occur or tracking_url_would_change:
            action = _ACTION_UPDATE_AND_CREATE
        else:
            action = _ACTION_CREATE_ONLY

    return PlanResult(
        article_id=article_id,
        program_id=program_id,
        program_name=program.name,
        current_provider=program.provider,
        desired_provider=_TARGET_PROVIDER,
        provider_update_would_occur=provider_update_would_occur,
        tracking_url_would_change=tracking_url_would_change,
        destination_scheme="https",
        destination_host=url_facts.destination_host,
        destination_query_param_names=tuple(sorted(url_facts.query_param_names)),
        fragment_present=url_facts.fragment_present,
        destination_url_sha256=hashlib.sha256(
            url_facts.destination_url.encode("utf-8")
        ).hexdigest(),
        link_identity_hash=link_hash,
        active_target_exists=active_target_exists,
        existing_target_id=existing_target_id,
        existing_target_status=existing_target_status,
        action=action,
        idempotency_fingerprint=idempotency_fingerprint,
    )


@dataclass(frozen=True)
class ExecuteResult:
    article_id: int
    program_id: int
    action_taken: str
    program_updated: bool
    target_id: int
    target_status: str
    target_superseded_by_id: int | None
    link_identity_hash: str
    idempotency_fingerprint: str


def execute_onboarding(
    session: Session, *, article_id: int, program_id: int, hidden_url: str
) -> ExecuteResult:
    plan = _preflight(
        session, article_id=article_id, program_id=program_id, hidden_url=hidden_url
    )

    if plan.action == _ACTION_CONFLICT:
        raise OnboardingPreflightError(
            "an existing affiliate link target for this article/program does not "
            "match the requested onboarding identity (fail closed; destination "
            "changes belong to the dedicated supersede workflow, not this CLI)"
        )

    program_updated = False
    # D-F2C/D 自己レビューでの修正: プログラム更新は action が
    # _ACTION_UPDATE_AND_CREATE (= 既存 target が一切存在しないと preflight が
    # 確認済み) のときだけ行う。provider_update_would_occur /
    # tracking_url_would_change 単体の再評価に頼らない -- REPLAY (既存
    # target がこの完全一致 identity で既にある) のとき、理論上 out-of-band に
    # program.tracking_url が乖離していても、target が既に存在する限り
    # tracking_url を casually に書き換えてはならない (§15)。
    if plan.action == _ACTION_UPDATE_AND_CREATE:
        update_fields: dict[str, str] = {}
        if plan.provider_update_would_occur:
            update_fields["provider"] = _TARGET_PROVIDER
        if plan.tracking_url_would_change:
            update_fields["tracking_url"] = hidden_url
        AffiliateProgramService(session).update_program(
            program_id, AffiliateProgramUpdate(**update_fields)
        )
        program_updated = True

    idempotency_key = _compute_idempotency_key(
        article_id=article_id,
        affiliate_program_id=program_id,
        link_identity_hash=plan.link_identity_hash,
    )
    target = AffiliateLinkTargetService(session).create_target(
        article_id=article_id,
        affiliate_program_id=program_id,
        idempotency_key=idempotency_key,
    )

    return ExecuteResult(
        article_id=article_id,
        program_id=program_id,
        action_taken=plan.action,
        program_updated=program_updated,
        target_id=target.id,
        target_status=target.status,
        target_superseded_by_id=target.superseded_by_id,
        link_identity_hash=plan.link_identity_hash,
        idempotency_fingerprint=plan.idempotency_fingerprint,
    )


def _print_plan(result: PlanResult) -> None:
    print("=== Make Affiliate Onboarding Plan (READ-ONLY, no DB mutation) ===")
    print(f"article_id                    = {result.article_id}")
    print(f"program_id                    = {result.program_id}")
    print(f"program_name                  = {result.program_name}")
    print(f"current_provider              = {result.current_provider}")
    print(f"desired_provider              = {result.desired_provider}")
    print(f"provider_update_would_occur   = {result.provider_update_would_occur}")
    print(f"tracking_url_would_change     = {result.tracking_url_would_change}")
    print(f"destination_scheme            = {result.destination_scheme}")
    print(f"destination_host              = {result.destination_host}")
    print(f"destination_query_param_names = {list(result.destination_query_param_names)}")
    print(f"fragment_present              = {result.fragment_present}")
    print(f"destination_url_sha256        = {result.destination_url_sha256}")
    print(f"link_identity_hash            = {result.link_identity_hash}")
    print(f"active_target_exists          = {result.active_target_exists}")
    print(f"existing_target_id            = {result.existing_target_id}")
    print(f"existing_target_status        = {result.existing_target_status}")
    print(f"action                        = {result.action}")
    print(f"idempotency_fingerprint       = {result.idempotency_fingerprint}")
    print(f"would_execute                 = {result.would_execute}")
    print()
    if result.action == _ACTION_CONFLICT:
        print(
            "CONFLICT: an existing target does not match this onboarding identity. "
            "--execute would fail closed; this CLI never supersedes a destination."
        )
    print("no DB write performed. Pass --execute to perform the intended write(s).")


def _print_execute(result: ExecuteResult) -> None:
    print("=== Make Affiliate Onboarding Execution ===")
    print(f"article_id                    = {result.article_id}")
    print(f"program_id                    = {result.program_id}")
    print(f"action_taken                  = {result.action_taken}")
    print(f"program_updated               = {result.program_updated}")
    print(f"target_id                     = {result.target_id}")
    print(f"target_status                 = {result.target_status}")
    print(f"target_superseded_by_id       = {result.target_superseded_by_id}")
    print(f"link_identity_hash            = {result.link_identity_hash}")
    print(f"idempotency_fingerprint       = {result.idempotency_fingerprint}")


def _read_hidden_url() -> str:
    return getpass.getpass(
        prompt="Real Make affiliate URL (hidden input, not echoed, never logged): "
    )


def cmd_onboard(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        # §10 の要求順序: identity を先に検証し、機微な URL の入力は
        # article/program/relationship が有効だと確認できてから求める。
        _validate_identity(session, article_id=args.article_id, program_id=args.program_id)
        hidden_url = _read_hidden_url()

        if not args.execute:
            result = _preflight(
                session,
                article_id=args.article_id,
                program_id=args.program_id,
                hidden_url=hidden_url,
            )
            _print_plan(result)
            return EXIT_OK

        plan = _preflight(
            session,
            article_id=args.article_id,
            program_id=args.program_id,
            hidden_url=hidden_url,
        )
        print("=== Pre-write safe preflight summary (about to execute) ===")
        _print_plan(plan)
        print()
        result = execute_onboarding(
            session,
            article_id=args.article_id,
            program_id=args.program_id,
            hidden_url=hidden_url,
        )
        _print_execute(result)
        return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="onboard_make_affiliate",
        description=(
            "D-F2C/D: safely onboard the existing production Make AffiliateProgram "
            "(provider direct -> make) and create its first AffiliateLinkTarget. "
            "PLAN by default (read-only, zero DB mutation); pass --execute to "
            "perform the intended provider/tracking_url update and target creation. "
            "The real affiliate URL is never a CLI argument -- it is always read "
            "interactively via hidden (non-echoing) input."
        ),
    )
    parser.add_argument("--article-id", type=int, required=True)
    parser.add_argument("--program-id", type=int, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required to perform the intended provider/tracking_url update and "
        "target creation; PLAN (read-only) is the default",
    )
    parser.set_defaults(func=cmd_onboard)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI 境界は安全側に倒す (§19: redact)
        print(f"FAILED: {_safe_error_message(exc)}")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
