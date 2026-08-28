# 開発メモ

## 現在のスコープ

この基盤で実装済みのもの:

- `Settings` (`app/config/settings.py`) — pydantic-settings ベース。`.env` から読み込む。`get_settings()` は `lru_cache`。
- DB 接続 (`app/config/database.py`) — `build_engine()` / `SessionLocal` / `get_session()` / `check_database_connection()`。
- `Base` モデル (`app/models/base.py`) — `DeclarativeBase` + 制約命名規約 + `TimestampMixin`。
- ステータス Enum (`app/models/enums.py`) — `StrEnum` ベース。
- モデル: `Source` / `Keyword` / `KeywordScore` / `KeywordSignal` / `KeywordScoreSignal` / `AffiliateProgram` / `Article` / `ArticleAffiliateProgram` / `ArticleMetric`。
- Alembic (`migrations/`) — `env.py` は `Base.metadata` と `Settings` を使用。リビジョン: `1892d6df0222` (初期) → `5be5b19f3007` (Keyword `category`/`opportunity_score`, Article `published_url`) → `ca2a50f798ec` (`keyword_scores`) → `1689a1b083a8` (`keyword_signals` / `keyword_score_signals`)。Phase 2B-2 は DB 変更なし。
- 外部連携: `google-ads` (公式 client library) — Google Ads Keyword Historical Metrics 収集用。未設定でもアプリは起動する。
- FastAPI アプリ (`app/main.py`) — `/health` + `/api/v1` router。

### Phase 1A (Keyword / Article の Repository・Service・Schema)

- Schema: `app/keyword/schemas.py` / `app/article/schemas.py` (Pydantic)。
- Repository: `app/repositories/{keyword,article}_repository.py` — DB アクセスのみ、commit しない。
- Service: `app/services/{keyword,article}_service.py` — ビジネスロジック + トランザクション境界。
- status 遷移: `app/services/status_transitions.py`。
- Application 例外: `app/exceptions.py` (`EntityNotFoundError` / `DuplicateEntityError` / `InvalidStatusTransitionError`)。
- 設計の詳細・層の責務・遷移表・将来方針は [architecture.md](architecture.md) を参照。
- 未実装 (Phase 1A 対象外): FastAPI CRUD エンドポイント、Source の Service、Keyword scoring。
  (AffiliateProgram の Repository / Service / API は Phase 2B-6A で実装。)

### Phase 1B (Keyword / Article の REST API)

- ルータ: `app/api/v1/{keywords,articles}.py`、集約は `app/api/v1/router.py`、prefix `/api/v1`。
- DI: `app/api/dependencies.py` — 既存 `get_session()` を `Depends` で再利用し、
  `get_keyword_service` / `get_article_service` で Service を組み立てる (DI framework なし)。
- 例外変換: `app/api/exception_handlers.py` — `ApplicationError` / `SQLAlchemyError` の
  application-level handler。Router 内で個別 try/except しない。
  レスポンスは `{"error": {"code": "...", "message": "..."}}` に統一
  (`entity_not_found`→404 / `duplicate_entity`→409 / `invalid_status_transition`→409 /
  内部エラー→500)。validation error は FastAPI 標準の 422 を維持。
