"""C11: 汎用の tracking URL の登録と、program 単位の host の許可 (2026-10-02)。

**URL・host・案件はすべて合成の fixture** (``*.example.test``)。本物の ASP の値ではない。
外には何も送らない (socket を塞いで確かめる)。
"""

from __future__ import annotations

import io
import json
import socket

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.affiliate import program_host_approvals as approvals
from app.affiliate.destination_policy import (
    AGGREGATE_PROVIDER_LABELS,
    DEFAULT_DESTINATION_HOST_POLICY,
    is_destination_approved,
)
from app.exceptions import AffiliateLinkTargetError
from app.models import (
    AffiliateLinkTarget,
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    ArticleLinkSubstitutionMapping,
)
from app.services.affiliate_link_target_service import AffiliateLinkTargetService
from app.services.affiliate_tracking_intake_service import (
    AffiliateTrackingIntakeService,
    IntakeError,
    inspect_url,
)

SECRET = "SECRETCODE987"
URL_A = f"https://track.vendor-a.example.test/c/12345?aff={SECRET}&sub=x"
URL_B = f"https://track.vendor-b.example.test/r?ref={SECRET}B"
OBSERVED = "2026-10-01T23:00:00+09:00"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _seed(session, *programs):
    for name, provider in programs:
        session.add(AffiliateProgram(name=name, provider=provider, status="active",
                                     landing_page_url=f"https://www.{name.lower()}.example.test/"))
    session.add(Article(title="t", slug="a1", keyword_id=None, body="# body\n"))
    session.commit()


def _record_status(session, tmp_path, pid, status="approved", evidence="provider_dashboard"):
    from app.services.affiliate_inventory_service import AffiliateInventoryService

    AffiliateInventoryService(session, capabilities={},
                              verifications_path=tmp_path / "v.jsonl").verify(
        program_id=pid, source="provider dashboard", verified_by="human",
        observed_at=OBSERVED, fields={"status_at_provider": status}, evidence_kind=evidence,
        execute=True)  # fmt: skip


def _svc(session, tmp_path, *, approve=True):
    """既定: すべての案件の承認を提供元の画面で確かめた記録を置く (順番の関門を通す)。"""

    if approve:
        for program in session.scalars(select(AffiliateProgram)):
            _record_status(session, tmp_path, program.id)
    return AffiliateTrackingIntakeService(session, approvals_path=tmp_path / "approvals.json",
                                          verifications_path=tmp_path / "v.jsonl")


def _host_kw(name, provider, host, **extra):
    return {"expect_name": name, "expect_provider": provider, "host": host,
            "source": "provider dashboard", "decided_by": "human", "observed_at": OBSERVED,
            **extra}  # fmt: skip


# -- tracking URL の登録 ---------------------------------------------------------------------
def test_onboard_plans_by_default_and_writes_only_the_tracking_url(session, tmp_path) -> None:
    _seed(session, ("Alpha", "direct"))
    svc = _svc(session, tmp_path)
    plan = svc.plan_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    assert plan["action"] == "would_register_tracking_url"
    assert session.get(AffiliateProgram, 1).tracking_url is None
    assert plan["host_authorized_for_program"] is False  # landing host でも許可にならない
    assert plan["landing_host_matches"] is False
    done = svc.execute_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    assert done["action"] == "registered_tracking_url"
    assert session.get(AffiliateProgram, 1).tracking_url == URL_A  # 入力そのまま (書き換えない)
    for model in (AffiliateLinkTarget, ArticleLinkSubstitutionMapping):
        assert session.scalar(select(func.count()).select_from(model)) == 0
    assert not (tmp_path / "approvals.json").exists()  # 登録は許可を作らない
    again = svc.execute_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    assert again["action"] == "already_registered"


def test_the_plan_never_contains_the_url_path_or_query_values(session, tmp_path) -> None:
    _seed(session, ("Alpha", "direct"))
    plan = _svc(session, tmp_path).plan_onboard(1, expect_name="Alpha", expect_provider="direct",
                                                raw_url=URL_A)  # fmt: skip
    text = json.dumps(plan)
    assert SECRET not in text and "/c/12345" not in text and "https://" not in text
    assert list(plan["url"]["query_param_names"]) == ["aff", "sub"]  # 名前だけ
    assert len(plan["url"]["sha256_prefix"]) == 16
    assert plan["url"]["host"] == "track.vendor-a.example.test"


