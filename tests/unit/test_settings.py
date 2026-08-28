from app.config.settings import Settings


def test_default_settings_use_sqlite() -> None:
    config = Settings()

    assert config.app_name == "affiliate-ai"
    assert config.database_url.startswith("sqlite")
    assert config.is_sqlite is True


def test_settings_can_switch_to_postgresql(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:password@localhost:5432/affiliate_ai",
    )

    config = Settings()

    assert config.is_sqlite is False
    assert config.database_url.startswith("postgresql+psycopg://")


def test_google_ads_optional_and_unconfigured_by_default(monkeypatch) -> None:
    for key in (
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_CUSTOMER_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    # ローカルに実 credential 入りの .env があってもテストは決定的にする
    # (このテストの意図は「何も与えられなければフィールドは None」の確認)。
    config = Settings(_env_file=None)

    # 未設定でも Settings は生成できる (アプリ起動を妨げない)
    assert config.google_ads_developer_token is None
    assert config.google_ads_configured is False
    # ターゲティングの default は日本向け
    assert config.google_ads_geo_target_id == 2392
    assert config.google_ads_language_id == 1005


def test_google_ads_configured_when_all_credentials_present(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dummy")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "dummy")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "dummy")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "dummy")
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "1234567890")

    assert Settings().google_ads_configured is True
