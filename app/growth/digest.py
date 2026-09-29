"""Growth Action のまとめ (digest) の選び方 (C9-A、``growth-action-digest/1``、pure)。

人が判断する価値のある候補だけを **少数** (既定 5 件まで、足りなければ少ないまま) 選ぶ。

- 対象: いま動ける (``active`` + ``actionable_now``)、まだ扱われていない (レビュー中・承認済み・
  却下・変換済み・見送り・置き換えでない)、同じ意味の状態 (同じ ``candidate_fingerprint``) で
  まだ知らせていないもの。証拠が変われば新しい指紋になるので、もう一度知らせてよい。
- 並べ方: C9 の成分の順 (証拠の強さ → 機会 → 収益との関係 → 手間 (少ない方) → 急ぎ)。
  **1 つの点数を作らない。** 選んだ理由と、次の候補より前に来た理由を成分の言葉で残す。
- 多様さ: まず行動の種類ごとに一番よいものを 1 つずつ (良い順)、残りは全体の順で埋める
  (弱い調整。候補が少なければ同じ種類が複数でもよい)。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping

from app.growth.inbox import ACTIONABLE_NOW, entry_sort_key

DIGEST_SCHEMA = "growth-action-digest/1"
MAX_ITEMS = 5
#: 並べ方の成分 (順に比べる)。effort は少ない方が前。
ORDER_COMPONENTS = (("evidence_strength", "stronger evidence", False),
                    ("potential_opportunity", "higher opportunity", False),
                    ("monetization_relevance", "stronger monetization relevance", False),
                    ("effort", "lower effort", True),
                    ("recency_urgency", "more urgent", False))  # fmt: skip

# -- 除外の理由 -----------------------------------------------------------------------------------
EXCLUDED_NOT_ACTIVE = "not_active"
EXCLUDED_NOT_ACTIONABLE = "not_actionable_now"
EXCLUDED_ALREADY_NOTIFIED = "already_notified_same_state"
EXCLUDED_LIMIT = "digest_limit"


def _sha(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)  # fmt: skip
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def notification_fingerprint(entry: Mapping) -> str:
    """同じ意味の状態の通知の指紋 (候補の指紋が同じなら同じ)。"""

    return _sha({"schema": DIGEST_SCHEMA, "opportunity_key": entry["opportunity_key"],
                 "candidate_fingerprint": entry["candidate_fingerprint"]})


def digest_identity(members: Iterable[Mapping]) -> str:
    return _sha({"schema": DIGEST_SCHEMA,
                 "members": sorted(notification_fingerprint(m) for m in members)})


def eligibility(entry: Mapping, notified: Mapping[str, int]) -> tuple[bool, str, str]:
    """(対象か, 除外の理由の種類, 詳しい理由)。"""

    if entry.get("status") in ("active", "observed") and entry.get("availability") != (
            ACTIONABLE_NOW):
        reasons = "; ".join(entry.get("availability_reasons") or [])
        return False, str(entry.get("availability")), reasons
    if entry.get("status") != "active":
        return False, EXCLUDED_NOT_ACTIVE, f"status {entry.get('status')}"
    delivery = notified.get(entry["candidate_fingerprint"])
    if delivery is not None:
        return False, EXCLUDED_ALREADY_NOTIFIED, f"notified in delivery #{delivery}"
    return True, "", ""


def _levels(entry: Mapping) -> dict:
    return {name: entry["priority"][name]["level"] for name, _label, _asc in ORDER_COMPONENTS}


def why_before(a: Mapping, b: Mapping) -> str:
    """``a`` が ``b`` より前に来た理由 (最初に違う成分)。"""

    for name, label, ascending in ORDER_COMPONENTS:
        ra, rb = a["priority"][name]["rank"], b["priority"][name]["rank"]
        if ra != rb and ((ra < rb) if ascending else (ra > rb)):
            return (f"{label} ({name}: {a['priority'][name]['level']} vs "
                    f"{b['priority'][name]['level']})")
    return "tie on every component (deterministic tie-break by action type and subject)"


def select(entries: Iterable[Mapping], *, notified: Mapping[str, int] | None = None,
           limit: int = MAX_ITEMS) -> dict:  # fmt: skip
    """まとめに入れる候補を選ぶ (決定的)。1 つの点数は作らない。"""

    notified = notified or {}
    eligible, excluded = [], []
    for entry in entries:
        ok, kind, detail = eligibility(entry, notified)
        if ok:
            eligible.append(entry)
        else:
            excluded.append({"id": entry.get("id"), "opportunity_key": entry["opportunity_key"],
                             "action_type": entry["action_type"], "reason": kind,
                             "detail": detail})  # fmt: skip
    ordered = sorted(eligible, key=entry_sort_key)
    picked: list[Mapping] = []
    diversity: dict[str, str] = {}
    seen_types: set[str] = set()
    for entry in ordered:  # 1 回目: 行動の種類ごとに一番よいもの
        if len(picked) >= limit:
            break
        if entry["action_type"] not in seen_types:
            seen_types.add(entry["action_type"])
            picked.append(entry)
    global_top = {id(e) for e in ordered[:limit]}
    for entry in picked:
        if id(entry) not in global_top:
            diversity[entry["candidate_fingerprint"]] = (
                "diversity adjustment: best of its action type, included ahead of a "
                "same-type candidate")
    for entry in ordered:  # 2 回目: 全体の順で埋める (同じ種類でもよい)
        if len(picked) >= limit:
            break
        if entry not in picked:
            picked.append(entry)
    picked = sorted(picked, key=entry_sort_key)
    chosen = {id(e) for e in picked}
    selected = []
    for i, entry in enumerate(picked):
        nxt = picked[i + 1] if i + 1 < len(picked) else next(
            (e for e in ordered if id(e) not in chosen), None)
        selected.append({
            **{k: entry.get(k) for k in ("id", "opportunity_key", "revision", "action_type",
                                          "subject_id", "article_id", "keyword_id",
                                          "evidence_state", "rationale", "blockers",
                                          "candidate_fingerprint", "recommendation")},
            "components": _levels(entry),
            "evidence": [e.get("reason") for e in entry.get("evidence") or ()],
            "notification_fingerprint": notification_fingerprint(entry),
            "selection_reason": diversity.get(entry["candidate_fingerprint"])
            or f"rank {ordered.index(entry) + 1} of {len(ordered)} eligible by the C9 "
               "component order",
            "why_before_next": why_before(entry, nxt) if nxt is not None else None,
        })  # fmt: skip
    for entry in ordered:
        if id(entry) not in chosen:
            excluded.append({"id": entry.get("id"), "opportunity_key": entry["opportunity_key"],
                             "action_type": entry["action_type"], "reason": EXCLUDED_LIMIT,
                             "detail": f"limit {limit}; after "
                                       f"{picked[-1]['opportunity_key'] if picked else '-'}"})
    counts = Counter(e["reason"] for e in excluded)
    return {
        "schema": DIGEST_SCHEMA, "limit": limit,
        "selected": selected,
        "excluded": excluded,
        "counts": {"input": len(eligible) + len(excluded) - counts[EXCLUDED_LIMIT],
                   "eligible": len(eligible), "selected": len(selected),
                   "excluded_by_reason": dict(sorted(counts.items()))},
        "diversity": {"action_types_selected": dict(sorted(Counter(
            e["action_type"] for e in selected).items())),
            "adjusted": len(diversity)},
        "digest_identity": digest_identity(selected) if selected else None,
        "ordering": [label for _name, label, _asc in ORDER_COMPONENTS],
        "score": None,
    }  # fmt: skip


def render_text(plan: Mapping, *, tz_label: str) -> str:
    """まとめのメールの本文 (個別のレビューだけ。一括承認は無い)。"""

    lines = [f"Growth Action のまとめ ({len(plan['selected'])} 件)",
             "それぞれ個別に確認してください。一括の承認はありません。承認は「次の段階へ進めて",
             "よい」という許可だけで、WordPress・Threads・公開は何も起きません。", ""]
    for i, item in enumerate(plan["selected"], 1):
        comps = ", ".join(f"{k}={v}" for k, v in item["components"].items())
        lines += [f"{i}. {item['action_type']} {item['subject_id']} "
                  f"[{item['evidence_state']}]",
                  f"   {item['rationale']}",
                  f"   根拠: {'; '.join(item['evidence'])}",
                  f"   成分: {comps}",
                  f"   選んだ理由: {item['selection_reason']}"]
        if item.get("recommendation", {}).get("angle"):
            lines.append(f"   勧め: 切り口 {item['recommendation']['angle']}")
        if item.get("blockers"):
            lines.append(f"   止める理由: {'; '.join(item['blockers'])}")
        cid, fp = item["id"], item["candidate_fingerprint"]
        lines += [f"   確認: uv run python scripts/manage_growth_actions.py explain {cid}",
                  f"   レビュー: uv run python scripts/manage_growth_actions.py review {cid} "
                  f"--fingerprint {fp} --execute",
                  "   (その後の approve / reject も、レビューの指紋が合うときだけ)", ""]
    lines.append(f"時刻の基準: {tz_label}")
    return "\n".join(lines)


__all__ = ["DIGEST_SCHEMA", "EXCLUDED_ALREADY_NOTIFIED", "EXCLUDED_LIMIT",
           "EXCLUDED_NOT_ACTIONABLE", "EXCLUDED_NOT_ACTIVE", "MAX_ITEMS", "ORDER_COMPONENTS",
           "digest_identity", "eligibility", "notification_fingerprint", "render_text", "select",
           "why_before"]
