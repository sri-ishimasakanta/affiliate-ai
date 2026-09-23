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

    # Google Search Console (Phase 3C-5F)。
    # Domain property は "sc-domain:<host>"、URL-prefix は "https://<host>/"。
    search_console_property_uri: str | None = None
    # service account JSON key file の **パス** のみを持つ (中身は import 時に読まない)。
    # secret 本体は Settings にも DB にも入れない。
    search_console_credentials_file: str | None = None

    # --- GA4 (Google Analytics Data API) ---
    # GA4 property の数値 id ("properties/" 接頭辞は不要)。未設定なら取り込みを行わない。
    ga4_property_id: str | None = None

    # --- 運用通知 (C8) ---
    # 設定されているときだけ Webhook 通知が有効になる。値は DB にもログにも出さない。
    operations_webhook_url: str | None = None

    # --- 運用メール (C8.7) ---
    # 明示的に有効化したときだけ SMTP に接続する。既定は無効で、未設定でも
    # 取り込み・評価・監視はそのまま動く。
    # password は **DB にも log にも alert evidence にも例外にも出さない**。
    # 宛先はソースに定数で書かず、環境 / .env から読む。
    operations_email_enabled: bool = False
    operations_email_smtp_host: str | None = None
    operations_email_smtp_port: int = 587
    operations_email_username: str | None = None
    operations_email_password: str | None = None
    operations_email_from: str | None = None
    #: カンマ区切りで複数指定できる (本番は 1 件)。
    operations_email_to: str | None = None
    operations_email_use_starttls: bool = True
    #: 件名の接頭辞。運用者がどの環境から来たかを見分けるため。
    operations_email_subject_prefix: str = "BizFluxLab"

    # Affiliate redirect runtime (WordPress MU-plugin, Phase 3C-5F-D)。
    # base URL は wordpress_base_url を再利用する。共有 HMAC 鍵は projection を
    # **push (execute)** するときだけ必須。plan / dry-run では不要。default は置かない。
    # 実値は絶対に print / log / commit / CLI 出力 / test snapshot に含めない。
    affiliate_runtime_shared_secret: str | None = None

    # --- モバイル承認中継 (C8.8.1) ---
    # **affiliate runtime とは別の secret を使う。** リダイレクト/クリックの経路と
    # 「人の承認を運ぶ経路」は信頼ドメインが違うので、鍵を共有しない。片方が漏れても
    # もう片方は無事である必要がある。
    # default は置かない。fallback もしない -- 未設定なら認証付きの中継 API は
    # すべて fail closed になる。実値は print / log / commit / CLI 出力 /
    # notification_deliveries / test snapshot に絶対に含めない。
    # WordPress 側の対応する定数は ``BFL_APPROVAL_RELAY_SECRET``。
    approval_relay_shared_secret: str | None = None

    # D-C3-C synthetic runtime click probe (production runtime-only, no local
    # AffiliateLinkTarget)。probe の外部 state file (token を含む) のパス。
    # 未設定でも通常のアプリ動作には一切影響しない。実 Human パスはコードに
    # ハードコードしない — ここか CLI の --state-file で明示的に与える。
    affiliate_probe_state_file: str | None = None

    # Make affiliate API (Phase E1)。read-only commission/stat import のみ。
    # payout 実行エンドポイントはこのプロジェクトのスコープ外 (実装しない)。
    # 実値は絶対に print / log / commit / CLI 出力 / test snapshot に含めない。
    # make_api_base_url は Human の Make zone-specific な API base を、
    # "/api/v2" まで含めて設定する (例: "https://eu1.make.com/api/v2" /
    # "https://eu2.make.com/api/v2" / "https://us1.make.com/api/v2" -- zone は
    # ここに固定しない、Phase E1.2 §5)。MakeAffiliateClient はこの値へ
    # "/affiliate/commissions" 等を直接連結するだけで、"/api/v2" を追加しない。
    make_api_base_url: str | None = None
    make_api_token: str | None = None

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
    def search_console_property_configured(self) -> bool:
        """Search Console の property URI が設定されているか。"""

        return bool(self.search_console_property_uri)

    @property
    def search_console_configured(self) -> bool:
        """Search Console へ read-only アクセスするのに必要な設定が揃っているか
        (JSON の妥当性検証は :mod:`app.search_console.credentials` の責務)。"""

        return bool(self.search_console_property_uri and self.search_console_credentials_file)

    @property
    def affiliate_runtime_push_configured(self) -> bool:
        """affiliate projection を WordPress runtime へ push (execute) するのに必要な
        設定が揃っているか。dry-run / plan では不要。"""

        return bool(self.wordpress_base_url and self.affiliate_runtime_shared_secret)

    @property
    def make_api_configured(self) -> bool:
        """Make affiliate API へ read-only アクセスするのに必要な設定が揃っているか
        (token の妥当性検証はしない — 実際の呼び出しで 401 として現れる)。"""

        return bool(self.make_api_base_url and self.make_api_token)

    @property
    def approval_relay_configured(self) -> bool:
        """モバイル承認中継を認証付きで呼べるか。

        **値そのものは決して外へ出さない** -- 診断で示してよいのはこの真偽だけ。
        """

        return bool((self.approval_relay_shared_secret or "").strip())

    @property
    def operations_email_recipients(self) -> list[str]:
        """設定された宛先。空白やカンマの揺れを吸収する。"""

        raw = self.operations_email_to or ""
        return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]

    @property
    def operations_email_configured(self) -> bool:
        """SMTP 送信に必要な設定が揃っているか (有効化されていることを含む)。

        password の中身は検証しない -- 実際の送信で認証エラーとして現れる。
        """

        return bool(
            self.operations_email_enabled
            and self.operations_email_smtp_host
            and self.operations_email_from
            and self.operations_email_recipients
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
