"""scripts/repair_article_fact_timestamps.py の検証。

一度きりの timestamp 修復 script の mapping / identity / classification /
dry-run 不変性 / atomic apply / 再実行の安全性をカバーする。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import Article, ArticleFact, Source
from scripts import repair_article_fact_timestamps as cli

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
PROD_JST = datetime(2026, 8, 28, 14, 0, tzinfo=JST)
PRICE_JST = datetime(2026, 8, 28, 15, 0, tzinfo=JST)

# JSON fact key -> fact dict。s_prod / s_price の 2 source を跨ぐ。
_TOOL_FACTS: dict[str, dict] = {
    "official_product_name": {
        "value": "Tool", "value_status": "verified",
        "source": "s_prod", "checked_at": PROD_JST.isoformat(),
    },
    "official_url": {
        "value": "https://ex.com/", "value_status": "verified",
        "source": "s_prod", "checked_at": PROD_JST.isoformat(),
    },
    "primary_use_cases": {
        "value": ["a", "b"], "value_status": "verified",
        "source": "s_prod", "checked_at": PROD_JST.isoformat(),
    },
    "key_features": {
        "value": ["x", "y"], "value_status": "verified",
        "source": "s_price", "checked_at": PRICE_JST.isoformat(),
    },
    "pricing_summary": {
        "value": "free plan available", "value_status": "verified",
        "source": "s_price", "checked_at": PRICE_JST.isoformat(),
    },
    "free_plan_available": {
        "value": True, "value_status": "verified",
        "source": "s_price", "checked_at": PRICE_JST.isoformat(),
    },
    "japan_business_support": {
        "value_status": "unknown", "unknown_reason": "not stated",
        "source": "s_price", "checked_at": PRICE_JST.isoformat(),
    },
}
_PROD_FACT_KEYS = {"official_product_name", "official_url", "primary_use_cases"}
_PRICE_FACT_KEYS = {
    "key_features", "pricing_summary", "free_plan_available", "japan_business_support"
}


def _factory(session: Session):
    @contextmanager
    def _f() -> Iterator[Session]:
        yield session

    return _f


def _payload(article_id: int = 1, **over) -> dict:
    p = {
        "article_id": article_id,
        "sources": [
            {
                "tmp_id": "s_prod", "source_type": "official_product",
                "source_url": "https://ex.com/", "title": "Prod",
                "checked_at": PROD_JST.isoformat(),
            },
            {
                "tmp_id": "s_price", "source_type": "official_pricing",
                "source_url": "https://ex.com/pricing", "title": "Price",
                "checked_at": PRICE_JST.isoformat(),
            },
        ],
        "tools": [
            {
                "subject_ref": "Tool",
                "affiliate_program_id": None,
                "facts": {k: dict(v) for k, v in _TOOL_FACTS.items()},
            }
        ],
    }
    p.update(over)
    return p


def _stored(iso: str, mode: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if mode == "broken":  # offset を落とした naive local wall-clock (= 修正前の DB)
        return dt.replace(tzinfo=None)
    if mode == "correct":  # 正しい UTC naive wall-clock
        return to_storage_utc(dt)
    raise ValueError(mode)


def _seed(
    session: Session,
    *,
    mode: str = "broken",
    payload: dict | None = None,
    article_status: str = "planned",
) -> tuple[int, dict[str, Source]]:
    payload = payload or _payload()
    art = Article(title="t", slug="s", keyword_id=None, status=article_status)
    session.add(art)
    session.flush()

    src_by_tmp: dict[str, Source] = {}
    for s in payload["sources"]:
        row = Source(
            article_id=art.id,
            source_type=s["source_type"],
            source_url=s["source_url"],
            title=s["title"],
            checked_at=_stored(s["checked_at"], mode),
        )
        session.add(row)
        session.flush()
        src_by_tmp[s["tmp_id"]] = row

    for tool in payload["tools"]:
        for fk, fd in tool["facts"].items():
            session.add(
                ArticleFact(
                    article_id=art.id,
                    subject_ref=tool["subject_ref"],
                    affiliate_program_id=tool.get("affiliate_program_id"),
                    fact_key=fk,
                    fact_value=fd.get("value"),
                    value_status=fd["value_status"],
                    unknown_reason=fd.get("unknown_reason"),
                    source_id=(
                        src_by_tmp[fd["source"]].id if fd.get("source") else None
                    ),
                    checked_at=_stored(fd["checked_at"], mode),
                )
            )
    session.flush()
    session.commit()
    return art.id, src_by_tmp


def _write(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "facts.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _run(session: Session, path: Path, *, article_id: int, apply: bool = False):
    return cli.run_repair(
        article_id=article_id,
        path=path,
        apply=apply,
        session_factory=_factory(session),
        now=NOW,
    )


def _rows(session: Session, article_id: int) -> list[dict]:
    session.expire_all()
    out: list[dict] = []
    for s in session.scalars(
        select(Source).where(Source.article_id == article_id).order_by(Source.id)
    ):
        out.append(
            {
                "kind": "source", "id": s.id, "checked_at": s.checked_at,
                "source_url": s.source_url, "source_type": s.source_type,
                "title": s.title, "fact_value": None, "value_status": None,
                "source_id": None, "subject_ref": None, "fact_key": None,
            }
        )
    for f in session.scalars(
        select(ArticleFact)
        .where(ArticleFact.article_id == article_id)
        .order_by(ArticleFact.id)
    ):
        out.append(
            {
                "kind": "fact", "id": f.id, "checked_at": f.checked_at,
                "source_url": None, "source_type": None, "title": None,
                "fact_value": f.fact_value, "value_status": f.value_status,
                "source_id": f.source_id, "subject_ref": f.subject_ref,
                "fact_key": f.fact_key,
            }
        )
    return out


def _snapshot(session: Session, article_id: int):
    return [(r["kind"], r["id"], r["checked_at"]) for r in _rows(session, article_id)]


# -- dry-run classification -------------------------------------------


def test_dry_run_classifies_broken_as_needs_repair(
    session: Session, tmp_path: Path
) -> None:
    aid, _ = _seed(session, mode="broken")
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)

    assert res.ok
    assert res.applied is False
    assert (res.source_json_count, res.source_db_count, res.source_matched) == (2, 2, 2)
    assert (res.fact_json_count, res.fact_db_count, res.fact_matched) == (7, 7, 7)
    assert res.source_needs_repair == 2 and res.source_already_correct == 0
    assert res.fact_needs_repair == 7 and res.fact_already_correct == 0
    assert res.total_would_update == 9
    assert res.article_status == "planned"
    assert res.article_body_is_none is True
    assert res.primary_count == 0


def test_dry_run_diffs_carry_correct_utc_instant(
    session: Session, tmp_path: Path
) -> None:
    aid, _ = _seed(session, mode="broken")
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)

    assert len(res.diffs) == 9
    for d in res.diffs:
        assert d.classification == "needs_repair"
        assert d.new_checked_at == d.json_checked_at.astimezone(UTC).replace(tzinfo=None)
        assert d.old_checked_at - d.new_checked_at == timedelta(hours=9)


def test_dry_run_writes_nothing(session: Session, tmp_path: Path) -> None:
    aid, _ = _seed(session, mode="broken")
    before = _rows(session, aid)
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)
    assert res.ok
    assert res.applied is False
    assert _rows(session, aid) == before


def test_already_repaired_is_already_correct(
    session: Session, tmp_path: Path
) -> None:
    aid, _ = _seed(session, mode="correct")
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)

    assert res.ok
    assert res.source_needs_repair == 0 and res.source_already_correct == 2
    assert res.fact_needs_repair == 0 and res.fact_already_correct == 7
    assert res.total_would_update == 0


def test_mixed_repaired_and_broken_classified_per_row(
    session: Session, tmp_path: Path
) -> None:
    aid, src = _seed(session, mode="broken")
    # s_price とその facts だけ先に正しい値へ直しておく。
    sp = session.get(Source, src["s_price"].id)
    sp.checked_at = to_storage_utc(PRICE_JST)
    for f in session.scalars(
        select(ArticleFact).where(ArticleFact.article_id == aid)
    ):
        if f.fact_key in _PRICE_FACT_KEYS:
            f.checked_at = to_storage_utc(PRICE_JST)
    session.commit()

    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)

    assert res.ok
    assert res.source_needs_repair == 1 and res.source_already_correct == 1
    assert res.fact_needs_repair == len(_PROD_FACT_KEYS)
    assert res.fact_already_correct == len(_PRICE_FACT_KEYS)
    assert res.total_would_update == 1 + len(_PROD_FACT_KEYS)


def test_unexpected_timestamp_rejected_and_apply_refuses(
    session: Session, tmp_path: Path
) -> None:
    aid, src = _seed(session, mode="broken")
    bad = session.get(Source, src["s_prod"].id)
    bad.checked_at = datetime(2020, 1, 1, 0, 0)
    session.commit()
    before = _rows(session, aid)

    path = _write(tmp_path, _payload(article_id=aid))
    res = _run(session, path, article_id=aid)
    assert not res.ok
    assert any("unexpected current checked_at" in e for e in res.errors)

    res_apply = _run(session, path, article_id=aid, apply=True)
    assert not res_apply.ok
    assert res_apply.applied is False
    assert _rows(session, aid) == before


# -- mapping safety -------------------------------------------------


def test_duplicate_source_mapping_rejected(
    session: Session, tmp_path: Path
) -> None:
    aid, _ = _seed(session, mode="broken")
    session.add(
        Source(
            article_id=aid, source_type="official_product",
            source_url="https://ex.com/", title="Prod",
            checked_at=PROD_JST.replace(tzinfo=None),
        )
    )
    session.commit()
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)
    assert not res.ok
    assert any("multiple DB sources" in e for e in res.errors)


def test_missing_source_mapping_rejected(session: Session, tmp_path: Path) -> None:
    aid, _ = _seed(session, mode="broken")
    payload = _payload(article_id=aid)
    payload["sources"].append(
        {
            "tmp_id": "s_extra", "source_type": "official_help",
            "source_url": "https://ex.com/help", "title": "Help",
            "checked_at": PROD_JST.isoformat(),
        }
    )
    res = _run(session, _write(tmp_path, payload), article_id=aid)
    assert not res.ok
    assert any("source not found in DB" in e for e in res.errors)


def test_extra_db_source_rejected(session: Session, tmp_path: Path) -> None:
    aid, _ = _seed(session, mode="broken")
    session.add(
        Source(
            article_id=aid, source_type="official_help",
            source_url="https://ex.com/other", title="Other",
            checked_at=PROD_JST.replace(tzinfo=None),
        )
    )
    session.commit()
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)
    assert not res.ok
    assert any("extra DB sources not present in JSON" in e for e in res.errors)


def test_duplicate_fact_mapping_rejected(session: Session, tmp_path: Path) -> None:
    aid, src = _seed(session, mode="broken")
    session.add(
        ArticleFact(
            article_id=aid, subject_ref="Tool", affiliate_program_id=None,
            fact_key="official_url", fact_value="https://ex.com/",
            value_status="verified", unknown_reason=None,
            source_id=src["s_prod"].id, checked_at=PROD_JST.replace(tzinfo=None),
        )
    )
    session.commit()
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)
    assert not res.ok
    assert any("multiple DB facts" in e for e in res.errors)


def test_missing_fact_mapping_rejected(session: Session, tmp_path: Path) -> None:
    aid, _ = _seed(session, mode="broken")
    payload = _payload(article_id=aid)
    payload["tools"][0]["facts"]["target_users"] = {
        "value": ["devs"], "value_status": "verified",
        "source": "s_prod", "checked_at": PROD_JST.isoformat(),
    }
    res = _run(session, _write(tmp_path, payload), article_id=aid)
    assert not res.ok
    assert any("fact not found in DB: Tool/target_users" in e for e in res.errors)


def test_extra_db_fact_rejected(session: Session, tmp_path: Path) -> None:
    aid, src = _seed(session, mode="broken")
    session.add(
        ArticleFact(
            article_id=aid, subject_ref="Tool", affiliate_program_id=None,
            fact_key="category", fact_value="misc", value_status="verified",
            unknown_reason=None, source_id=src["s_prod"].id,
            checked_at=PROD_JST.replace(tzinfo=None),
        )
    )
    session.commit()
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)
    assert not res.ok
    assert any("extra DB facts not present in JSON" in e for e in res.errors)


def test_fact_source_relation_mismatch_rejected(
    session: Session, tmp_path: Path
) -> None:
    aid, src = _seed(session, mode="broken")
    f = session.scalars(
        select(ArticleFact).where(ArticleFact.fact_key == "official_url")
    ).one()
    f.source_id = src["s_price"].id  # JSON は s_prod を指す
    session.commit()
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)
    assert not res.ok
    assert any("source_id mismatch" in e for e in res.errors)


def test_source_type_mismatch_rejected(session: Session, tmp_path: Path) -> None:
    aid, _ = _seed(session, mode="broken")
    payload = _payload(article_id=aid)
    payload["sources"][0]["source_type"] = "official_help"
    res = _run(session, _write(tmp_path, payload), article_id=aid)
    assert not res.ok
    assert any("source_type mismatch" in e for e in res.errors)


def test_fact_value_content_mismatch_rejected(
    session: Session, tmp_path: Path
) -> None:
    aid, _ = _seed(session, mode="broken")
    payload = _payload(article_id=aid)
    payload["tools"][0]["facts"]["pricing_summary"]["value"] = "TOTALLY DIFFERENT"
    res = _run(session, _write(tmp_path, payload), article_id=aid)
    assert not res.ok
    assert any("fact_value differs" in e for e in res.errors)


def test_json_article_id_mismatch_rejected(
    session: Session, tmp_path: Path
) -> None:
    aid, _ = _seed(session, mode="broken")
    res = _run(
        session, _write(tmp_path, _payload(article_id=aid + 999)), article_id=aid
    )
    assert not res.ok
    assert any("does not match --article-id" in e for e in res.errors)


def test_article_not_found_rejected(session: Session, tmp_path: Path) -> None:
    res = _run(session, _write(tmp_path, _payload(article_id=999)), article_id=999)
    assert not res.ok
    assert any("Article 999 not found" in e for e in res.errors)


def test_preflight_article_status_mismatch_rejected(
    session: Session, tmp_path: Path
) -> None:
    aid, _ = _seed(session, mode="broken", article_status="drafting")
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)
    assert not res.ok
    assert any("Article status is 'drafting'" in e for e in res.errors)


def test_preflight_article_body_present_rejected(
    session: Session, tmp_path: Path
) -> None:
    aid, _ = _seed(session, mode="broken")
    art = session.get(Article, aid)
    art.body = "draft body"
    session.commit()
    res = _run(session, _write(tmp_path, _payload(article_id=aid)), article_id=aid)
    assert not res.ok
    assert any("Article.body is not None" in e for e in res.errors)


# -- apply --------------------------------------------------------


def test_apply_updates_timestamps_only(session: Session, tmp_path: Path) -> None:
    aid, src = _seed(session, mode="broken")
    before = {(r["kind"], r["id"]): r for r in _rows(session, aid)}

    res = _run(
        session, _write(tmp_path, _payload(article_id=aid)), article_id=aid,
        apply=True,
    )
    assert res.ok and res.applied
    assert res.total_would_update == 9

    after = {(r["kind"], r["id"]): r for r in _rows(session, aid)}
    assert set(before) == set(after)
    for key, b in before.items():
        a = after[key]
        assert a["checked_at"] != b["checked_at"]
        for field in (
            "fact_value", "value_status", "source_id", "source_url",
            "source_type", "title", "subject_ref", "fact_key",
        ):
            assert a[field] == b[field], (key, field)

    assert session.get(Source, src["s_prod"].id).checked_at == to_storage_utc(PROD_JST)
    assert session.get(Source, src["s_price"].id).checked_at == to_storage_utc(PRICE_JST)


def test_apply_rolls_back_on_commit_failure(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid, _ = _seed(session, mode="broken")
    before = _snapshot(session, aid)

    def boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError):
        _run(
            session, _write(tmp_path, _payload(article_id=aid)), article_id=aid,
            apply=True,
        )
    monkeypatch.undo()
    assert _snapshot(session, aid) == before


def test_rerun_after_apply_is_noop(session: Session, tmp_path: Path) -> None:
    aid, _ = _seed(session, mode="broken")
    path = _write(tmp_path, _payload(article_id=aid))

    r1 = _run(session, path, article_id=aid, apply=True)
    assert r1.applied and r1.total_would_update == 9

    r2 = _run(session, path, article_id=aid, apply=True)
    assert r2.ok
    assert r2.total_would_update == 0
    assert r2.source_already_correct == 2 and r2.fact_already_correct == 7


# -- CLI arg parsing --------------------------------------------


def test_neither_flag_means_dry_run() -> None:
    args = cli._parse_args(["--article-id", "1", "--file", "x.json"])
    assert args.apply is False and args.dry_run is False


def test_dry_run_and_apply_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli._parse_args(
            ["--article-id", "1", "--file", "x.json", "--dry-run", "--apply"]
        )


def test_file_not_found_returns_bad_input(capsys: pytest.CaptureFixture) -> None:
    rc = cli.main(["--article-id", "1", "--file", "does-not-exist.json"])
    assert rc == cli.EXIT_BAD_INPUT


def test_no_external_imports() -> None:
    src = Path("scripts/repair_article_fact_timestamps.py").read_text(
        encoding="utf-8"
    ).lower()
    for token in (
        "import requests", "from requests", "import httpx", "import urllib.request",
        "urllib.request", "http.client",
    ):
        assert token not in src