@pytest.mark.parametrize("bad", [
    "http://track.vendor-a.example.test/x", "https://user:pw@track.vendor-a.example.test/",
    "https://track.vendor-a.example.test:8443/", "https://track.vendor-a.example.test/x#frag",
    "https://track.vendor-a.example.test\\@evil.example.test/", "https://bizfluxlab.com/x",
    "javascript:alert(1)", "",
])  # fmt: skip
def test_unsafe_urls_are_refused_without_echoing_them(bad) -> None:
    with pytest.raises(IntakeError) as exc:
        inspect_url(bad)
    if bad:
        assert bad not in str(exc.value)


def test_identity_duplicates_and_replacement_are_refused(session, tmp_path) -> None:
    _seed(session, ("Alpha", "direct"), ("Beta", "direct"))
    svc = _svc(session, tmp_path)
    with pytest.raises(IntakeError, match="not 'Alpha' / 'Impact'"):
        svc.plan_onboard(1, expect_name="Alpha", expect_provider="Impact", raw_url=URL_A)
    svc.execute_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    with pytest.raises(IntakeError, match="already registered on program"):
        svc.plan_onboard(2, expect_name="Beta", expect_provider="direct", raw_url=URL_A)
    with pytest.raises(IntakeError, match="already has a different tracking URL") as exc:
        svc.plan_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_B)
    assert SECRET not in str(exc.value)
    assert session.get(AffiliateProgram, 1).tracking_url == URL_A


@pytest.mark.parametrize("status,evidence", [
    (None, None), ("not_applied", "provider_dashboard"), ("applied", "provider_dashboard"),
    ("pending", "provider_email"), ("rejected", "provider_dashboard"),
    ("approved", "human_recollection"),
])  # fmt: skip
def test_tracking_intake_waits_for_a_provider_verified_approval(session, tmp_path, status,
                                                                evidence) -> None:  # fmt: skip
    _seed(session, ("Alpha", "direct"))
    if status:
        _record_status(session, tmp_path, 1, status, evidence)
    svc = _svc(session, tmp_path, approve=False)
    with pytest.raises(IntakeError, match="partnership status"):
        svc.plan_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    session.get(AffiliateProgram, 1).tracking_url = URL_A
    session.commit()
    with pytest.raises(IntakeError, match="partnership status"):
        svc.plan_host("approve", 1, **_host_kw("Alpha", "direct", "track.vendor-a.example.test"))
    # 記憶の「承認」は、提供元で確かめた状態としては数えない
    assert svc.partnership(1)[0] == ("unknown" if evidence in (None, "human_recollection")
                                     else status)


def test_a_later_rejection_closes_the_gate_again(session, tmp_path) -> None:
    _seed(session, ("Alpha", "direct"))
    svc = _svc(session, tmp_path)
    assert svc.plan_onboard(1, expect_name="Alpha", expect_provider="direct",
                            raw_url=URL_A)["action"] == "would_register_tracking_url"
    _record_status(session, tmp_path, 1, "rejected")
    with pytest.raises(IntakeError, match="'rejected'"):
        svc.plan_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)


def test_inactive_programs_cannot_take_a_tracking_url(session, tmp_path) -> None:
    session.add(AffiliateProgram(name="Paused", provider="direct", status="paused"))
    session.commit()
    with pytest.raises(IntakeError, match="not active"):
        _svc(session, tmp_path).plan_onboard(1, expect_name="Paused", expect_provider="direct",
                                             raw_url=URL_A)  # fmt: skip


# -- program 単位の host の許可 ----------------------------------------------------------------
def test_host_approval_plans_first_and_binds_to_the_registered_host(session, tmp_path) -> None:
    _seed(session, ("Alpha", "direct"))
    svc = _svc(session, tmp_path)
    with pytest.raises(IntakeError, match="no tracking URL"):
        svc.plan_host("approve", 1, **_host_kw("Alpha", "direct", "track.vendor-a.example.test"))
    svc.execute_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    # catalog の landing page の host は、tracking URL の host でなければ許可できない
    with pytest.raises(IntakeError, match="only that host can be approved"):
        svc.plan_host("approve", 1, **_host_kw("Alpha", "direct", "www.alpha.example.test"))
    plan = svc.plan_host("approve", 1, **_host_kw("Alpha", "direct", "TRACK.vendor-a.example.test"))
    assert plan["action"] == "would_approve"
    assert plan["record"]["host"] == "track.vendor-a.example.test"
    assert not (tmp_path / "approvals.json").exists()
    done = svc.execute_host("approve", 1, **_host_kw("Alpha", "direct",
                                                     "track.vendor-a.example.test"))
    assert done["action"] == "approved" and done["record"]["id"] == 1
    assert svc.status(1)[0]["host_authorized"] is True
    assert svc.status(1)[0]["authorization_rule"] == "program"
    text = (tmp_path / "approvals.json").read_text("utf-8")
    assert SECRET not in text and "https://" not in text  # host と識別子だけ


