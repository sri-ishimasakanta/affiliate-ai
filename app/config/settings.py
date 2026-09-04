from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """アプリケーション全体の設定。

    値は環境変数または ``.env`` から読み込む。
    開発段階では SQLite を利用し、``DATABASE_URL`` を差し替えることで
    PostgreSQL などへ変更できる。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # アプリケーション
    app_name: str = "affiliate-ai"
    debug: bool = False

    # データベース
    # 例) PostgreSQL へ切り替える場合:
    #   DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/affiliate_ai
    database_url: str = "sqlite:///./affiliate_ai.db"
    database_echo: bool = False

    # Google Ads (Keyword Historical Metrics 収集)。
    # 未設定でもアプリは起動する。collector 実行時にのみ必須項目を検証する。
    # 秘密情報には default 値を設定しない。
    google_ads_developer_token: str | None = None
    google_ads_client_id: str | None = None
    google_ads_client_secret: str | None = None
    google_ads_refresh_token: str | None = None
    google_ads_customer_id: str | None = None
    google_ads_login_customer_id: str | None = None

    # ターゲティング (default は日本向け)。magic number をコードに書かず
    # ここから取得する。geo=2392(日本) / language=1005(日本語)。
    google_ads_geo_target_id: int = 2392
    google_ads_language_id: int = 1005

    # WordPress 連携 (Phase 3C-5)。未設定でもアプリ・preview は動く。
    # 実際の外部通信を行うフェーズでのみ必須。secret には default を置かない。
    wordpress_base_url: str | None = None
    wordpress_username: str | None = None
    wordpress_app_password: str | None = None
    # V1 の初回外部アクションは常に draft。publish は明示的な別アクション。
    wordpress_default_post_status: str = "draft"
    wordpress_verify_tls: bool = True

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def wordpress_configured(self) -> bool:
        """WordPress へ接続するのに必要な設定が揃っているか (認証はしない)。"""

        return all(
            (
                self.wordpress_base_url,
                self.wordpress_username,
                self.wordpress_app_password,
            )
        )

    @property
    def google_ads_configured(self) -> bool:
        """collector 実行に必要な Google Ads credential が揃っているか。"""

        return all(
            (
                self.google_ads_developer_token,
                self.google_ads_client_id,
                self.google_ads_client_secret,
                self.google_ads_refresh_token,
                self.google_ads_customer_id,
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
