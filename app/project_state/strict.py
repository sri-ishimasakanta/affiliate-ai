"""報告の形の確かめ (schema) と ``--strict`` の終了の契約 (pure)。

``--strict`` が 0 以外で終わるのは次のときだけ:

1. 止めるべき (``blocking``) 食い違いがある
2. ``hard`` / ``expected_state`` の不変条件が ``critical`` / ``high`` の重さで破れている
3. 必要な強い出どころが読めない (git・DB・リポジトリ。live のときは WordPress・スケジューラも)
4. 報告の形が契約と違う

次のものでは失敗しない: media 99 の重複・在庫の保守 OFF・git の未 push・携帯の表示の確認待ち・
直したドキュメント・まだ時刻の来ていない確認・advisory の目安。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.project_state.precedence import CLASSIFICATIONS, SEVERITY

TOP_KEYS = (
    "generated_at",
    "generator_version",
    "mode",
    "project",
    "git",
    "quality",
    "database",
    "wordpress",
    "featured_images",
    "taxonomy",
    "monetization",
    "threads",
    "approvals",
    "analytics",
    "scheduler",
    "facts",
    "drift",
    "invariants",
    "timing",
    "documentation_health",
    "warnings",
    "decisions",
    "known_issues",
    "next_actions",
    "source_freshness",
)
FINDING_KEYS = (
    "id",
    "area",
    "field",
    "authoritative_value",
    "conflicting_value",
    "authoritative_source",
    "conflicting_source",
    "classification",
    "severity",
    "blocking",
    "recommended_resolution",
)
FACT_KEYS = ("value", "authority", "source", "observed_at", "confidence", "conflicts")
REQUIRED_SOURCES = ("project", "git", "database", "threads", "analytics")
REQUIRED_LIVE_SOURCES = ("wordpress", "scheduler")


def validate(report: Mapping) -> list[str]:
    problems = [f"missing top-level key {k}" for k in TOP_KEYS if k not in report]
    for item in report.get("drift") or []:
        fid = item.get("id")
        missing = [k for k in FINDING_KEYS if k not in item]
        if missing:
            problems.append(f"drift {item.get('id')} lacks {missing}")
        if item.get("classification") not in CLASSIFICATIONS:
            problems.append(f"drift {fid} has classification {item.get('classification')}")
        if item.get("severity") not in SEVERITY:
            problems.append(f"drift {item.get('id')} has severity {item.get('severity')}")
    for name, item in (report.get("facts") or {}).items():
        missing = [k for k in FACT_KEYS if k not in item]
        if missing:
            problems.append(f"fact {name} lacks {missing}")
    for item in (report.get("invariants") or {}).get("results") or []:
        if item.get("result") not in ("pass", "fail", "unknown"):
            problems.append(f"invariant {item.get('id')} has result {item.get('result')}")
    for item in report.get("warnings") or []:
        if item.get("severity") not in SEVERITY:
            problems.append(f"warning {item.get('id')} has severity {item.get('severity')}")
    return problems


def failures(report: Mapping) -> list[str]:
    """``--strict`` を失敗させる理由 (空なら 0 で終わる)。"""

    out = [f"schema: {p}" for p in validate(report)]
    out += [f"blocking drift: {f['id']}" for f in report.get("drift") or [] if f.get("blocking")]
    out += [
        f"invariant {i['id']} failed ({i['level']}, {i['severity']})"
        for i in (report.get("invariants") or {}).get("results") or []
        if i["result"] == "fail"
        and i["level"] in ("hard", "expected_state")
        and i["severity"] in ("critical", "high")
    ]
    required = REQUIRED_SOURCES + (REQUIRED_LIVE_SOURCES if report.get("mode") == "live" else ())
    for name in required:
        status = ((report.get(name) or {}).get("provenance") or {}).get("status")
        if status in ("unavailable", "error"):
            out.append(f"required source unavailable: {name}")
    out += [
        f"blocking warning: {w['id']}"
        for w in report.get("warnings") or []
        if w.get("blocking") and not w["id"].startswith("drift-")
    ]
    return out
