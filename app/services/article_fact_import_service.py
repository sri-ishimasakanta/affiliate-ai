"""1 つの JSON import file を **1 transaction** で取り込む Service。

Source を数件作った後に Fact validation で失敗したら Source / Fact とも全 rollback する
(partial state を作らない)。このため sub-service の commit は使わず、Repository を
直接使い、この Service が transaction owner になる。DB write 以外の副作用なし
(Web アクセスなし)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.article.schemas import ArticleFactCreate, SourceCreate
from app.exceptions import FactValidationError
from app.models import Source
from app.repositories.article_repository import ArticleRepository
from app.repositories.source_repository import SourceRepository
from app.services.article_fact_service import ArticleFactService
from app.services.source_service import SourceService


@dataclass
class FactImportResult:
    article_id: int
    sources_created: int = 0
    sources_reused: int = 0
    facts_created: int = 0
    facts_skipped_same: int = 0
    dry_run: bool = False
    messages: list[str] = field(default_factory=list)


def _parse_dt(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise FactValidationError(f"{field_name} must be an ISO-8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FactValidationError(f"{field_name} is not ISO-8601: {value!r}") from exc


class ArticleFactImportService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._sources_repo = SourceRepository(session)
        self._source_service = SourceService(session)
        self._fact_service = ArticleFactService(session)

    def run(
        self, *, article_id: int, payload: dict, dry_run: bool = False
    ) -> FactImportResult:
        result = FactImportResult(article_id=article_id, dry_run=dry_run)

        if self._articles.get_by_id(article_id) is None:
            raise FactValidationError(f"Article {article_id} not found")
        body_article_id = payload.get("article_id")
        if body_article_id is not None and int(body_article_id) != article_id:
            raise FactValidationError(
                "file article_id does not match --article-id"
            )

        raw_sources = payload.get("sources") or []
        raw_tools = payload.get("tools") or []
        if not isinstance(raw_sources, list) or not isinstance(raw_tools, list):
            raise FactValidationError("'sources' and 'tools' must be lists")

        try:
            tmp_to_source: dict[str, Source] = {}
            for idx, raw in enumerate(raw_sources):
                tmp_id, source = self._resolve_source(article_id, raw, idx, result, dry_run)
                if tmp_id in tmp_to_source:
                    raise FactValidationError(f"duplicate source tmp_id: {tmp_id!r}")
                if source is not None:
                    tmp_to_source[tmp_id] = source

            for t_idx, tool in enumerate(raw_tools):
                self._process_tool(
                    article_id, tool, t_idx, tmp_to_source, result, dry_run
                )

            if dry_run:
                self._session.rollback()
            else:
                self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return result

    # -- sources --------------------------------------------------
    def _resolve_source(
        self,
        article_id: int,
        raw: object,
        idx: int,
        result: FactImportResult,
        dry_run: bool,
    ) -> tuple[str, Source | None]:
        if not isinstance(raw, dict):
            raise FactValidationError(f"sources[{idx}] must be an object")
        tmp_id = str(raw.get("tmp_id") or "").strip()
        if not tmp_id:
            raise FactValidationError(f"sources[{idx}].tmp_id is required")

        payload = SourceCreate(
            source_type=raw.get("source_type"),
            source_url=str(raw.get("source_url") or ""),
            title=raw.get("title"),
            checked_at=_parse_dt(raw.get("checked_at"), f"sources[{idx}].checked_at"),
        )
        canonical = self._source_service.safe_url(payload.source_url)
        checked_at = self._source_service.require_aware_not_future(payload.checked_at)

        existing = self._sources_repo.find_observation(
            article_id=article_id, source_url=canonical, checked_at=checked_at
        )
        if existing is not None:
            result.sources_reused += 1
            return tmp_id, existing

        result.sources_created += 1
        if dry_run:
            result.messages.append(f"sources[{idx}] ok (dry-run): {canonical}")
            # dry-run でも後続 fact の source 参照解決のため一時的に flush する
            entity = self._sources_repo.create(
                article_id=article_id,
                source_type=payload.source_type,
                source_url=canonical,
                title=payload.title,
                checked_at=checked_at,
            )
            return tmp_id, entity
        entity = self._sources_repo.create(
            article_id=article_id,
            source_type=payload.source_type,
            source_url=canonical,
            title=payload.title,
            checked_at=checked_at,
        )
        return tmp_id, entity

    # -- tools / facts ----------------------------------------
    def _process_tool(
        self,
        article_id: int,
        tool: object,
        t_idx: int,
        tmp_to_source: dict[str, Source],
        result: FactImportResult,
        dry_run: bool,
    ) -> None:
        if not isinstance(tool, dict):
            raise FactValidationError(f"tools[{t_idx}] must be an object")
        subject_ref = str(tool.get("subject_ref") or "").strip()
        if not subject_ref:
            raise FactValidationError(f"tools[{t_idx}].subject_ref is required")
        affiliate_program_id = tool.get("affiliate_program_id")
        facts = tool.get("facts") or {}
        if not isinstance(facts, dict):
            raise FactValidationError(f"tools[{t_idx}].facts must be an object")

        for fact_key, raw in facts.items():
            if not isinstance(raw, dict):
                raise FactValidationError(
                    f"tools[{t_idx}].facts.{fact_key} must be an object"
                )
            source_id: int | None = None
            tmp_source = raw.get("source")
            if tmp_source is not None:
                key = str(tmp_source).strip()
                if key not in tmp_to_source:
                    raise FactValidationError(
                        f"tools[{t_idx}].facts.{fact_key}: unknown source tmp_id {key!r}"
                    )
                source_id = tmp_to_source[key].id

            payload = ArticleFactCreate(
                subject_ref=subject_ref,
                affiliate_program_id=affiliate_program_id,
                fact_key=fact_key,
                fact_value=raw.get("value"),
                value_status=raw.get("value_status"),
                unknown_reason=raw.get("unknown_reason"),
                source_id=source_id,
                checked_at=_parse_dt(
                    raw.get("checked_at"),
                    f"tools[{t_idx}].facts.{fact_key}.checked_at",
                ),
            )
            _entity, created = self._fact_service.append_validated(article_id, payload)
            if created:
                result.facts_created += 1
            else:
                result.facts_skipped_same += 1
