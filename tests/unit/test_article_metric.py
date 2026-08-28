from app.models import ArticleMetric


def test_ctr_is_computed_from_impressions_and_clicks() -> None:
    metric = ArticleMetric(impressions=1000, clicks=25)

    assert metric.ctr == 0.025


def test_ctr_is_none_when_no_impressions() -> None:
    metric = ArticleMetric(impressions=0, clicks=0)

    assert metric.ctr is None


def test_conversion_rate_is_computed_from_clicks_and_conversions() -> None:
    metric = ArticleMetric(clicks=200, conversions=10)

    assert metric.conversion_rate == 0.05


def test_conversion_rate_is_none_when_no_clicks() -> None:
    metric = ArticleMetric(clicks=0, conversions=0)

    assert metric.conversion_rate is None


def test_derived_values_are_not_stored_columns() -> None:
    column_names = set(ArticleMetric.__table__.columns.keys())

    assert "ctr" not in column_names
    assert "conversion_rate" not in column_names
