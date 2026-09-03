"""DraftGenerationRun の prepare / execute / submit-result オーケストレーション。

- ``prepare``       : PromptPackage を組み hash を確認し ``status=prepared`` の run を
                      append する。**LLM call なし / Article status 変更なし** (§37)。
- ``execute``       : integrity 検証 → gate → Tx1 で ``prepared→running`` と
                      Article ``planned→drafting`` を 1 transaction。V1 は manual mode
                      のみで外部呼び出しをせず、保存済み ``rendered_prompt`` を返す (§41)。
- ``submit_result`` : manual mode の Human 出力を parse・validate し ``succeeded`` /
                      ``failed`` を確定する (§44)。**Article.body / meta は変更しない** (§52)。

Repository は commit しない。この Service が transaction owner (§55)。
execute はビルダー/レンダラを再呼び出しせず、run に保存済みの artifact を正とする (§42)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.article.draft_input_canonical import canonical_json
from app.article.draft_output_contract import DraftContractError, parse_draft_output
from app.article.draft_output_validators import validate_draft_output
from app.article.draft_prompt_canonical import (
    compute_prompt_input_hash,
    compute_rendered_prompt_hash,
)
from app.article.draft_prompt_package import (
    EditorialOverridesV1,
    build_prompt_package,
)
from app.article.draft_prompt_render import render_prompt
from app.article.fact_freshness import to_storage_utc
from app.exceptions import (
    DraftGenerationNotReadyError,
    DraftGenerationStateError,
    EntityNotFoundError,
    PromptInputChangedError,
)
from app.models import DraftGenerationRun
from app.models.draft_generation_run import (
    EXECUTION_MODES,
    MODE_MANUAL,
    PROMPT_BUILDER_VERSION,
    PROMPT_PACKAGE_VERSION,
    PROMPT_TEMPLATE_VERSION,
    RUN_PREPARED,
    RUN_RUNNING,
)
from app.models.enums import ArticleStatus
from app.repositories.article_repository import ArticleRepository
from app.repositories.draft_generation_run_repository import (
    DraftGenerationRunRepository,
)
from app.repositories.draft_input_snapshot_repository import (
    DraftInputSnapshotRepository,
)
from app.services.draft_generation_adapters import ManualAdapter, sanitize_provider_error
from app.services.status_transitions import (
    ARTICLE_TRANSITIONS,
    ensure_transition_allowed,
)

_ENTITY = "DraftGenerationRun"
_ARTICLE = "Article"
_ALLOWED_ARTICLE_STATES = frozenset(
    {ArticleStatus.PLANNED.value, ArticleStatus.DRAFTING.value}
)


class DraftGenerationRunService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._snapshots = DraftInputSnapshotRepository(session)
        self._runs = DraftGenerationRunRepository(session)

    # -- prepare -----------------------------------------------------
    def prepare(
        self,
        article_id: int,
        *,
        snapshot_id: int,
        expected_prompt_hash: str,
        expected_rendered_prompt_hash: str,
        execution_mode: str,
        editorial_overrides: EditorialOverridesV1,
        provider: str | None = None,
        model: str | None = None,
        generation_parameters: dict | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[DraftGenerationRun, bool]:
        now = now or datetime.now(UTC)

        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        snap = self._snapshots.get_by_id(snapshot_id)
        if snap is None:
            raise EntityNotFoundError("DraftInputSnapshot", snapshot_id)
        if snap.article_id != article_id:
            raise DraftGenerationNotReadyError(
                f"snapshot {snapshot_id} belongs to article {snap.article_id}"
            )
        if execution_mode not in EXECUTION_MODES:
            raise DraftGenerationStateError(
                f"unknown execution_mode {execution_mode!r}"
            )
        if str(article.status) not in _ALLOWED_ARTICLE_STATES:
            raise DraftGenerationStateError(
                f"Article status {article.status!r} does not allow generation "
                "(only planned / drafting)"
            )
        if article.body is not None:
            raise DraftGenerationStateError(
                "Article.body is already set; refusing to prepare a new generation "
                "run (promotion is a separate phase)"
            )

        package = build_prompt_package(
            snapshot_payload=snap.payload,
            snapshot_id=snap.id,
            snapshot_content_hash=snap.content_hash,
            overrides=editorial_overrides,
            now=now,
        )
        rendered = render_prompt(package)
        prompt_input_hash = compute_prompt_input_hash(package)
        rendered_prompt_hash = compute_rendered_prompt_hash(rendered)

        if expected_prompt_hash != prompt_input_hash:
            raise PromptInputChangedError(
                "expected_prompt_hash", expected_prompt_hash, prompt_input_hash
            )
        if expected_rendered_prompt_hash != rendered_prompt_hash:
            raise PromptInputChangedError(
                "expected_rendered_prompt_hash",
                expected_rendered_prompt_hash,
                rendered_prompt_hash,
            )

        identity = {
            "article_id": article_id,
            "snapshot_id": snap.id,
            "prompt_input_hash": prompt_input_hash,
            "rendered_prompt_hash": rendered_prompt_hash,
            "execution_mode": execution_mode,
            "provider": provider,
            "model": model,
        }
        gp_canon = canonical_json(generation_parameters or {})

        # idempotency_key: 既存 run があればそれを返すか衝突エラー (§39)
        if idempotency_key is not None:
            existing = self._runs.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    self._runs.identity_of(existing) == identity
                    and canonical_json(existing.generation_parameters or {}) == gp_canon
                ):
                    return existing, True
                raise DraftGenerationStateError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    "different generation identity"
                )

        # 非終端の同一 identity run があれば再利用 (§38)
        dup = self._runs.find_non_terminal_by_identity(identity)
        if dup is not None and canonical_json(dup.generation_parameters or {}) == gp_canon:
            return dup, True

        try:
            run = self._runs.append(
                article_id=article_id,
                snapshot_id=snap.id,
                snapshot_content_hash=snap.content_hash,
                execution_mode=execution_mode,
                provider=provider,
                model=model,
                prompt_template_version=PROMPT_TEMPLATE_VERSION,
                prompt_builder_version=PROMPT_BUILDER_VERSION,
                prompt_package=package,
                prompt_input_hash=prompt_input_hash,
                rendered_prompt=rendered,
                rendered_prompt_hash=rendered_prompt_hash,
                editorial_overrides=editorial_overrides.model_dump(mode="json"),
                generation_parameters=generation_parameters,
                idempotency_key=idempotency_key,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(run)
        return run, False

    # -- execute ---------------------------------------------------
    def execute(
        self, article_id: int, run_id: int, *, now: datetime | None = None
    ) -> tuple[DraftGenerationRun, str, str | None]:
        now = now or datetime.now(UTC)
        run = self._get_owned_run(article_id, run_id)
        self._verify_frozen_artifact(run)

        if run.status != RUN_PREPARED:
            raise DraftGenerationStateError(
                f"run {run_id} is {run.status!r}, expected {RUN_PREPARED!r}"
            )
        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        if str(article.status) not in _ALLOWED_ARTICLE_STATES:
            raise DraftGenerationStateError(
                f"Article status {article.status!r} does not allow execute"
            )
        other_running = self._runs.find_running_by_article(article_id)
        if other_running is not None:
            raise DraftGenerationStateError(
                f"run {other_running.id} for this article is already running"
            )

        if run.execution_mode != MODE_MANUAL:
            raise DraftGenerationStateError(
                f"execution_mode {run.execution_mode!r} is not enabled in this "
                "build (only manual)"
            )

        try:
            self._runs.mark_running(run, started_at=to_storage_utc(now))
            if str(article.status) == ArticleStatus.PLANNED.value:
                ensure_transition_allowed(
                    _ARTICLE,
                    ArticleStatus.PLANNED,
                    ArticleStatus.DRAFTING,
                    ARTICLE_TRANSITIONS,
                )
                article.status = ArticleStatus.DRAFTING.value
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(run)

        adapter = ManualAdapter()
        if not adapter.is_synchronous():
            return run, "submit_result", run.rendered_prompt
        # 将来 api / local_cli 用 (この build では到達しない)
        return run, "await_result", None  # pragma: no cover

    # -- submit-result (manual) ---------------------------------
    def submit_result(
        self,
        article_id: int,
        run_id: int,
        raw_output: str,
        *,
        now: datetime | None = None,
    ) -> DraftGenerationRun:
        now = now or datetime.now(UTC)
        run = self._get_owned_run(article_id, run_id)
        if run.status != RUN_RUNNING:
            raise DraftGenerationStateError(
                f"run {run_id} is {run.status!r}, expected {RUN_RUNNING!r}"
            )
        if run.execution_mode != MODE_MANUAL:
            raise DraftGenerationStateError(
                "submit-result is only for manual execution_mode"
            )

        try:
            parsed = parse_draft_output(raw_output)
        except DraftContractError as exc:
            try:
                self._runs.mark_failed(
                    run,
                    error_message=sanitize_provider_error(str(exc)),
                    finished_at=to_storage_utc(now),
                    raw_output=raw_output,
                )
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise
            self._session.refresh(run)
            return run

        report = validate_draft_output(parsed=parsed, package=run.prompt_package)
        try:
            self._runs.mark_succeeded(
                run,
                raw_output=raw_output,
                parsed_body=parsed.body_markdown,
                parsed_meta_description=parsed.meta_description[:400],
                generation_notes=list(parsed.generation_notes),
                validation_report=report,
                token_usage=None,
                finished_at=to_storage_utc(now),
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(run)
        return run

    # -- read ----------------------------------------------------
    def list_for_article(self, article_id: int) -> list[DraftGenerationRun]:
        self._ensure_article(article_id)
        return self._runs.list_by_article(article_id)

    def get(self, article_id: int, run_id: int) -> DraftGenerationRun:
        return self._get_owned_run(article_id, run_id)

    # -- helpers -----------------------------------------------
    def _ensure_article(self, article_id: int) -> None:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)

    def _get_owned_run(self, article_id: int, run_id: int) -> DraftGenerationRun:
        self._ensure_article(article_id)
        run = self._runs.get_by_id(run_id)
        if run is None or run.article_id != article_id:
            raise EntityNotFoundError(_ENTITY, run_id)
        return run

    def _verify_frozen_artifact(self, run: DraftGenerationRun) -> None:
        """execute 前: 保存済み artifact 自身の整合性を検証 (再 build しない, §6)。"""

        if compute_prompt_input_hash(run.prompt_package) != run.prompt_input_hash:
            raise DraftGenerationNotReadyError(
                f"run {run.id}: stored prompt_package hash does not match "
                "prompt_input_hash"
            )
        if compute_rendered_prompt_hash(run.rendered_prompt) != run.rendered_prompt_hash:
            raise DraftGenerationNotReadyError(
                f"run {run.id}: stored rendered_prompt hash does not match "
                "rendered_prompt_hash"
            )
        snap = self._snapshots.get_by_id(run.snapshot_id)
        if snap is None:
            raise DraftGenerationNotReadyError(
                f"run {run.id}: bound snapshot {run.snapshot_id} not found"
            )
        if snap.content_hash != run.snapshot_content_hash:
            raise DraftGenerationNotReadyError(
                f"run {run.id}: bound snapshot content_hash changed"
            )
        if snap.article_id != run.article_id:
            raise DraftGenerationNotReadyError(
                f"run {run.id}: snapshot/article binding mismatch"
            )
        pkg = run.prompt_package
        if (
            pkg.get("prompt_package_version") != PROMPT_PACKAGE_VERSION
            or pkg.get("prompt_builder_version") != PROMPT_BUILDER_VERSION
        ):  # noqa: E501 - 将来 v2 では別 execute path。今は v1 のみ
            raise DraftGenerationNotReadyError(
                f"run {run.id}: prompt package version not executable by this build"
            )