- status 変更専用スキーマ: `KeywordStatusUpdate` / `ArticleStatusUpdate` (Enum、不正値は 422)。
- 起動: `uv run uvicorn app.main:app --reload` / ドキュメント: `/docs`, `/openapi.json`。
- エンドポイント一覧は [README.md](../README.md#rest-api-phase-1b) を参照。
- 未実装 (Phase 1B 対象外): 認証・認可、Source/ArticleMetric API、
  汎用 BaseRouter / CRUD framework。(AffiliateProgram API は Phase 2B-6A で実装。)
- テスト: `app/config/database.build_engine()` はインメモリ SQLite (`sqlite://`) に対して
  `StaticPool` を使い、`TestClient` のワーカースレッドからも同一 DB を参照できるようにした。
  API テストは `dependency_overrides[get_session]` でテスト用セッションへ差し替えるため
  実 `affiliate_ai.db` には触れない。

### Phase 2A (Keyword Opportunity Score)

- 純粋計算: `app/keyword/scoring.py` (`OpportunityScoreInput` / `OpportunityScoreResult` /
  `calculate_opportunity_score`)。V1 weights は `OPPORTUNITY_SCORE_WEIGHTS` に集約 (合計 1.0)。
- モデル: `app/models/keyword_score.py` (`keyword_scores`、履歴 immutable、`updated_at` なし、
  各 component と `total_score` に `CheckConstraint` 0〜100)。migration `ca2a50f798ec`。
- Repository: `app/repositories/keyword_score_repository.py` (`create` / `get_latest` /
  `list_by_keyword`、commit しない)。
- Service: `app/services/keyword_scoring_service.py` (`score_keyword` / `get_latest_score` /
  `list_score_history`)。`score_keyword` は履歴追加 + `Keyword.opportunity_score` 更新 +
  `discovered→analyzed` を 1 トランザクションで実施 (失敗時 rollback)。
- API: `POST/GET /api/v1/keywords/{id}/scores`、`GET .../scores/latest`。
- スコア仕様・status ルール・将来方針は [architecture.md](architecture.md) の
  「Opportunity Score V1」を参照。
- 未実装 (Phase 2A 対象外): 外部 SEO データ取得、AI によるスコア推定、
  `selected`/`rejected` の自動判定。

### Phase 2B-1 (Keyword Signal / Evidence 基盤)

- Enum: `KeywordSignalComponent` (`app/models/enums.py`) — 値は `COMPONENT_NAMES` と完全一致。
- モデル: `app/models/keyword_signal.py` (`keyword_signals`、immutable、`raw_data` は generic
  `sa.JSON`、`normalized_value` に `CheckConstraint` 0〜100)、
  `app/models/keyword_score_signal.py` (`keyword_score_signals`、association、
  `UniqueConstraint(keyword_score_id, keyword_signal_id)`)。migration `1689a1b083a8`。
- Repository: `keyword_signal_repository.py` (`create` / `get_by_id` / `get_latest` /
  `list_by_keyword` / `list_by_component`)、`keyword_score_signal_repository.py`
  (`create` / `list_signals_for_score`)。いずれも commit しない。
- Service: `keyword_signal_service.py` (`create_signal` / `get_signal` / `get_latest_signal` /
  `list_signals`)。`KeywordScoringService` に `score_keyword_from_latest_signals` と
  `list_score_signals` を追加。
- `score_keyword_from_latest_signals`: 7 component の最新 Signal → `calculate_opportunity_score`
  → `KeywordScore` 作成 + `Keyword.opportunity_score` 更新 + `discovered→analyzed` +
  使用 7 Signal を `KeywordScoreSignal` で紐付け、を 1 トランザクションで実施。1 つでも
  Signal 不足なら `IncompleteSignalSetError` (409 `incomplete_signal_set`) で何も作らない。
- 正規化ロジックは持たない (`normalized_value` を受け取るのみ)。手動スコア API は不変更。
- データフロー・3 層 (Signal=根拠/履歴、Score=スナップショット、`opportunity_score`=cache)・
  Google Ads / Trends の将来方針は [architecture.md](architecture.md) を参照。
- 未実装 (Phase 2B-1 対象外): 外部 API 実通信、provider 別 normalizer、Provider interface。

### Phase 2B-2 (Google Ads search_demand 自動収集)

- 依存: `google-ads` (公式 client library) を `[project.dependencies]` に追加。DB migration なし。
- Provider: `app/keyword/providers/google_ads.py` — `GoogleAdsKeywordMetricsProvider`
  (client 生成は遅延 import / `GenerateKeywordHistoricalMetrics` 実行 / SDK → DTO 変換)。
  DTO: `GoogleAdsKeywordMetrics` / `MonthlySearchVolume` (frozen dataclass)。
- Normalizer: `app/keyword/normalizers/search_demand.py` —
  `normalize_search_demand(avg_monthly_searches) -> 0〜100`
  (`min(100, 20*log10(x+1))`、小数第 2 位、DB/SDK 非依存)。`NORMALIZER_NAME`/`_VERSION`。
- Service: `app/services/keyword_metrics_collection_service.py` —
  `collect_google_ads_search_demand(keyword_id)` (1 トランザクション / 失敗時 rollback)。
  Google Ads 固有処理は `KeywordSignalService` に押し込まず本 Service に集約。
- API: `POST /api/v1/keywords/{id}/signals/google-ads/search-demand` (body なし → 201)。
- Signal 履歴の "最新" を `observed_at DESC, id DESC` に統一
  (`KeywordSignalRepository`)。バックフィルしても latest の意味が崩れない。
- 例外: `ProviderNotConfiguredError` (503 `provider_not_configured`) /
  `ExternalProviderDataError` (502 `external_provider_data_error`) /
  `ExternalProviderError` (502 `external_provider_error`)。
- collector が生成するのは `search_demand` Signal のみ。広告 `competition` /
  CPC は `raw_data` に保存するだけ (`competition_ease` / `commercial_intent` は自動生成しない)。
- 詳細は [architecture.md](architecture.md) の「Google Ads search_demand collector」を参照。

#### Google Ads の env キー

`.env` に以下を設定する (実値は絶対にコミットしない。`.env` は gitignore 済み。
キー名のみ `.env.example` にある):

| キー | 必須 | 備考 |
| --- | --- | --- |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | 収集時のみ | 秘密情報 |
| `GOOGLE_ADS_CLIENT_ID` | 収集時のみ | 秘密情報 |
| `GOOGLE_ADS_CLIENT_SECRET` | 収集時のみ | 秘密情報 |
| `GOOGLE_ADS_REFRESH_TOKEN` | 収集時のみ | 秘密情報 |
| `GOOGLE_ADS_CUSTOMER_ID` | 収集時のみ | ハイフンなし 10 桁 |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | 任意 | MCC 経由の場合 |
| `GOOGLE_ADS_GEO_TARGET_ID` | 任意 | 未設定なら 2392 (日本) |
| `GOOGLE_ADS_LANGUAGE_ID` | 任意 | 未設定なら 1005 (日本語) |

**Google Ads 連携を使わない場合は未設定でよい。** その場合でもアプリ・
`/health`・Keyword/Article/Signal/Score API は通常どおり起動・動作する
(`Settings` の Google Ads フィールドは `str | None` で default `None`)。
credential は collector を呼んだときにのみ検証される。

**実 credential は `.env` にのみ保存し、Git へ commit しないこと。**
`.env` は `.gitignore` 済み。`.env.example` にはキー名だけを置く。

#### Google Ads 実接続確認 (Smoke Test CLI) — Phase 2B-2.5

実 credential をローカル `.env` に設定した状態で、Provider が
`GenerateKeywordHistoricalMetrics` まで実通信できることだけを確認する。
**DB (Session / Keyword / KeywordSignal / commit) には一切触れない。**

```bash
uv run python scripts/check_google_ads.py --keyword "AI 議事録 おすすめ"
```

- `--keyword` は必須。`--show-months N` で直近 N か月の `monthly_search_volumes` も表示。
- 表示するのは `keyword` / `avg_monthly_searches` / `competition` /
  `competition_index` / `low_top_of_page_bid_micros` /
  `high_top_of_page_bid_micros` / `monthly_search_volumes` の件数のみ。
- **developer token / client id / client secret / refresh token / OAuth
  credential・`customer_id` は成功時も例外時も一切出力しない。**
  SDK 内部例外もそのまま print せず、安全な固定文言 or 例外型名のみ表示する。
- exit code: `0` 成功 / `2` 未設定 (`ProviderNotConfiguredError`) /
  `3` 通信エラー (`ExternalProviderError`) / `4` 指標なし
  (`ExternalProviderDataError` / 空結果) / `1` 想定外。

#### Google Ads 複数キーワード分析 (Analyze CLI)

Phase 2B-3 (`commercial_intent`) の設計に入る前段として、複数キーワードの
Google Ads 指標をまとめて取得し、分布を観察するための道具。
**DB (Session / Keyword / KeywordSignal / KeywordScore / commit) には一切触れない。
`commercial_intent` / `competition_ease` の算出も行わない** (実データ収集のみ)。

```bash
uv run python scripts/analyze_google_ads_keywords.py \
  --keyword "AI 議事録 おすすめ" \
  --keyword "AI 議事録 無料" \
  --keyword "AI 議事録 比較" \
  --output google_ads_keyword_metrics.csv \
  --show-months 12
```

- `--keyword` は複数回指定 (必須)。空白のみ / 重複は除去する。
- 既存の `GoogleAdsKeywordMetricsProvider` を再利用し、**1 リクエストで全 keyword を渡す**
  (keyword ごとに API を呼ばない)。SDK オブジェクトは CLI 側へ出さない。
- 各 keyword を表形式で表示: `keyword` / `avg_monthly_searches` / `competition` /
  `competition_index` / `low_top_of_page_bid_micros` / `high_top_of_page_bid_micros` /
  `low_bid` (`= low_top_of_page_bid_micros / 1,000,000`) /
  `high_bid` (`= high_top_of_page_bid_micros / 1,000,000`) /
  `monthly_search_volumes` の件数。
- `--output PATH` で CSV 書き出し (列: `keyword`, `avg_monthly_searches`, `competition`,
  `competition_index`, `low_top_of_page_bid_micros`, `high_top_of_page_bid_micros`,
  `low_top_of_page_bid`, `high_top_of_page_bid`, `monthly_search_volumes_count`。
  値なしは空欄)。
- `--show-months N` で各 keyword の直近 N か月の `monthly_search_volumes` も表示。
- secret 保護は `check_google_ads.py` と同一方針: developer token / client id /
  client secret / refresh token / OAuth credential / `customer_id` は成功時も例外時も
  出力しない。SDK 内部例外は安全な固定文言 or 例外型名のみ。
- exit code は `check_google_ads.py` と同じ (`0`/`1`/`2`/`3`/`4`)。
- **注意: `competition` / `competition_index` は広告主の競争度であり、SEO organic の
  `competition_ease` とは別物。`competition_index` を `competition_ease` に流用しないこと。**

### Phase 2B-3 (Google Ads commercial_intent 自動収集)

Google Ads historical metrics と **キーワード文字列** から `commercial_intent`
(0〜100) の Signal を生成する。

- 純粋計算: `app/keyword/normalizers/commercial_intent.py`
  (`classify_query_intent` / `normalize_cpc_score` / `normalize_ad_competition_score` /
  `score_commercial_intent` / `calculate_commercial_intent`)。DB/SDK/FastAPI 非依存。
- V1 formula: `commercial_intent = query_intent_score*0.60 + cpc_score*0.30 + ad_competition*0.10`
  - **Query Intent が 60%** (keyword 文字列から純粋判定。`price`95 / `compare`90 /
    `recommend`90 / `b2b`85 / `tool`65 / `free`45 / `generic`40 / `how_to`20 /
    `informational`10。複数該当は最高 score)。
  - **Low CPC が 30%** (`low_top_of_page_bid_micros / 1_000_000` を円とみなし
    `100*(1-exp(-low_bid/250))`)。**250 は JPY calibration 定数**。V1 は
    日本市場・JPY アカウント前提。
  - **Ad competition が 10%** (`competition_index` を 0〜100 のまま採用)。
- **missing data は 0 点ではなく weight 再正規化**。CPC / `competition_index` が
  取れない要素は欠測扱いで除外し、`Σ(value*weight) / Σ(利用できた weight)` で算出。
  `evidence_coverage` = 利用できた元 weight 合計 (query のみ 0.60 / +competition 0.70 /
  +CPC 0.90 / 全部 1.00)。`market_evidence_available` は CPC か competition の
  どちらかがあれば true。
- **`high_top_of_page_bid` は V1 では score に使わない** (外れ値で不安定なため。
  raw_data には保存)。
- **Google Ads の `competition` / `competition_index` は SEO competition ではない。**
  `competition_ease` へ流用しない。
- Service: `KeywordMetricsCollectionService.collect_google_ads_commercial_intent(keyword_id)`
  (1 トランザクション / 失敗時 rollback / provider = `google_ads`)。
- API: `POST /api/v1/keywords/{id}/signals/google-ads/commercial-intent` (body なし → 201)。
  例外は search-demand と同じ (503 / 502 / 404)。新規例外・新規 provider 名なし。
- DB schema 変更なし (`commercial_intent` は既存の `KeywordSignalComponent`)。
- `scores/from-signals` は不変更。7 component が揃わなければ `IncompleteSignalSetError`
  のまま。`scoring.py` の weights / formula は変更禁止。
- 詳細は [architecture.md](architecture.md) の
  「Google Ads commercial_intent collector (Phase 2B-3)」。

### Phase 2B-4 (Google Ads trend 自動収集)

既存の Google Ads historical metrics の `monthly_search_volumes` **のみ** から
`trend` (0〜100) の Signal を生成する。**Google Trends API / pytrends は V1 では未使用。**

- 純粋計算: `app/keyword/normalizers/trend.py` (`calculate_trend` /
  `trend_from_monthly_searches` / `prepare_monthly_series` / `TrendResult`)。DB/SDK/FastAPI 非依存。
- Trend V1:
  - Google Ads `monthly_search_volumes` を利用 (新しい外部 API は導入しない)。
  - year/month 昇順にソートした **最新 6 か月** を使用。
  - **前半 3 か月平均 vs 後半 3 か月平均** を比較。
  - `change_ratio = (recent_3 - previous_3) / max((recent_3 + previous_3)/2, 1.0)` を
    [-1,1] に clamp、`trend_score = clamp0_100(50 + 50*change_ratio)` を小数第 2 位。
  - **50 = flat / >50 = growth / <50 = decline**。
  - **検索ボリュームの絶対量は評価しない** (search_demand の担当)。
  - symmetric change ratio なので previous_3 が 0 でもゼロ除算しない。
- monthly data:
  - **最低 6 か月の有効データが必要**。`None` の月は除外、除外後 6 未満は
    `ExternalProviderDataError` (502)。負値も provider data error 相当。7 か月以上は
    最新 6 か月のみ使用。新規例外は追加しない。
  - `period_start`/`period_end` は search_demand / commercial_intent と同じ
    (全 monthly data の最古〜最新月)。
- Service: `KeywordMetricsCollectionService.collect_google_ads_trend(keyword_id)`
  (1 トランザクション / 失敗時 rollback / provider = `google_ads`)。Repository は commit しない。
- API: `POST /api/v1/keywords/{id}/signals/google-ads/trend` (body なし → 201)。
  例外は search-demand / commercial-intent と同じ (503 / 502 / 404)。
- DB schema 変更なし (`trend` は既存の `KeywordSignalComponent`)。
- `search_demand` / `commercial_intent` の既存挙動、`scores/from-signals`、
  `scoring.py` の weights / formula は変更しない。
- 詳細は [architecture.md](architecture.md) の
  「Google Ads trend collector (Phase 2B-4)」。

### Phase 2B-5 (site_relevance ルールベース導出)

keyword が現在のサイトテーマ (AI・生成AI・業務効率化・業務自動化) にどの程度
関連するかを 0〜100 で評価する。

- **Site Relevance V1 は rule-based。外部 API なし・LLM API なし・Google Ads なし。
  完全ローカル・決定論的。**
- 純粋計算: `app/keyword/normalizers/site_relevance.py`
  (`calculate_site_relevance` / `normalize_keyword` / `SiteRelevanceResult`)。DB/SDK/FastAPI 非依存。
- site profile = **`ai_business_automation` v1**。関連語を `CORE_THEME` (base 80) /
  `RELEVANT_TOOL` (75) / `BUSINESS_PRODUCTIVITY` (70) / `ADJACENT_USE_CASE` (60) の
  グループに分け、vocabulary は normalizer 内の定数に集約 (計算ロジックと疎結合)。
- formula: `matched groups の base 最大値 + multi_group_bonus(+10, group>=2) +
  business_context_bonus(+10)` を 0〜100 clamp。matched group が無ければ
  out-of-scope 語あり → **0** (明確なサイトテーマ外)、それ以外 → **20** (unknown/general)。
- **料金 / 比較 / おすすめ / ランキング / 無料 / 使い方 / とは / 導入 は score に影響しない**
  (`"ChatGPT 料金"` と `"ChatGPT 使い方"` は原則同じ site_relevance)。commercial intent の
  違いは `commercial_intent` component の担当。
- keyword normalization: Unicode NFKC → casefold → 空白正規化 (pure, unit test 可能)。
  `AI` / `Make` / `RPA` 等の ASCII 語は英数字境界で照合し `maker` の `make` 等を誤検知しない。
- Service: `KeywordSignalService.derive_site_relevance(keyword_id)`
  (Metrics Collection Service には置かない)。Repository は commit しない。再実行で
  新 Signal を追記 (immutable history 維持)。`provider = site_profile` /
  `source_reference = site-profile:ai-business-automation:v1` /
  `period_start = period_end = null` (静的評価)。
- API: `POST /api/v1/keywords/{id}/signals/site-relevance` (body なし → 201)。
  外部 provider を使わないので 502 / 503 は無し、Keyword 無しの 404 のみ。
- DB schema 変更なし (`site_relevance` は既存の `KeywordSignalComponent`)。
- `scoring.py` / `search_demand` / `commercial_intent` / `trend` / `scores/from-signals` は不変更。
- 将来的に semantic relevance / Search Console / 記事 embedding へ拡張可能だが V1 では未実装。
- 詳細は [architecture.md](architecture.md) の「Site Relevance signal (Phase 2B-5)」。

### Phase 2B-6A (Affiliate Catalog Foundation)

`affiliate_opportunity` Signal の採点 (Phase 2B-6B) を実データから行えるよう、
`AffiliateProgram` カタログの管理基盤を先に用意した。**Signal 採点は未実装。**

- **DB (migration `abfa2f774ff4`, add-only)**: `affiliate_programs` に
  `match_terms` (`sa.JSON`、keyword と案件を関連付ける検索語配列、nullable) と
  `currency` (`String(3)`、報酬の通貨、nullable) を追加。他の候補列
  (`is_recurring` / `epc` / `conversion_rate` / `approval_rate` / `cookie_days` /
  `advertiser` / `keyword_affiliate_programs` 中間テーブル) は今回 **追加しない**
  (2B-6B で未使用のため。将来 migration 候補)。
- **Schema**: `app/affiliate/schemas.py` — `AffiliateProgramCreate` / `Update` / `Read`。
  `name` strip・空白拒否 / `currency` strip→uppercase→3 文字英字のみ /
  `match_terms` は各要素 trim・空要素除去・重複除去・入力順維持 /
  `commission_value` は None または 0 以上 / `commission_type` は既存互換のため
  自由文字列 (新規入力は `fixed` / `percentage` 推奨、DB enum 化・CheckConstraint なし)。
  `Read.match_terms` は常に `list[str]` (null は `[]`)。
- **Repository**: `app/repositories/affiliate_program_repository.py` —
  `create` / `get_by_id` / `get_by_name_and_provider` / `list` (`status` /
  `provider` / `category` filter) / `list_active` / `update` / `delete`。commit しない。
- **Service**: `app/services/affiliate_program_service.py` — `create_program` /
  `get_program` / `list_programs` / `update_program` / `delete_program`。
  transaction は Service 所有 (commit / 失敗時 rollback)、not found は
  `EntityNotFoundError`、**重複ポリシー = 同一 `name`+`provider` は
  `DuplicateEntityError` (409、upsert しない)**。`tracking_url` 等の値はログ・
  例外メッセージへ出さない。
- **API**: `/api/v1/affiliate-programs` の CRUD (POST 201 / GET / PATCH 200 /
  DELETE 204 / 404 / validation 422)。HTTP status・エラー形式は Keyword / Article API と共通。
- **CSV importer**: `scripts/import_affiliate_programs.py`
  (`--file PATH` 必須 / `--dry-run`)。外部 ASP API・スクレイピングなし。
  CSV は 1 行目ヘッダ、`name` 必須、任意列 `provider` `category` `commission_type`
  `commission_value` `currency` `landing_page_url` `tracking_url` `notes` `status`
  `match_terms`。**`match_terms` はパイプ区切り** (`議事録|AI 議事録|文字起こし`)。
  重複 (`name`+`provider`) は **skip** (上書きしない)。`--dry-run` は DB へ commit
  せずパース・検証のみ。1 行のエラーでもセル値・`tracking_url` を出力しない。
- **`ArticleAffiliateProgram` の N:M は変更なし** (primary ルール / link 管理 API /
  記事側の案件選択は未実装のまま)。
- 詳細は [architecture.md](architecture.md) の「Affiliate Catalog (Phase 2B-6A)」。
- 分析 CLI: `scripts/analyze_affiliate_opportunities.py` — keyword と **active**
  AffiliateProgram の `match_terms` を照合し、matched program 数 / provider 分布 /
  commission 情報 (fixed / percentage を混同せず currency 別) を表・CSV で出力。
  **DB read-only、KeywordSignal を作らない、採点しない。** `tracking_url` /
  `landing_page_url` は出力しない。

### Phase 2B-6B (affiliate_opportunity V1)

keyword に対する **供給側** の評価 (active Affiliate Catalog にどれだけ収益化案件が
あり、どの程度儲かるか)。`commercial_intent` (検索者の購買意図) とは別物。
外部 API / LLM / ASP API なし。完全ローカル・決定論的。**DB schema 変更なし。**

- 照合ルールの共有: `app/keyword/affiliate_matching.py`
  (`normalize_for_match` / `term_matches` / `match_programs` / `ProgramFacts` /
  `MatchedProgram`)。分析 CLI と production が同一 helper を import
  (CLI の private 実装を production から import しない)。正規化は
  `site_relevance.normalize_keyword` を再利用 (site_relevance 側は無変更)。
- 純粋計算: `app/keyword/normalizers/affiliate_opportunity.py`
  (`calculate_affiliate_opportunity` / `AffiliateOpportunityResult`)。DB/SDK/FastAPI 非依存。
- V1 formula: `program_match_score * 0.55 + commission_score * 0.35 + provider_spread_score * 0.10`
  - **program_match_score**: `n==0 → 0`、`n>0 → 100*(1-exp(-n/4.0))` (案件数の限界効用逓減)。
  - **commission_score**: percentage commission のみ `min(100, pct*2.5)` (最大値採用)。
    **fixed commission は score 不使用** (JPY/USD の公平な calibration が無いため。
    FX 換算しない。`raw_data.fixed_commissions` に provenance として残す)。
  - **provider_spread_score**: `min(100, distinct_provider*40)`。`direct` 潰れのため
    weight 0.10 の弱い補助指標。
  - **missing commission は 0 点でなく weight 再正規化** (揃えば `available_weight`
    1.00 / commission 欠測なら 0.65)。
  - **0 match → `affiliate_opportunity = 0` / `market_evidence_available = false`**。
    0 match は「現在の active catalog に直接 match する案件が無い」だけであり、
    市場に案件が無い意味ではない。catalog completeness は保証しない。
- Service: `KeywordSignalService.derive_affiliate_opportunity(keyword_id)`
  (catalog は read-only、Service が commit / rollback、immutable history 維持)。
  `provider = affiliate_catalog` / `source_reference = affiliate-catalog:local:v1` /
  `period_start = period_end = None`。
- **`raw_data` に `tracking_url` / `landing_page_url` / affiliate ID / credential /
  ASP account 情報は保存しない。**
- API: `POST /api/v1/keywords/{id}/signals/affiliate-opportunity` (body なし → 201)。
  ローカル catalog のため 502 / 503 なし、Keyword 無しの 404 のみ。
- `scores/from-signals` は不変更。自動 Signal は 5/7
  (search_demand / commercial_intent / trend / site_relevance / affiliate_opportunity)。
  `competition_ease` / `originality` 不足で `IncompleteSignalSetError` 継続。
- 詳細は [architecture.md](architecture.md) の
  「Affiliate Opportunity signal (Phase 2B-6B)」。

### Phase 2B-7 (originality V1)

`originality` = **サイト内部カニバリゼーション可能性の逆指標**（既存の内部 Keyword /
Article と検索意図がどれだけ重複しないか）。**Google 検索の外部競合
(`competition_ease`) とは別物。** 外部 API / LLM / embedding / vector DB / 追加 pip
dependency なし。決定論的。**DB schema 変更なし。**

- 類似度 helper: `app/keyword/text_similarity.py`（pure、標準ライブラリのみ）—
  `character_bigram_dice` / `sequence_similarity` / `text_similarity`。
  `similarity = max(char bigram Dice, difflib.SequenceMatcher(autojunk=False).ratio)`。
  正規化は `site_relevance.normalize_keyword`（NFKC → casefold → 空白正規化）後に
  さらに空白除去。**token Jaccard / TF-IDF / trigram / suffix 削除 / intent adjustment
  は不使用。Article body も不使用。**
- 純粋計算: `app/keyword/normalizers/originality.py`
  (`calculate_originality` / `OriginalityCandidate` / `OriginalityResult`)。
- 比較対象 corpus:
  - 既存 Keyword 文字列（status ∈ {analyzed, selected, assigned}、`discovered`/`rejected`
    除外、**current keyword 自身は id で除外**）→ `keyword` candidate（weight 1.00）。
  - Article（status ∈ {approved, published, rewrite}、他は除外、current keyword に
    紐づく Article は除外）→ `article_keyword`（担当 Keyword 文字列、JOIN、weight 1.00）
    / `article_title`（weight **0.80**）。
- formula: `originality = round(clamp(100 * (1 - max_effective_similarity), 0, 100), 2)`。
  `effective = raw_similarity * evidence_weight`。
  - Keyword 完全一致 → originality **0**。title のみ完全一致 → effective 0.80 →
    originality **20**（意図的仕様）。
  - tie-break: `effective DESC → kind priority (keyword<article_keyword<article_title)
    → id ASC`（決定論的）。
- **empty corpus → originality 100.0**、ただし `corpus_available = false` /
  `evidence_coverage = 0.0`。「比較対象が現在の内部 corpus に無い」の意味であり
  独創性の証明ではない。**（現 dev DB は Keyword / Article ともに 0 件なので、
  当面すべての導出がこの分岐に入る。）**
- Service: `KeywordSignalService.derive_originality(keyword_id)`（corpus は read-only、
  Service が commit / rollback、immutable history 維持）。`provider = internal_corpus`
  / `source_reference = internal-corpus:v1` / `period_start = period_end = None`。
- Repository: `KeywordRepository.list_originality_candidates` / `count`、
  `ArticleRepository.list_originality_candidates`（1 JOIN で linked keyword text を取得、
  N+1 回避）/ `count(keyword_id=…)`。いずれも read-only、commit しない。
- **`raw_data` に `Article.body` 全文 / `meta_description` / `published_url` /
  credential / 個人情報は保存しない。** most similar は Keyword id+text / Article id+title まで。
- API: `POST /api/v1/keywords/{id}/signals/originality`（body なし → 201。empty corpus
  でも 201 + `normalized_value = 100.0`。Keyword 無しの 404 のみ、502/503 なし）。
- `scores/from-signals` 不変更。自動 Signal は 6/7（+ originality）、残り
  `competition_ease` のみ不足で `IncompleteSignalSetError` 継続。
- migration なし。V1 は Python 全件比較で十分（1 万件超 + 高頻度再計算で
  fingerprint / FTS を将来検討）。semantic similarity は V2 以降。
- 詳細は [architecture.md](architecture.md) の「Originality signal (Phase 2B-7)」。

### Phase 2B-8 (competition_ease V1 — 追加実費 0 円)

`competition_ease` = **Google Organic SEO の攻略しやすさ**（100 = 競合が弱い /
0 = 競合が強い）。**外部 SEO API / SERP API / スクレイピング / LLM / DataForSEO を
一切使わず、追加実費 0 円。** 無料ツール等で確認した Organic SEO Keyword Difficulty
（0 easy 〜 100 hard）を manual / CSV で投入する。これで Opportunity Score の
**7 / 7 component が揃う**。

- 純粋計算: `app/keyword/normalizers/competition_ease.py` —
  `competition_ease = round(clamp(100 - keyword_difficulty, 0, 100), 2)`。
  validation: required / numeric / finite / 0〜100。**NaN・Infinity・負値・>100・bool
  を reject**。`difficulty_scale = "0_easy_100_hard"` を必ず provenance へ。
- **Google Ads の `competition` / `competition_index` は使わない**（広告市場の競争度で
  あり Organic SEO Difficulty ではない）。Google Ads provider は無変更。
- schema: `CompetitionEaseManualCreate`（`keyword_difficulty` 0〜100 / `source_name`
  必須・strip / `source_reference` 任意（credential 禁止）/ `observed_at` 任意）。
- Service: `KeywordSignalService.derive_competition_ease_manual(keyword_id, payload)`
  (`component = competition_ease` / `provider = manual_keyword_difficulty` /
  `source_reference` 入力 or `manual-keyword-difficulty:v1` / `observed_at` 入力 or
  生成時 UTC / `period_start = period_end = None`)。commit / rollback / immutable history。
- API: `POST /api/v1/keywords/{id}/signals/competition-ease/manual`（body 必須、201、
  Keyword 無し 404、validation 422、**502 / 503 なし**）。
- CSV: `scripts/import_competition_ease.py`（`--file` 必須 / `--dry-run` / `--force`）。
  列 `keyword` / `keyword_difficulty` / `source_name`（必須）、`source_reference` /
  `observed_at`（任意）。**Keyword は既存 exact lookup のみ（新規作成しない）**。
  同一 CSV 内の keyword 重複は invalid。最新 Signal が `(difficulty, source_name,
  source_reference)` 一致なら default で skip、`--force` で同値でも新 history 追加。
- **同じ分析 batch では同じ Difficulty source を推奨。異なる source の Difficulty を
  絶対値として直接比較することには注意**（V1 は source 間補正なし）。
- raw_data: `keyword_difficulty` / `competition_ease` / `difficulty_scale` /
  `source_name` / `evidence_available` / `evidence_coverage` / `collection_method`
  (`"manual"`) / `normalizer_version` / `normalizer{name,version}`。**credential /
  API key / password / account ID / Google Ads competition / tracking parameter は
  保存しない。**
- **`scoring.py` / Opportunity Score weights / DB schema / migration は無変更。**
  7 component（search_demand / commercial_intent / affiliate_opportunity /
  competition_ease / trend / originality / site_relevance）が揃えば
  `scores/from-signals` は SUCCESS（既存 `scoring.py` をそのまま使用）。
- 将来候補（今回は実装しない）: Search Console 由来データ / optional paid SEO provider。
- 詳細は [architecture.md](architecture.md) の「Competition Ease signal (Phase 2B-8)」。

### Phase 2C-1 (Keyword Analysis Workflow — 追加実費ゼロ)

Phase 2B までの 7 component 個別処理を、実運用フローにまとめる。**新機能の formula は
無し。`scoring.py` 不変。DB schema 変更なし。DataForSEO / 有料 SEO / SERP / LLM /
embedding / scraper を使わず `requests` / `httpx` も import しない。**

無料運用フロー:
`keywords → auto 6 signals → competition template → (無料ツールで Difficulty 記入 →
import_competition_ease で投入) → 7/7 → Opportunity Score → ranking CSV`

- Service: `app/services/keyword_analysis_service.py` (`KeywordAnalysisService`) —
  `resolve_keywords` / `readiness` / `components_to_generate` / `collect_auto_signals` /
  `score_ready` / `ranking_rows` / `competition_ease_missing`。各 component の既存
  normalizer / Service / `score_keyword_from_latest_signals` をそのまま呼ぶ。
- CLI: `scripts/run_keyword_analysis.py`（orchestration の入口のみ）。
  `--keyword`（複数）/ `--input CSV` / `--create-missing`（default は既存 keyword のみ）/
  `--collect-auto-signals` / `--refresh` / `--export-competition-template PATH` /
  `--competition-source NAME` / `--score-ready` / `--output PATH` / `--dry-run`。
- **Google Ads bulk**: `KeywordMetricsCollectionService.collect_google_ads_signals_bulk(
  [(keyword_id, components), ...])` を追加。全 keyword の text を **1 回の
  `fetch_historical_metrics`** で取得し、既存 normalizer / `_build_*_raw_data` を再利用して
  `search_demand` / `commercial_intent` / `trend` を導出（keyword ごとに 3 回呼ばない）。
  必要 keyword が 0 なら provider を呼ばない。fetch 全体の失敗は provider error として伝播。
  **既存単体 collector は無変更。**
- **CJK keyword 照合**: Google Ads は応答で CJK keyword を分かち書きし直して返すことが
  ある（`AI 議事録 おすすめ` → `ai 議事 録 おすすめ`）。`_match_metrics` は
  NFKC + casefold + 空白正規化での**完全一致を優先**し、一致しない場合のみ空白を
  無視した compact key で照合する（fuzzy match なし）。曖昧なら割り当てず、単一応答
  fallback は bulk では無効（単体 collector のみ）。
- **rerun policy**: default は最新 Signal / Score があれば reuse（GA も呼ばない）、
  `--refresh` で再生成（新 history）。immutable history 維持。cache framework なし。
- **`--dry-run`**: DB write / Signal / Score / **Google Ads API** を一切行わず予定だけ表示。
- ranking CSV: `keyword` / `keyword_id` / 7 component latest 値 / `opportunity_score` /
  `analysis_status` / `missing_components`。並びは complete を score DESC → keyword ASC、
  その後 incomplete を keyword ASC（決定論的、`Keyword.opportunity_score` cache と一致）。
- competition template は `import_competition_ease.py` と同一ヘッダで、
  competition_ease が無い keyword のみ出力（importer ロジックは非コピー）。
- 詳細は [architecture.md](architecture.md) の「Keyword Analysis Workflow (Phase 2C-1)」。

## モデル関係

- `Article : Source = 1 : N` — `sources.article_id` (FK, `ondelete=CASCADE`, ORM は `delete-orphan`)。
  Source は記事の情報根拠 (引用元・参照元) を管理する。
- `Article : AffiliateProgram = N : N` — 中間モデル `ArticleAffiliateProgram`
  (`article_affiliate_programs`)。`is_primary` を保持。
  `UniqueConstraint(article_id, affiliate_program_id)` で重複登録を禁止。
- `Keyword : Article = 1 : N` — `articles.keyword_id` (FK, `ondelete=SET NULL`)。
- `Article : ArticleMetric = 1 : N` — `UniqueConstraint(article_id, metric_date, provider)`。

## 派生値の扱い

`ArticleMetric` の CTR・CVR は `impressions` / `clicks` / `conversions` から一意に
決まるため DB には保存せず、`ArticleMetric.ctr` / `ArticleMetric.conversion_rate`
プロパティで算出する (データ不整合の防止)。

## マイグレーション

- 正式手段は Alembic。`Base.metadata.create_all()` はテスト (`tests/conftest.py`) の
  一時 SQLite 用途に限定する。
- 適用: `uv run alembic upgrade head`
- 追加: モデル変更後に `uv run alembic revision --autogenerate -m "..."` を実行し、
  生成物を必ずレビューする。
- 接続先 URL は `migrations/env.py` が `get_settings().database_url` から取得する。

## Source & Verified Fact / FactPack (Phase 3B)

- **`Source` は immutable な観測記録。** 既存 `sources` テーブルを無変更で再利用
  (`source_type` に `official_pricing` 等の値)。`SourceService` は CREATE / GET / LIST /
  DELETE のみ (PATCH なし)。URL safety は `app/article/source_url_safety.py`
  (https のみ・credential query は reject・tracking query は除去して canonicalize・
  既知 tracking / affiliate redirect ホストは reject)。Fact から参照中の Source 削除は
  `entity_in_use` (409)。
- **`ArticleFact` は immutable 履歴。** `is_current` を持たず、現在値は
  `(article_id, subject_ref, fact_key)` ごとに `checked_at DESC, id DESC`。
  「更新」は新しい行の append。17 persistent fact key は `app/article/fact_keys.py`。
  `value_status` = verified / unknown / not_applicable。**missing (行なし) と unknown は
  別概念。** freshness は `app/article/fact_freshness.py` (料金 30 / 機能 90 / 静的 180 日)。
- **`FactPackService.build()` は DB write 禁止。** Source / Fact の最新値 + ArticlePlan
  から毎回集約。readiness gate は required fact (product名 / URL / use_cases /
  key_features / pricing / free_plan) が verified (pricing 2 つは explicit unknown も可)
  かつ fresh なら drafting 可。
- **CLI**: `uv run python scripts/import_article_facts.py --article-id 1 --file facts.json
  [--dry-run]` — JSON、1 file = 1 transaction (sub-service の commit を使わず
  `ArticleFactImportService` が transaction owner)、`--dry-run` は write 0、
  **Web アクセスなし** (human が公式ページで確認した結果を転記するだけ)。
- **比較対象 subject 集合 = Article の `ArticleAffiliateProgram` links** (V1 固定・
  known limitation)。**`Product` model は作らない** (`subject_ref` str + nullable
  `affiliate_program_id` で identity)。migration は add-only の `article_facts` 1 つのみ。

## Article Planning V1 (Phase 3A)

- **ArticlePlan は DB 非永続。** `ArticlePlanService.plan_for_keyword(keyword_id)` が
  Keyword + 最新 7 Signal + live Affiliate Catalog + originality provenance から
  `ArticlePlanDTO` を **決定論的** に毎回生成する (read-only、commit しない)。
- 純粋ロジックは `app/article/planning.py` (DB / FastAPI / 外部 API 非依存)。
  記事タイプ判定の優先順位・outline template・slug 案・compliance / guardrail を持つ。
  keyword 固有の長文は hard-code せず一般化した rule / template を使う。
- **slug 案**は新 romanization 依存を足さず NFKC + casefold ベースで Unicode-safe に
  生成し、日本語文字を残す。`articles.slug` はグローバル一意なので生成時に
  `ArticleRepository.get_by_slug` で衝突を確認し、衝突時は連番案 + warning。
  最終的な slug は approve request で human が override する。
- **atomic approval**: `ArticlePlanService.approve` は sub-service を経由せず repo を
  直接使い、1 回だけ commit する (partial state を作らない)。
- **1 Article 1 primary** は `ArticleAffiliateProgramService` がアプリ層で保証する。
  DB の partial unique index は今回作らない (SQLite の制限ではなく、V1 で migration を
  増やさず single-user / local 前提で運用するため。multi-worker 化時に再検討)。
- **LLM / Google Ads / Ahrefs / SERP / 有料 SEO API を一切呼ばない。migration なし。
  追加実費 0 円。** `meta_description` は Phase 3B で扱うため Article model は無変更。
- 企画案の JSON エクスポート: `uv run python scripts/export_article_plan.py
  --keyword "業務効率化 ツール おすすめ"` (read-only・1 件のみ・LLM なし)。

## SQLite から PostgreSQL への移行方針

- 接続文字列は `DATABASE_URL` の 1 箇所のみ変更する。
- `psycopg` ドライバを追加する: `uv add "psycopg[binary]"`。
- 制約名は `app/models/base.py` の `NAMING_CONVENTION` で固定済み。
- ステータスは文字列カラムで保存しているため、ネイティブ ENUM 型の移行は不要。
- SQLite 固有挙動 (外部キー制約が既定で無効、型アフィニティ) に依存しないこと。
- SQLite 向けに `env.py` は `render_as_batch=True` を使用する。
