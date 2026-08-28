"""DraftInputSnapshot REST エンドポイントの検証 (preview / freeze / list / detail)。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ArticleFact, DraftInputSnapshot
from tests.support.draft_input_fixture import build_scenario


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(DraftInputSnapshot))


def test_preview_endpoint_is_read_only(
    api_client: TestClient, session: Session
) -> None:
    sc = build_scenario(session, n_tools=3)
    resp = api_client.get(f"/api/v1/articles/{sc.article_id}/draft-input-preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_version"] == "draft_input_v1"
    assert body["gate_status"]["can_freeze"] is True
    assert body["payload"]["article"]["id"] == sc.article_id
    assert _count(session) == 0


def test_freeze_endpoint_creates_snapshot(
    api_client: TestClient, session: Session
) -> None:
    sc = build_scenario(session, n_tools=3)
    h = api_client.get(
        f"/api/v1/articles/{sc.article_id}/draft-input-preview"
    ).json()["content_hash"]

    resp = api_client.post(
        f"/api/v1/articles/{sc.article_id}/draft-input-snapshots",
        json={"expected_content_hash": h},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["already_frozen"] is False
    assert body["snapshot"]["content_hash"] == h
    assert body["snapshot"]["drafting_allowed_at_freeze"] is True
    assert _count(session) == 1


def test_freeze_endpoint_drift_returns_409(
    api_client: TestClient, session: Session
) -> None:
    sc = build_scenario(session, n_tools=3)
    h = api_client.get(
        f"/api/v1/articles/{sc.article_id}/draft-input-preview"
    ).json()["content_hash"]
    row = session.scalars(
        select(ArticleFact).where(ArticleFact.fact_key == "official_url").limit(1)
    ).one()
    row.fact_value = "https://example.com/moved"
    session.commit()

    resp = api_client.post(
        f"/api/v1/articles/{sc.article_id}/draft-input-snapshots",
        json={"expected_content_hash": h},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "snapshot_input_changed"
    assert _count(session) == 0


def test_freeze_endpoint_not_ready_returns_409(
    api_client: TestClient, session: Session
) -> None:
    sc = build_scenario(session, n_tools=3, article_body="already drafting")
    resp = api_client.post(
        f"/api/v1/articles/{sc.article_id}/draft-input-snapshots",
        json={"expected_content_hash": "a" * 64},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "draft_input_not_ready"
    assert _count(session) == 0


def test_list_and_detail_endpoints(
    api_client: TestClient, session: Session
) -> None:
    sc = build_scenario(session, n_tools=3)
    h = api_client.get(
        f"/api/v1/articles/{sc.article_id}/draft-input-preview"
    ).json()["content_hash"]
    snap_id = api_client.post(
        f"/api/v1/articles/{sc.article_id}/draft-input-snapshots",
        json={"expected_content_hash": h},
    ).json()["snapshot"]["id"]

    listing = api_client.get(
        f"/api/v1/articles/{sc.article_id}/draft-input-snapshots"
    )
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert "payload" not in rows[0]  # summary は payload を含まない
    assert rows[0]["content_hash"] == h

    detail = api_client.get(
        f"/api/v1/articles/{sc.article_id}/draft-input-snapshots/{snap_id}"
    )
    assert detail.status_code == 200
    assert detail.json()["payload"]["snapshot_version"] == "draft_input_v1"


def test_detail_wrong_article_returns_404(
    api_client: TestClient, session: Session
) -> None:
    sc = build_scenario(session, n_tools=2)
    h = api_client.get(
        f"/api/v1/articles/{sc.article_id}/draft-input-preview"
    ).json()["content_hash"]
    snap_id = api_client.post(
        f"/api/v1/articles/{sc.article_id}/draft-input-snapshots",
        json={"expected_content_hash": h},
    ).json()["snapshot"]["id"]
    other = build_scenario(session, n_tools=2, suffix="b")
    resp = api_client.get(
        f"/api/v1/articles/{other.article_id}/draft-input-snapshots/{snap_id}"
    )
    assert resp.status_code == 404


def test_no_patch_or_delete_routes(api_client: TestClient, session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    h = api_client.get(
        f"/api/v1/articles/{sc.article_id}/draft-input-preview"
    ).json()["content_hash"]
    snap_id = api_client.post(
        f"/api/v1/articles/{sc.article_id}/draft-input-snapshots",
        json={"expected_content_hash": h},
    ).json()["snapshot"]["id"]
    base = f"/api/v1/articles/{sc.article_id}/draft-input-snapshots/{snap_id}"
    assert api_client.patch(base, json={}).status_code == 405
    assert api_client.delete(base).status_code == 405
