"""C11 前半: ASP・提携案件・リンクの棚卸しと、人の確認の記録 (2026-10-01)。

- 棚卸し (``inventory``): 手元の DB を **読むだけ** (書き込みは rollback)。外に問い合わせない。
- 人の確認 (``verify``): 人が ASP の画面で確かめた事実を、追記だけのファイル
  ``data/affiliate/program_verifications.jsonl`` (git 管理外) に足す。既定は PLAN。URL・秘密・
  個人の情報は入れない (トラッキング URL は今までどおり案件の記録に人が入れる)。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.affiliate.destination_safety import AffiliateDestinationError, normalize_host
from app.models import (
    AffiliateCommissionFact,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    ArticleLinkSubstitutionMapping,
)
from app.n_track import metrics as mm
from app.revenue import affiliate_inventory as inv
from app.social.note import safety

CAPABILITIES_PATH = (Path(__file__).resolve().parents[1] / "config"
                     / "affiliate_provider_capabilities.json")  # fmt: skip
VERIFICATIONS_PATH = Path("data/affiliate/program_verifications.jsonl")
VERIFICATION_SCHEMA = "affiliate-program-verification/1"
_PROGRAM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PROVIDER_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")


class InventoryError(ValueError):
    pass


def load_capabilities(path: Path | None = None) -> dict:
    return json.loads((path or CAPABILITIES_PATH).read_text(encoding="utf-8"))


def _rows(session: Session, model, columns: tuple[str, ...]) -> list[dict]:
    return [{c: getattr(r, c) for c in columns} for r in session.scalars(select(model))]


class AffiliateInventoryService:
    def __init__(self, session: Session, *, capabilities: dict | None = None,
                 verifications_path: Path | str = VERIFICATIONS_PATH,
                 approvals_path: Path | None = None) -> None:  # fmt: skip
        self._session = session
        self._approvals_path = approvals_path
        self._capabilities = capabilities if capabilities is not None else load_capabilities()
        self._verifications = Path(verifications_path)

    # -- 人の確認の記録 ------------------------------------------------------------------------
    def verifications(self) -> list[dict]:
        if not self._verifications.exists():
            return []
        out = []
        for number, line in enumerate(self._verifications.read_text("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("schema") != VERIFICATION_SCHEMA or record.get("id") != len(out) + 1:
                raise InventoryError(f"{self._verifications} line {number} is not the next "
                                     f"{VERIFICATION_SCHEMA} record (do not edit the file)")
            out.append(record)
        return out

    def verify(self, *, program_id: int, source: str, verified_by: str, observed_at: str,
               fields: dict, execute: bool = False, now: datetime | None = None) -> dict:
        """人が ASP の画面で確かめた事実を足す (既定は PLAN)。推測の値は入れない。"""

        now = now or datetime.now(UTC)
        program = self._session.get(AffiliateProgram, program_id)
        self._session.rollback()
        if program is None:
            raise InventoryError(f"program {program_id} does not exist")
        clean = _check_fields(fields)
        if not clean:
            raise InventoryError("give at least one verified field")
        by = (verified_by or "").strip()
        if not by or "@" in by:
            raise InventoryError("verified_by is a short name (no email)")
        src = _text(source, "source")
        try:
            seen = datetime.fromisoformat(observed_at)
        except (TypeError, ValueError):
            raise InventoryError("observed_at must be an ISO time with a timezone") from None
        if seen.tzinfo is None or seen > now:
            raise InventoryError("observed_at needs a timezone and cannot be in the future")
        records = self.verifications()
        record = {"schema": VERIFICATION_SCHEMA, "id": len(records) + 1, "program_id": program_id,
                  "provider": program.provider,
                  "verified_at": seen.astimezone(UTC).isoformat(timespec="seconds"),
                  "entered_at": now.astimezone(UTC).isoformat(timespec="seconds"),
                  "verified_by": by[:64], "source": src, "fields": clean,
                  "provenance": "human_entry"}  # fmt: skip
        if not execute:
            return {"recorded": False, "reason": "PLAN (re-run with --execute)", "record": record}
        self._verifications.parent.mkdir(parents=True, exist_ok=True)
        with self._verifications.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return {"recorded": True, "record": record}

    # -- 棚卸し (読むだけ) -------------------------------------------------------------------
    def inventory(self, *, now: datetime | None = None,
                  verification_max_age_days: int | None = None) -> dict:
        now = now or datetime.now(UTC)
        s = self._session
        data = {
            "programs": _rows(s, AffiliateProgram, ("id", "name", "provider", "category",
                                                    "commission_type", "commission_value",
                                                    "currency", "landing_page_url",
                                                    "tracking_url", "status")),
            "article_programs": _rows(s, ArticleAffiliateProgram,
                                      ("article_id", "affiliate_program_id", "is_primary")),
            "articles": _rows(s, Article, ("id", "title", "status", "monetization_mode")),
            "targets": _rows(s, AffiliateLinkTarget, ("id", "token", "article_id",
                                                      "affiliate_program_id", "destination_url",
                                                      "status")),
            "mappings": _rows(s, ArticleLinkSubstitutionMapping,
                              ("id", "article_id", "affiliate_link_target_id", "status")),
            "clicks": _rows(s, AffiliateOutboundClick, ("token",)),
            "commissions": _rows(s, AffiliateCommissionFact, ("affiliate_program_id",)),
        }  # fmt: skip
        s.rollback()
        for p in data["programs"]:
            p["status"] = getattr(p["status"], "value", p["status"])
        for key in ("targets", "mappings", "articles"):
            for row in data[key]:
                row["status"] = getattr(row["status"], "value", row["status"])
        report = inv.build(**data, capabilities=self._capabilities,
                           verifications=self.verifications(), now=now,
                           verification_max_age_days=verification_max_age_days,
                           tracking=self._tracking(data["programs"]))  # fmt: skip
        return report  # URL・token の中身は報告に入らない (有る / 無い と数だけ)


    def _tracking(self, programs: list[dict]) -> dict:
        """program ごとの tracking URL の host と、その program に許されているか。

        URL そのものは棚卸しに渡さない。
        """

        from app.affiliate.destination_policy import is_destination_approved
        from app.affiliate.destination_safety import validate_destination_url
        from app.affiliate.program_host_approvals import load_program_host_policy

        policy = load_program_host_policy(self._approvals_path)
        out = {}
        for p in programs:
            if not p.get("tracking_url"):
                continue
            try:
                host = validate_destination_url(p["tracking_url"]).destination_host
            except AffiliateDestinationError:
                out[p["id"]] = {"host": "invalid", "authorized": False, "rule": None}
                continue
            entries = policy.get(p["id"]) or ()
            program_rule = (p["name"], p.get("provider") or "", host) in entries
            authorized = is_destination_approved(
                program_id=p["id"], program_name=p["name"], provider=p.get("provider"),
                destination_host=host, program_policy=policy)  # fmt: skip
            out[p["id"]] = {"host": host, "authorized": authorized,
                            "rule": ("program" if program_rule else "provider")
                            if authorized else None}  # fmt: skip
        return out


def _text(value, field: str) -> str:
    text = (value or "").strip()
    if len(text) < 3:
        raise InventoryError(f"{field} is required (where it was seen)")
    if mm._PERSONAL.search(text) or mm._CREDENTIAL.search(text) or safety.sanitize(text)[1]:
        raise InventoryError(f"{field} contains a URL, personal data or a secret")
    return text[:200]


def _check_fields(fields: dict) -> dict:
    """人が ASP の画面で確かめた値だけを通す。分からない能力は "unknown" (false にしない)。
    書かなかった項目は記録しない (missing のまま)。URL・秘密は入らない。"""

    out = {}
    for key, value in (fields or {}).items():
        if value is None:
            continue
        if key == "status_at_provider":
            if value not in inv.PROVIDER_STATUSES:
                raise InventoryError(f"status_at_provider must be one of {inv.PROVIDER_STATUSES}")
        elif key in ("provider_program_id", "link_id"):
            if not _PROGRAM_ID.match(str(value)) or mm._CREDENTIAL.search(str(value)):
                raise InventoryError(f"{key} is a short id (no URL, no secret)")
            value = str(value)
        elif key in inv.TRISTATE_FIELDS:
            if not (value is True or value is False or value == "unknown"):  # 1 / 0 は通さない
                raise InventoryError(f"{key} is true, false or unknown")
        elif key in ("tracking_url_obtained", "commission_terms_confirmed"):
            if not isinstance(value, bool):
                raise InventoryError(f"{key} is true or false (leave it out when unknown)")
        elif key == "cookie_window_days":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InventoryError("cookie_window_days is a whole number of days")
        elif key == "actual_provider":
            if not _PROVIDER_LABEL.match(str(value)) or mm._CREDENTIAL.search(str(value)):
                raise InventoryError("actual_provider is a short platform name (no URL)")
            value = str(value)
        elif key == "pause_end_notice":
            if value not in inv.NOTICE_STATES:
                raise InventoryError(f"pause_end_notice must be one of {inv.NOTICE_STATES}")
        elif key == "notice_effective_date":
            try:
                value = date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise InventoryError("notice_effective_date is a date (YYYY-MM-DD)") from None
        elif key == "commission_type_observed":
            if value not in inv.COMMISSION_TYPES:
                raise InventoryError(f"commission_type_observed must be one of "
                                     f"{inv.COMMISSION_TYPES}")
        elif key == "commission_value_observed":
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise InventoryError("commission_value_observed is a number >= 0")
            value = float(value)
        elif key == "commission_currency_observed":
            if not re.fullmatch(r"[A-Z]{3}", str(value)):
                raise InventoryError("commission_currency_observed is a 3-letter code (JPY, USD)")
        elif key == "landing_host_observed":
            try:
                value = normalize_host(str(value))
            except AffiliateDestinationError:
                raise InventoryError("landing_host_observed is a host name (no URL)") from None
            if "/" in str(fields[key]) or ":" in str(fields[key]):
                raise InventoryError("landing_host_observed is a host name (no URL)")
        else:
            raise InventoryError(f"unknown field {key}")
        out[key] = value
    if "notice_effective_date" in out and out.get("pause_end_notice") in (None, "none_seen"):
        raise InventoryError("notice_effective_date needs a pause or end notice")
    return out


__all__ = ["AffiliateInventoryService", "CAPABILITIES_PATH", "InventoryError",
           "VERIFICATIONS_PATH", "load_capabilities"]
