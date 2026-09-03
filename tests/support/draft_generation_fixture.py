"""DraftGenerationRun / DraftPromptPackage テスト用: 実 frozen Snapshot を用意する。

``tests/support/draft_input_fixture.build_scenario`` で Article + Facts を作り、
production の preview/freeze path で DraftInputSnapshot を 1 件凍結する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.article.draft_prompt_package import EditorialOverridesV1
from app.models import DraftInputSnapshot
from app.services.draft_input_snapshot_service import DraftInputSnapshotService
from tests.support.draft_input_fixture import Scenario, build_scenario


@dataclass
class FrozenScenario:
    scenario: Scenario
    snapshot: DraftInputSnapshot
    article_id: int
    snapshot_id: int
    now: datetime


def frozen_scenario(
    session: Session, *, n_tools: int = 7, with_unknown: bool = True, suffix: str = ""
) -> FrozenScenario:
    sc = build_scenario(session, n_tools=n_tools, with_unknown=with_unknown, suffix=suffix)
    svc = DraftInputSnapshotService(session)
    preview = svc.preview(sc.article_id, now=sc.now)
    resp = svc.freeze(sc.article_id, preview.content_hash, now=sc.now)
    return FrozenScenario(
        scenario=sc,
        snapshot=resp.snapshot,
        article_id=sc.article_id,
        snapshot_id=resp.snapshot.id,
        now=sc.now,
    )


def default_overrides(fs: FrozenScenario) -> EditorialOverridesV1:
    payload = fs.snapshot.payload
    subjects = [t["subject_ref"] for t in payload["tools"]]
    # fixture では ai_features が unknown (with_unknown=True)。
    do_not_assert = [f"{s}/ai_features" for s in subjects[:2]]
    return EditorialOverridesV1(
        primary=subjects[0],
        comparison_set_size=len(subjects),
        axis_rulings=[
            {
                "axis": "法人契約・請求書払い",
                "action": "SOFTEN",
                "instruction": "比較表で tool 別 yes/no を作らない。導入時の注意点で未確認と明記。",
            }
        ],
        japanese_support_ruling={
            "verified_true": [],
            "unknown": [],
            "not_researched": subjects,
            "rule": "本記事では未確認。false 扱いしない。",
        },
        do_not_assert=do_not_assert,
        commission_to_llm=False,
    )
