"""管理用 CLI: keyword idea 候補の expansion 判定 (keep / merge / reject) を表示する。

    uv run python -m scripts.plan_keyword_expansion --ideas-file ideas.json
    uv run python -m scripts.plan_keyword_expansion --ideas-file ideas.json --format json

入力 (``--ideas-file``, JSON) は次のどちらか:

* ``scripts.discover_keyword_ideas --output`` の出力:
  ``{"ideas": [{"cluster", "keyword", "metrics"}]}``
* 手書き: ``{"candidates": {"B": ["keyword", ...], "C": [...]}}`` (指標なし = ``not_available``)

**read-only**: Google へは一切通信しない / DB write なし / keyword は追加しない / LLM なし。
production の keyword・article・active affiliate catalog を読み、C2.2 の制作キューと比較して
理由コード付きで判定する。affiliate coverage は live DB の catalog から解決する。
入力・設定の検証に失敗した場合は DB を開く前に終了する。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.article.cluster_plan import ClusterConfigError, load_cluster_config  # noqa: E402
from app.article.keyword_expansion import (  # noqa: E402
    NOT_AVAILABLE,
    ExpansionDecision,
    ExpansionPlan,
    ExpansionRulesError,
    IdeaCandidate,
    load_expansion_rules,
)
from app.config.database import SessionLocal  # noqa: E402
from app.keyword.idea_seeds import TARGET_CLUSTERS  # noqa: E402
from app.services.keyword_expansion_service import KeywordExpansionService  # noqa: E402

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "app" / "config"
DEFAULT_CLUSTER_CONFIG = _CONFIG_DIR / "content_clusters.json"
DEFAULT_RULES = _CONFIG_DIR / "keyword_expansion_rules.json"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_INPUT = 3
EXIT_UNEXPECTED = 4

MAX_IDEAS = 5000


class IdeasInputError(ValueError):
    pass


def _metrics(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise IdeasInputError("metrics must be an object or null")
    return value


def load_ideas(path: str | Path) -> list[IdeaCandidate]:
    """discover の出力、または ``{"candidates": {cluster: [keyword]}}`` を読み込んで検証する。"""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IdeasInputError(f"ideas file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise IdeasInputError(f"ideas file is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise IdeasInputError("ideas file must be a JSON object")

    items: list[tuple[str | None, object, dict | None]] = []
    if "ideas" in raw:
        if not isinstance(raw["ideas"], list):
            raise IdeasInputError("'ideas' must be a list")
        for entry in raw["ideas"]:
            if not isinstance(entry, dict):
                raise IdeasInputError("every idea must be an object")
            metrics = _metrics(entry.get("metrics"))
            items.append((entry.get("cluster"), entry.get("keyword"), metrics))
    elif "candidates" in raw:
        if not isinstance(raw["candidates"], dict):
            raise IdeasInputError("'candidates' must be an object of cluster -> keyword list")
        for cluster, keywords in raw["candidates"].items():
            if not isinstance(keywords, list):
                raise IdeasInputError("every cluster's candidates must be a list")
            items.extend((cluster, keyword, None) for keyword in keywords)
    else:
        raise IdeasInputError("ideas file needs an 'ideas' list or a 'candidates' object")

    if len(items) > MAX_IDEAS:
        raise IdeasInputError(f"too many ideas (max {MAX_IDEAS})")
    candidates: list[IdeaCandidate] = []
    for cluster, keyword, metrics in items:
        if not isinstance(keyword, str) or not keyword.strip():
            raise IdeasInputError("every idea needs a non-empty keyword string")
        if cluster is not None and cluster not in TARGET_CLUSTERS:
            raise IdeasInputError(f"unknown cluster {cluster!r} (allowed: {list(TARGET_CLUSTERS)})")
        candidates.append(IdeaCandidate(keyword=keyword.strip(), cluster=cluster, metrics=metrics))
    return candidates


def _metric_text(decision: ExpansionDecision) -> str:
    if decision.metrics == NOT_AVAILABLE:
        return "metrics=not_available"
    m = decision.metrics
    avg = m.get("avg_monthly_searches") if isinstance(m, dict) else None
    comp = m.get("competition") if isinstance(m, dict) else None
    return f"avg={avg if avg is not None else NOT_AVAILABLE} competition={comp or NOT_AVAILABLE}"


def _print_table(plan: ExpansionPlan) -> None:
    s = plan.summary
    print("=== Keyword Expansion Plan (READ-ONLY: no Google call, no DB write, no insert) ===")
    print(
        f"candidates={s['candidates']} keep={s['keep']} merge={s['merge']} reject={s['reject']} "
        f"metrics_available={s['metrics_available']}"
    )
    for kind in ("keep", "merge", "reject"):
        rows = [d for d in plan.decisions if d.decision == kind]
        print(f"\n-- {kind} ({len(rows)}) --")
        for d in rows:
            head = f"{d.cluster or '-'} | {d.keyword} | {d.reason_code}"
            if kind == "keep":
                aff = f"affiliate={d.affiliate.level}({d.affiliate.program_count})"
                print(
                    f"   {head} | {d.intent}/{d.serp_family} | risk={d.overlap_risk} | {aff} | "
                    f"{_metric_text(d)}"
                )
                if d.overlap_note:
                    print(f"      {d.overlap_note}")
            elif kind == "merge":
                print(f"   {head} -> {d.target} [{d.target_kind}]")
            else:
                print(f"   {head}" + (f" (rule {d.matched_rule})" if d.matched_rule else ""))
    if plan.warnings:
        print("\n-- warnings --")
        for warning in plan.warnings:
            print(f"   {warning}")


def run(
    *,
    ideas_file: str | Path,
    cluster_config: str | Path = DEFAULT_CLUSTER_CONFIG,
    rules_path: str | Path = DEFAULT_RULES,
    output_format: str = "table",
    session_factory=SessionLocal,
) -> int:
    try:
        config = load_cluster_config(cluster_config)
        rules = load_expansion_rules(rules_path)
    except (ClusterConfigError, ExpansionRulesError) as exc:
        print("CONFIG ERROR:")
        for error in exc.errors:
            print(f"  - {error}")
        return EXIT_CONFIG
    try:
        candidates = load_ideas(ideas_file)
    except IdeasInputError as exc:
        print(f"INPUT ERROR: {exc}")
        return EXIT_INPUT

    with session_factory() as session:
        try:
            plan = KeywordExpansionService(session).plan(config, rules, candidates)
        except ClusterConfigError as exc:
            print("CONFIG ERROR:")
            for error in exc.errors:
                print(f"  - {error}")
            return EXIT_CONFIG
        finally:
            session.rollback()

    if output_format == "json":
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_table(plan)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="plan_keyword_expansion",
        description=(
            "Read-only keep/merge/reject planner for keyword-idea candidates "
            "(no Google call, no DB write, no keyword insert)."
        ),
    )
    parser.add_argument("--ideas-file", required=True)
    parser.add_argument("--config", default=str(DEFAULT_CLUSTER_CONFIG))
    parser.add_argument("--rules", default=str(DEFAULT_RULES))
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    try:
        return run(
            ideas_file=args.ideas_file,
            cluster_config=args.config,
            rules_path=args.rules,
            output_format=args.format,
        )
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
