"""WordPressContentUpdateRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ (commit は将来の execution service が
Transaction A / Transaction B の境界を制御する -- D-D5A.1 §23)。汎用 ``update`` /
``delete`` は持たない。``running`` 作成後の変更は狭い terminal 遷移メソッド
(``mark_succeeded`` / ``mark_failed`` / ``mark_outcome_unknown``) のみ、各メソッドは
:func:`wp_content_update_run_transition_allowed` で妥当性を検証してから mutate する。

D-D5B は local foundation のみ -- live preflight GET も WordPress write もこの
repository からは一切行わない (``add_running`` はローカル DB へ ``running`` 行を
append するだけ)。

D-D5B.1: ``running`` は「WordPress へ request が実際に送信済みであることの証明」
ではない -- durable に記録されるのは preflight 完了後・network-write 境界の
**直前** の update 試行/意図であり、commit 後 HTTP 送信前に crash する window が
理論上あるため、``running`` は意図的に blocking/ambiguous なまま (D-D5A.1
§16-17)。``add_running`` は preflight 証跡 (``expected_pre_update_...`` /
``observed_pre_update_...``) の両方を必須引数として要求し、欠落を flush 前に
拒否する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import WordPressContentUpdateRunError
from app.models import WordPressContentUpdateRun
from app.models.wordpress_content_update_run import (
    WP_CONTENT_UPDATE_FAILED,
    WP_CONTENT_UPDATE_OUTCOME_UNKNOWN,
    WP_CONTENT_UPDATE_RUNNING,
    WP_CONTENT_UPDATE_SUCCEEDED,
    wp_content_update_run_transition_allowed,
)

_LATEST_ORDER = (
    WordPressContentUpdateRun.created_at.desc(),
    WordPressContentUpdateRun.id.desc(),
)

# running/outcome_unknown な run は「まだ解決していない」= 新しい run の開始を
# blocking する (D-D5A.1 §16-17: outcome_unknown への自動 retry は禁止)。
_BLOCKING_STATUSES = (WP_CONTENT_UPDATE_RUNNING, WP_CONTENT_UPDATE_OUTCOME_UNKNOWN)


class WordPressContentUpdateRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # -- create (running から直接始まる -- prepared は持たない) -----------------
    def add_running(
        self,
        *,
        article_id: int,
        wordpress_post_id: str,
        article_publication_artifact_id: int,
        artifact_hash: str,
        method: str,
        endpoint_path: str,
        update_payload_json: str,
        update_payload_hash: str,
        content_update_request_identity_hash: str,
        target_content_update_request_identity_hash: str,
        target_base_url: str,
        request_content_hash: str,
        expected_pre_update_wordpress_raw_content_hash: str,
        observed_pre_update_wordpress_raw_content_hash: str,
        observed_pre_update_modified_gmt_raw: str | None = None,
        idempotency_key: str | None = None,
        started_at: datetime | None = None,
    ) -> WordPressContentUpdateRun:
        """``running`` 行を append する。

        D-D5B.1: ``running`` は live preflight 成功後にのみ作られる (D-D5A.1
        §21-23 -- durable な ``prepared`` 相当の事前状態は存在しない)。よって
        ``expected_pre_update_wordpress_raw_content_hash`` /
        ``observed_pre_update_wordpress_raw_content_hash`` の両方が必ず既知の
        はずであり、明示的な required keyword にして「欠落した preflight
        証跡」を late な DB ``IntegrityError`` より前、flush 前に拒否する。
        ``observed_pre_update_modified_gmt_raw`` は informational/audit のみ
        なので required にしない (D-D5A.1 §11, D-D5B.1 §4)。

        D-D5B.2: D-D5A.1 の確定した契約では、expected と observed が preflight
        時点で一致した場合にのみ (``UPDATE_REQUIRED`` かつ non-noop の場合)
        ``running`` 行が作られる -- 不一致 (``WORDPRESS_CURRENT_CONTENT_DRIFT``)
        は将来の D-D5C 分類 service の責務で、そこで run を一切作らずに fail
        closed する。したがって **永続化された** running 行は必ず
        ``expected == observed`` を満たす。この repository はその分類判断を
        代行しない (reason code はここに持ち込まない) が、モデル契約として
        「不一致な preflight 証跡から running 行を作れない」ことは flush 前に
        独立して guard する -- DB の ``CheckConstraint`` 一本に頼らない
        defense-in-depth (§6-7)。
        """

        self._require_raw_hash(
            "expected_pre_update_wordpress_raw_content_hash",
            expected_pre_update_wordpress_raw_content_hash,
        )
        self._require_raw_hash(
            "observed_pre_update_wordpress_raw_content_hash",
            observed_pre_update_wordpress_raw_content_hash,
        )
        self._require_pre_update_raw_match(
            expected_pre_update_wordpress_raw_content_hash,
            observed_pre_update_wordpress_raw_content_hash,
        )

        entity = WordPressContentUpdateRun(
            status=WP_CONTENT_UPDATE_RUNNING,
            article_id=article_id,
            wordpress_post_id=wordpress_post_id,
            article_publication_artifact_id=article_publication_artifact_id,
            artifact_hash=artifact_hash,
            method=method,
            endpoint_path=endpoint_path,
            update_payload_json=update_payload_json,
            update_payload_hash=update_payload_hash,
            content_update_request_identity_hash=content_update_request_identity_hash,
            target_content_update_request_identity_hash=(
                target_content_update_request_identity_hash
            ),
            target_base_url=target_base_url,
            request_content_hash=request_content_hash,
            expected_pre_update_wordpress_raw_content_hash=(
                expected_pre_update_wordpress_raw_content_hash
            ),
            observed_pre_update_wordpress_raw_content_hash=(
                observed_pre_update_wordpress_raw_content_hash
            ),
            observed_pre_update_modified_gmt_raw=observed_pre_update_modified_gmt_raw,
            idempotency_key=idempotency_key,
            started_at=started_at,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    @staticmethod
    def _require_raw_hash(field_name: str, value: str | None) -> None:
        # D-D5A.1 §3 の hash namespace 分離は保つ -- ここでは shape (64 lowercase
        # hex) は検証しない。hash 形式の検証は既存の project convention 通り、
        # 呼び出し元の service 層の責務 (例: WordPressPublicationRunService.prepare
        # の ``_HEX64_RE``)。この guard は「preflight 証跡が欠落したまま running
        # 行を作らせない」という non-null 要件のみを守る (D-D5B.1 §5-6)。
        if not value:
            raise WordPressContentUpdateRunError(
                f"{field_name} is required to create a running "
                "WordPressContentUpdateRun (live preflight evidence must be known "
                "before a running row is durably created)"
            )

    @staticmethod
    def _require_pre_update_raw_match(expected: str, observed: str) -> None:
        # D-D5B.2: これは分類ロジックではない -- WORDPRESS_CURRENT_CONTENT_DRIFT
        # という reason code はここに持ち込まない (それは将来の D-D5C 分類
        # service の責務)。ここで拒否するのは「不一致な preflight 証跡を持つ
        # running 行はこのモデルの契約上そもそも作れない」という run-model
        # invariant のみ。DB の CheckConstraint (ck_wordpress_content_update_runs_
        # pre_update_raw_match) と同じ不変条件を、late な IntegrityError より前に
        # 拒否する defense-in-depth。
        if expected != observed:
            raise WordPressContentUpdateRunError(
                "expected_pre_update_wordpress_raw_content_hash does not match "
                "observed_pre_update_wordpress_raw_content_hash; a running "
                "WordPressContentUpdateRun cannot be created with mismatched "
                "pre-update raw baseline evidence"
            )

    # -- reads --------------------------------------------------------------
    def get_by_id(self, run_id: int) -> WordPressContentUpdateRun | None:
        return self._session.get(WordPressContentUpdateRun, run_id)

    def get_by_idempotency_key(self, key: str) -> WordPressContentUpdateRun | None:
        stmt = select(WordPressContentUpdateRun).where(
            WordPressContentUpdateRun.idempotency_key == key
        )
        return self._session.scalars(stmt).first()

    def list_by_article(self, article_id: int) -> list[WordPressContentUpdateRun]:
        stmt = (
            select(WordPressContentUpdateRun)
            .where(WordPressContentUpdateRun.article_id == article_id)
            .order_by(*_LATEST_ORDER)
        )
        return list(self._session.scalars(stmt).all())

    def latest_succeeded_for_post(
        self, *, article_id: int, wordpress_post_id: str
    ) -> WordPressContentUpdateRun | None:
        """直近の succeeded run -- 次回 operation の ``expected_pre_update_...``
        baseline 解決 (D-D5A.1 §3) および no-op 判定 (D-D5A.1 §6) の入力になる。"""

        stmt = (
            select(WordPressContentUpdateRun)
            .where(
                WordPressContentUpdateRun.article_id == article_id,
                WordPressContentUpdateRun.wordpress_post_id == wordpress_post_id,
                WordPressContentUpdateRun.status == WP_CONTENT_UPDATE_SUCCEEDED,
            )
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def find_blocking_run_for_post(
        self, *, article_id: int, wordpress_post_id: str
    ) -> WordPressContentUpdateRun | None:
        """未解決 (running / outcome_unknown) な run -- 存在する限り新しい execute は
        blocked される (D-D5A.1 §16-17: 自動 retry は禁止、Human 解決が必要)。"""

        stmt = (
            select(WordPressContentUpdateRun)
            .where(
                WordPressContentUpdateRun.article_id == article_id,
                WordPressContentUpdateRun.wordpress_post_id == wordpress_post_id,
                WordPressContentUpdateRun.status.in_(_BLOCKING_STATUSES),
            )
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def find_succeeded_by_target_identity(
        self, target_content_update_request_identity_hash: str
    ) -> WordPressContentUpdateRun | None:
        stmt = (
            select(WordPressContentUpdateRun)
            .where(
                WordPressContentUpdateRun.target_content_update_request_identity_hash
                == target_content_update_request_identity_hash,
                WordPressContentUpdateRun.status == WP_CONTENT_UPDATE_SUCCEEDED,
            )
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    # -- narrow terminal transitions (running -> exactly one terminal state) ---
    def mark_succeeded(
        self,
        run: WordPressContentUpdateRun,
        *,
        http_status: int,
        response_content_raw_hash: str,
        response_content_rendered_hash: str | None = None,
        wordpress_modified_at: datetime | None = None,
        wordpress_modified_gmt_raw: str | None = None,
        response_snapshot: dict[str, Any] | None = None,
        finished_at: datetime,
    ) -> WordPressContentUpdateRun:
        """succeeded には最低限 ``http_status`` と ``response_content_raw_hash``
        (post-update read-back の観測値。normative) が必須 (D-D5A.1 §9, D-D5B §17)。
        ``response_content_raw_hash`` を ``request_content_hash`` と比較・一致検証
        することはない -- 2 つの namespace は別物 (D-D5A.1 §3)。
        """

        self._require_transition(run, WP_CONTENT_UPDATE_SUCCEEDED)
        run.status = WP_CONTENT_UPDATE_SUCCEEDED
        run.http_status = http_status
        run.response_content_raw_hash = response_content_raw_hash
        run.response_content_rendered_hash = response_content_rendered_hash
        run.wordpress_modified_at = wordpress_modified_at
        run.wordpress_modified_gmt_raw = wordpress_modified_gmt_raw
        run.response_snapshot = response_snapshot
        run.error_message = None
        run.finished_at = finished_at
        self._session.flush()
        return run

    def mark_failed(
        self,
        run: WordPressContentUpdateRun,
        *,
        error_message: str,
        finished_at: datetime,
        http_status: int | None = None,
        provider_error_code: str | None = None,
    ) -> WordPressContentUpdateRun:
        """呼び出し側が既に「definitive failure」と分類した結果を persist するだけ
        -- この repository は 4xx/5xx が definitive かどうかを判定しない
        (D-D5A.1 §21, D-D5B §18)。"""

        self._require_transition(run, WP_CONTENT_UPDATE_FAILED)
        run.status = WP_CONTENT_UPDATE_FAILED
        run.error_message = error_message
        run.finished_at = finished_at
        run.http_status = http_status
        run.provider_error_code = provider_error_code
        self._session.flush()
        return run

    def mark_outcome_unknown(
        self,
        run: WordPressContentUpdateRun,
        *,
        error_message: str,
        finished_at: datetime,
        http_status: int | None = None,
        provider_error_code: str | None = None,
    ) -> WordPressContentUpdateRun:
        """ambiguous な outcome (timeout / transport failure) を persist する。
        terminal のまま -- 自動で succeeded/failed へは絶対に遷移しない
        (D-D5A.1 §16-17)。"""

        self._require_transition(run, WP_CONTENT_UPDATE_OUTCOME_UNKNOWN)
        run.status = WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
        run.error_message = error_message
        run.finished_at = finished_at
        run.http_status = http_status
        run.provider_error_code = provider_error_code
        self._session.flush()
        return run

    @staticmethod
    def _require_transition(run: WordPressContentUpdateRun, target: str) -> None:
        if not wp_content_update_run_transition_allowed(run.status, target):
            raise WordPressContentUpdateRunError(
                f"run {run.id}: '{run.status}' -> '{target}' is not allowed"
            )
