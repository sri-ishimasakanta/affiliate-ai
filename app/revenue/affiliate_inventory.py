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

from collections import Counter, defaultdict
from datetime import datetime

CATALOG_STATUSES = ("active", "paused", "ended", "unknown")
PROVIDER_STATUSES = ("applied", "approved", "active", "paused", "rejected", "ended", "unknown")
ATTRIBUTION_CLASSES = ("FULL", "PARTIAL", "MANUAL", "UNKNOWN", "NONE")


def capability(capabilities: dict, provider: str, key: str):
    """提供元の能力の値 (true / false / "unknown")。記録が無ければ "unknown"。"""

    entry = ((capabilities.get("providers") or {}).get(provider) or {}).get("capabilities", {})
    return (entry.get(key) or {}).get("value", "unknown")


def latest_verifications(records: list[dict]) -> dict[int, dict]:
    """案件ごとの最新の人の確認 (追記だけの記録。後の記録が前を置き換える)。"""

    out: dict[int, dict] = {}
    for record in sorted(records, key=lambda r: (r["verified_at"], r["id"])):
        out[int(record["program_id"])] = record
    return out


def attribution_class(program: dict, *, has_active_target: bool, capabilities: dict,
                      verification: dict | None) -> tuple[str, str]:
    """帰属のできる度合いと理由。

    - FULL: 提供元が SubID を受け付け、成果に参照が戻ることが確かめられている (証拠つき)。
    - PARTIAL: ``/go/`` の行き先があり、記事ごとのクリックは分かる。成果は記事に結べない。
    - MANUAL: 成果は提供元の画面だけで見える (人が写す) と確かめられ、リンクもある。
    - NONE: SubID も成果の参照も無いと確かめられ、リンクも無い。
    - UNKNOWN: それ以外 (今の情報では分からない)。
    """

    provider = program["provider"]
    subid = (verification or {}).get("fields", {}).get("subid_supported",
                                                       capability(capabilities, provider,
                                                                  "subid_in_tracking_url"))
    clickref = capability(capabilities, provider, "click_reference_in_commission_report")
    manual = capability(capabilities, provider, "commission_report_manual")
    if subid is True and clickref is True and has_active_target:
        return "FULL", "SubID and the conversion reference are verified"
    if has_active_target:
        api = capability(capabilities, provider, "commission_import_api")
        if manual is True and api is not True:
            return "MANUAL", "article-level clicks; conversions only in the provider dashboard"
        return "PARTIAL", ("article-level clicks through /go/; conversions cannot be tied to an "
                           "article (no verified conversion reference)")
    if subid is False and clickref is False:
        return "NONE", "no SubID and no conversion reference (verified), and no tracked link"
    return "UNKNOWN", "no tracked link and the provider capabilities are not verified"


def build(*, programs: list[dict], article_programs: list[dict], articles: list[dict],
          targets: list[dict], mappings: list[dict], clicks: list[dict],
          commissions: list[dict], capabilities: dict, verifications: list[dict],
          now: datetime, verification_max_age_days: int | None = None) -> dict:
    """棚卸しの全体 (読むだけ)。入力はすべて dict の一覧 (DB の行の写し)。"""

    # 提供元の記録の無い案件は "unrecorded" (推測で埋めない。能力はすべて unknown になる)
    programs = [{**p, "provider": p.get("provider") or "unrecorded"} for p in programs]
    verified = latest_verifications(verifications)
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
        subid = fields.get("subid_supported",
                           capability(capabilities, p["provider"], "subid_in_tracking_url"))
        last_verified = (v or {}).get("verified_at")
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
            ("cookie_window_days", fields.get("cookie_window_days") is not None
             or isinstance(capability(capabilities, p["provider"], "cookie_window_days"), int)),
            ("last_verified", last_verified is not None),
        ) if not present]  # fmt: skip
        program_rows.append({
            "id": pid, "name": p["name"], "provider": p["provider"], "category": p.get("category"),
            "catalog_status": p.get("status") or "unknown",
            "provider_status": fields.get("status_at_provider", "unknown"),
            "has_tracking_url": bool(p.get("tracking_url")),
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
            "attribution": attr, "attribution_reason": why, "missing_fields": missing,
        })  # fmt: skip

    for row in program_rows:
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
            "source": ((capabilities.get("providers") or {}).get(provider) or {}).get("kind")
            or "not described",
            "human_action_required": any(r["next_action"] for r in rows),
        }  # fmt: skip

    coverage = _coverage(published, article_programs, active_targets, active_mappings,
                         program_by_id)  # fmt: skip
    ops = _operations(program_rows, coverage, active_targets, active_mappings, targets_by_id,
                      program_by_id, clicks_by_token, target_tokens)  # fmt: skip
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
        },  # fmt: skip
        "providers": providers, "programs": program_rows, "coverage": coverage,
        "operations": ops, "human_action_queue": queue,
        "subid_design": _subid_design(program_rows),
        "reading": ("local facts only; missing / unknown are not guessed; no tracking URL is "
                    "created, no parameter is added and no link is replaced"),
    }  # fmt: skip


def _next_action(row: dict) -> str | None:
    if row["catalog_status"] == "unknown":
        return "confirm the program status at the provider"
    if row["catalog_status"] in ("paused", "ended"):
        return "confirm whether the program resumed or ended; plan replacement for its articles"
    if not row["has_tracking_url"]:
        return "obtain the tracking URL at the provider (after confirming approval)"
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
                program_by_id, clicks_by_token, target_tokens):
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
            "tokens": sum(1 for tok in clicks_by_token if tok not in target_tokens),
            "clicks": sum(n for tok, n in clicks_by_token.items() if tok not in target_tokens)},
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
        if not r["has_tracking_url"] and r["catalog_status"] == "active":
            items.append({**base, "reason": "no tracking URL", "required_value":
                          "approval status and the tracking URL from the provider dashboard",
                          "priority": "P1" if assigned else "P2",
                          "blocks": f"links for {assigned} assigned published article(s)"})
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
    return sorted(items, key=lambda i: (order[i["priority"]], i["provider"], i["program_id"]))


def _subid_design(program_rows) -> dict:
    confirmed = sorted({r["provider"] for r in program_rows if r["subid_supported"] is True})
    return {"providers_with_verified_subid": confirmed,
            "state": "designed" if confirmed else "not_applicable_yet",
            "note": ("no provider has a verified SubID capability: no tracking identity is added "
                     "to any URL. Internally, the /go/ token already identifies article x program; "
                     "a SubID scheme is designed only for a provider whose support is verified")
            if not confirmed else "see docs/operations/affiliate-infrastructure.md"}  # fmt: skip


__all__ = ["ATTRIBUTION_CLASSES", "CATALOG_STATUSES", "PRIORITY_RULES", "PROVIDER_STATUSES",
           "attribution_class", "build", "capability", "latest_verifications"]
