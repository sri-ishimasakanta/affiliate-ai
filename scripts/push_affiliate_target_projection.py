"""管理用 CLI: control-plane の affiliate target snapshot を WordPress runtime へ push。

    uv run python scripts/push_affiliate_target_projection.py            # PLAN のみ (無通信)
    uv run python scripts/push_affiliate_target_projection.py --execute  # 実 POST 1 回

- default は PLAN / DRY-RUN。0 WordPress request。secret も署名も要らない。
- ``--execute`` のときだけ ``AFFILIATE_RUNTIME_SHARED_SECRET`` + ``WORDPRESS_BASE_URL``
  が必須。POST は **ちょうど 1 回**。auto retry しない。ネットワーク不明時はそう報告する。
- 出力に secret / signature / destination_url / affiliate query / full token を
  一切含めない。token は先頭 4 文字だけ表示する。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.affiliate.projection import (  # noqa: E402
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_STATUS_DISABLED,
    build_snapshot_from_targets,
)
from app.affiliate.projection_push_client import (  # noqa: E402
    execute_projection_push,
    prepare_projection_push,
)
from app.affiliate.projection_signing import PROJECTION_ENDPOINT_PATH  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.exceptions import AffiliateProjectionPushError  # noqa: E402
from app.repositories.affiliate_link_target_repository import (  # noqa: E402
    AffiliateLinkTargetRepository,
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_NOT_CONFIGURED = 2
EXIT_PUSH_ERROR = 3


def _mask(token: str) -> str:
    return f"{token[:4]}…" if len(token) > 4 else "…"


def _endpoint_host(base_url: str | None) -> str:
    if not base_url:
        return "<WORDPRESS_BASE_URL unset>"
    netloc = urlsplit(base_url).netloc or "<invalid base url>"
    return f"{netloc}{PROJECTION_ENDPOINT_PATH}"


def run(
    *,
    execute: bool,
    settings=None,
    session_factory=SessionLocal,
    transport=None,
    now: int | None = None,
) -> int:
    settings = settings or get_settings()

    with session_factory() as session:
        targets = AffiliateLinkTargetRepository(session).list_all_ordered_by_token()
        snapshot, ineligible = build_snapshot_from_targets(targets)

    active = sum(1 for t in snapshot.targets if t.status == PROJECTION_STATUS_ACTIVE)
    disabled = sum(
        1 for t in snapshot.targets if t.status == PROJECTION_STATUS_DISABLED
    )

    print("=== Affiliate target projection push (PLAN) ===")
    print(f"schema_version           = {snapshot.schema_version}")
    print(f"projection_snapshot_hash = {snapshot.projection_snapshot_hash}")
    print(f"eligible_target_count    = {len(snapshot.targets)}")
    print(f"  active                 = {active}")
    print(f"  disabled               = {disabled}  (includes superseded)")
    print(f"excluded_target_count    = {len(ineligible)}  (not runtime-projectable in V1)")
    print(f"endpoint                 = {_endpoint_host(settings.wordpress_base_url)}")
    print(f"push_configured          = {settings.affiliate_runtime_push_configured}")
    for t in snapshot.targets:
        print(
            f"  token={_mask(t.token)} v{t.projection_version} {t.status} "
            f"entry_hash={t.projection_entry_hash[:12]}…"
        )
    if ineligible:
        print(
            "WARNING: some targets are not runtime-projection-eligible in V1; "
            "they stay in control-plane storage but are excluded from the snapshot."
        )

    if not execute:
        print("execute                  = false")
        print("plan only: 0 WordPress requests. pass --execute to push.")
        return EXIT_OK

    if not settings.affiliate_runtime_push_configured:
        print(
            "NOT CONFIGURED: --execute needs WORDPRESS_BASE_URL and "
            "AFFILIATE_RUNTIME_SHARED_SECRET"
        )
        return EXIT_NOT_CONFIGURED

    try:
        prepared = prepare_projection_push(
            snapshot,
            base_url=settings.wordpress_base_url,
            shared_secret=settings.affiliate_runtime_shared_secret,
            now=now,
        )
        result = execute_projection_push(
            prepared,
            transport=transport,
            verify_tls=settings.wordpress_verify_tls,
        )
    except AffiliateProjectionPushError as exc:
        # exc の文言に secret / signature / body / destination は含まれない。
        print(f"PUSH FAILED: {exc.reason}")
        if exc.server_code:
            print(f"server_code = {exc.server_code}")
        return EXIT_PUSH_ERROR

    print("=== executed (exactly 1 request) ===")
    print(f"http_status              = {result.http_status}")
    print(f"projection_snapshot_hash = {result.projection_snapshot_hash}")
    print(f"received_count           = {result.received_count}")
    print(f"inserted_count           = {result.inserted_count}")
    print(f"updated_count            = {result.updated_count}")
    print(f"unchanged_count          = {result.unchanged_count}")
    if result.received_count == 0:
        print("result: empty-snapshot push accepted (idempotent no-op).")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="push_affiliate_target_projection",
        description="control-plane snapshot を WordPress runtime へ push する (管理用)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="実 POST を 1 回行う (指定しなければ PLAN のみ・無通信)",
    )
    args = parser.parse_args(argv)
    try:
        return run(execute=args.execute)
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
