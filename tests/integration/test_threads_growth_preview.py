"""Growth Post の下見 (2026-10-01)。**提案を保存しない・その日の呼び出しの記録に数えない。**"""

from __future__ import annotations

from datetime import date

from tests.integration.test_threads_growth_posts import (
    BODY_A,
    GrowthLuna,
    _growth,
    _growth_rows,
    _no_side_effects,
)

DAY = date(2026, 10, 2)


def test_preview_plans_three_different_growth_strategies_without_calls(session, tmp_path) -> None:
    fake = GrowthLuna([])
    previews = _growth(session, tmp_path, fake).preview(day=DAY, count=3)
    assert len(previews) == 3 and fake.calls == 0
    signatures = {p["strategy"]["signature"] for p in previews}
    families = {p["strategy"]["family"] for p in previews}
    assert len(signatures) == 3 and len(families) == 3
    assert all(p["topic"] == "インサイト祭り" for p in previews)  # 10/4 まで


def test_generated_previews_are_checked_but_never_saved(session, tmp_path) -> None:
    fake = GrowthLuna([BODY_A, BODY_A, BODY_A])
    previews = _growth(session, tmp_path, fake).preview(day=DAY, count=3, generate=True)
    assert fake.calls == 3 and all("ok" in p and "similarity" in p for p in previews)
    assert previews[1]["versus_other_previews"][0]["preview"] == 1
    assert _growth_rows(session) == []  # 提案を作らない
    assert not (tmp_path / "growth" / f"{DAY.isoformat()}.openai.json").exists()  # 記録しない
    _no_side_effects(session)
