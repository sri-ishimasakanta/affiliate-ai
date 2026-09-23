from app.models import (
    AffiliateClickImportRun,
    AffiliateCommissionFact,
    AffiliateCommissionImportRun,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
    AffiliateTargetProjectionPushRun,
    Article,
    ArticleAffiliateProgram,
    ArticleDraftPromotion,
    ArticleFact,
    ArticleLinkSubstitutionMapping,
    ArticleMetric,
    ArticlePublicationArtifact,
    Base,
    ChangeApplication,
    ChangeRequestApproval,
    DraftGenerationRun,
    DraftInputSnapshot,
    Ga4ImportRun,
    Ga4PageDaily,
    Keyword,
    KeywordScore,
    KeywordScoreSignal,
    KeywordSignal,
    NotificationDelivery,
    OperationsAlert,
    OperationsLock,
    OperationsRun,
    OperationsStepRun,
    RevenueOptimizationCandidate,
    RevenueOptimizationRun,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
    SearchConsoleQueryDaily,
    SeoImprovementCandidate,
    SeoImprovementRun,
    Source,
    WordPressContentUpdateReconciliation,
    WordPressContentUpdateRun,
    WordPressDraftRun,
    WordPressPublicationRun,
)

# DraftGenerationRun は lifecycle record なので created_at + updated_at を持つ
# (immutable history モデルとは対照)。
TIMESTAMPED_MODELS = (
    Source,
    Keyword,
    AffiliateProgram,
    Article,
    ArticleMetric,
    DraftGenerationRun,
    # current fact row (UPSERT-on-reimport) -- 変更のたび updated_at が進む。
    AffiliateCommissionFact,
    # GA4 の日次指標 (UPSERT-on-reimport) -- 再取り込みで updated_at が進む。
    Ga4PageDaily,
)

IMMUTABLE_HISTORY_MODELS = (
    KeywordScore,
    KeywordSignal,
    KeywordScoreSignal,
    ArticleFact,
    DraftInputSnapshot,
    ArticleDraftPromotion,
    WordPressDraftRun,
    WordPressPublicationRun,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
    SearchConsoleQueryDaily,
    # 狭い lifecycle (active -> disabled/superseded) のみ。updated_at は持たない。
    AffiliateLinkTarget,
    # append-only な取り込み実行記録 (running -> succeeded/failed)。updated_at なし。
    AffiliateClickImportRun,
    # provider-faithful な click replica (append-only)。updated_at なし。
    AffiliateOutboundClick,
    # projection push の auditable な実行記録 (running -> succeeded/failed/
    # outcome_unknown)。updated_at なし。
    AffiliateTargetProjectionPushRun,
    # 狭い lifecycle (active -> superseded/revoked) のみ。updated_at は持たない。
    ArticleLinkSubstitutionMapping,
    # immutable-content + set-once 承認 (approved_at/approved_artifact_hash のみ
    # NULL -> 値へ 1 回遷移)。updated_at は持たない。
    ArticlePublicationArtifact,
    # content update の auditable な実行記録 (running -> succeeded/failed/
    # outcome_unknown)。prepared を持たず running から直接始まる。updated_at なし。
    WordPressContentUpdateRun,
    # commission import の auditable な実行記録 (running -> succeeded/failed)。
    # append-only。updated_at なし。
    AffiliateCommissionImportRun,
    # outcome_unknown な content update run を事後照合した結果 (append-only)。
    # run 行を書き換えず独立した事実として残す。updated_at なし。
    WordPressContentUpdateReconciliation,
    # GA4 取り込みの auditable な実行記録 (prepared -> running -> succeeded/failed)。
    # append-only。updated_at なし。
    Ga4ImportRun,
    # SEO 改善候補の評価 1 回分とその候補 (append-only)。updated_at なし。
    SeoImprovementRun,
    SeoImprovementCandidate,
    # 収益最適化候補の評価 1 回分とその候補 (append-only)。updated_at なし。
    RevenueOptimizationRun,
    RevenueOptimizationCandidate,
    # 定期運用の実行記録・ステップ・アラート (append-only)。updated_at なし。
    OperationsRun,
    OperationsStepRun,
    OperationsAlert,
    # 人の承認判断と適用の試行 (append-only)。決定も失敗も上書きしない。
    # ChangeRequest 自身は status が進むため mutable (updated_at を持つ)。
    ChangeRequestApproval,
    ChangeApplication,
    # 通知の送信試行 (append-only)。成功も失敗も上書きしない。
    NotificationDelivery,
)


