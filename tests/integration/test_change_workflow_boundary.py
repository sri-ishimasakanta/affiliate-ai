"""C8 の自動運用と C9 の変更適用のあいだの境界 (C9.2)。

**スケジューラは記事を変更しない。** C8 は取り込み・評価・監視だけを自動化し、
承認 (C9.1) と適用 (C9.2) は人が明示的に起動したときにしか動かない。

ここで pin するのは「そう書いてある」ではなく「そうなっている」ことである:

- 自動運用のステップ一覧に、承認/適用に相当するものが存在しない。
- 自動運用を走らせても ``change_requests`` / ``change_request_approvals`` /
  ``change_applications`` に行が増えない。
- 運用モジュールは C9 の承認・適用 service を import していない。
"""

from __future__ import annotations

import inspect

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChangeApplication, ChangeRequest, ChangeRequestApproval
from app.services import operations_runner_service as runner_mod
from app.services.operations_runner_service import DAILY_STEPS, WEEKLY_STEPS

_FORBIDDEN = ("approve", "apply", "change_request", "change_application")


def test_no_scheduled_step_approves_or_applies_a_change() -> None:
    for step in set(DAILY_STEPS) | set(WEEKLY_STEPS):
        assert not any(word in step for word in ("approve", "apply", "publish", "update"))


def test_the_operations_runner_does_not_import_the_change_services() -> None:
    source = inspect.getsource(runner_mod)

    assert "change_request_service" not in source
    assert "change_application_service" not in source
    assert "ChangeRequestService" not in source
    assert "ChangeApplicationService" not in source


def test_the_runner_has_no_method_that_could_apply_a_change() -> None:
    names = [
        name for name, _ in inspect.getmembers(runner_mod.OperationsRunner, inspect.isfunction)
    ]

    for name in names:
        assert not any(word in name for word in _FORBIDDEN), name


def test_planning_operations_creates_no_change_rows(session: Session) -> None:
    runner = runner_mod.OperationsRunner(lambda: session, settings=_Settings())

    runner.plan(profile="daily")

    assert session.scalars(select(ChangeRequest)).all() == []
    assert session.scalars(select(ChangeRequestApproval)).all() == []
    assert session.scalars(select(ChangeApplication)).all() == []


class _Settings:
    wordpress_base_url = "https://example.com"
    search_console_property_uri = "sc-domain:example.com"
    search_console_credentials_file = None
    ga4_property_id = None
