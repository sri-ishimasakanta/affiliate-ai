from app.models import (
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    ArticleDraftPromotion,
    ArticleFact,
    ArticleMetric,
    Base,
    DraftGenerationRun,
    DraftInputSnapshot,
    Keyword,
    KeywordScore,
    KeywordScoreSignal,
    KeywordSignal,
    Source,
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
)

IMMUTABLE_HISTORY_MODELS = (
    KeywordScore,
    KeywordSignal,
    KeywordScoreSignal,
    ArticleFact,
    DraftInputSnapshot,
    ArticleDraftPromotion,
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
        "article_facts",
        "keyword_scores",
        "keyword_signals",
        "keyword_score_signals",
        "draft_input_snapshots",
        "draft_generation_runs",
        "article_draft_promotions",
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