def test_direct_programs_are_isolated_from_each_other(session, tmp_path) -> None:
    _seed(session, ("Alpha", "direct"), ("Beta", "direct"))
    svc = _svc(session, tmp_path)
    svc.execute_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    svc.execute_host("approve", 1, **_host_kw("Alpha", "direct", "track.vendor-a.example.test"))
    policy = svc.policy()
    host = "track.vendor-a.example.test"
    assert is_destination_approved(program_id=1, program_name="Alpha", provider="direct",
                                   destination_host=host, program_policy=policy)
    # 同じ direct でも program B は program A の host を使えない
    assert not is_destination_approved(program_id=2, program_name="Beta", provider="direct",
                                       destination_host=host, program_policy=policy)
    # 名前や provider が変われば許可は効かない
    assert not is_destination_approved(program_id=1, program_name="Alpha2", provider="direct",
                                       destination_host=host, program_policy=policy)
    assert not is_destination_approved(program_id=1, program_name="Alpha", provider="Impact",
                                       destination_host=host, program_policy=policy)
    # 知らない host・部分一致は拒否
    for other in ("evil.example.test", "x.track.vendor-a.example.test", "vendor-a.example.test"):
        assert not is_destination_approved(program_id=1, program_name="Alpha", provider="direct",
                                           destination_host=other, program_policy=policy)


def test_aggregate_labels_never_get_a_provider_level_rule() -> None:
    assert not AGGREGATE_PROVIDER_LABELS & set(DEFAULT_DESTINATION_HOST_POLICY)
    wide = {"direct": frozenset({"x.example.test"})}
    assert not is_destination_approved(program_id=9, program_name="Any", provider="direct",
                                       destination_host="x.example.test", policy=wide)


def test_make_keeps_its_provider_level_rule() -> None:
    assert is_destination_approved(program_id=1, program_name="Make", provider="make",
                                   destination_host="www.make.com")
    assert not is_destination_approved(program_id=1, program_name="Make", provider="make",
                                       destination_host="make.com")


def test_link_target_creation_honors_program_approvals_only(session, tmp_path) -> None:
    _seed(session, ("Alpha", "direct"), ("Beta", "direct"))
    for pid in (1, 2):
        session.add(ArticleAffiliateProgram(article_id=1, affiliate_program_id=pid))
    session.commit()
    svc = _svc(session, tmp_path)
    svc.execute_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    session.get(AffiliateProgram, 2).tracking_url = "https://track.vendor-a.example.test/other"
    session.commit()
    empty = AffiliateLinkTargetService(session, program_host_policy={})
    with pytest.raises(AffiliateLinkTargetError, match="not independently approved"):
        empty.create_target(article_id=1, affiliate_program_id=1)
    svc.execute_host("approve", 1, **_host_kw("Alpha", "direct", "track.vendor-a.example.test"))
    service = AffiliateLinkTargetService(session, program_host_policy=svc.policy())
    assert service.create_target(article_id=1, affiliate_program_id=1).destination_host == (
        "track.vendor-a.example.test")
    with pytest.raises(AffiliateLinkTargetError, match="not independently approved"):
        service.create_target(article_id=1, affiliate_program_id=2)  # B は A の host を使えない


@pytest.mark.parametrize("host", ["10.0.0.1", "localhost", "*.vendor.example.test",
                                  "bizfluxlab.com", "example.com", "https://x.example.test/"])
def test_unsafe_hosts_cannot_be_approved(host) -> None:
    with pytest.raises(approvals.HostApprovalError):
        approvals.approvable_host(host)


def test_revoke_and_append_only_file(session, tmp_path) -> None:
    _seed(session, ("Alpha", "direct"))
    svc = _svc(session, tmp_path)
    svc.execute_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    host = "track.vendor-a.example.test"
    svc.execute_host("approve", 1, **_host_kw("Alpha", "direct", host))
    assert svc.plan_host("approve", 1, **_host_kw("Alpha", "direct", host))["action"] == (
        "already_approved")
    svc.execute_host("revoke", 1, **_host_kw("Alpha", "direct", host))
    assert svc.status(1)[0]["host_authorized"] is False
    records = approvals.load_records(tmp_path / "approvals.json")
    assert [r["action"] for r in records] == ["approve", "revoke"]
    path = tmp_path / "approvals.json"
    path.write_text(path.read_text("utf-8").replace('"id": 2', '"id": 7'), encoding="utf-8")
    with pytest.raises(approvals.HostApprovalError, match="do not edit"):
        approvals.load_records(path)


