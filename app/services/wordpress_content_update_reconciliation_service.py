"""WordPressContentUpdateReconciliationService -- ``outcome_unknown`` で終わった
content-update run を、live な **読み取りだけ** で事後照合する。

背景 (D-D5A.1 §16-17): ``outcome_unknown`` は「WordPress へ write request を
送ったが、その効果を検証できなかった」という terminal な事実であり、自動 retry も
``succeeded`` への遷移も禁止されている。一方で write が実際には成功していた場合、
その run は永久に未解決として新しい content update を blocking し続ける。

この service はその 1 点だけを解く:

1. 対象 run が ``outcome_unknown`` **であることのみ** を受け入れる
   (``succeeded`` / ``failed`` / ``running`` は拒否 -- 確定済みの結果を後から
   塗り替えない)。
2. run が指定された article / WordPress post を所有していることを検証する。
3. WordPress を **ちょうど 1 回 GET** する (write は絶対に発行しない)。
4. live な post が run の意図した payload と content-equivalent かを
   :func:`compare_wordpress_content` で判定する。raw hash と request hash を
   等値比較することはない (別 namespace -- D-D5A.1 §3)。
5. 一致すれば ``reconciled_succeeded`` を、しなければ ``unresolved`` を
   :class:`WordPressContentUpdateReconciliation` として append する。run 行の
   status は **どちらの場合も書き換えない**。

``reconciled_succeeded`` の記録がある run だけが blocking から外れる
(:meth:`WordPressContentUpdateRunRepository.find_blocking_run_for_post`)。
``unresolved`` は blocking を維持したまま「調べたが解決しなかった」事実を残す
fail-closed な結果であり、その article の以後の content update は人間が介入する
まで進まない。

2 回目以降の呼び出しは既存の記録をそのまま返す no-op で、GET も追加の行も
発生しない (run ごとに verdict は 1 つだけ -- UNIQUE 制約で DB 側も保証する)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import get_settings
from app.exceptions import WordPressContentUpdateReconciliationError
from app.models import WordPressContentUpdateRun
from app.models.wordpress_content_update_reconciliation import (
    WP_CU_RECONCILED_SUCCEEDED,
    WP_CU_RECONCILED_UNRESOLVED,
)
from app.models.wordpress_content_update_run import WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
from app.repositories.article_repository import ArticleRepository
from app.repositories.wordpress_content_update_reconciliation_repository import (
    WordPressContentUpdateReconciliationRepository,
)
from app.wordpress.client import WordPressClient
from app.wordpress.content_reconciliation import compare_wordpress_content

_SUPPORTED_WORDPRESS_STATUSES = frozenset({"publish", "draft"})

# -- unresolved reason codes (機械可読。人間向けの reason とは別) --------------
REASON_WORDPRESS_FETCH_FAILED = "WORDPRESS_FETCH_FAILED"
REASON_WORDPRESS_POST_ID_MISMATCH = "WORDPRESS_POST_ID_MISMATCH"
REASON_WORDPRESS_STATUS_MISMATCH = "WORDPRESS_STATUS_MISMATCH"
REASON_WORDPRESS_CONTENT_MISSING = "WORDPRESS_CONTENT_MISSING"
REASON_CONTENT_NOT_EQUIVALENT = "CONTENT_NOT_EQUIVALENT"
REASON_TITLE_MISMATCH = "TITLE_MISMATCH"
REASON_EXCERPT_MISMATCH = "EXCERPT_MISMATCH"


@dataclass(frozen=True)
class ContentUpdateReconciliationResult:
    """安全な事実のみを含む immutable な照合結果 (本文・token は含まない)。"""

    run_id: int
    article_id: int
    wordpress_post_id: str
    reconciliation_id: int
    verdict: str
    unresolved_reason_code: str | None
    already_reconciled: bool
    observed_wordpress_status: str | None
    observed_wordpress_raw_content_hash: str | None
    raw_content_changed_since_attempt: bool | None

    @property
    def resolved(self) -> bool:
        return self.verdict == WP_CU_RECONCILED_SUCCEEDED


class WordPressContentUpdateReconciliationService:
    def __init__(
        self, session: Session, *, wordpress_client: WordPressClient | None = None
    ) -> None:
        self._session = session
        self._wordpress_client = wordpress_client
        self._articles = ArticleRepository(session)
        self._reconciliations = WordPressContentUpdateReconciliationRepository(session)

    def reconcile(
        self,
        *,
        run_id: int,
        expected_article_id: int,
        expected_wordpress_post_id: str,
        expected_wordpress_status: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> ContentUpdateReconciliationResult:
        run = self._session.get(WordPressContentUpdateRun, run_id)
        if run is None:
            raise WordPressContentUpdateReconciliationError(f"run {run_id} not found")

        # -- 既に verdict がある run は no-op (GET も追加の行も発生しない) ------
        existing = self._reconciliations.get_for_run(run.id)
        if existing is not None:
            return ContentUpdateReconciliationResult(
                run_id=run.id,
                article_id=existing.article_id,
                wordpress_post_id=existing.wordpress_post_id,
                reconciliation_id=existing.id,
                verdict=existing.verdict,
                unresolved_reason_code=existing.unresolved_reason_code,
                already_reconciled=True,
                observed_wordpress_status=existing.observed_wordpress_status,
                observed_wordpress_raw_content_hash=(existing.observed_wordpress_raw_content_hash),
                raw_content_changed_since_attempt=_as_bool(
                    existing.comparison_json, "raw_content_changed_since_attempt"
                ),
            )

        # -- outcome_unknown 以外は一切受け付けない --------------------------
        if run.status != WP_CONTENT_UPDATE_OUTCOME_UNKNOWN:
            raise WordPressContentUpdateReconciliationError(
                f"run {run.id}: only '{WP_CONTENT_UPDATE_OUTCOME_UNKNOWN}' runs can be "
                f"reconciled (status is '{run.status}')"
            )

        # -- 所有権 (article / post) -----------------------------------------
        if run.article_id != expected_article_id:
            raise WordPressContentUpdateReconciliationError(
                f"run {run.id} belongs to article {run.article_id}, not {expected_article_id}"
            )
        if str(run.wordpress_post_id) != str(expected_wordpress_post_id):
            raise WordPressContentUpdateReconciliationError(
                f"run {run.id} targets post {run.wordpress_post_id!r}, "
                f"not {str(expected_wordpress_post_id)!r}"
            )
        article = self._articles.get_by_id(run.article_id)
        if article is None:
            raise WordPressContentUpdateReconciliationError(f"article {run.article_id} not found")
        if str(article.wordpress_post_id or "") != str(run.wordpress_post_id):
            raise WordPressContentUpdateReconciliationError(
                f"article {article.id} no longer owns post {run.wordpress_post_id!r}"
            )
        if expected_wordpress_status not in _SUPPORTED_WORDPRESS_STATUSES:
            raise WordPressContentUpdateReconciliationError(
                f"unsupported expected wordpress status: {expected_wordpress_status!r}"
            )
        if not (reason or "").strip():
            raise WordPressContentUpdateReconciliationError("reason is required")

        intended_content = _intended_content(run)
        if intended_content is None:
            raise WordPressContentUpdateReconciliationError(
                f"run {run.id}: stored update payload is unreadable"
            )

        # -- exactly ONE read-only GET; この service は write を一切行わない ---
        client = self._wordpress_client or WordPressClient(get_settings())
        try:
            live = client.get_post(int(run.wordpress_post_id))
        except Exception as exc:  # noqa: BLE001 - 読み取り失敗は unresolved の証跡
            return self._record(
                run,
                verdict=WP_CU_RECONCILED_UNRESOLVED,
                unresolved_reason_code=REASON_WORDPRESS_FETCH_FAILED,
                reason=reason,
                comparison={"fetch_error": type(exc).__name__},
                idempotency_key=idempotency_key,
            )

        live_id = live.get("id")
        live_status = live.get("status") if isinstance(live.get("status"), str) else None
        live_raw = _raw_only(live.get("content"))
        live_title = _raw_only(live.get("title"))
        live_excerpt = _raw_only(live.get("excerpt"))
        live_modified_gmt = live.get("modified_gmt")
        live_modified_gmt = live_modified_gmt if isinstance(live_modified_gmt, str) else None

        if live_id != int(run.wordpress_post_id):
            return self._record(
                run,
                verdict=WP_CU_RECONCILED_UNRESOLVED,
                unresolved_reason_code=REASON_WORDPRESS_POST_ID_MISMATCH,
                reason=reason,
                comparison={"observed_post_id": live_id},
                observed_wordpress_status=live_status,
                observed_modified_gmt_raw=live_modified_gmt,
                idempotency_key=idempotency_key,
            )
        if live_status != expected_wordpress_status:
            return self._record(
                run,
                verdict=WP_CU_RECONCILED_UNRESOLVED,
                unresolved_reason_code=REASON_WORDPRESS_STATUS_MISMATCH,
                reason=reason,
                comparison={"expected_wordpress_status": expected_wordpress_status},
                observed_wordpress_status=live_status,
                observed_modified_gmt_raw=live_modified_gmt,
                idempotency_key=idempotency_key,
            )
        if not live_raw:
            return self._record(
                run,
                verdict=WP_CU_RECONCILED_UNRESOLVED,
                unresolved_reason_code=REASON_WORDPRESS_CONTENT_MISSING,
                reason=reason,
                comparison={},
                observed_wordpress_status=live_status,
                observed_modified_gmt_raw=live_modified_gmt,
                idempotency_key=idempotency_key,
            )

        observed_raw_hash = compute_text_hash(live_raw)
        equivalence = compare_wordpress_content(intended_content, live_raw)
        # raw namespace 内の正当な比較: 試行前の baseline から live が動いたか。
        # 動いていなければ write は反映されていない (それ自体は結論ではなく証跡)。
        raw_changed = observed_raw_hash != run.observed_pre_update_wordpress_raw_content_hash
        title_matches = live_title == (article.title or "")
        excerpt_matches = live_excerpt == (article.meta_description or "")

        comparison: dict[str, object] = {
            **equivalence.as_dict(),
            "raw_content_changed_since_attempt": raw_changed,
            "title_matches_article": title_matches,
            "excerpt_matches_article": excerpt_matches,
        }

        # content-equivalence が主たる判定。title/excerpt は content-update payload の
        # 一部ではないが、post 全体が想定どおりの状態かを fail-closed に確かめる。
        if not equivalence.equivalent:
            code = REASON_CONTENT_NOT_EQUIVALENT
        elif not title_matches:
            code = REASON_TITLE_MISMATCH
        elif not excerpt_matches:
            code = REASON_EXCERPT_MISMATCH
        else:
            code = None

        return self._record(
            run,
            verdict=(WP_CU_RECONCILED_SUCCEEDED if code is None else WP_CU_RECONCILED_UNRESOLVED),
            unresolved_reason_code=code,
            reason=reason,
            comparison=comparison,
            observed_wordpress_status=live_status,
            observed_wordpress_raw_content_hash=observed_raw_hash,
            observed_modified_gmt_raw=live_modified_gmt,
            idempotency_key=idempotency_key,
        )

    # -- persistence (transaction owner) -------------------------------------
    def _record(
        self,
        run: WordPressContentUpdateRun,
        *,
        verdict: str,
        unresolved_reason_code: str | None,
        reason: str,
        comparison: dict[str, object],
        observed_wordpress_status: str | None = None,
        observed_wordpress_raw_content_hash: str | None = None,
        observed_modified_gmt_raw: str | None = None,
        idempotency_key: str | None = None,
    ) -> ContentUpdateReconciliationResult:
        row = self._reconciliations.add(
            wordpress_content_update_run_id=run.id,
            article_id=run.article_id,
            wordpress_post_id=str(run.wordpress_post_id),
            verdict=verdict,
            unresolved_reason_code=unresolved_reason_code,
            reason=reason.strip(),
            observed_wordpress_status=observed_wordpress_status,
            observed_wordpress_raw_content_hash=observed_wordpress_raw_content_hash,
            observed_modified_gmt_raw=observed_modified_gmt_raw,
            pre_update_wordpress_raw_content_hash=(
                run.observed_pre_update_wordpress_raw_content_hash
            ),
            request_content_hash=run.request_content_hash,
            comparison_json=comparison,
            idempotency_key=idempotency_key,
        )
        self._session.commit()
        return ContentUpdateReconciliationResult(
            run_id=run.id,
            article_id=row.article_id,
            wordpress_post_id=row.wordpress_post_id,
            reconciliation_id=row.id,
            verdict=row.verdict,
            unresolved_reason_code=row.unresolved_reason_code,
            already_reconciled=False,
            observed_wordpress_status=row.observed_wordpress_status,
            observed_wordpress_raw_content_hash=row.observed_wordpress_raw_content_hash,
            raw_content_changed_since_attempt=_as_bool(
                comparison, "raw_content_changed_since_attempt"
            ),
        )


def _intended_content(run: WordPressContentUpdateRun) -> str | None:
    """run が送ろうとした exact な payload から ``content`` を取り出す。"""

    try:
        payload = json.loads(run.update_payload_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    return content if isinstance(content, str) and content else None


def _raw_only(field: object) -> str | None:
    if isinstance(field, dict):
        value = field.get("raw")
        return value if isinstance(value, str) else None
    return field if isinstance(field, str) else None


def _as_bool(comparison: object, key: str) -> bool | None:
    if isinstance(comparison, dict):
        value = comparison.get(key)
        if isinstance(value, bool):
            return value
    return None
