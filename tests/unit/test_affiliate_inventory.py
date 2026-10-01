"""C11 前半: ASP・提携案件・リンクの棚卸し (2026-10-01)。

**このテストの案件・記事・リンク・クリックはすべて合成の fixture。** 本物の ASP の値ではない。
棚卸しは読むだけで、無いもの・分からないものを 0 や false に変えないことを確かめる。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.models import AffiliateProgram
from app.revenue import affiliate_inventory as inv
from app.services.affiliate_inventory_service import (
    AffiliateInventoryService,
    InventoryError,
    load_capabilities,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CAPS = {"capability_keys": {"subid_in_tracking_url": "", "click_reference_in_commission_report": "",
                            "commission_import_api": "", "commission_report_manual": "",
                            "cookie_window_days": ""},
        "providers": {"apinet": {"kind": "network", "capabilities": {
            "commission_import_api": {"value": True, "evidence": "fixture"},
            "click_reference_in_commission_report": {"value": False,
                                                     "evidence": "fixture"}}}}}  # fmt: skip


def _program(pid, provider="net", status="active", tracking=None, **extra):
    return {"id": pid, "name": f"P{pid}", "provider": provider, "category": None,
            "commission_type": extra.get("commission_type"),
            "commission_value": extra.get("commission_value"), "currency": extra.get("currency"),
            "landing_page_url": None, "tracking_url": tracking, "status": status}  # fmt: skip


def _article(aid, mode="affiliate", status="published"):
    return {"id": aid, "title": f"A{aid}", "status": status, "monetization_mode": mode}


def _build(programs, *, article_programs=(), articles=(), targets=(), mappings=(), clicks=(),
           commissions=(), capabilities=CAPS, verifications=(), max_age=None):  # fmt: skip
    return inv.build(programs=list(programs), article_programs=list(article_programs),
                     articles=list(articles), targets=list(targets), mappings=list(mappings),
                     clicks=list(clicks), commissions=list(commissions),
                     capabilities=capabilities, verifications=list(verifications), now=NOW,
                     verification_max_age_days=max_age)  # fmt: skip


def _target(tid, aid, pid, token, dest="https://vendor.example/a", status="active"):
    return {"id": tid, "token": token, "article_id": aid, "affiliate_program_id": pid,
            "destination_url": dest, "status": status}


def _mapping(mid, aid, tid, status="active"):
    return {"id": mid, "article_id": aid, "affiliate_link_target_id": tid, "status": status}


def _row(report, pid):
    return next(r for r in report["programs"] if r["id"] == pid)


def test_missing_values_stay_missing_and_unknown_not_zero_or_false() -> None:
    report = _build([_program(1, provider=None)])
    row = _row(report, 1)
    assert row["provider"] == "unrecorded"
    assert row["provider_status"] == "unknown" and row["commission"] == "missing"
    assert row["subid_supported"] == "unknown"
    assert {"tracking_url", "commission_terms", "currency", "cookie_window_days",
            "provider_status_verified", "last_verified"} <= set(row["missing_fields"])
    caps = report["providers"]["unrecorded"]["capabilities"]
    assert set(caps.values()) == {"unknown"}
    # 成果が 0 件でも、成果を読めないことと区別する (提供元の能力は unknown のまま)
    assert row["commission_facts"] == 0 and row["attribution"] == "UNKNOWN"


def test_unknown_and_paused_program_status_are_reported() -> None:
    report = _build([_program(1, status="unknown"), _program(2, status="paused")],
                    article_programs=[{"article_id": 5, "affiliate_program_id": 2,
                                       "is_primary": True}],
                    articles=[_article(5), _article(6)])  # fmt: skip
    ops = report["operations"]
    assert ops["unknown_status_programs"] == [1] and ops["paused_or_ended_programs"] == [2]
    assert ops["articles_needing_replacement"] == [5]
    queue = {(i["program_id"], i["reason"]): i["priority"] for i in report["human_action_queue"]}
    assert queue[(1, "program status is unknown")] == "P2"
    assert queue[(2, "program is paused in the catalog")] == "P1"  # 公開記事を止めている


def test_active_program_without_tracking_url_is_a_gap_and_priority_follows_blocking() -> None:
    report = _build([_program(1), _program(2)],
                    article_programs=[{"article_id": 5, "affiliate_program_id": 1,
                                       "is_primary": True}],
                    articles=[_article(5)])  # fmt: skip
    assert report["operations"]["catalog_active_without_tracking_url"] == [1, 2]
    items = [(i["program_id"], i["priority"]) for i in report["human_action_queue"]
             if i["reason"] == "no tracking URL"]
    assert items == [(1, "P1"), (2, "P2")]
    assert report["coverage"][5]["state"] == "program_without_link"


def test_tracking_url_without_placement_and_inactive_placement() -> None:
    report = _build([_program(1, tracking="https://t.example/1"),
                     _program(2, status="ended", tracking="https://t.example/2")],
                    articles=[_article(5)], targets=[_target(10, 5, 2, "tok2")],
                    mappings=[_mapping(100, 5, 10)])  # fmt: skip
    ops = report["operations"]
    assert ops["tracking_url_without_placement"] == [1]
    assert ops["placement_with_inactive_program"] == [100]
    assert _row(report, 1)["next_action"].startswith("map the tracked link")
    # 記事の中の置き換えは手元の作業。ASP の画面でしか分からない作業とは分ける
    assert "net" in ops["human_asp_login_required"]  # 案件 2 の状態の確認


def test_content_without_monetization_path_separates_supporting_articles() -> None:
    report = _build([_program(1, tracking="https://t.example/1"), _program(2)],
                    article_programs=[{"article_id": 1, "affiliate_program_id": 1,
                                       "is_primary": True},
                                      {"article_id": 1, "affiliate_program_id": 2,
                                       "is_primary": False}],
                    articles=[_article(1), _article(2), _article(3, mode="supporting"),
                              _article(4, mode=None), _article(9, status="draft")],
                    targets=[_target(10, 1, 1, "tok1")],
                    mappings=[_mapping(100, 1, 10)])  # fmt: skip
    cov, ops = report["coverage"], report["operations"]
    assert 9 not in cov  # 公開記事だけ
    assert cov[1]["state"] == "linked" and cov[1]["assigned_programs_without_link"] == [2]
    assert ops["linked_articles_with_unlinked_programs"] == {1: [2]}
    assert ops["articles_without_monetization_path"] == [2, 4]
    assert ops["supporting_articles_without_program"] == [3]
    assert ops["articles_missing_monetization_mode"] == [4]
    assert cov[1]["placements"][0]["position"] == "external-link occurrence (ordinal)"


def test_never_verified_and_stale_verification() -> None:
    records = [{"id": 1, "program_id": 1, "verified_at": (NOW - timedelta(days=100)).isoformat(),
                "fields": {"status_at_provider": "approved"}},
               {"id": 2, "program_id": 2, "verified_at": (NOW - timedelta(days=3)).isoformat(),
                "fields": {"status_at_provider": "active"}}]  # fmt: skip
    programs = [_program(1), _program(2), _program(3)]
    report = _build(programs, verifications=records, max_age=90)
    ops = report["operations"]
    assert ops["never_verified_programs"] == [3] and ops["stale_verification_programs"] == [1]
    assert _row(report, 2)["provider_status"] == "active"
    # 期限を決めていなければ古さは判断しない (一度も確かめていないものだけ)
    assert _build(programs, verifications=records)["operations"][
        "stale_verification_programs"] == []  # fmt: skip


def test_latest_verification_supersedes_earlier_ones() -> None:
    records = [{"id": 1, "program_id": 1, "verified_at": "2026-09-01T00:00:00+00:00",
                "fields": {"status_at_provider": "applied"}},
               {"id": 2, "program_id": 1, "verified_at": "2026-09-20T00:00:00+00:00",
                "fields": {"status_at_provider": "approved"}}]  # fmt: skip
    assert inv.latest_verifications(records)[1]["id"] == 2


def test_duplicate_links_and_shared_tokens_are_detected() -> None:
    report = _build([_program(1, tracking="https://t.example/x"),
                     _program(2, tracking="https://t.example/x")],
                    articles=[_article(5)],
                    targets=[_target(10, 5, 1, "a", dest="https://same.example"),
                             _target(11, 5, 2, "b", dest="https://same.example")],
                    mappings=[_mapping(100, 5, 10), _mapping(101, 5, 10)])  # fmt: skip
    ops = report["operations"]
    assert ops["duplicate_destination_across_programs"] == [[1, 2]]
    assert ops["duplicate_tracking_url_across_programs"] == [[1, 2]]
    assert ops["token_shared_by_several_placements"] == [
        {"article_id": 5, "target_id": 10, "placements": 2}]


def test_attribution_classification() -> None:
    def cls(provider_caps, *, target, verification=None):
        caps = {"providers": {"x": {"capabilities": {k: {"value": v}
                                                     for k, v in provider_caps.items()}}}}
        return inv.attribution_class({"provider": "x"}, has_active_target=target,
                                     capabilities=caps, verification=verification)[0]

    full = {"subid_in_tracking_url": True, "click_reference_in_commission_report": True}
    assert cls(full, target=True) == "FULL"
    assert cls(full, target=False) == "UNKNOWN"  # リンクが無ければ FULL ではない
    assert cls({"commission_report_manual": True}, target=True) == "MANUAL"
    assert cls({"commission_report_manual": True, "commission_import_api": True},
               target=True) == "PARTIAL"
    assert cls({}, target=True) == "PARTIAL"
    none = {"subid_in_tracking_url": False, "click_reference_in_commission_report": False}
    assert cls(none, target=False) == "NONE"
    assert cls({}, target=False) == "UNKNOWN"
    # 人の確認が提供元の既定を上書きする
    assert cls({"click_reference_in_commission_report": True}, target=True,
               verification={"fields": {"subid_supported": True}}) == "FULL"


def test_unknown_capability_is_never_guessed_and_no_subid_is_designed() -> None:
    report = _build([_program(1, provider="apinet", tracking="https://t.example/1")],
                    articles=[_article(5)], targets=[_target(10, 5, 1, "tok")],
                    mappings=[_mapping(100, 5, 10)])  # fmt: skip
    row = _row(report, 1)
    assert row["subid_supported"] == "unknown" and row["attribution"] == "PARTIAL"
    assert report["subid_design"]["state"] == "not_applicable_yet"
    assert report["subid_design"]["providers_with_verified_subid"] == []
    p3 = [i for i in report["human_action_queue"] if i["priority"] == "P3"]
    assert [i["program_id"] for i in p3] == [1]


def test_the_shipped_capability_file_claims_nothing_without_evidence() -> None:
    caps = load_capabilities()
    for provider, entry in caps["providers"].items():
        for key, cap in entry["capabilities"].items():
            assert key in caps["capability_keys"], (provider, key)
            if cap["value"] != "unknown":
                assert cap["evidence"], (provider, key)
            else:
                assert cap["evidence"] is None, (provider, key)
    # SubID に対応していると確かめた提供元はまだ無い
    assert all(e["capabilities"]["subid_in_tracking_url"]["value"] == "unknown"
               for e in caps["providers"].values())


def test_report_never_contains_urls_tokens_or_secrets() -> None:
    report = _build([_program(1, tracking="https://t.example/secret-path?aff=XYZ123")],
                    articles=[_article(5)],
                    targets=[_target(10, 5, 1, "TOKENVALUE123", dest="https://dest.example/q")],
                    mappings=[_mapping(100, 5, 10)],
                    clicks=[{"token": "TOKENVALUE123"}, {"token": "ORPHANTOKEN999"}])  # fmt: skip
    text = json.dumps(report, default=str)
    for leaked in ("t.example", "dest.example", "XYZ123", "TOKENVALUE123", "ORPHANTOKEN999"):
        assert leaked not in text
    assert report["operations"]["clicks_with_unknown_token"] == {"tokens": 1, "clicks": 1}
    assert _row(report, 1)["clicks_all_time"] == 1


def test_manual_revenue_needs_a_migration_and_is_not_entered() -> None:
    """手入力の ASP 成果は、今の台帳の CHECK に入らない (migration が要る。今回はしない)。"""

    from app.models.n_track import MM_SUBJECT_KINDS
    from app.n_track import metrics as mm

    assert "affiliate_program" not in MM_SUBJECT_KINDS
    assert "affiliate_program" not in mm.CATALOG


# -- service / CLI (SQLite の合成データ) ---------------------------------------------------------
def _service(session, tmp_path):
    session.add_all([AffiliateProgram(name="Fixture A", provider="net", status="active"),
                     AffiliateProgram(name="Fixture B", provider=None, status="unknown")])
    session.commit()
    return AffiliateInventoryService(session, capabilities=CAPS,
                                     verifications_path=tmp_path / "v.jsonl")


def test_verify_plans_by_default_and_appends_only_with_execute(session, tmp_path) -> None:
    service = _service(session, tmp_path)
    kw = {"program_id": 1, "source": "provider dashboard", "verified_by": "human",
          "observed_at": "2026-10-01T10:00:00+09:00",
          "fields": {"status_at_provider": "approved", "cookie_window_days": 30}}
    plan = service.verify(**kw, now=NOW)
    assert plan["recorded"] is False and not (tmp_path / "v.jsonl").exists()
    done = service.verify(**kw, execute=True, now=NOW)
    assert done["recorded"] is True and done["record"]["provenance"] == "human_entry"
    assert len(service.verifications()) == 1
    row = _row(service.inventory(now=NOW), 1)
    assert row["provider_status"] == "approved" and row["verification"] == "verified"
    assert "cookie_window_days" not in row["missing_fields"]
    # DB には書かない
    assert session.get(AffiliateProgram, 1).status == "active"


@pytest.mark.parametrize("fields", [{}, {"subid_supported": "yes"}, {"cookie_window_days": -1},
                                    {"status_at_provider": "maybe"}, {"tracking_url": "https://x"},
                                    {"provider_program_id": "https://x.example/p"},
                                    {"provider_program_id": "api_key=abcdef0123456789"},
                                    ])  # fmt: skip
def test_verify_refuses_guesses_urls_and_unknown_fields(session, tmp_path, fields) -> None:
    service = _service(session, tmp_path)
    with pytest.raises(InventoryError):
        service.verify(program_id=1, source="provider dashboard", verified_by="human",
                       observed_at="2026-10-01T10:00:00+09:00", fields=fields, execute=True,
                       now=NOW)  # fmt: skip
    assert not (tmp_path / "v.jsonl").exists()


@pytest.mark.parametrize("source,by,observed", [
    ("https://dashboard.example/login", "human", "2026-10-01T10:00:00+09:00"),
    ("password: hunter2hunter2", "human", "2026-10-01T10:00:00+09:00"),
    ("provider dashboard", "someone@example.com", "2026-10-01T10:00:00+09:00"),
    ("provider dashboard", "human", "2026-10-01T10:00:00"),
    ("provider dashboard", "human", "2026-12-01T10:00:00+09:00"),
])  # fmt: skip
def test_verify_refuses_secrets_personal_data_and_bad_times(session, tmp_path, source, by,
                                                            observed) -> None:  # fmt: skip
    service = _service(session, tmp_path)
    with pytest.raises(InventoryError):
        service.verify(program_id=1, source=source, verified_by=by, observed_at=observed,
                       fields={"status_at_provider": "approved"}, execute=True, now=NOW)


def test_a_hand_edited_verification_file_is_refused(session, tmp_path) -> None:
    service = _service(session, tmp_path)
    (tmp_path / "v.jsonl").write_text(json.dumps({"schema": "affiliate-program-verification/1",
                                                  "id": 5}) + "\n", encoding="utf-8")
    with pytest.raises(InventoryError):
        service.verifications()


def test_the_cli_is_read_only_and_verify_plans_by_default(engine, tmp_path, capsys) -> None:
    from sqlalchemy.orm import sessionmaker

    from scripts.affiliate_inventory import main

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        s.add(AffiliateProgram(name="Fixture A", provider="net", status="active",
                               tracking_url="https://t.example/hidden"))
        s.commit()
    path = tmp_path / "v.jsonl"
    for command in ("providers", "programs", "coverage", "missing-links", "stale", "attribution",
                    "actions", "ops", "report"):
        assert main([command], session_factory=factory, now=NOW, verifications_path=path) == 0
    assert main(["program", "1"], session_factory=factory, now=NOW, verifications_path=path) == 0
    assert main(["program", "9"], session_factory=factory, now=NOW, verifications_path=path) == 2
    out = capsys.readouterr().out
    assert "t.example" not in out and "read-only" in out
    args = ["verify", "1", "--status", "approved", "--source", "provider dashboard", "--by",
            "human", "--observed-at", "2026-10-01T10:00:00+09:00"]
    assert main(args, session_factory=factory, now=NOW, verifications_path=path) == 0
    assert not path.exists()
    assert main(args + ["--execute"], session_factory=factory, now=NOW,
                verifications_path=path) == 0  # fmt: skip
    assert len(path.read_text("utf-8").splitlines()) == 1