def test_all_tables_registered() -> None:
    tables = set(Base.metadata.tables)

    assert tables == {
        "sources",
        "keywords",
        "affiliate_programs",
        "articles",
        "article_metrics",
        "article_affiliate_programs",
        "article_content_subjects",
        "article_facts",
        "keyword_scores",
        "keyword_signals",
        "keyword_score_signals",
        "draft_input_snapshots",
        "draft_generation_runs",
        "article_draft_promotions",
        "article_editorial_revisions",
        "article_reference_facts",
        "wordpress_draft_runs",
        "wordpress_publication_runs",
        "search_console_import_runs",
        "seo_improvement_runs",
        "revenue_optimization_runs",
        "operations_runs",
        "operations_step_runs",
        "operations_alerts",
        "operations_locks",
        "change_requests",
        "change_request_approvals",
        "change_applications",
        "notification_deliveries",
        "revenue_optimization_candidates",
        "seo_improvement_candidates",
        "ga4_import_runs",
        "ga4_page_daily",
        "search_console_page_daily",
        "search_console_query_daily",
        "affiliate_link_targets",
        "affiliate_click_import_runs",
        "affiliate_outbound_clicks",
        "affiliate_target_projection_push_runs",
        "article_link_substitution_mappings",
        "article_publication_artifacts",
        "wordpress_content_update_runs",
        "wordpress_content_update_reconciliations",
        "affiliate_commission_import_runs",
        "affiliate_commission_facts",
    }


def test_models_expose_expected_tablenames() -> None:
    assert Source.__tablename__ == "sources"
    assert Keyword.__tablename__ == "keywords"
    assert AffiliateProgram.__tablename__ == "affiliate_programs"
    assert Article.__tablename__ == "articles"
    assert ArticleMetric.__tablename__ == "article_metrics"
    assert ArticleAffiliateProgram.__tablename__ == "article_affiliate_programs"
    assert KeywordScore.__tablename__ == "keyword_scores"
    assert KeywordSignal.__tablename__ == "keyword_signals"
    assert KeywordScoreSignal.__tablename__ == "keyword_score_signals"


def test_timestamp_columns_present_on_every_model() -> None:
    for model in TIMESTAMPED_MODELS:
        columns = set(model.__table__.columns.keys())
        assert {"created_at", "updated_at"} <= columns


def test_association_model_has_created_at_only() -> None:
    columns = set(ArticleAffiliateProgram.__table__.columns.keys())

    assert "created_at" in columns
    assert "updated_at" not in columns


def test_immutable_history_models_have_created_at_only() -> None:
    # 履歴/association レコードは immutable。created_at のみで updated_at を持たない。
    for model in IMMUTABLE_HISTORY_MODELS:
        columns = set(model.__table__.columns.keys())
        assert "created_at" in columns, model.__name__
        assert "updated_at" not in columns, model.__name__


def test_operations_lock_is_deliberately_mutable() -> None:
    """排他ロックだけは append-only ではない。

    取得/解放でひとつの行を書き換える性質のため、``created_at`` も ``updated_at``
    も持たない。代わりに ``acquired_at`` / ``heartbeat_at`` / ``released_at`` で
    「いつ誰が持っているか」を表す。
    """

    columns = set(OperationsLock.__table__.columns.keys())
    assert "created_at" not in columns
    assert "updated_at" not in columns
    assert {"acquired_at", "heartbeat_at", "released_at", "owner_run_id"} <= columns
