"""scripts/probe_search_console_analytics.py の構造的な安全性チェック。

実 Google リクエストも DB Session もこのテストでは発生しない。
"""

from __future__ import annotations

from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_search_console_analytics.py"


def test_probe_script_opens_no_db_session() -> None:
    src = _SCRIPT.read_text(encoding="utf-8")
    lowered = src.lower()
    assert "sessionlocal" not in lowered
    assert "get_session" not in lowered
    assert "sqlalchemy" not in lowered
    assert "import_service" not in lowered
    assert "searchconsoleimportservice" not in lowered


def test_probe_script_does_not_paginate_or_mutate() -> None:
    src = _SCRIPT.read_text(encoding="utf-8")
    # probe は provider.probe_query のみ (fetch_* のページング経路を使わない)
    assert "probe_query" in src
    assert "fetch_page_daily" not in src
    assert "fetch_query_daily" not in src
    for verb in (".put(", ".patch(", ".delete("):
        assert verb not in src


def test_probe_script_imports_cleanly() -> None:
    from scripts.probe_search_console_analytics import (
        EXIT_OK,
        EXIT_PERMISSION_DENIED,
        main,
    )

    assert EXIT_OK == 0
    assert EXIT_PERMISSION_DENIED == 4
    assert callable(main)
