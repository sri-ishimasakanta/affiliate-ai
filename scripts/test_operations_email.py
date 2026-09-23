"""管理用 CLI: 運用メール設定の確認とテスト送信 (C8.7)。

    # 設定を確認するだけ (既定。SMTP に接続しない)
    uv run python scripts/test_operations_email.py

    # 実際にテストメールを 1 通送る
    uv run python scripts/test_operations_email.py --execute

**password は決して表示しない。** 出せるのは「設定されているか」だけである。
宛先はソースに定数で持たず、``OPERATIONS_EMAIL_TO`` から読む。

テストメールには本番の秘密情報を一切含めない (どのホストから出たかと、
確認用の時刻だけ)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.article.fact_freshness import to_storage_utc  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    DELIVERY_FAILED,
    DELIVERY_SENT,
    NOTIFICATION_TEST,
    NotificationDelivery,
)
from app.operations.email import (  # noqa: E402
    EmailConfigError,
    build_email_config,
    describe_email_settings,
)
from app.services.operations_notification_service import _fingerprint, _hint  # noqa: E402

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_SEND_FAILED = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="実際にテストメールを送る (既定は確認のみ)"
    )
    parser.add_argument("--json", dest="json_path", help="設定状況を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    described = describe_email_settings(settings)

    print("=== operations email configuration ===")
    print(f"enabled             = {described.get('enabled')}")
    print(f"smtp_host           = {described.get('smtp_host')}")
    print(f"smtp_port           = {described.get('smtp_port')}")
    print(f"use_starttls        = {described.get('use_starttls')}")
    print(f"sender              = {described.get('sender')}")
    print(f"recipients          = {described.get('recipients')}")
    print(f"username_configured = {described.get('username_configured')}")
    print(f"password_configured = {described.get('password_configured')}")
    if described.get("reason"):
        print(f"not configured      : {described['reason']}")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(described, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_path}")

    if not args.execute:
        print("\nPLAN のみ。実際に送るには --execute を付ける。")
        print("password は表示しない (設定済みかどうかだけを出す)。")
        return EXIT_OK if described.get("reason") is None else EXIT_NOT_CONFIGURED

    try:
        config = build_email_config(settings)
    except EmailConfigError as exc:
        print(f"\nrefused: {exc.reason}")
        return EXIT_NOT_CONFIGURED

    from app.operations.email import EmailNotifier

    now = datetime.now(UTC)
    subject = config.subject("Operations email test")
    body = "\n".join(
        [
            "BizFluxLab の運用メール設定を確認するためのテストです。",
            "",
            f"sent at   : {now.isoformat()}",
            f"smtp host : {config.host}:{config.port}",
            f"starttls  : {config.use_starttls}",
            "",
            "このメールに本番の秘密情報は含まれない。",
            "これが届いていれば、日次インシデントと週次レポートも同じ経路で届く。",
        ]
    )
    result = EmailNotifier(config).send_report(subject=subject, body=body)
    print(f"\ndelivered = {result.delivered}")
    if result.detail:
        print(f"detail    = {result.detail}")

    with SessionLocal() as session:
        row = NotificationDelivery(
            channel="email",
            notification_type=NOTIFICATION_TEST,
            recipient_fingerprint=_fingerprint(config.recipients),
            recipient_hint=_hint(config.recipients),
            subject=subject[:255],
            outcome=DELIVERY_SENT if result.delivered else DELIVERY_FAILED,
            error_category=None if result.delivered else result.detail,
            attempted_at=to_storage_utc(now),
            finished_at=to_storage_utc(datetime.now(UTC)),
        )
        session.add(row)
        session.commit()
        print(f"delivery recorded as #{row.id}")

    return EXIT_OK if result.delivered else EXIT_SEND_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
