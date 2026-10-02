"""C11 前半: ASP・提携案件・リンクの棚卸し (2026-10-01。pure、外に問い合わせない)。

手元の DB の事実 (案件・記事との結びつき・``/go/`` の行き先・記事の中の置き換え・クリック・
成果) と、提供元の能力 (``app/config/affiliate_provider_capabilities.json``)・人の確認の記録
(``data/affiliate/program_verifications.jsonl``) から、次を出す:

- 提供元ごと・案件ごとの棚卸し (無いもの・分からないものは ``missing`` / ``unknown``。推測しない)
- 記事ごとの収益の導線 (リンクあり・案件はあるがリンク無し・案件なし・状態の分からない案件)
- 置き場所 (今ある粒度は記事の中の外部リンクの順番だけ。CTA・節などは記録されていない)
- 日々の運用で見るべきもの (ops)
- 帰属のできる度合い (FULL / PARTIAL / MANUAL / UNKNOWN / NONE)。規則は ``attribution_class``
- 人が ASP の側でする作業の一覧 (優先度は「何を止めているか」だけで決める)

**書かない。** トラッキング URL を作らない・パラメータを足さない・リンクを置き換えない。
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urlsplit

CATALOG_STATUSES = ("active", "paused", "ended", "unknown")
#: ASP / 広告主の側の提携の状態 (catalog の status とは別。catalog の active は「候補として
#: 扱っている」だけで、提携の承認を意味しない)。人の確認の記録だけの値 (migration なし)。
PROVIDER_STATUSES = ("not_registered", "not_applied", "applied", "pending", "approved", "active",
                     "rejected", "paused", "ended", "unknown")  # fmt: skip
#: 確認の根拠。今の画面・メールで確かめたもの (provider_*) と、人の記憶 (human_recollection) を
#: 分ける。記憶は「提供元で確認済み」とは表示しない。
EVIDENCE_KINDS = ("provider_dashboard", "provider_email", "human_recollection")
PROVIDER_EVIDENCE = ("provider_dashboard", "provider_email")
REJECTION_REASONS = ("site_size_or_traffic", "content_or_category", "region_or_language",
                     "policy", "other", "not_stated")  # fmt: skip
REAPPLICATION_PLANS = ("deferred", "planned", "not_planned", "undecided")
#: 登録 -> 審査 -> 承認 -> tracking の段階
REGISTRATION_BUCKETS = ("A_APPLY_OR_REGISTER", "B_WAITING_REVIEW", "C_REJECTED_OR_DEFERRED",
                        "D_APPROVED_NEEDS_TRACKING", "E_READY_FOR_ONBOARDING",
                        "ONBOARDED")  # fmt: skip
ATTRIBUTION_CLASSES = ("FULL", "PARTIAL", "MANUAL", "UNKNOWN", "NONE")
#: 人の確認で true / false / "unknown" をとる能力 (書かなければ記録なし = missing)
TRISTATE_FIELDS = ("subid_supported", "click_reporting_supported", "conversion_reporting_supported",
                   "content_source_attribution_supported")  # fmt: skip
NOTICE_STATES = ("none_seen", "pause_announced", "end_announced", "unknown")
COMMISSION_TYPES = ("percentage", "fixed", "tiered", "hybrid", "other")
#: 実クリックと分ける既知の合成 probe (token の SHA-256 だけ。token は持たない)。
#: 根拠: docs/operations/synthetic-runtime-click-e2e.md (2026-09-13 の /go の E2E 確認)。
KNOWN_SYNTHETIC_PROBE_FINGERPRINTS = {
    "dc207140d0a49fb28cd432cfd2784745806918a8e175aacb7ba6aec1c9ae4a45":
        "docs/operations/synthetic-runtime-click-e2e.md",
}  # fmt: skip
#: 能力 -> (人の確認の項目, 提供元の設定の項目)
CAPABILITY_SOURCES = {
    "subid": ("subid_supported", "subid_in_tracking_url"),
    "click_reporting": ("click_reporting_supported", None),
    "conversion_reporting": ("conversion_reporting_supported", "commission_import_api"),
    "source_attribution": ("content_source_attribution_supported",
                           "click_reference_in_commission_report"),
}  # fmt: skip


def capability(capabilities: dict, provider: str, key: str):
    """提供元の能力の値 (true / false / "unknown")。記録が無ければ "unknown"。"""

    entry = ((capabilities.get("providers") or {}).get(provider) or {}).get("capabilities", {})
    return (entry.get(key) or {}).get("value", "unknown")


def latest_verifications(records: list[dict]) -> dict[int, dict]:
    """案件ごとの最新の人の確認の記録 (記録そのもの)。"""

    out: dict[int, dict] = {}
    for record in sorted(records, key=lambda r: (r["verified_at"], r["id"])):
        out[int(record["program_id"])] = record
    return out


def split_by_evidence(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """提供元で確かめた記録と、人の記憶の記録に分ける (根拠の書かれていない記録は記憶側)。"""

    provider = [r for r in records if r.get("evidence_kind") in PROVIDER_EVIDENCE]
    reported = [r for r in records if r.get("evidence_kind") not in PROVIDER_EVIDENCE]
    return provider, reported


def registration_bucket(row: dict) -> tuple[str, str, str]:
    """(bucket, 状態, 根拠)。提供元で確かめた状態を先に使い、無ければ人の記憶 (そう表示する)。

    tracking URL が DB にあり、host が許され、記事にリンクがあるものは ONBOARDED (提携の証拠は
    発行された URL。状態そのものは別に確かめていない)。
    """

    verified = row["verified"]["fields"].get("status_at_provider")
    reported = row["reported"]["fields"].get("status_at_provider")
    status, evidence = ((verified, "provider_verified") if verified
                        else (reported, "human_reported") if reported else ("unknown", "none"))
    obtained = row["verified"]["fields"].get("tracking_url_obtained") is True
    if status in ("rejected", "paused", "ended"):
        return "C_REJECTED_OR_DEFERRED", status, evidence
    # 登録済みの tracking URL は、提供元が発行した提携の証拠 (状態を別に確かめていなくても)
    issued = evidence if verified else "tracking_url_issued"
    if row["has_tracking_url"] and row["tracking_host_authorized"] and row["active_link_targets"]:
        return "ONBOARDED", status, issued
    if row["has_tracking_url"]:
        return "E_READY_FOR_ONBOARDING", status, issued
    if status in ("approved", "active"):
        if obtained:
            return "E_READY_FOR_ONBOARDING", status, evidence
        return "D_APPROVED_NEEDS_TRACKING", status, evidence
    if status in ("applied", "pending"):
        return "B_WAITING_REVIEW", status, evidence
    return "A_APPLY_OR_REGISTER", status, evidence


def merge_verifications(records: list[dict]) -> dict[int, dict]:
    """項目ごとに最新の値をとる (状態だけの確認が、前に確かめた SubID を消さない)。

    返り値: program_id -> {"fields": {項目: 値}, "provenance": {項目: {verified_at, record_id,
    verified_by, source}}, "last_verified": 最後の確認の時刻}
    """

    out: dict[int, dict] = {}
    for record in sorted(records, key=lambda r: (r["verified_at"], r["id"])):
        merged = out.setdefault(int(record["program_id"]),
                                {"fields": {}, "provenance": {}, "last_verified": None})
        for key, value in (record.get("fields") or {}).items():
            merged["fields"][key] = value
            merged["provenance"][key] = {"verified_at": record["verified_at"],
                                         "record_id": record["id"],
                                         "verified_by": record.get("verified_by"),
                                         "source": record.get("source"),
                                         "evidence_kind": record.get("evidence_kind")}  # fmt: skip
        merged["last_verified"] = record["verified_at"]
    return out


def effective_capabilities(provider: str, capabilities: dict, fields: dict) -> dict:
    """program ごとの能力: 人の確認 > 提供元の設定 (証拠つき) > unknown。出どころも返す。

    人の確認の "unknown" は「見たが分からなかった」で、それも unknown のまま (false にしない)。
    """

    out = {}
    for name, (field, config_key) in CAPABILITY_SOURCES.items():
        if field in fields:
            out[name] = {"value": fields[field], "source": "human_verification"}
            continue
        value = capability(capabilities, provider, config_key) if config_key else "unknown"
        if name == "conversion_reporting" and value is not True:
            value = "unknown"  # 取り込み API が無いことは、画面に成果が出ないことを意味しない
        out[name] = {"value": value,
                     "source": "provider_config" if value != "unknown" else "none"}
    return out


def attribution_class(program: dict, *, has_active_target: bool, capabilities: dict,
                      verification: dict | None) -> tuple[str, str]:
    """帰属のできる度合いと理由 (``verification`` は ``{"fields": {...}}``)。

    - FULL: SubID を受け付け、成果に記事・掲載元の参照が戻る (どちらも確認済み) + リンクあり。
    - MANUAL: リンクあり。成果は提供元の画面で見える (確認済み) が、取り込み API は無い。
    - PARTIAL: リンクあり (``/go/`` で記事ごとのクリックは分かる)。成果は記事に結べない。
    - NONE: SubID も成果の参照も無いと確かめられ、リンクも無い。
    - UNKNOWN: それ以外 (今の情報では分からない)。
    """

    provider = program["provider"]
    eff = effective_capabilities(provider, capabilities, (verification or {}).get("fields", {}))
    subid, ref = eff["subid"]["value"], eff["source_attribution"]["value"]
    manual = capability(capabilities, provider, "commission_report_manual")
    if subid is True and ref is True and has_active_target:
        return "FULL", "SubID and the conversion reference are verified"
    if has_active_target:
        api = capability(capabilities, provider, "commission_import_api")
        dashboard = eff["conversion_reporting"]["value"] is True or manual is True
        if dashboard and api is not True:
            return "MANUAL", "article-level clicks; conversions only in the provider dashboard"
        return "PARTIAL", ("article-level clicks through /go/; conversions cannot be tied to an "
                           "article (no verified conversion reference)")
    if subid is False and ref is False:
        return "NONE", "no SubID and no conversion reference (verified), and no tracked link"
    return "UNKNOWN", "no tracked link and the provider capabilities are not verified"


def build(*, programs: list[dict], article_programs: list[dict], articles: list[dict],
          targets: list[dict], mappings: list[dict], clicks: list[dict],
          commissions: list[dict], capabilities: dict, verifications: list[dict],
          now: datetime, verification_max_age_days: int | None = None,
          tracking: dict | None = None,
          known_probe_fingerprints: dict | None = None) -> dict:  # fmt: skip
    """棚卸しの全体 (読むだけ)。入力はすべて dict の一覧 (DB の行の写し)。

    ``tracking``: program_id -> {"host", "authorized", "rule"} (tracking URL の host と、その
    program に許されているか。URL そのものは渡さない)。
    """

    # 提供元の記録の無い案件は "unrecorded" (推測で埋めない。能力はすべて unknown になる)
    programs = [{**p, "provider": p.get("provider") or "unrecorded"} for p in programs]
    provider_records, reported_records = split_by_evidence(verifications)
    verified = merge_verifications(provider_records)
    reported = merge_verifications(reported_records)
    tracking = tracking or {}
    probes = (KNOWN_SYNTHETIC_PROBE_FINGERPRINTS if known_probe_fingerprints is None
              else known_probe_fingerprints)  # fmt: skip
    published = {a["id"]: a for a in articles if a.get("status") == "published"}
    by_program_articles: dict[int, list[dict]] = defaultdict(list)
    for row in article_programs:
        by_program_articles[row["affiliate_program_id"]].append(row)
    active_targets = [t for t in targets if t["status"] == "active"]
    targets_by_id = {t["id"]: t for t in targets}
    active_mappings = [m for m in mappings if m["status"] == "active"]
    clicks_by_token = Counter(c["token"] for c in clicks)
    target_tokens = {t["token"] for t in targets}
    commissions_by_program = Counter(c["affiliate_program_id"] for c in commissions)
    program_by_id = {p["id"]: p for p in programs}

    program_rows = []
    for p in programs:
        pid = p["id"]
        assigned = by_program_articles.get(pid, [])
        p_targets = [t for t in active_targets if t["affiliate_program_id"] == pid]
        p_mappings = [m for m in active_mappings
                      if targets_by_id.get(m["affiliate_link_target_id"], {}).get(
                          "affiliate_program_id") == pid]  # fmt: skip
        v = verified.get(pid)
        fields = (v or {}).get("fields", {})
        eff = effective_capabilities(p["provider"], capabilities, fields)
        subid = eff["subid"]["value"]
        last_verified = (v or {}).get("last_verified")
        track = tracking.get(pid) or {}
        age = None
        if last_verified:
            age = (now - datetime.fromisoformat(last_verified)).days
        verification_state = ("never_verified" if v is None else
                              "stale" if (verification_max_age_days is not None and age is not None
                                          and age > verification_max_age_days) else
                              "verified")  # fmt: skip
        attr, why = attribution_class(p, has_active_target=bool(p_targets),
                                      capabilities=capabilities, verification=v)
        missing = [name for name, present in (
            ("tracking_url", bool(p.get("tracking_url"))),
            ("commission_terms", p.get("commission_type") is not None
             and p.get("commission_value") is not None),
            ("currency", bool(p.get("currency"))),
            ("provider_status_verified", "status_at_provider" in fields),
            ("provider_program_id", bool(fields.get("provider_program_id"))),
            ("subid_capability", isinstance(subid, bool)),
            ("click_reporting", isinstance(eff["click_reporting"]["value"], bool)),
            ("conversion_reporting", isinstance(eff["conversion_reporting"]["value"], bool)),
            ("source_attribution", isinstance(eff["source_attribution"]["value"], bool)),
            ("cookie_window_days", fields.get("cookie_window_days") is not None
             or isinstance(capability(capabilities, p["provider"], "cookie_window_days"), int)),
            ("last_verified", last_verified is not None),
        ) if not present]  # fmt: skip
        program_rows.append({
            "id": pid, "name": p["name"], "provider": p["provider"], "category": p.get("category"),
            "catalog_status": p.get("status") or "unknown",
            "provider_status": fields.get("status_at_provider", "unknown"),
            "has_tracking_url": bool(p.get("tracking_url")),
            "tracking_host": track.get("host"),
            "tracking_host_authorized": track.get("authorized", False),
            "tracking_host_rule": track.get("rule"),
            "landing_page": "present" if p.get("landing_page_url") else "missing",
            "commission": ({"type": p.get("commission_type"), "value": p.get("commission_value"),
                            "currency": p.get("currency") or "missing"}
                           if p.get("commission_type") else "missing"),
            "articles_assigned": len(assigned),
            "articles_assigned_published": sum(1 for a in assigned
                                               if a["article_id"] in published),
            "primary_in": sum(1 for a in assigned if a.get("is_primary")),
            "active_link_targets": len(p_targets), "active_placements": len(p_mappings),
            "clicks_all_time": sum(clicks_by_token.get(t["token"], 0) for t in p_targets),
            "commission_facts": commissions_by_program.get(pid, 0),
            "last_verified": last_verified, "verification": verification_state,
            "subid_supported": subid,
            "capabilities": eff,
            # catalog (DB) と人の確認は出どころが違う。片方でもう片方を上書きしない
            "catalog": _catalog_facts(p),
            "verified": {"fields": fields, "provenance": (v or {}).get("provenance", {})},
            # 人の記憶 (提供元で確かめていない)。能力・状態の「確認済み」には使わない
            "reported": {"fields": (reported.get(pid) or {}).get("fields", {}),
                         "provenance": (reported.get(pid) or {}).get("provenance", {})},
            "differences": _differences(p, fields),
            "attribution": attr, "attribution_reason": why, "missing_fields": missing,
        })  # fmt: skip

    for row in program_rows:
        bucket, status, evidence = registration_bucket(row)
        row["registration"] = {"bucket": bucket, "partnership_status": status,
                               "evidence": evidence,
                               "rejection_reason": _pick(row, "rejection_reason"),
                               "reapplication_plan": _pick(row, "reapplication_plan"),
                               "account_registered": _pick(row, "account_registered")}
        row["next_action"] = _next_action(row)

    providers = {}
    for provider in sorted({p["provider"] for p in programs}):
        rows = [r for r in program_rows if r["provider"] == provider]
        providers[provider] = {
            "programs": len(rows),
            "catalog_active": sum(1 for r in rows if r["catalog_status"] == "active"),
            "provider_status_verified": sum(1 for r in rows if r["provider_status"] != "unknown"),
            "with_tracking_url": sum(1 for r in rows if r["has_tracking_url"]),
            "without_tracking_url": sum(1 for r in rows if not r["has_tracking_url"]),
            "attribution": dict(Counter(r["attribution"] for r in rows)),
            "capabilities": {k: capability(capabilities, provider, k) for k in
                             (capabilities.get("capability_keys") or {})},
            # 人の確認を提供元ごとに数えるだけ (設定のファイルは書き換えない)
            "observed_capabilities": {
                name: dict(Counter(_tri(r["capabilities"][name]) for r in rows
                                   if r["capabilities"][name]["source"] == "human_verification"))
                for name in CAPABILITY_SOURCES},
            "actual_provider_observed": sorted({r["verified"]["fields"]["actual_provider"]
                                                for r in rows
                                                if "actual_provider" in r["verified"]["fields"]}),
            "source": ((capabilities.get("providers") or {}).get(provider) or {}).get("kind")
            or "not described",
            "human_action_required": any(r["next_action"] for r in rows),
        }  # fmt: skip

    coverage = _coverage(published, article_programs, active_targets, active_mappings,
                         program_by_id)  # fmt: skip
    ops = _operations(program_rows, coverage, active_targets, active_mappings, targets_by_id,
                      program_by_id, clicks_by_token, target_tokens, probes)  # fmt: skip
    queue = _human_queue(program_rows, coverage)
    return {
        "counts": {
            "providers": len(providers), "programs": len(programs),
            "catalog_status": dict(Counter(r["catalog_status"] for r in program_rows)),
            "provider_status_verified": sum(1 for r in program_rows
                                            if r["provider_status"] != "unknown"),
            "with_tracking_url": sum(1 for r in program_rows if r["has_tracking_url"]),
            "without_tracking_url": sum(1 for r in program_rows if not r["has_tracking_url"]),
            "attribution": dict(Counter(r["attribution"] for r in program_rows)),
            "published_articles": len(published),
            "articles": dict(Counter(a["state"] for a in coverage.values())),
            "never_verified_programs": sum(1 for r in program_rows
                                           if r["verification"] == "never_verified"),
            "registration_buckets": dict(Counter(r["registration"]["bucket"]
                                                 for r in program_rows)),
        },  # fmt: skip
        "providers": providers, "programs": program_rows, "coverage": coverage,
        "operations": ops, "human_action_queue": queue,
        "deferred_programs": [{"program_id": r["id"], "program": r["name"],
                               "provider": r["provider"], **r["registration"]}
                              for r in program_rows
                              if r["registration"]["bucket"] == "C_REJECTED_OR_DEFERRED"],
        "status_semantics": ("catalog_status active = the program is a candidate in the catalog; "
                             "it does not mean the partnership is approved. The partnership "
                             "status comes only from human verification records; "
                             "human_recollection is shown as human_reported, never as "
                             "provider-verified"),
        "subid_design": _subid_design(program_rows),
        "reading": ("local facts only; missing / unknown are not guessed; no tracking URL is "
                    "created, no parameter is added and no link is replaced"),
    }  # fmt: skip


def _tri(cap: dict) -> str:
    return {True: "true", False: "false"}.get(cap["value"], "unknown")


def _catalog_facts(p: dict) -> dict:
    landing = p.get("landing_page_url")
    return {"status": p.get("status") or "unknown", "provider": p["provider"],
            "commission_type": p.get("commission_type"),
            "commission_value": p.get("commission_value"), "currency": p.get("currency"),
            "landing_host": (urlsplit(landing).hostname or "invalid") if landing else None,
            "provenance": "catalog (affiliate_programs)"}  # fmt: skip


def _differences(p: dict, fields: dict) -> list[dict]:
    """catalog と人の確認が食い違う項目 (どちらも上書きしない。人が catalog を別に直す)。"""

    cat = _catalog_facts(p)
    pairs = (("provider", "actual_provider"), ("commission_type", "commission_type_observed"),
             ("commission_value", "commission_value_observed"),
             ("currency", "commission_currency_observed"),
             ("landing_host", "landing_host_observed"))  # fmt: skip
    out = []
    for cat_key, field in pairs:
        if field in fields and cat[cat_key] is not None and cat[cat_key] != fields[field]:
            out.append({"field": cat_key, "catalog": cat[cat_key], "verified": fields[field]})
    status = fields.get("status_at_provider")
    if status in ("paused", "ended", "rejected") and cat["status"] == "active":
        out.append({"field": "status", "catalog": cat["status"], "verified": status})
    return out


def _pick(row: dict, field: str):
    """提供元で確かめた値 > 人の記憶 > None。"""

    if field in row["verified"]["fields"]:
        return row["verified"]["fields"][field]
    return row["reported"]["fields"].get(field)


_BUCKET_ACTION = {
    "A_APPLY_OR_REGISTER": "confirm whether an account and an application exist at the provider; "
                           "if not, register and apply",
    "B_WAITING_REVIEW": "wait for the review result; record approved / rejected when it arrives",
    "C_REJECTED_OR_DEFERRED": None,
    "D_APPROVED_NEEDS_TRACKING": "obtain the tracking URL at the provider",
}


def _next_action(row: dict) -> str | None:
    if row["catalog_status"] == "unknown":
        return "confirm the program status at the provider"
    if row["catalog_status"] in ("paused", "ended"):
        return "confirm whether the program resumed or ended; plan replacement for its articles"
    bucket = row["registration"]["bucket"]
    if bucket in _BUCKET_ACTION:
        return _BUCKET_ACTION[bucket]
    if not row["has_tracking_url"]:
        return "register the obtained tracking URL locally (onboard)"
    if not row["tracking_host_authorized"]:
        return "approve the tracking URL host for this program (approve-host, after checking it)"
    if row["active_placements"] == 0:
        return "map the tracked link into an article (existing link mapping flow)"
    if "subid_capability" in row["missing_fields"]:
        return "confirm SubID / conversion-reference support at the provider"
    return None


def _coverage(published, article_programs, active_targets, active_mappings, program_by_id):
    by_article = defaultdict(list)
    for row in article_programs:
        by_article[row["article_id"]].append(row["affiliate_program_id"])
    target_article = {t["id"]: t["article_id"] for t in active_targets}
    target_program = {t["id"]: t["affiliate_program_id"] for t in active_targets}
    placements = defaultdict(list)
    for m in active_mappings:
        if m["affiliate_link_target_id"] in target_article:
            placements[m["article_id"]].append({"mapping_id": m["id"],
                                                "target_id": m["affiliate_link_target_id"],
                                                "position": "external-link occurrence (ordinal)"})
    out = {}
    for aid, article in sorted(published.items()):
        programs = by_article.get(aid, [])
        statuses = {pid: (program_by_id.get(pid) or {}).get("status", "unknown")
                    for pid in programs}
        linked_programs = {target_program[pl["target_id"]] for pl in placements.get(aid, [])}
        mode = article.get("monetization_mode")
        if linked_programs:
            state = "linked"
        elif programs and any(s == "unknown" for s in statuses.values()):
            state = "program_status_unknown"
        elif programs:
            state = "program_without_link"
        elif mode == "supporting":
            # 編集上の意図 (比較・解説で affiliate 記事へ送る)。抜けではない
            state = "supporting_no_program"
        else:
            state = "no_program"
        out[aid] = {
            "title": article.get("title"), "monetization_mode": article.get("monetization_mode")
            or "missing",
            "affiliate_opportunity": article.get("monetization_mode") == "affiliate",
            "programs": programs,
            "program_statuses": statuses,
            "placements": placements.get(aid, []),
            "assigned_programs_without_link": [pid for pid in programs
                                               if pid not in linked_programs],
            "inactive_program_assigned": [pid for pid, s in statuses.items()
                                          if s in ("paused", "ended")],
            "state": state,
        }  # fmt: skip
    return out


def _operations(program_rows, coverage, active_targets, active_mappings, targets_by_id,
                program_by_id, clicks_by_token, target_tokens, probes):
    unmatched = {tok: n for tok, n in clicks_by_token.items() if tok not in target_tokens}
    probe_tokens = {tok for tok in unmatched
                    if hashlib.sha256(tok.encode("utf-8")).hexdigest() in probes}
    destinations = defaultdict(set)
    for t in active_targets:
        destinations[t.get("destination_url")].add(t["affiliate_program_id"])
    shared = Counter((m["article_id"], m["affiliate_link_target_id"]) for m in active_mappings)
    tracking_urls = defaultdict(set)
    for p in program_by_id.values():
        tracking_urls[(p.get("tracking_url") or "").strip()].add(p["id"])
    return {
        "catalog_active_without_tracking_url": [r["id"] for r in program_rows
                                                if r["catalog_status"] == "active"
                                                and not r["has_tracking_url"]],
        "tracking_url_without_placement": [r["id"] for r in program_rows if r["has_tracking_url"]
                                           and r["active_placements"] == 0],
        "placement_with_inactive_program": [
            m["id"] for m in active_mappings
            if (program_by_id.get(targets_by_id.get(m["affiliate_link_target_id"], {})
                                  .get("affiliate_program_id")) or {}).get("status") != "active"],
        "never_verified_programs": [r["id"] for r in program_rows
                                    if r["verification"] == "never_verified"],
        "stale_verification_programs": [r["id"] for r in program_rows
                                        if r["verification"] == "stale"],
        "paused_or_ended_programs": [r["id"] for r in program_rows
                                     if r["catalog_status"] in ("paused", "ended")],
        "unknown_status_programs": [r["id"] for r in program_rows
                                    if r["catalog_status"] == "unknown"],
        "duplicate_destination_across_programs": sorted(
            sorted(pids) for dest, pids in destinations.items() if dest and len(pids) > 1),
        "duplicate_tracking_url_across_programs": sorted(
            sorted(pids) for url, pids in tracking_urls.items() if url and len(pids) > 1),
        "token_shared_by_several_placements": [{"article_id": a, "target_id": t, "placements": n}
                                               for (a, t), n in shared.items() if n > 1],
        # 案件の無い affiliate / 区分の無い記事 (supporting は意図どおりなので別に数える)
        "articles_without_monetization_path": [aid for aid, c in coverage.items()
                                               if c["state"] == "no_program"],
        "supporting_articles_without_program": [aid for aid, c in coverage.items()
                                                if c["state"] == "supporting_no_program"],
        "linked_articles_with_unlinked_programs": {
            aid: c["assigned_programs_without_link"] for aid, c in coverage.items()
            if c["state"] == "linked" and c["assigned_programs_without_link"]},
        "articles_missing_monetization_mode": [aid for aid, c in coverage.items()
                                               if c["monetization_mode"] == "missing"],
        "articles_with_unknown_program_status": [aid for aid, c in coverage.items()
                                                 if any(s == "unknown"
                                                        for s in c["program_statuses"].values())],
        "articles_needing_replacement": [aid for aid, c in coverage.items()
                                         if c["inactive_program_assigned"]],
        # token の中身は出さない (数だけ)
        "clicks_with_unknown_token": {
            "tokens": sum(1 for tok in unmatched if tok not in probe_tokens),
            "clicks": sum(n for tok, n in unmatched.items() if tok not in probe_tokens)},
        # 既知の合成 probe (読むときに分けるだけ。元のクリックの行は変えない)
        "synthetic_probe_clicks": {
            "tokens": len(probe_tokens), "clicks": sum(unmatched[t] for t in probe_tokens),
            "evidence": sorted({probes[hashlib.sha256(t.encode("utf-8")).hexdigest()]
                                for t in probe_tokens})},
        "tracking_url_host_not_authorized": [r["id"] for r in program_rows if r["has_tracking_url"]
                                             and not r["tracking_host_authorized"]],
        "catalog_differs_from_verification": {r["id"]: r["differences"] for r in program_rows
                                              if r["differences"]},
        # ASP の画面でしか分からない作業がある提供元 (記事の中の置き換えは手元の作業なので除く)
        "human_asp_login_required": sorted({r["provider"] for r in program_rows
                                            if r["next_action"] and not r["next_action"].startswith(
                                                "map the tracked link")}),
        "not_checked_here": ["destination reachability (HTTP) is never requested", "program end "
                             "at the provider is only known from a human verification"],
    }  # fmt: skip


#: 人の作業の優先度 (何を止めているかだけで決める。好みの順位ではない)。
PRIORITY_RULES = {
    "P1": "blocks monetization of a published article that is already assigned to the program",
    "P2": "blocks knowing whether the program is usable at all (status / tracking URL)",
    "P3": "blocks article-level conversion attribution for a program that already has links",
    "P4": "metadata that sizes revenue (commission terms, cookie window, provider program id)",
}


_QUEUE_STEP = {
    "A_APPLY_OR_REGISTER": ("registration / application at the provider not confirmed",
                            "whether an account and an application exist (register and apply "
                            "if not); record the status with its evidence"),
    "B_WAITING_REVIEW": ("application waiting for review", "the review result"),
    "D_APPROVED_NEEDS_TRACKING": ("approved; no tracking URL",
                                  "the tracking URL from the provider dashboard"),
    "E_READY_FOR_ONBOARDING": ("tracking URL obtained; local intake pending",
                               "onboard (hidden input) and approve-host"),
}


def _human_queue(program_rows, coverage):
    items = []
    for r in program_rows:
        assigned = r["articles_assigned_published"]
        base = {"provider": r["provider"], "program_id": r["id"], "program": r["name"]}
        if r["catalog_status"] == "unknown":
            items.append({**base, "reason": "program status is unknown", "required_value":
                          "status at the provider (applied / approved / active / paused / rejected "
                          "/ ended)", "priority": "P1" if assigned else "P2",
                          "blocks": f"{assigned} assigned published article(s); whether to use it"})
        elif r["catalog_status"] in ("paused", "ended"):
            items.append({**base, "reason": f"program is {r['catalog_status']} in the catalog",
                          "required_value": "current status at the provider",
                          "priority": "P1" if assigned else "P2",
                          "blocks": f"{assigned} assigned published article(s) (replacement)"})
        bucket = r["registration"]["bucket"]
        prio = "P1" if assigned else "P2"
        blocks = f"links for {assigned} assigned published article(s)"
        intake_done = r["has_tracking_url"] and r["tracking_host_authorized"]
        if r["catalog_status"] == "active" and bucket in _QUEUE_STEP and not (
                bucket == "E_READY_FOR_ONBOARDING" and intake_done):
            reason, required = _QUEUE_STEP[bucket]
            if bucket == "A_APPLY_OR_REGISTER" and r["registration"]["partnership_status"] in (
                    "not_registered", "not_applied"):
                reason = f"not applied yet ({r['registration']['partnership_status']})"
            items.append({**base, "step": bucket, "reason": reason, "required_value": required,
                          "priority": prio, "blocks": blocks})
        if bucket in ("C_REJECTED_OR_DEFERRED", "A_APPLY_OR_REGISTER", "B_WAITING_REVIEW"):
            continue  # 承認の前に tracking・能力・metadata の作業は並べない
        if r["has_tracking_url"] and not r["tracking_host_authorized"] and bucket != (
                "E_READY_FOR_ONBOARDING"):
            items.append({**base, "reason": "tracking URL host not authorized for this program",
                          "required_value": "confirmation that the tracking URL host is the "
                          "provider's (approve-host)", "priority": "P1" if assigned else "P2",
                          "blocks": f"link targets for {assigned} assigned published article(s)"})
        if r["has_tracking_url"] and "subid_capability" in r["missing_fields"]:
            items.append({**base, "reason": "SubID / conversion reference support unknown",
                          "required_value": "whether the provider accepts a SubID / clickref on "
                          "the tracking URL and returns it with conversions",
                          "priority": "P3", "blocks": "article-level conversion attribution"})
        for name, label in (("cookie_window_days", "cookie window (days)"),
                            ("provider_program_id", "program / advertiser id at the provider"),
                            ("commission_terms", "commission type and value")):
            if name in r["missing_fields"] and r["catalog_status"] != "ended":
                items.append({**base, "reason": f"{label} not recorded", "required_value": label,
                              "priority": "P4", "blocks": "revenue sizing / attribution window"})
    order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
    steps = {b: i for i, b in enumerate(REGISTRATION_BUCKETS)}
    return sorted(items, key=lambda i: (order[i["priority"]], steps.get(i.get("step"), 9),
                                        i["provider"], i["program_id"]))


def _subid_design(program_rows) -> dict:
    confirmed = sorted({r["provider"] for r in program_rows if r["subid_supported"] is True})
    return {"providers_with_verified_subid": confirmed,
            "state": "designed" if confirmed else "not_applicable_yet",
            "note": ("no provider has a verified SubID capability: no tracking identity is added "
                     "to any URL. Internally, the /go/ token already identifies article x program; "
                     "a SubID scheme is designed only for a provider whose support is verified")
            if not confirmed else "see docs/operations/affiliate-infrastructure.md"}  # fmt: skip


__all__ = ["ATTRIBUTION_CLASSES", "CATALOG_STATUSES", "EVIDENCE_KINDS", "PRIORITY_RULES",
           "PROVIDER_EVIDENCE", "PROVIDER_STATUSES", "REGISTRATION_BUCKETS", "attribution_class",
           "build", "capability", "latest_verifications", "registration_bucket",
           "split_by_evidence"]
