"""C11: 汎用の tracking URL の登録と、program 単位の host の許可 (2026-10-02)。

Make 専用の ``scripts/onboard_make_affiliate.py`` の安全な作りを、全 provider / program に
広げたもの。

- 登録 (``onboard``): 人が ASP の画面で取った tracking URL を、**ローカル DB の
  ``affiliate_programs.tracking_url`` にだけ** 入れる。既定は PLAN。``--execute`` のときだけ書く。
  link target・記事の中の置き換え・WordPress への反映 (projection) は **しない**。
- 許可 (``approve_host`` / ``revoke_host``): 登録した URL の host を、その program だけに許す
  記録を足す (``app/affiliate/program_host_approvals.py``)。既定は PLAN。
- URL の全体・query の値・token は返さない・出さない。出すのは scheme・host・query の **名前**・
  長さ・SHA-256 の先頭だけ。例外の文言にも URL を入れない。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.affiliate import program_host_approvals as approvals
from app.affiliate.destination_policy import is_destination_approved
from app.affiliate.destination_safety import AffiliateDestinationError, validate_destination_url
from app.affiliate.schemas import AffiliateProgramUpdate
from app.models import AffiliateLinkTarget, AffiliateProgram
from app.models.enums import AffiliateProgramStatus
from app.revenue import affiliate_inventory as inv
from app.services.affiliate_inventory_service import (
    VERIFICATIONS_PATH,
    AffiliateInventoryService,
    InventoryError,
    _text,
)
from app.services.affiliate_program_service import AffiliateProgramService

FINGERPRINT_SHOWN = 16


class IntakeError(ValueError):
    """登録・許可の前提が満たされない。メッセージに URL を含めない。"""


@dataclass(frozen=True)
class UrlFacts:
    scheme: str
    host: str
    query_param_names: tuple[str, ...]
    path_present: bool
    length: int
    sha256_prefix: str


def url_sha256(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def inspect_url(raw: str) -> UrlFacts:
    """検証だけ (書き換えない)。だめなら URL を含まない理由で raise。"""

    if not isinstance(raw, str):
        raise IntakeError("tracking URL is empty")
    raw = raw.strip("\r\n")
    if "\\" in raw:
        raise IntakeError("tracking URL must not contain a backslash")
    try:
        facts = validate_destination_url(raw)
    except AffiliateDestinationError as exc:
        raise IntakeError(str(exc)) from None
    parts = urlsplit(raw)
    if parts.fragment:
        raise IntakeError("tracking URL must not contain a fragment")
    names = tuple(sorted({n for n, _ in parse_qsl(parts.query, keep_blank_values=True)}))
    return UrlFacts(scheme="https", host=facts.destination_host, query_param_names=names,
                    path_present=parts.path not in ("", "/"), length=len(raw),
                    sha256_prefix=url_sha256(raw)[:FINGERPRINT_SHOWN])  # fmt: skip


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return validate_destination_url(url).destination_host
    except AffiliateDestinationError:
        return "invalid"


class AffiliateTrackingIntakeService:
    def __init__(self, session: Session, *, approvals_path: Path | None = None,
                 verifications_path: Path | None = None) -> None:  # fmt: skip
        self._session = session
        self._approvals_path = approvals_path or approvals.DEFAULT_PATH
        self._verifications_path = verifications_path or VERIFICATIONS_PATH

    def partnership(self, program_id: int) -> tuple[str, str | None]:
        """(提供元で確かめた最新の提携の状態, その根拠)。記憶だけの記録は数えない。"""

        records = AffiliateInventoryService(
            self._session, capabilities={},
            verifications_path=self._verifications_path).verifications()  # fmt: skip
        provider, _reported = inv.split_by_evidence(records)
        merged = inv.merge_verifications(provider).get(program_id) or {}
        status = merged.get("fields", {}).get("status_at_provider", "unknown")
        prov = merged.get("provenance", {}).get("status_at_provider") or {}
        return status, prov.get("evidence_kind")

    def require_approved(self, program: AffiliateProgram) -> None:
        """登録 -> 審査 -> 承認 -> tracking の順番。承認を提供元で確かめるまで先に進まない。"""

        status, _evidence = self.partnership(program.id)
        if status not in ("approved", "active"):
            raise IntakeError(
                f"program {program.id} partnership status is {status!r} (provider-verified); "
                "tracking intake needs approved / active recorded from the provider dashboard "
                "or email (verify --evidence provider_dashboard --status approved)")

    # -- 共通 --------------------------------------------------------------------------------
    def program(self, program_id: int, *, expect_name: str,
                expect_provider: str) -> AffiliateProgram:  # fmt: skip
        """program の存在と identity (名前・provider) を確かめる。URL を求める前に呼ぶ。"""

        program = self._session.get(AffiliateProgram, program_id)
        if program is None:
            raise IntakeError(f"program {program_id} does not exist")
        if program.name != expect_name or (program.provider or "") != expect_provider:
            raise IntakeError(f"program {program_id} is {program.name!r} / provider "
                              f"{program.provider!r}, not {expect_name!r} / {expect_provider!r}")
        return program

    def policy(self):
        return approvals.load_program_host_policy(self._approvals_path)

    def _authorized(self, program: AffiliateProgram, host: str | None) -> bool:
        return bool(host) and is_destination_approved(
            program_id=program.id, program_name=program.name, provider=program.provider,
            destination_host=host, program_policy=self.policy())  # fmt: skip

    # -- tracking URL の登録 ------------------------------------------------------------------
    def plan_onboard(self, program_id: int, *, expect_name: str, expect_provider: str,
                     raw_url: str) -> dict:  # fmt: skip
        program = self.program(program_id, expect_name=expect_name,
                               expect_provider=expect_provider)
        self.require_approved(program)
        facts = inspect_url(raw_url)
        url = raw_url.strip("\r\n")
        if str(program.status) != AffiliateProgramStatus.ACTIVE.value:
            raise IntakeError(f"program {program_id} catalog status is {program.status!r}, not "
                              "active (update the catalog status first, separately)")
        others = [p.id for p in self._session.scalars(select(AffiliateProgram))
                  if p.id != program.id and p.tracking_url
                  and url_sha256(p.tracking_url) == url_sha256(url)]  # fmt: skip
        if others:
            raise IntakeError(f"the same tracking URL is already registered on program(s) "
                              f"{others} (one tracking URL belongs to one program)")
        targets = [t.id for t in self._session.scalars(select(AffiliateLinkTarget))
                   if url_sha256(t.destination_url) == url_sha256(url)
                   and t.affiliate_program_id != program.id]  # fmt: skip
        if targets:
            raise IntakeError(f"the same URL is the destination of link target(s) {targets} of "
                              "another program")
        if program.tracking_url and program.tracking_url == url:
            action = "already_registered"
        elif program.tracking_url:
            raise IntakeError(f"program {program_id} already has a different tracking URL "
                              f"(sha256 {url_sha256(program.tracking_url)[:FINGERPRINT_SHOWN]}); "
                              "replacing it is a separate operation and is refused here")
        else:
            action = "would_register_tracking_url"
        landing = _host_of(program.landing_page_url)
        self._session.rollback()
        return {
            "program_id": program.id, "program_name": program.name, "provider": program.provider,
            "url": asdict(facts), "action": action,
            "host_authorized_for_program": self._authorized(program, facts.host),
            "catalog_landing_host": landing,
            "landing_host_matches": landing == facts.host if landing else None,
            "note": ("a catalog landing host is not an authorization; registering a tracking "
                     "URL creates no link target, changes no article and pushes nothing to "
                     "WordPress"),
        }  # fmt: skip

    def execute_onboard(self, program_id: int, *, expect_name: str, expect_provider: str,
                        raw_url: str) -> dict:  # fmt: skip
        plan = self.plan_onboard(program_id, expect_name=expect_name,
                                 expect_provider=expect_provider, raw_url=raw_url)
        if plan["action"] == "would_register_tracking_url":
            AffiliateProgramService(self._session).update_program(
                program_id, AffiliateProgramUpdate(tracking_url=raw_url.strip("\r\n")))
            plan = {**plan, "action": "registered_tracking_url"}
        return {**plan, "executed": True}

    # -- host の許可 -------------------------------------------------------------------------
    def _host_record(self, action: str, program_id: int, *, expect_name: str,
                     expect_provider: str, host: str, source: str, decided_by: str,
                     observed_at: str,
                     now: datetime | None = None) -> tuple[dict, AffiliateProgram]:  # fmt: skip
        now = now or datetime.now(UTC)
        program = self.program(program_id, expect_name=expect_name,
                               expect_provider=expect_provider)
        try:
            normalized = approvals.approvable_host(host)
            src = _text(source, "source")
        except (approvals.HostApprovalError, InventoryError) as exc:
            raise IntakeError(str(exc)) from None
        by = (decided_by or "").strip()
        if not by or "@" in by or len(by) > 64:
            raise IntakeError("decided_by is a short name (no email)")
        try:
            seen = datetime.fromisoformat(observed_at)
        except (TypeError, ValueError):
            raise IntakeError("observed_at must be an ISO time with a timezone") from None
        if seen.tzinfo is None or seen > now:
            raise IntakeError("observed_at needs a timezone and cannot be in the future")
        record = {"action": action, "program_id": program.id, "program_name": program.name,
                  "provider": program.provider or "", "host": normalized, "decided_by": by,
                  "observed_at": seen.astimezone(UTC).isoformat(timespec="seconds"),
                  "entered_at": now.astimezone(UTC).isoformat(timespec="seconds"),
                  "source": src}  # fmt: skip
        return record, program

    def plan_host(self, action: str, program_id: int, **kw) -> dict:
        if action not in approvals.ACTIONS:
            raise IntakeError(f"unknown action {action!r}")
        record, program = self._host_record(action, program_id, **kw)
        registered = _host_of(program.tracking_url)
        current = (program.name, program.provider or "", record["host"]) in (
            self.policy().get(program.id) or ())
        self._session.rollback()
        if action == "approve":
            self.require_approved(program)
            if registered is None:
                raise IntakeError(f"program {program_id} has no tracking URL; register it first "
                                  "(onboard), then approve its host")
            if registered != record["host"]:
                raise IntakeError(f"host {record['host']!r} is not the host of the registered "
                                  f"tracking URL ({registered!r}); only that host can be approved")
            state = "already_approved" if current else "would_approve"
        else:
            state = "would_revoke" if current else "not_approved"
        return {"action": state, "record": record, "registered_tracking_host": registered,
                "file": str(self._approvals_path)}

    def execute_host(self, action: str, program_id: int, **kw) -> dict:
        plan = self.plan_host(action, program_id, **kw)
        if plan["action"] in ("would_approve", "would_revoke"):
            written = approvals.append_record(plan["record"], self._approvals_path)
            return {**plan, "action": plan["action"].replace("would_", "") + "d",
                    "record": written, "executed": True,
                    "next": "review the git diff of the approvals file and commit it"}  # fmt: skip
        return {**plan, "executed": True}

    # -- 状態 (読むだけ) -----------------------------------------------------------------------
    def status(self, program_id: int | None = None) -> list[dict]:
        rows = []
        policy = self.policy()
        programs = list(self._session.scalars(
            select(AffiliateProgram).order_by(AffiliateProgram.id)))  # fmt: skip
        for p in programs:
            if program_id is not None and p.id != program_id:
                continue
            host = _host_of(p.tracking_url)
            authorized = bool(host and host != "invalid") and is_destination_approved(
                program_id=p.id, program_name=p.name, provider=p.provider,
                destination_host=host, program_policy=policy)  # fmt: skip
            rule = None
            if authorized:
                rule = ("program" if (p.name, p.provider or "", host) in (policy.get(p.id) or ())
                        else "provider")
            rows.append({
                "program_id": p.id, "program_name": p.name, "provider": p.provider,
                "catalog_status": str(p.status), "tracking_url": "present" if p.tracking_url
                else "missing", "tracking_host": host,
                "tracking_sha256_prefix": url_sha256(p.tracking_url)[:FINGERPRINT_SHOWN]
                if p.tracking_url else None,
                "host_authorized": authorized, "authorization_rule": rule,
                "program_approved_hosts": sorted(e[2] for e in policy.get(p.id) or ()),
            })  # fmt: skip
        self._session.rollback()
        return rows


__all__ = ["AffiliateTrackingIntakeService", "IntakeError", "UrlFacts", "inspect_url",
           "url_sha256"]
