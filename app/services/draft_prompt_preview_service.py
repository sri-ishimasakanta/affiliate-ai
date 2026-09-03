"""DraftPromptPackage の read-only preview (§32/§33)。

DB write 0 / LLM 0。frozen Snapshot payload + 検証済み overrides から PromptPackage と
rendered prompt を組み、静的な安全検査サマリを返す。builder / renderer は pure。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.article.draft_prompt_canonical import (
    compute_prompt_input_hash,
    compute_rendered_prompt_hash,
)
from app.article.draft_prompt_package import (
    FORBIDDEN_PACKAGE_KEYS,
    EditorialOverridesV1,
    build_prompt_package,
)
from app.article.draft_prompt_render import render_prompt
from app.exceptions import DraftGenerationNotReadyError, EntityNotFoundError
from app.repositories.article_repository import ArticleRepository
from app.repositories.draft_input_snapshot_repository import (
    DraftInputSnapshotRepository,
)

_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "token", "secret", "password", "credential", "authorization"}
)


def _count_forbidden_keys(obj: object, keys: frozenset[str]) -> int:
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys:
                n += 1
            n += _count_forbidden_keys(v, keys)
    elif isinstance(obj, list):
        for item in obj:
            n += _count_forbidden_keys(item, keys)
    return n


class DraftPromptPreviewService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._snapshots = DraftInputSnapshotRepository(session)

    def preview(
        self,
        article_id: int,
        *,
        snapshot_id: int,
        overrides: EditorialOverridesV1,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(UTC)

        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError("Article", article_id)
        snap = self._snapshots.get_by_id(snapshot_id)
        if snap is None:
            raise EntityNotFoundError("DraftInputSnapshot", snapshot_id)
        if snap.article_id != article_id:
            raise DraftGenerationNotReadyError(
                f"snapshot {snapshot_id} belongs to article {snap.article_id}, "
                f"not {article_id}"
            )

        package = build_prompt_package(
            snapshot_payload=snap.payload,
            snapshot_id=snap.id,
            snapshot_content_hash=snap.content_hash,
            overrides=overrides,
            now=now,
        )
        rendered = render_prompt(package)
        prompt_input_hash = compute_prompt_input_hash(package)
        rendered_prompt_hash = compute_rendered_prompt_hash(rendered)

        unknown_cells = sum(len(t["unknown_fact_keys"]) for t in package["comparison_tools"])
        usable_total = sum(len(t["usable_facts"]) for t in package["comparison_tools"])
        not_researched_total = sum(
            len(t["not_researched_fact_keys"]) for t in package["comparison_tools"]
        )

        validation_summary = {
            "forbidden_structural_keys": _count_forbidden_keys(
                package, FORBIDDEN_PACKAGE_KEYS
            ),
            "secret_keys": _count_forbidden_keys(package, _SECRET_KEYS),
            "snapshot_binding_valid": package["snapshot_content_hash"] == snap.content_hash,
            "rendered_hash_valid": (
                compute_rendered_prompt_hash(rendered) == rendered_prompt_hash
            ),
            "comparison_tools": len(package["comparison_tools"]),
            "usable_facts_total": usable_total,
            "unknown_fact_restrictions": unknown_cells,
            "not_researched_fact_keys_total": not_researched_total,
            "commission_to_llm": package["editorial_overrides"]["commission_to_llm"],
        }
        estimated_size = {
            "prompt_package_chars": len(str(package)),
            "rendered_prompt_chars": len(rendered),
            "rendered_prompt_bytes": len(rendered.encode("utf-8")),
            "rendered_prompt_estimated_tokens": len(rendered) // 3,
        }

        return {
            "article_id": article_id,
            "snapshot_id": snap.id,
            "prompt_package_version": package["prompt_package_version"],
            "prompt_builder_version": package["prompt_builder_version"],
            "template_version": package["template_version"],
            "prompt_input_hash": prompt_input_hash,
            "rendered_prompt_hash": rendered_prompt_hash,
            "prompt_package": package,
            "rendered_prompt": rendered,
            "validation_summary": validation_summary,
            "estimated_size": estimated_size,
        }
