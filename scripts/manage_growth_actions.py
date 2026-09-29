"""Growth Action の受け箱 (C9 Batch 2)。**既定は読むだけ。**

    uv run python scripts/manage_growth_actions.py list
    uv run python scripts/manage_growth_actions.py list --all --action-type review_internal_links
    uv run python scripts/manage_growth_actions.py show <id|opportunity_key>
    uv run python scripts/manage_growth_actions.py explain <id|opportunity_key>
    uv run python scripts/manage_growth_actions.py history <id>
    uv run python scripts/manage_growth_actions.py refresh            # PLAN (書かない)
    uv run python scripts/manage_growth_actions.py refresh --execute  # C9 の履歴の表だけに書く
    uv run python scripts/manage_growth_actions.py review <id> --execute
    uv run python scripts/manage_growth_actions.py approve <review_id> --fingerprint <sha> --execute
    uv run python scripts/manage_growth_actions.py reject <review_id> --fingerprint <sha> \\
        --reason "..." --execute
    uv run python scripts/manage_growth_actions.py dismiss <id> --reason "..." --execute
    # C9-B: 変換で作った手元の依頼 (Threads の生成・記事の計画・変更の準備)
    uv run python scripts/manage_growth_actions.py handoff list [--workflow W] [--status S]
    uv run python scripts/manage_growth_actions.py handoff show <request_id>
    uv run python scripts/manage_growth_actions.py handoff approve-plan <request_id> --execute
    uv run python scripts/manage_growth_actions.py handoff reject-plan <request_id> \
        --reason "..." --execute
    uv run python scripts/manage_growth_actions.py handoff materialize <request_id> \
        --article-id <id> --execute
    uv run python scripts/manage_growth_actions.py handoff prepare <request_id> \
        --downstream-type change_request|editorial_revision|link_mapping --downstream-id <id> \
        --execute
    uv run python scripts/manage_growth_actions.py handoff close <request_id> --reason "..." \
        --execute

書くのは ``--execute`` のときの C9 の 3 つの表 (``growth_action_*``) だけ。**承認は次の段階へ
進めてよいという許可だけ** で、WordPress・Threads・公開・アフィリエイトの設定・メール・外の API に
触れない。履歴の表が無い DB (migration 前) では、``list`` / ``show`` / ``explain`` / ``refresh``
(PLAN) だけが動く。``handoff`` の書き込みは ``growth_handoff_requests`` だけ (記事を作らない・
変更を作らない・生成しない。先の承認・適用・公開は、その流れの独自のまま)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.growth.analysis import ACTION_TYPES, EVIDENCE_STATES  # noqa: E402
from app.growth.conversion import plan_conversion  # noqa: E402
from app.growth.inbox import group_by_action  # noqa: E402
from app.models.growth_action import GA_STATUSES  # noqa: E402
from app.models.growth_handoff import GH_STATUSES, GH_WORKFLOWS  # noqa: E402
from app.services.growth_action_service import (  # noqa: E402
    GrowthActionError,
    GrowthActionHistory,
    GrowthActionReviewService,
    build_inbox,
    filter_entries,
    inbox_counts,
)

SIDE_EFFECTS = ("WordPress writes = 0, Threads writes = 0, publications = 0, emails = 0, "
                "external calls = 0")


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    sub = parser.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list")
    lst.add_argument("--all", action="store_true", help="重複・覆われたもの・情報も出す")
    lst.add_argument("--status", choices=GA_STATUSES)
    lst.add_argument("--action-type", choices=ACTION_TYPES, dest="action_type")
    lst.add_argument("--article-id", type=int, dest="article_id")
    lst.add_argument("--keyword-id", type=int, dest="keyword_id")
    lst.add_argument("--evidence-state", choices=EVIDENCE_STATES, dest="evidence_state")
    lst.add_argument("--actionable-only", action="store_true", dest="actionable_only")
    lst.add_argument("--requires-review", action="store_true", dest="requires_review")
    lst.add_argument("--per-type", type=int, default=3, dest="per_type",
                     help="表で 1 つの行動の種類に出す件数 (残りは件数だけ)")
    for name in ("show", "explain", "history"):
        sub.add_parser(name).add_argument("target")
    ref = sub.add_parser("refresh")
    ref.add_argument("--execute", action="store_true")
    rev = sub.add_parser("review")
    rev.add_argument("candidate_id", type=int)
    rev.add_argument("--fingerprint", default=None,
                     help="まとめ・一覧で見た候補の指紋 (合わなければ断る)")
    rev.add_argument("--execute", action="store_true")
    sub.add_parser("digest-plan", help="Growth Action のまとめの PLAN (読むだけ・送らない)")
    send = sub.add_parser("digest-send", help="まとめを送る (既定は PLAN。本物の送信は方針で無効)")
    send.add_argument("--execute", action="store_true")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("review_id", type=int)
        p.add_argument("--fingerprint", required=True, help="レビューに見せた版の指紋")
        p.add_argument("--reason", default=None)
        p.add_argument("--execute", action="store_true")
    dis = sub.add_parser("dismiss")
    dis.add_argument("candidate_id", type=int)
    dis.add_argument("--reason", required=True)
    dis.add_argument("--execute", action="store_true")
    hand = sub.add_parser("handoff", help="C9-B の手元の依頼 (既定は読むだけ)")
    hsub = hand.add_subparsers(dest="handoff_command", required=True)
    hl = hsub.add_parser("list")
    hl.add_argument("--workflow", choices=GH_WORKFLOWS, default=None)
    hl.add_argument("--status", choices=GH_STATUSES, default=None)
    hsub.add_parser("show").add_argument("request_id", type=int)
    for name in ("approve-plan", "reject-plan", "materialize", "prepare", "close"):
        hp = hsub.add_parser(name)
        hp.add_argument("request_id", type=int)
        hp.add_argument("--reason", default=None)
        hp.add_argument("--execute", action="store_true")
        if name == "materialize":
            hp.add_argument("--article-id", type=int, required=True, dest="article_id")
        if name == "prepare":
            hp.add_argument("--downstream-type", required=True, dest="downstream_type",
                            choices=("change_request", "editorial_revision", "link_mapping"))
            hp.add_argument("--downstream-id", type=int, required=True, dest="downstream_id")
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    from app.services.growth_handoff_service import GrowthHandoffError

    with session_factory() as session:
        try:
            code = _run(args, session, settings, as_of)
        except (GrowthActionError, GrowthHandoffError) as exc:
            session.rollback()
            print(f"refused: {exc.reason}")
            code = 2
    print(f"side effects: {SIDE_EFFECTS}")
    return code


def _emit(args, payload, table: str) -> None:
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(table)


def _find(entries: list[dict], target: str) -> dict:
    for e in entries:
        if str(e.get("id")) == target or e["opportunity_key"] == target:
            return e
    raise GrowthActionError(f"no current growth action matches {target!r}")


def _run(args, session, settings, as_of) -> int:
    command = args.command
    if command in ("digest-plan", "digest-send"):
        from app.services.growth_action_digest_service import GrowthActionDigestService

        service = GrowthActionDigestService(session, settings=settings)
        if command == "digest-send" and args.execute:
            if not service._policy.sending_enabled:
                raise GrowthActionError(
                    "growth action digest sending is disabled in growth_action_policy.json "
                    "(the first real production send needs a human decision); nothing was sent")
            result = service.send(now=as_of, execute=True)
        else:
            result = service.plan(now=as_of)
        _emit(args, result, render_digest(result))
        return 0
    if command in ("list", "show", "explain", "refresh"):
        box = build_inbox(session, settings=settings, now=as_of, days=args.days)
        entries = box["entries"]
        if command == "list":
            shown = filter_entries(
                entries, status=args.status, action_type=args.action_type,
                article_id=args.article_id, keyword_id=args.keyword_id,
                evidence_state=args.evidence_state, actionable_only=args.actionable_only,
                requires_review=args.requires_review, include_all=args.all)  # fmt: skip
            _emit(args, {"as_of": box["as_of"], "history_source": box["history_source"],
                         "counts": inbox_counts(entries), "entries": shown},
                  render_list(box, shown, per_type=args.per_type))  # fmt: skip
            return 0
        if command == "refresh":
            plan = box["plan"]
            payload = {"plan": plan.as_dict(), "executed": False, "written": None}
            if args.execute:
                payload["written"] = GrowthActionHistory(session).apply_refresh(
                    plan, now=datetime.fromisoformat(box["as_of"]))
                payload["executed"] = True
            _emit(args, payload, render_refresh(payload))
            return 0
        entry = _find(entries, args.target)
        link = linkage(session, settings, entry.get("id"))
        if command == "show":
            _emit(args, {**entry, "linkage": link}, render_entry(entry) + render_linkage(link))
        else:
            conversion = plan_conversion(entry).as_dict()
            _emit(args, {**entry, "conversion": conversion, "linkage": link},
                  render_explain(entry, conversion) + render_linkage(link))  # fmt: skip
        return 0
    if command == "handoff":
        return _handoff(args, session, as_of)
    history = GrowthActionHistory(session)
    reviews = GrowthActionReviewService(session)
    if command == "history":
        if not history.tables_ready():
            raise GrowthActionError("growth action history tables are missing (migration "
                                    "74bfaf6c9c9f is not applied)")
        payload = history.history(int(args.target))
        payload["linkage"] = linkage(session, settings, int(args.target))
        _emit(args, payload, json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    if not args.execute:
        print(f"PLAN: {command} would write only the growth_action_* tables; "
              "re-run with --execute")
        return 0
    if command == "review":
        review = reviews.request_review(args.candidate_id,
                                        expected_candidate_fingerprint=args.fingerprint)
        print(f"review #{review.id} pending for growth action {review.candidate_id}; "
              f"fingerprint {review.candidate_fingerprint}")
    elif command == "approve":
        review = reviews.approve(args.review_id, expected_candidate_fingerprint=args.fingerprint,
                                 reason=args.reason)
        print(f"review #{review.id} approved (permission only; nothing was executed)")
    elif command == "reject":
        review = reviews.reject(args.review_id, expected_candidate_fingerprint=args.fingerprint,
                                reason=args.reason or "")
        print(f"review #{review.id} rejected")
    elif command == "dismiss":
        row = reviews.dismiss(args.candidate_id, reason=args.reason)
        print(f"growth action {row.id} dismissed")
    return 0


def _handoff(args, session, as_of) -> int:
    """C9-B の手元の依頼を見る・人の判断を記録する (書くのは growth_handoff_requests だけ)。"""

    from datetime import UTC

    from app.services.growth_handoff_service import (
        HANDOFF_REVISION,
        GrowthHandoffService,
        handoff_ready,
    )

    if not handoff_ready(session):
        raise GrowthActionError(f"growth handoff request table is missing (migration "
                                f"{HANDOFF_REVISION} is not applied)")
    service = GrowthHandoffService(session)
    command = args.handoff_command
    if command == "list":
        rows = [service.observe(r) for r in service.list(workflow=args.workflow,
                                                          status=args.status)]
        _emit(args, {"requests": rows}, "\n".join(render_handoff(r) for r in rows)
              or "(no handoff requests)")
        return 0
    if command == "show":
        row = service.get(args.request_id)
        payload = {**service.observe(row), "frozen": row.frozen_json,
                   "frozen_hash": row.frozen_hash,
                   "source_growth_action_id": row.source_growth_action_id,
                   "source_review_id": row.source_review_id,
                   "resolution_note": row.resolution_note, "decided_by": row.decided_by}
        _emit(args, payload, render_handoff(payload) + "\n"
              + json.dumps(row.frozen_json, ensure_ascii=False, indent=2, default=str))
        return 0
    if not args.execute:
        print(f"PLAN: handoff {command} would write only growth_handoff_requests; "
              "re-run with --execute")
        return 0
    now = as_of or datetime.now(UTC)
    if command == "approve-plan":
        service.decide_planning(args.request_id, approve=True, now=now, reason=args.reason)
        meaning = ("approved (permission only: create the article in the existing plan flow, "
                   "then link it with handoff materialize)")
    elif command == "reject-plan":
        service.decide_planning(args.request_id, approve=False, now=now, reason=args.reason)
        meaning = "rejected"
    elif command == "materialize":
        service.materialize(args.request_id, article_id=args.article_id, now=now)
        meaning = f"linked to article {args.article_id} (the article keeps its own workflow)"
    elif command == "prepare":
        service.prepare(args.request_id, downstream_type=args.downstream_type,
                        downstream_id=args.downstream_id, now=now)
        meaning = (f"linked to {args.downstream_type} {args.downstream_id} (its own approval and "
                   "apply step remain)")
    else:
        service.close(args.request_id, now=now, reason=args.reason or "")
        meaning = "cancelled"
    session.commit()
    print(f"handoff request #{args.request_id} {meaning}")
    return 0


def render_handoff(h: dict) -> str:
    kind = h.get("lane") or h.get("change_type") or ""
    lines = [f"handoff #{h['id']} {h['workflow']} {kind} — {h['status']}; article "
             f"{_v(h.get('article_id'))} keyword {_v(h.get('keyword_id'))}"
             + (f"; angle {h['requested_angle']}" if h.get("requested_angle") else "")]
    if h.get("generation_request_id"):
        lines.append(f"  generation request {h['generation_request_id']}")
    if h.get("source_drift"):
        lines.append(f"  source drift since conversion: {', '.join(h['source_drift'])} "
                     "(prepare will refuse; close and re-review)")
    for d in h.get("downstream") or []:
        extra = ", ".join(f"{k} {v}" for k, v in d.items() if k not in ("type", "id") and v)
        lines.append(f"  downstream {d['type']} #{d['id']}: {extra}")
    lines.append(f"  external effect: {_v(h.get('external_effect'))} "
                 "(converted ≠ approved ≠ published / applied)")
    return "\n".join(lines)


def linkage(session, settings, candidate_id) -> dict:
    """レビュー・変換・変換の先の状態・実際の変化の時刻・最新の観測 (読むだけ)。"""

    from sqlalchemy import select

    from app.models.growth_action import GrowthActionConversion, GrowthActionReview
    from app.services.growth_action_conversion_service import (
        GrowthActionConversionService,
        GrowthActionOutcomeService,
        conversions_ready,
    )

    out = {"review": None, "notification": None, "conversion": None, "downstream": [],
           "latest_outcome": None, "measurement": []}
    if candidate_id is None or not GrowthActionHistory(session).tables_ready():
        return out
    from app.services.growth_action_digest_service import GrowthActionDigestService

    out["notification"] = GrowthActionDigestService(
        session, settings=settings).notification_state().get(candidate_id)
    review = session.scalars(select(GrowthActionReview).where(
        GrowthActionReview.candidate_id == candidate_id)).first()  # fmt: skip
    if review is not None:
        out["review"] = {"id": review.id, "status": review.status,
                         "decided_at": review.decided_at.isoformat() if review.decided_at
                         else None}  # fmt: skip
    if not conversions_ready(session):
        return out
    conversion = session.scalars(select(GrowthActionConversion).where(
        GrowthActionConversion.candidate_id == candidate_id)).first()  # fmt: skip
    if conversion is None:
        return out
    service = GrowthActionConversionService(session, settings=settings)
    out["conversion"] = {"id": conversion.id, "status": conversion.status,
                         "downstream_type": conversion.downstream_type,
                         "downstream_ids": list(conversion.downstream_ids_json),
                         "executed_at": conversion.executed_at.isoformat()}  # fmt: skip
    out["downstream"] = service.downstream(conversion)
    from app.services.growth_measurement_service import GrowthMeasurementService

    # C9-C: 候補 → レビュー → 変換 → 引き渡し → 実際の変化 → 観測、を 1 本の流れで見せる。
    measurement = GrowthMeasurementService(session, settings=settings)
    for item in measurement.anchors():
        if item["anchor"]["growth_action_id"] != candidate_id:
            continue
        m = measurement.measure(item).as_dict()
        out["measurement"].append({k: m[k] for k in (
            "anchor", "lifecycle", "measurement_state", "waiting", "next_measurement_at")})
    outcomes = GrowthActionOutcomeService(session, settings=settings)
    anchors = [a for a in outcomes.anchors() if a.growth_action_id == candidate_id]
    if anchors:
        result = outcomes.outcome(anchors[0]).as_dict()
        reached = [c for c in result["checkpoints"] if c["state"] != "waiting"]
        out["latest_outcome"] = {"measurement_state": result["measurement_state"],
                                 "checkpoint": (reached[-1] if reached
                                                else result["checkpoints"][0])}  # fmt: skip
    return out


def render_linkage(link: dict) -> str:
    lines = ["", f"review: {_v(link['review'])}",
             f"last notified: {_v(link.get('notification'))}"]
    if link["conversion"]:
        c = link["conversion"]
        lines.append(f"conversion #{c['id']}: {c['status']} → {c['downstream_type']} "
                     f"{c['downstream_ids']} at {c['executed_at']}")
    else:
        lines.append("conversion: none")
    for d in link["downstream"]:
        lines.append(f"downstream {d['type']} #{d['id']}: {d['state']}; effective_at "
                     f"{d.get('effective_at') or '— (not applied)'}")
        if d.get("handoff"):
            lines.append(render_handoff(d["handoff"]))
    for m in link.get("measurement") or []:
        life = m["lifecycle"]
        lines.append("lifecycle: candidate → review → conversion → " + " → ".join(
            f"{s['name']}={s['state'] or '—'}" for s in life["stages"]))
        lines.append(f"effective: {life['effective_at'] or '— (not in effect)'}"
                     + (f" [{life['effective_event']}]" if life["effective_event"] else "")
                     + f"; measurement {m['measurement_state']}; next "
                     f"{m['next_measurement_at'] or '—'}")
        lines.append("waiting: " + ("; ".join(m["waiting"]) or "nothing"))
    if link["latest_outcome"]:
        o = link["latest_outcome"]
        lines.append(f"latest outcome: {o['measurement_state']} "
                     f"({o['checkpoint']['name']}: {o['checkpoint']['state']})")
    return "\n".join(lines)


def render_digest(result: dict) -> str:
    sel = result["selection"]
    c = sel["counts"]
    head = ("SENT" if (result.get("delivery") or {}).get("sent") else
            "EXECUTED (not sent)" if result.get("executed") else "PLAN (not sent)")
    lines = [f"Growth Action digest {head} — as of {result['as_of']}",
             f"sending enabled: {result['sending_enabled']}; cadence {result['cadence_days']} "
             f"day(s); last sent {_v(result['last_sent_at'])}; due {result['due']}; "
             f"in window {result['in_window']}; would notify {result['would_notify']}",
             "waiting for: " + ("; ".join(result["waiting_for"]) or "nothing"),
             f"eligible {c['eligible']}, selected {c['selected']} (max {sel['limit']}); excluded: "
             + ", ".join(f"{k}={v}" for k, v in c["excluded_by_reason"].items()),
             "diversity: " + ", ".join(f"{k}={v}" for k, v in
                                       sel["diversity"]["action_types_selected"].items()),
             "order: " + " → ".join(sel["ordering"]) + " (no single score)", ""]  # fmt: skip
    for i, item in enumerate(sel["selected"], 1):
        comps = ", ".join(f"{k}={v}" for k, v in item["components"].items())
        lines += [f"{i}. [{item['id']}] {item['action_type']} {item['subject_id']} "
                  f"[{item['evidence_state']}]", f"   components: {comps}",
                  f"   why selected: {item['selection_reason']}",
                  f"   ahead of the next: {_v(item['why_before_next'])}",
                  f"   review: manage_growth_actions.py review {item['id']} --fingerprint "
                  f"{item['candidate_fingerprint']} --execute"]  # fmt: skip
        if item.get("recommendation", {}).get("angle"):
            lines.append(f"   recommendation: angle {item['recommendation']['angle']}")
    if not sel["selected"]:
        lines.append("(nothing to review)")
    return "\n".join(lines)


def _v(value) -> str:
    return "—" if value is None else str(value)


def _components(entry: dict) -> str:
    p = entry["priority"]
    return (f"evidence={p['evidence_strength']['level']} potential="
            f"{p['potential_opportunity']['level']} urgency={p['recency_urgency']['level']} "
            f"effort={p['effort']['level']} monetization={p['monetization_relevance']['level']}")


def render_list(box: dict, shown: list[dict], *, per_type: int) -> str:
    counts = inbox_counts(box["entries"])
    lines = [f"Growth Action inbox (history: {box['history_source']}) — as of {box['as_of']}",
             "counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()), ""]
    for action, items in group_by_action(shown).items():
        lines.append(f"## {action} ({len(items)})")
        for e in items[:per_type]:
            variant = f" {e['variant']}" if e.get("variant") else ""
            lines.append(
                f"- [{_v(e.get('id'))}] {e['subject_id']}{variant}"
                f" | {e['status']}/{e['availability']} | rev {e['revision']} | "
                f"{e['evidence_state']} | {_components(e)}")  # fmt: skip
            if e.get("blockers"):
                lines.append(f"    blockers: {'; '.join(e['blockers'])}")
            if e.get("availability_reasons") and e["availability"] != "actionable_now":
                lines.append(f"    coverage: {'; '.join(e['availability_reasons'][:2])}")
        if len(items) > per_type:
            lines.append(f"  … +{len(items) - per_type} more (use --action-type {action})")
        lines.append("")
    if not shown:
        lines.append("(nothing to review)")
    return "\n".join(lines)


def render_entry(e: dict) -> str:
    lines = [f"growth action [{_v(e.get('id'))}] {e['opportunity_key']} (revision {e['revision']})",
             f"status {e['status']} / {e['availability']}; evidence {e['evidence_state']}",
             f"rationale: {e['rationale']}", f"priority: {_components(e)}",
             f"first seen {_v(e.get('first_seen_at'))}, last seen {_v(e.get('last_seen_at'))}, "
             f"seen {_v(e.get('seen_count'))}; review {_v(e.get('review'))}",
             f"candidate fingerprint {e['candidate_fingerprint']}",
             f"evidence fingerprint {e['evidence_fingerprint']}"]  # fmt: skip
    for ev in e["evidence"]:
        lines.append(f"- evidence ({ev['basis']}, {_v(ev['source_engine'])}): {ev['reason']}")
    for b in e.get("blockers") or ():
        lines.append(f"- blocker: {b}")
    for r in e.get("availability_reasons") or ():
        lines.append(f"- coverage: {r}")
    fresh = ", ".join(f"{k}={v['state']}" for k, v in (e.get("freshness") or {}).items())
    lines.append(f"freshness: {fresh}")
    return "\n".join(lines)


def render_explain(e: dict, conversion: dict) -> str:
    return "\n".join([
        render_entry(e), "",
        f"approval means: permission to proceed only (nothing is executed); "
        f"requires human approval: {e['requires_human_approval']}; external write needed "
        f"later: {_v(e['external_write_required'])}; reversible: {e['reversible']}",
        f"conversion: {conversion['support']} → {_v(conversion['target_workflow'])} "
        f"(execution mode {conversion['execution_mode']})"
        + (f"; not executable: {conversion['missing']}" if conversion.get("missing") else ""),
        *[f"  - {s}" for s in conversion["steps"]],
        f"  note: {conversion['note']}",
    ])


def render_refresh(payload: dict) -> str:
    plan = payload["plan"]
    lines = [f"refresh {'EXECUTED' if payload['executed'] else 'PLAN'} — as of {plan['as_of']}; "
             f"history tables {'ready' if plan['tables_ready'] else 'missing (migration '
             + plan['required_revision'] + ' not applied)'}",
             "counts: " + json.dumps(plan["counts"], ensure_ascii=False)]  # fmt: skip
    if payload["written"]:
        w = payload["written"]
        lines.append(f"written: {len(w['created'])} new revision(s), {len(w['reobserved'])} "
                     f"re-observed, {len(w['not_observed'])} no longer observed")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
