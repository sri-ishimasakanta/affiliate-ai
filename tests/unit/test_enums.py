from enum import StrEnum

from app.models import AffiliateProgramStatus, ArticleStatus, KeywordStatus


def test_status_enums_are_str_enums() -> None:
    for enum_cls in (ArticleStatus, KeywordStatus, AffiliateProgramStatus):
        assert issubclass(enum_cls, StrEnum)


def test_article_status_values() -> None:
    assert [status.value for status in ArticleStatus] == [
        "idea",
        "planned",
        "drafting",
        "review",
        "approved",
        "published",
        "rewrite",
        "archived",
    ]


def test_keyword_status_values() -> None:
    assert [status.value for status in KeywordStatus] == [
        "discovered",
        "analyzed",
        "selected",
        "assigned",
        "rejected",
    ]


def test_affiliate_program_status_values() -> None:
    assert [status.value for status in AffiliateProgramStatus] == [
        "active",
        "paused",
        "ended",
        "unknown",
    ]


def test_str_enum_compares_and_serialises_as_plain_string() -> None:
    # DB には素の文字列として保存されるため、str としての等価性が重要。
    assert ArticleStatus.PUBLISHED == "published"
    assert f"{KeywordStatus.SELECTED}" == "selected"
    assert ArticleStatus("review") is ArticleStatus.REVIEW
    assert isinstance(ArticleStatus.IDEA, str)
