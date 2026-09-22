"""管理用 CLI: cluster 定義から read-only な制作キューと cannibalization 判定を表示する。

    uv run python -m scripts.plan_content_clusters
    uv run python -m scripts.plan_content_clusters --format json
    uv run python -m scripts.plan_content_clusters --config app/config/content_clusters.json

**read-only**: DB write なし / HTTP なし / LLM 呼び出しなし。記事の生成・公開は行わない
(drafting は引き続き手動・human 承認)。cluster 定義 (``app/config/content_clusters.json``) の検証に
失敗した場合は DB を開く前に終了する。Make / Google / WordPress の credential は読まない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.article.cluster_plan import (  # noqa: E402
    ClusterConfigError,
    ContentQueue,
    QueueEntry,
    load_cluster_config,
)
from app.config.database import SessionLocal  # noqa: E402
from app.services.content_queue_service import ContentQueueService  # noqa: E402

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "app" / "config" / "content_clusters.json"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_UNEXPECTED = 3


def _score(entry: QueueEntry) -> str:
    return "unscored" if entry.opportunity_score is None else f"{entry.opportunity_score:.2f}"


def _tier_text(affiliate) -> str:
    if affiliate.strong_program_count is None:
        return ""
    text = f" tier=strong:{affiliate.strong_program_count}/weak:{affiliate.weak_program_count}"
    text += (
        f" fit=core:{len(affiliate.core_weak_program_names)}"
        f"/loose:{len(affiliate.loose_weak_program_names)}"
        f"/unreviewed:{len(affiliate.unreviewed_weak_program_names)}"
    )
    if affiliate.alias_only_strong_program_names:
        text += f" alias_only={','.join(affiliate.alias_only_strong_program_names)}"
    return text


def _line(entry: QueueEntry) -> str:
    tmpl = entry.template.template_version or entry.template.reason
    parts = [
        f"{entry.cluster_id}/{entry.role}",
        entry.keyword,
        f"score={_score(entry)}",
        f"type={entry.article_type or 'unclassified'}",
        f"template={tmpl}",
        f"affiliate={entry.affiliate.level}({entry.affiliate.program_count})"
        + _tier_text(entry.affiliate),
        f"facts={entry.fact_research.requirement}:{entry.fact_research.status}",
    ]
    return " | ".join(parts)


def _print_table(queue: ContentQueue) -> None:
    s = queue.summary
    print("=== Content Cluster Plan (READ-ONLY: no DB write, no HTTP, no LLM) ===")
    print(
        f"assigned={s['assigned_keywords']} unassigned={s['unassigned_keywords']} "
        f"new_slots={s['new_slots']} merged={s['merged']} blocked={s['blocked']} "
        f"template_ready_slots={s['template_ready_slots']}"
    )
    readiness = s.get("production_readiness")
    if readiness is not None:
        # C2.5.8: affiliate 無しは「作らない」ではない (supporting_only は作れる)
        print(
            f"production readiness (slots): affiliate_ready={readiness['affiliate_ready']} "
            f"supporting_only={readiness['supporting_only']} blocked={readiness['blocked']}"
        )
    print(f"\n-- new slots ({len(queue.slots)}), in production order --")
    for entry in queue.slots:
        print(f"{entry.position:>2}. {_line(entry)}")
        if entry.absorbed_keywords:
            print(f"      absorbs: {', '.join(entry.absorbed_keywords)}")
        if entry.prerequisites:
            print(f"      prerequisites: {', '.join(entry.prerequisites)}")
        if entry.notes:
            print(f"      notes: {', '.join(entry.notes)}")
        if entry.monetization is not None:
            m = entry.monetization
            extra = f" ({', '.join(m.blockers)})" if m.blockers else ""
            print(
                f"      monetization: mode={m.recommended_mode or '-'} "
                f"readiness={m.production_readiness}{extra}"
            )
    print(f"\n-- merged into another slot ({len(queue.merged)}) --")
    for entry in queue.merged:
        print(f"   {entry.cluster_id} | {entry.keyword} -> {entry.merge_target_keyword}")
        print(f"      {entry.reason}")
    print(f"\n-- blocked ({len(queue.blocked)}) --")
    for entry in queue.blocked:
        print(f"   {entry.cluster_id} | {entry.keyword} | {entry.reason_code}")
        print(f"      {entry.reason}")
    print(f"\n-- unassigned keywords ({len(queue.unassigned)}) --")
    for item in queue.unassigned:
        score = "unscored" if item.opportunity_score is None else f"{item.opportunity_score:.2f}"
        print(f"   {item.keyword} | score={score}")
    if queue.deferred:
        print("\n-- deferred clusters --")
        for cluster in queue.deferred:
            print(f"   {cluster.id} | {cluster.name} | {cluster.reason}")
    if queue.warnings:
        print("\n-- warnings --")
        for warning in queue.warnings:
            print(f"   {warning}")


def run(
    *,
    config_path: str | Path = DEFAULT_CONFIG,
    output_format: str = "table",
    session_factory=SessionLocal,
) -> int:
    try:
        config = load_cluster_config(config_path)
    except ClusterConfigError as exc:
        print("CONFIG ERROR:")
        for error in exc.errors:
            print(f"  - {error}")
        return EXIT_CONFIG

    with session_factory() as session:
        try:
            queue = ContentQueueService(session).build(config)
        except ClusterConfigError as exc:
            print("CONFIG ERROR:")
            for error in exc.errors:
                print(f"  - {error}")
            return EXIT_CONFIG
        finally:
            session.rollback()

    if output_format == "json":
        print(json.dumps(queue.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_table(queue)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="plan_content_clusters",
        description=(
            "Read-only content production queue with cluster roles and cannibalization "
            "decisions (no DB write, no HTTP, no LLM)."
        ),
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    try:
        return run(config_path=args.config, output_format=args.format)
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
