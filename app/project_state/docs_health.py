"""ドキュメントの健康 (今の状態を述べる文が、強い出どころと食い違っていないか。pure)。

- 「今の状態を述べる文」だけを見る (``STATE_CLAIMS``)。過去の出来事の記録は古くない。
- 直したドキュメントには ``<!-- state-corrected: <日付> <何を> -->`` の印を残す。報告は
  それを「直したもの」として並べる。
- 実行の設定の中の説明の文 (policy の ``note``) は運用のドキュメントより弱い。設定そのものは
  変えずに、人が直す候補として出す。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path

from app.project_state.precedence import finding

CORRECTED_MARK = re.compile(r"<!-- state-corrected: (\d{4}-\d{2}-\d{2}[^>]*?) -->")
SCANNED = ("docs", "wordpress/mu-plugins")


def _claims() -> list[dict]:
    """今の状態を述べる文 → 強い出どころの値と比べる関数。"""

    return [
        {
            "id": "doc-threads-autopublish-disabled",
            "path": "docs/operations/threads-autopublish.md",
            "pattern": r"本番では無効",
            "field": "threads.automatic_publication",
            "claims": "automatic publication is disabled in production",
            "contradicted": lambda s: (
                _get(s, "threads", "policy", "automatic_publication_enabled") is True
            ),
            "authority": lambda s: "enabled (policy + worker start-up record)",
            "authoritative_source": "app/config/threads_operations_policy.json",
        },
        {
            "id": "doc-relay-not-deployed",
            "path": "wordpress/mu-plugins/bizfluxlab-approval-relay.README.md",
            "pattern": r"\*\*Status: NOT DEPLOYED\.\*\*",
            "field": "approvals.relay_deployment",
            "claims": "the relay is not deployed",
            "contradicted": lambda s: (
                _get(s, "approvals", "relay_t6_1_deployment_recorded") is True
            ),
            "authority": lambda s: "deployed (T6.1 deployment record, 2026-09-25)",
            "authoritative_source": "relay README §T6.1 deployment record",
        },
        {
            "id": "doc-stock-db-revision",
            "path": "docs/operations/threads-proposal-stock.md",
            "pattern": r"本番 DB は `([0-9a-f]{12})` のまま",
            "field": "database.revision",
            "claims": "the production DB is still at an older revision",
            "contradicted": lambda s, m=None: (
                bool(_get(s, "database", "db_revisions"))
                and (m is None or m not in (_get(s, "database", "db_revisions") or []))
            ),
            "authority": lambda s: str(_get(s, "database", "db_revisions")),
            "authoritative_source": "alembic_version (live, read-only)",
        },
    ]


def _get(state: Mapping, *path):
    node = state
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def check_claims(root: Path, state: Mapping) -> list[dict]:
    """食い違っている「今の状態」の文 → ``stale_doc`` の finding。"""

    out = []
    for claim in _claims():
        path = root / claim["path"]
        if not path.exists():
            continue
        match = re.search(claim["pattern"], path.read_text(encoding="utf-8"))
        if not match:
            continue
        captured = match.group(1) if match.groups() else None
        contradicted: Callable = claim["contradicted"]
        is_stale = contradicted(state, captured) if captured is not None else contradicted(state)
        if not is_stale:
            continue
        out.append(
            finding(
                claim["id"],
                "documentation",
                claim["field"],
                authoritative_value=claim["authority"](state),
                conflicting_value=f"{claim['claims']} ({match.group(0)})",
                authoritative_source=claim["authoritative_source"],
                conflicting_source=claim["path"],
                classification="stale_doc",
                severity="low",
                recommended_resolution=(
                    "correct the document (documentation only; no production change)"
                ),
            )
        )
    note = _get(state, "threads", "policy", "policy_note") or ""
    if _get(state, "threads", "policy", "automatic_publication_enabled") is True and (
        "disabled" in note.lower()
    ):
        out.append(
            finding(
                "config-note-threads-autopublish",
                "documentation",
                "threads.automatic_publication.note",
                authoritative_value="enabled",
                conflicting_value=note,
                authoritative_source="threads_operations_policy.json automatic_publication.enabled",
                conflicting_source="threads_operations_policy.json automatic_publication.note",
                classification="stale_doc",
                severity="low",
                recommended_resolution=(
                    "a human updates the note text in the policy file (the value is correct; "
                    "T7 does not edit runtime configuration)"
                ),
            )
        )
    return out


def corrected(root: Path) -> list[dict]:
    rows = []
    for base in SCANNED:
        directory = root / base
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            for mark in CORRECTED_MARK.findall(path.read_text(encoding="utf-8")):
                rows.append({"path": path.relative_to(root).as_posix(), "note": mark.strip()})
    return rows


def documentation_health(root: Path, state: Mapping, stale: list[dict]) -> dict:
    doc_stale = [f for f in stale if f["conflicting_source"].endswith(".md")]
    unresolved = [f for f in stale if not f["conflicting_source"].endswith(".md")]
    return {
        "stale_documents": [
            {
                "id": f["id"],
                "path": f["conflicting_source"],
                "claims": f["conflicting_value"],
                "live": f["authoritative_value"],
            }
            for f in doc_stale
        ],
        "corrected_documents": corrected(root),
        "unresolved_mismatches": [
            {
                "id": f["id"],
                "where": f["conflicting_source"],
                "text": f["conflicting_value"],
                "resolution": f["recommended_resolution"],
            }
            for f in unresolved
        ],
        "rule": "only statements that claim to describe the current state are checked; "
        "historical records are not stale",
    }
