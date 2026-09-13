"""管理用 CLI: D-C3-C synthetic runtime click E2E probe (production runtime-only)。

approved design (D-C3-C0 / C0.1 / C0.2) を実装する薄い wrapper。business logic は
すべて :mod:`app.affiliate.synthetic_probe` に委譲する。

    uv run python scripts/probe_synthetic_runtime_click.py --init   --state-file <path>
    uv run python scripts/probe_synthetic_runtime_click.py --status --state-file <path>
    uv run python scripts/probe_synthetic_runtime_click.py --active [--execute] --state-file <path>
    uv run python scripts/probe_synthetic_runtime_click.py --go     [--execute] --state-file <path>
    uv run python scripts/probe_synthetic_runtime_click.py --record-import      --state-file <path>
    uv run python scripts/probe_synthetic_runtime_click.py --disable [--execute] --state-file <path>
    uv run python scripts/probe_synthetic_runtime_click.py --reconcile-active  --observed present
    uv run python scripts/probe_synthetic_runtime_click.py --reconcile-go
    uv run python scripts/probe_synthetic_runtime_click.py --reconcile-disable --observed present

--state-file を省略した場合は ``AFFILIATE_RUNTIME_SHARED_SECRET`` と同じ設定機構
(``AFFILIATE_PROBE_STATE_FILE``) から解決する。どちらも無ければ fail closed。

full token / full ``/go`` URL は一切出力しない — ``token_fingerprint``
(SHA-256) と masked prefix のみ。normal action は default で **PLAN のみ**
(0 network)。live 通信は該当 action に明示的に ``--execute`` を付けたときだけ。

reconciliation (``--reconcile-*``) は通常 action からは絶対に暗黙実行されない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.affiliate import synthetic_probe as probe  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.exceptions import AffiliateProjectionPushError  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_STATE_ERROR = 3
EXIT_LOCK_CONTENTION = 4
EXIT_AMBIGUOUS = 5
EXIT_UNEXPECTED = 6

_ACTIONS = (
    "init",
    "status",
    "active",
    "go",
    "record_import",
    "disable",
    "reconcile_active",
    "reconcile_go",
    "reconcile_disable",
)

_SAFE_FIELDS = (
    "state",
    "destination_host",
    "link_identity_hash",
    "activated_at",
    "active_projection_entry_hash",
    "active_projection_snapshot_hash",
    "active_attempted_at",
    "active_confirmed_at",
    "click_attempted_at",
    "click_confirmed_at",
    "source_click_id",
    "import_run_id",
    "import_confirmed_at",
    "disabled_at",
    "disabled_projection_entry_hash",
    "disabled_projection_snapshot_hash",
    "disable_attempted_at",
    "disabled_confirmed_at",
    "click_contaminated",
)


def _print_state(state: dict) -> None:
    print(f"token_fingerprint                = {state['token_fingerprint']}")
    print(f"token_prefix                     = {probe.masked_prefix(state['token'])}")
    for field in _SAFE_FIELDS:
        print(f"{field:33} = {state[field]}")


def run(
    *,
    action: str,
    execute: bool = False,
    observed: str | None = None,
    state_file: str | None = None,
    settings=None,
    session_factory=SessionLocal,
    transport=None,
) -> int:
    if action not in _ACTIONS:
        raise ValueError(f"unknown action {action!r}")

    settings = settings or get_settings()
    try:
        path = probe.resolve_state_path(cli_path=state_file, settings=settings)
    except probe.SyntheticProbeConfigError as exc:
        print(f"NOT CONFIGURED: {exc}")
        return EXIT_CONFIG_ERROR

    try:
        with probe.ProbeLock(path) as lock:
            return _dispatch(
                action,
                path,
                lock=lock,
                execute=execute,
                observed=observed,
                settings=settings,
                session_factory=session_factory,
                transport=transport,
            )
    except probe.SyntheticProbeLockError as exc:
        print(f"LOCKED: {exc}")
        return EXIT_LOCK_CONTENTION


def _dispatch(
    action: str,
    path: Path,
    *,
    lock: probe.ProbeLock,
    execute: bool,
    observed: str | None,
    settings,
    session_factory,
    transport,
) -> int:
    try:
        if action == "init":
            state = probe.action_init(path)
        elif action == "status":
            state = probe.action_status(path)
        elif action == "active":
            with session_factory() as session:
                state = probe.action_active_plan(path, session)
            if execute:
                state, result = probe.action_active_execute(
                    path, settings=settings, transport=transport
                )
                print(
                    "active projection push: "
                    f"http_status={result.http_status} inserted={result.inserted_count}"
                )
        elif action == "go":
            if not execute:
                state = probe.action_go_plan(path)
                print("PLAN: --go expects HTTP 302 with")
                print(f"  Location = {probe.PROBE_DESTINATION_URL}")
                print("  Cache-Control containing 'no-store'")
                print("  X-Robots-Tag containing 'noindex, nofollow'")
            else:
                state = probe.action_go_execute(path, settings=settings, transport=transport)
        elif action == "record_import":
            with session_factory() as session:
                state = probe.action_record_import(path, session)
        elif action == "disable":
            with session_factory() as session:
                state = probe.action_disable_plan(path, session)
            if execute:
                state, result = probe.action_disable_execute(
                    path, settings=settings, transport=transport
                )
                print(
                    "disable projection push: "
                    f"http_status={result.http_status} updated={result.updated_count}"
                )
        elif action == "reconcile_active":
            state = probe.action_reconcile_active(path, observed=observed or "")
        elif action == "reconcile_go":
            state = probe.action_reconcile_go(path, settings=settings, transport=transport)
        elif action == "reconcile_disable":
            state = probe.action_reconcile_disable(path, observed=observed or "")
        else:  # pragma: no cover - guarded by run()
            raise ValueError(f"unknown action {action!r}")
    except probe.SyntheticProbeConfigError as exc:
        print(f"NOT CONFIGURED: {exc}")
        return EXIT_CONFIG_ERROR
    except probe.SyntheticProbeStateError as exc:
        print(f"STATE ERROR: {exc}")
        return EXIT_STATE_ERROR
    except probe.SyntheticProbeAmbiguousError as exc:
        print(f"AMBIGUOUS: {exc}")
        print("outcome unknown; state preserved for reconciliation; no auto-retry.")
        return EXIT_AMBIGUOUS
    except AffiliateProjectionPushError as exc:
        print(f"PROJECTION PUSH FAILED: {exc.reason}")
        if exc.server_code:
            print(f"server_code = {exc.server_code}")
        print("outcome unknown; state preserved for reconciliation; no auto-retry.")
        return EXIT_AMBIGUOUS

    lock.record_fingerprint(state["token_fingerprint"])
    _print_state(state)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_synthetic_runtime_click",
        description="D-C3-C synthetic runtime click E2E probe (管理用・production runtime-only)",
    )
    parser.add_argument("--state-file", default=None)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="対応する action の live 通信を実行する (省略時は PLAN のみ・無通信)",
    )
    parser.add_argument("--observed", choices=["present", "absent"], default=None)

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--active", action="store_true")
    group.add_argument("--go", action="store_true")
    group.add_argument("--record-import", action="store_true")
    group.add_argument("--disable", action="store_true")
    group.add_argument("--reconcile-active", action="store_true")
    group.add_argument("--reconcile-go", action="store_true")
    group.add_argument("--reconcile-disable", action="store_true")

    args = parser.parse_args(argv)
    if args.init:
        action = "init"
    elif args.status:
        action = "status"
    elif args.active:
        action = "active"
    elif args.go:
        action = "go"
    elif args.record_import:
        action = "record_import"
    elif args.disable:
        action = "disable"
    elif args.reconcile_active:
        action = "reconcile_active"
    elif args.reconcile_go:
        action = "reconcile_go"
    else:
        action = "reconcile_disable"

    if action in ("reconcile_active", "reconcile_disable") and args.observed is None:
        parser.error(f"--{action.replace('_', '-')} requires --observed present|absent")

    try:
        return run(
            action=action,
            execute=args.execute,
            observed=args.observed,
            state_file=args.state_file,
        )
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
