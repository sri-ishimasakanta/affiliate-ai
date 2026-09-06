"""管理用 DRY-RUN: control-plane target から WordPress runtime projection snapshot を組む。

    uv run python scripts/preview_affiliate_target_projection.py

- DB は **read only**。WordPress へは通信しない。projection push もしない。
- 出力は安全な要約のみ。``destination_url`` / ``tracking_url`` / affiliate query /
  credential は一切出力しない。token は先頭 4 文字だけ表示する。
- runtime-projection-ineligible な target (Unicode host 等) は snapshot から除外し、
  件数だけ報告する (control-plane 保存はそのまま)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.affiliate.projection import (  # noqa: E402
    PROJECTION_SCHEMA_VERSION,
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_STATUS_DISABLED,
    build_snapshot_from_targets,
)
from app.config.database import SessionLocal  # noqa: E402
from app.repositories.affiliate_link_target_repository import (  # noqa: E402
    AffiliateLinkTargetRepository,
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1


def _mask(token: str) -> str:
    return f"{token[:4]}…" if len(token) > 4 else "…"


def run(*, session_factory=SessionLocal) -> int:
    with session_factory() as session:
        targets = AffiliateLinkTargetRepository(session).list_all_ordered_by_token()
        snapshot, ineligible = build_snapshot_from_targets(targets)

    active = sum(1 for t in snapshot.targets if t.status == PROJECTION_STATUS_ACTIVE)
    disabled = sum(
        1 for t in snapshot.targets if t.status == PROJECTION_STATUS_DISABLED
    )

    print("=== Affiliate target projection (DRY RUN) ===")
    print(f"schema_version           = {PROJECTION_SCHEMA_VERSION}")
    print(f"control_plane_target_count = {len(targets)}")
    print(f"projected_target_count    = {len(snapshot.targets)}")
    print(f"  active_count            = {active}")
    print(f"  disabled_count          = {disabled}  (includes superseded)")
    print(f"runtime_ineligible_count  = {len(ineligible)}  (excluded from snapshot)")
    print(f"projection_snapshot_hash  = {snapshot.projection_snapshot_hash}")
    for t in snapshot.targets:
        print(
            f"  token={_mask(t.token)} v{t.projection_version} {t.status} "
            f"entry_hash={t.projection_entry_hash[:12]}…"
        )
    if ineligible:
        print(
            "WARNING: some targets are not runtime-projection-eligible in V1 "
            "(e.g. Unicode host); they remain in control-plane storage but are "
            "excluded from the snapshot."
        )
    print("dry-run: no DB write, no WordPress request, no projection push.")
    return EXIT_OK


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
