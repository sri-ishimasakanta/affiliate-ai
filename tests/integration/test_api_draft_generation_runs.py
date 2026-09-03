"""DraftGenerationRun REST エンドポイント検証。

preview / prepare / execute / submit-result / list / detail をカバーする。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, DraftGenerationRun
from tests.support.draft_generation_fixture import default_overrides, frozen_scenario

_TOOLS7 = ["Make", "HubSpot", "ClickUp", "monday.com", "Pipedrive", "Reclaim.ai", "Todoist"]


def _run_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(DraftGenerationRun))


def _ov_json(fs) -> dict:
    return default_overrides(fs).model_dump(mode="json")


def _good_output() -> str:
    tools_line = " / ".join(_TOOLS7)
    body = (
        "本記事は広告（アフィリエイト）を含みます。\n\n"
        "## 業務効率化ツールとは\n" + "解説。" * 200 + "\n\n"
        "## 選び方\n判断基準。\n\n"
        f"## おすすめ比較\n{tools_line} を比較します。料金は2026年8月時点。\n\n"
        "## 目的別おすすめ\n用途で選ぶ。\n\n"
        "## 導入時の注意点\n請求書払いは各社で異なり本記事では未確認。\n\n"
        "## FAQ\nQ&A。\n\n## まとめ\n結論。"
    )
    return json.dumps(
        {"meta_description": "業務効率化ツールのおすすめを目的別に比較し選び方を解説する記事です。",
         "body_markdown": body, "generation_notes": []},
        ensure_ascii=False,
    )


def _preview(api: TestClient, fs) -> dict:
    r = api.post(
        f"/api/v1/articles/{fs.article_id}/draft-generation-preview",
        json={"snapshot_id": fs.snapshot_id, "editorial_overrides": _ov_json(fs)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _prepare(api: TestClient, fs, pv: dict, **extra) -> dict:
    body = {
        "snapshot_id": fs.snapshot_id,
        "expected_prompt_hash": pv["prompt_input_hash"],
        "expected_rendered_prompt_hash": pv["rendered_prompt_hash"],
        "execution_mode": "manual",
        "editorial_overrides": _ov_json(fs),
    }
    body.update(extra)
    return api.post(f"/api/v1/articles/{fs.article_id}/draft-generation-runs", json=body)


def test_preview_endpoint_read_only(api_client: TestClient, session: Session) -> None:
    fs = frozen_scenario(session, n_tools=7)
    pv = _preview(api_client, fs)
    assert pv["template_version"] == "article_roundup_v1"
    assert pv["validation_summary"]["forbidden_structural_keys"] == 0
    assert pv["validation_summary"]["comparison_tools"] == 7
    assert _run_count(session) == 0


def test_prepare_then_execute_then_submit(
    api_client: TestClient, session: Session
) -> None:
    fs = frozen_scenario(session, n_tools=7)
    pv = _preview(api_client, fs)

    resp = _prepare(api_client, fs, pv)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["already_prepared"] is False
    run_id = body["run"]["id"]
    assert body["run"]["status"] == "prepared"
    assert session.get(Article, fs.article_id).status == "planned"

    ex = api_client.post(
        f"/api/v1/articles/{fs.article_id}/draft-generation-runs/{run_id}/execute"
    )
    assert ex.status_code == 200, ex.text
    assert ex.json()["run"]["status"] == "running"
    assert ex.json()["next_action"] == "submit_result"
    assert ex.json()["rendered_prompt"]
    assert session.get(Article, fs.article_id).status == "drafting"

    sr = api_client.post(
        f"/api/v1/articles/{fs.article_id}/draft-generation-runs/{run_id}/submit-result",
        json={"raw_output": _good_output()},
    )
    assert sr.status_code == 200, sr.text
    run = sr.json()["run"]
    assert run["status"] == "succeeded"
    assert run["parsed_body"]
    assert run["validation_report"]["overall"] in {"pass", "warn"}
    # Article.body は書かれない
    art = session.get(Article, fs.article_id)
    assert art.body is None and art.status == "drafting"


def test_prepare_drift_returns_409(api_client: TestClient, session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    pv = _preview(api_client, fs)
    pv["prompt_input_hash"] = "0" * 64
    resp = _prepare(api_client, fs, pv)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "prompt_input_changed"
    assert _run_count(session) == 0


def test_prepare_body_present_returns_409(
    api_client: TestClient, session: Session
) -> None:
    fs = frozen_scenario(session, n_tools=3)
    art = session.get(Article, fs.article_id)
    art.body = "draft exists"
    session.commit()
    pv = _preview(api_client, fs)
    resp = _prepare(api_client, fs, pv)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "draft_generation_state_error"


def test_list_summary_and_detail(api_client: TestClient, session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    pv = _preview(api_client, fs)
    run_id = _prepare(api_client, fs, pv).json()["run"]["id"]

    lst = api_client.get(f"/api/v1/articles/{fs.article_id}/draft-generation-runs")
    assert lst.status_code == 200
    rows = lst.json()
    assert len(rows) == 1
    assert "prompt_package" not in rows[0]
    assert "rendered_prompt" not in rows[0]

    det = api_client.get(
        f"/api/v1/articles/{fs.article_id}/draft-generation-runs/{run_id}"
    )
    assert det.status_code == 200
    d = det.json()
    assert d["prompt_package"]["template_version"] == "article_roundup_v1"
    assert d["rendered_prompt"].startswith("=== SYSTEM RULES")


def test_detail_wrong_article_404(api_client: TestClient, session: Session) -> None:
    fs = frozen_scenario(session, n_tools=2)
    pv = _preview(api_client, fs)
    run_id = _prepare(api_client, fs, pv).json()["run"]["id"]
    other = frozen_scenario(session, n_tools=2, suffix="b")
    r = api_client.get(
        f"/api/v1/articles/{other.article_id}/draft-generation-runs/{run_id}"
    )
    assert r.status_code == 404


def test_no_patch_or_delete(api_client: TestClient, session: Session) -> None:
    fs = frozen_scenario(session, n_tools=2)
    pv = _preview(api_client, fs)
    run_id = _prepare(api_client, fs, pv).json()["run"]["id"]
    base = f"/api/v1/articles/{fs.article_id}/draft-generation-runs/{run_id}"
    assert api_client.patch(base, json={}).status_code == 405
    assert api_client.delete(base).status_code == 405
