"""ArticleDraftPromotion テスト用: 実 path で succeeded な DraftGenerationRun を作る。

prepare -> execute -> submit-result を production service で通し、
Article を drafting / body None のまま succeeded run を 1 件用意する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Article, DraftGenerationRun
from app.models.draft_generation_run import MODE_MANUAL
from app.services.draft_generation_run_service import DraftGenerationRunService
from app.services.draft_prompt_preview_service import DraftPromptPreviewService
from tests.support.draft_generation_fixture import default_overrides, frozen_scenario

_TOOLS7 = [
    "Make", "HubSpot", "ClickUp", "monday.com", "Pipedrive", "Reclaim.ai", "Todoist",
]


@dataclass
class PromotableScenario:
    article_id: int
    snapshot_id: int
    run_id: int
    run: DraftGenerationRun
    body_markdown: str
    meta_description: str
    raw_output: str


_GOOD_META = (
    "業務効率化ツールのおすすめを、用途・料金（2026年8月時点）・無料プラン・自動化範囲・"
    "AI機能・外部サービス連携・日本語表示の観点から7つ比較し、目的別の選び方を"
    "分かりやすく解説します。本記事はPRを含みます。"
)


def _good_body(tools: list[str]) -> str:
    tools_line = " / ".join(tools)
    return (
        "本記事は広告（アフィリエイト）を含みます。PR 表記。\n\n"
        "## 業務効率化ツールとは\n" + "本文の解説です。" * 120 + "\n\n"
        "## 選び方\n判断の軸を示します。料金は2026年8月時点。各社 $0〜$29／月 が目安です。\n\n"
        f"## おすすめ業務効率化ツール比較\n{tools_line} を比較します。\n\n"
        "## 目的別おすすめ\n用途に応じて 1 つ選びます。\n\n"
        "## 導入時の注意点\n請求書払いは各社で異なり本記事では未確認です。\n\n"
        "## よくある質問\nQ. 無料ですか。A. 各社の無料プランを確認してください。\n\n"
        "## まとめ\n目的に合うものを選んでください。"
    )


def promotable_scenario(
    session: Session, *, n_tools: int = 7, suffix: str = ""
) -> PromotableScenario:
    fs = frozen_scenario(session, n_tools=n_tools, suffix=suffix)
    ov = default_overrides(fs)
    pv = DraftPromptPreviewService(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov
    )
    svc = DraftGenerationRunService(session)
    run, _ = svc.prepare(
        fs.article_id,
        snapshot_id=fs.snapshot_id,
        expected_prompt_hash=pv["prompt_input_hash"],
        expected_rendered_prompt_hash=pv["rendered_prompt_hash"],
        execution_mode=MODE_MANUAL,
        editorial_overrides=ov,
    )
    svc.execute(fs.article_id, run.id)

    tools = [t["subject_ref"] for t in run.prompt_package["comparison_tools"]]
    body = _good_body(tools)
    meta = _GOOD_META
    raw = json.dumps(
        {"meta_description": meta, "body_markdown": body, "generation_notes": []},
        ensure_ascii=False,
    )
    run = svc.submit_result(fs.article_id, run.id, raw)
    assert run.status == "succeeded"

    return PromotableScenario(
        article_id=fs.article_id,
        snapshot_id=fs.snapshot_id,
        run_id=run.id,
        run=run,
        body_markdown=body,
        meta_description=meta,
        raw_output=raw,
    )


def article_of(session: Session, article_id: int) -> Article:
    art = session.get(Article, article_id)
    assert art is not None
    return art
