"""システムの様子を 1 か所で見る (C10-3 / C10-F)。

**読むだけ。書かない・外に問い合わせない・送らない。**

    uv run python scripts/system_status.py                # summary (既定)
    uv run python scripts/system_status.py nightly        # 夜の分析 (最新の実行・数・積み残し)
    uv run python scripts/system_status.py data           # データの出所の鮮度
    uv run python scripts/system_status.py workflow       # 滞り・Growth の流れ・発見の候補・まとめ
    uv run python scripts/system_status.py alerts         # 開いている警告
    uv run python scripts/system_status.py summary --format json

「正常」なら ``healthy`` とだけ出る。行動が要る問題 (warning / error) だけが上に並ぶ。
警告の記録と知らせは毎朝 06:30 の運用の監視の段が行う (この CLI は記録しない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VIEWS = ("summary", "nightly", "data", "workflow", "alerts")


def _alerts(session) -> list[dict]:
    from sqlalchemy import select

    from app.models import OperationsAlert

    rows = session.scalars(select(OperationsAlert).where(OperationsAlert.status != "resolved")
                           .order_by(OperationsAlert.last_seen_at.desc()))  # fmt: skip
    return [{"id": r.id, "severity": r.severity, "status": r.status, "source": r.source,
             "type": r.alert_type, "title": r.title, "last_seen_at": str(r.last_seen_at),
             "occurrences": r.occurrence_count} for r in rows]  # fmt: skip


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - 1 つの見出しが読めなくても残りは出す
        return {"unavailable": f"{type(exc).__name__}: {exc}"[:200]}


def collect(view: str, *, session_factory=None, settings=None, health_factory=None) -> dict:
    from app.services.system_health_service import SystemHealthService

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    health_factory = health_factory or SystemHealthService
    out: dict = {"view": view}
    with session_factory() as session:
        health = health_factory(session, settings=settings).evaluate()
        health.pop("_objects", None)
        out["healthy"] = health["healthy"]
        out["actionable"] = health["actionable"]
        out["as_of"] = health["as_of"]
        if view in ("summary", "nightly"):
            out["nightly"] = health["summary"]["nightly"]
        if view in ("summary", "data"):
            out["data"] = health["summary"]["data"]
        if view in ("summary", "workflow"):
            out["workflow"] = health["summary"]["workflow"]
            out["worker"] = health["summary"]["worker"]
            out["db"] = health["summary"]["db"]
            out["growth_flow"] = _safe(lambda: _growth_flow(session, settings))
            out["discovery"] = _safe(lambda: _discovery(session, settings))
            out["growth_digest"] = _safe(lambda: _digest(session, settings))
        if view in ("summary", "alerts"):
            out["open_alerts"] = _alerts(session)
        if view == "summary":
            out["info"] = [f for f in health["findings"] if not f["actionable"]]
        session.rollback()
    out["side_effects"] = {"db_writes": 0, "external_calls": 0, "emails": 0}
    return out


def _growth_flow(session, settings) -> dict:
    from app.services.site_growth_orchestrator_service import SiteGrowthOrchestratorService

    status = SiteGrowthOrchestratorService(session, settings=settings).status()
    return {"stages": status["stages"], "by_action": status["by_action"]}


def _discovery(session, settings) -> dict:
    from app.services.discovery_promotion_service import DiscoveryPromotionService
    from app.services.nightly_analysis_service import tables_ready

    if not tables_ready(session):
        return {"tables_ready": False}
    plan = DiscoveryPromotionService(session, settings=settings).plan()
    return {"tracked": plan["tracked"], "eligible": plan["eligible"],
            "blocked_by_cannibalization": plan["blocked_by_cannibalization"],
            "shown": [f"[{v['candidate_id']}] {v['phrase']}" for v in plan["shown"]],
            "next": "manage_discovery_candidates.py plan (a human promotes one at a time)"}


def _digest(session, settings) -> dict:
    from app.services.growth_action_digest_service import GrowthActionDigestService

    plan = GrowthActionDigestService(session, settings=settings).plan()
    return {k: plan[k] for k in ("sending_enabled", "due", "in_window", "would_notify",
                                 "waiting_for", "last_sent_at")}


def _print_table(result: dict) -> None:
    print(f"=== system status: {result['view']} ({result['as_of']}) ===")
    print("healthy: no actionable problem" if result["healthy"]
          else f"ACTION NEEDED: {len(result['actionable'])} problem(s)")
    for f in result["actionable"]:
        print(f"  [{f['severity']}] {f['title']} — {f['summary']}")
    for key in ("nightly", "worker", "data", "workflow", "db", "growth_flow", "discovery",
                "growth_digest"):
        if key in result:
            print(f"-- {key}")
            value = result[key]
            if isinstance(value, dict):
                for k, v in value.items():
                    print(f"  {k}: {json.dumps(v, ensure_ascii=False, default=str)}")
    if "open_alerts" in result:
        print(f"-- open alerts ({len(result['open_alerts'])})")
        for a in result["open_alerts"]:
            print(f"  #{a['id']} [{a['severity']}/{a['status']}] {a['source']}: {a['title']}")
    for f in result.get("info") or []:
        print(f"  (info) {f['title']} — {f['summary']}")
    print("read-only: database writes = 0, external calls = 0, emails = 0")


def main(argv=None, *, session_factory=None, settings=None, health_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("view", nargs="?", choices=VIEWS, default="summary")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    result = collect(args.view, session_factory=session_factory, settings=settings,
                     health_factory=health_factory)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_table(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