@pytest.mark.parametrize("kw,match", [
    ({"source": "https://dashboard.example.test/x"}, "URL"),
    ({"decided_by": "someone@example.test"}, "no email"),
    ({"observed_at": "2026-10-01T23:00:00"}, "timezone"),
    ({"observed_at": "2099-01-01T00:00:00+09:00"}, "future"),
])  # fmt: skip
def test_host_approval_refuses_bad_evidence(session, tmp_path, kw, match) -> None:
    _seed(session, ("Alpha", "direct"))
    svc = _svc(session, tmp_path)
    svc.execute_onboard(1, expect_name="Alpha", expect_provider="direct", raw_url=URL_A)
    with pytest.raises(IntakeError, match=match):
        svc.plan_host("approve", 1, **_host_kw("Alpha", "direct", "track.vendor-a.example.test",
                                               **kw))


def test_the_shipped_approvals_file_is_empty_and_valid() -> None:
    assert approvals.load_records() == []  # 本物の P1 の host はまだ誰も許可していない


# -- CLI ----------------------------------------------------------------------------------
def test_the_cli_reads_the_url_hidden_and_never_prints_it(engine, tmp_path, capsys) -> None:
    from scripts.affiliate_tracking_intake import main

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        _seed(s, ("Alpha", "direct"))
        _record_status(s, tmp_path, 1)
    ap = tmp_path / "approvals.json"
    vp = tmp_path / "v.jsonl"
    base = ["onboard", "--program-id", "1", "--expect-name", "Alpha", "--expect-provider",
            "direct"]
    prompts = []
    assert main(base, session_factory=factory, approvals_path=ap, verifications_path=vp,
                prompt=lambda text: prompts.append(text) or URL_A) == 0  # fmt: skip
    assert prompts and "hidden" in prompts[0]
    with factory() as s:
        assert s.get(AffiliateProgram, 1).tracking_url is None  # PLAN
    assert main([*base, "--url-stdin", "--execute"], session_factory=factory, approvals_path=ap,
                verifications_path=vp, stdin=io.StringIO(URL_A + "\n")) == 0  # fmt: skip
    with factory() as s:
        assert s.get(AffiliateProgram, 1).tracking_url == URL_A
    # identity が違えば URL を求める前に止まる
    asked = []
    assert main(["onboard", "--program-id", "1", "--expect-name", "Wrong", "--expect-provider",
                 "direct"], session_factory=factory, approvals_path=ap, verifications_path=vp,
                prompt=lambda t: asked.append(t) or URL_A) == 2  # fmt: skip
    assert asked == []
    approve = ["approve-host", "--program-id", "1", "--expect-name", "Alpha", "--expect-provider",
               "direct", "--host", "track.vendor-a.example.test", "--source", "dashboard",
               "--by", "human", "--observed-at", OBSERVED]
    assert main(approve, session_factory=factory, approvals_path=ap,
                verifications_path=vp) == 0  # fmt: skip
    assert not ap.exists()
    assert main([*approve, "--execute"], session_factory=factory, approvals_path=ap,
                verifications_path=vp) == 0  # fmt: skip
    assert main(["status", "--program-id", "1"], session_factory=factory, approvals_path=ap,
                verifications_path=vp) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert SECRET not in out and "https://" not in out and "/c/12345" not in out
    assert "PLAN only" in out and '"host_authorized": true' in out


def test_the_cli_withholds_unexpected_error_messages(engine, tmp_path, capsys,
                                                     monkeypatch) -> None:  # fmt: skip
    from scripts.affiliate_tracking_intake import main

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        _seed(s, ("Alpha", "direct"))
        _record_status(s, tmp_path, 1)

    def boom(*_a, **_k):
        raise RuntimeError(f"db failure with parameter {URL_A}")

    monkeypatch.setattr(AffiliateTrackingIntakeService, "execute_onboard", boom)
    code = main(["onboard", "--program-id", "1", "--expect-name", "Alpha", "--expect-provider",
                 "direct", "--execute"], session_factory=factory, approvals_path=tmp_path / "a",
                verifications_path=tmp_path / "v.jsonl", prompt=lambda _t: URL_A)  # fmt: skip
    out = capsys.readouterr().out
    assert code == 1 and SECRET not in out and "message withheld" in out


def test_no_url_argument_exists() -> None:
    from scripts.affiliate_tracking_intake import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(["onboard", "--program-id", "1", "--expect-name", "A",
                              "--expect-provider", "direct", "--url", URL_A])
