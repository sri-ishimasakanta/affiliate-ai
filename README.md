# affiliate-ai

アフィリエイト記事の生成・運用を支援する AI プラットフォームの開発基盤。

## 必要環境

- Python 3.12 以上
- [uv](https://docs.astral.sh/uv/)

## セットアップ

```bash
uv sync
cp .env.example .env
```

## 開発コマンド

```bash
# 開発サーバ起動
uv run uvicorn app.main:app --reload

# DB マイグレーション適用
uv run alembic upgrade head

# マイグレーション作成 (モデル変更後)
uv run alembic revision --autogenerate -m "説明"

# Lint
uv run ruff check .

# テスト
uv run pytest

# Google Ads 実接続確認 (DB 保存なし。実 credential を .env に設定した状態で)
uv run python scripts/check_google_ads.py --keyword "AI 議事録 おすすめ"

# Google Ads 複数キーワードの指標をまとめて取得し比較 (DB 保存なし。CSV 出力は --output)
uv run python scripts/analyze_google_ads_keywords.py --keyword "AI 議事録 おすすめ" --keyword "AI 議事録 無料"

# アフィリエイト案件カタログをローカル CSV から投入 (--dry-run で検証のみ。ASP API なし)
uv run python scripts/import_affiliate_programs.py --file affiliate_programs.csv --dry-run

# keyword と active 案件の match_terms を照合し採点前の生データを出力 (DB read-only。採点なし)
uv run python scripts/analyze_affiliate_opportunities.py --input keywords.csv --output analysis.csv

# 無料ツールで調べた Organic SEO Keyword Difficulty を CSV でまとめて投入 (追加費用なし)
uv run python scripts/import_competition_ease.py --file keyword_difficulty.csv --dry-run

# キーワード分析ワークフロー: auto 6 signals → competition template → 7/7 → Opportunity Score → ranking (追加費用なし)
uv run python scripts/run_keyword_analysis.py --input keywords.csv --collect-auto-signals \
  --export-competition-template kd_template.csv --score-ready --output ranking.csv
```

## データベース

開発段階では SQLite (`sqlite:///./affiliate_ai.db`) を使用する。
`.env` の `DATABASE_URL` を差し替えることで PostgreSQL などへ変更できる。

```
DATABASE_URL=postgresql+psycopg://affiliate:affiliate@localhost:5432/affiliate_ai
```

エンジン生成は `app/config/database.py` の `build_engine()` に集約しており、
SQLite の場合のみ `check_same_thread=False` を、それ以外では
`pool_pre_ping=True` を適用する。

スキーマ変更は **Alembic を正式手段** とする (`Base.metadata.create_all()` は
テスト用途のみ)。マイグレーションスクリプトは `migrations/versions/` に置き、
接続先 URL は `migrations/env.py` が `Settings` (=`DATABASE_URL`) から取得する。

## ディレクトリ構成

```
app/
  config/        設定 (Settings) と DB 接続
  models/        SQLAlchemy モデル
  exceptions.py  Application 例外
  repositories/  永続化層 (Keyword / Article)
  services/      ドメインサービス (Keyword / Article)
  keyword/       schemas.py (キーワード調査ロジックは未実装)
  article/       schemas.py (記事生成ロジックは未実装)
  seo/           SEO 最適化 (未実装)
  affiliate/     広告案件マッチング (未実装)
  analytics/     成果分析 (未実装)
  wordpress/     WordPress 連携 (未実装)
  ai/            AI クライアント (未実装)
tests/
  unit/
  integration/
migrations/       Alembic (env.py / versions/)
scripts/
docs/
```

## 実装済みモデル

| モデル | テーブル | 概要 |
| --- | --- | --- |
| `Source` | `sources` | 記事の情報根拠 (引用元)。`Article : Source = 1 : N` |
| `Keyword` | `keywords` | 対象検索キーワード (`opportunity_score` = 最新スコアのキャッシュ) |
| `KeywordScore` | `keyword_scores` | Opportunity Score の計算履歴 (immutable、`Keyword : KeywordScore = 1 : N`) |
| `KeywordSignal` | `keyword_signals` | component 値の根拠となる観測データ (immutable 履歴、`raw_data` は generic JSON) |
| `KeywordScoreSignal` | `keyword_score_signals` | KeywordScore がどの Signal を使ったかの provenance (association) |
| `AffiliateProgram` | `affiliate_programs` | 紹介対象の広告案件。`match_terms` (keyword 関連付け用の検索語 JSON 配列) / `currency` (報酬の通貨、原則 JPY) を含む |
| `Article` | `articles` | 生成・管理する記事 (`published_url` を含む) |
| `ArticleAffiliateProgram` | `article_affiliate_programs` | 記事と広告案件の中間モデル (`is_primary`) |
| `ArticleMetric` | `article_metrics` | 記事ごと・日付ごとの成果指標 (CTR/CVR は派生プロパティ) |

Opportunity Score V1 (7 項目・重み・`competition_ease` の向き・履歴とキャッシュの
関係) は [docs/architecture.md](docs/architecture.md) を参照。

ステータスは `app/models/enums.py` の `StrEnum`
(`ArticleStatus` / `KeywordStatus` / `AffiliateProgramStatus`) を使用し、
DB には素の文字列カラムとして保存する (ネイティブ ENUM 型は使わない)。

## レイヤ構成 (Phase 1A)

Keyword / Article は Schema (Pydantic) → Service → Repository → モデル の順で分離。
Repository は DB アクセスのみ (commit しない)、Service がビジネスルールと
トランザクション境界を制御する。詳細は [docs/architecture.md](docs/architecture.md)。

外部データ収集 (Phase 2B): `External Provider → Provider (SDK→DTO) → Normalizer (pure)
→ KeywordSignal (根拠/履歴) → Opportunity Score`。Google Ads の `search_demand` /
`commercial_intent` / `trend` collector は実装済み (`commercial_intent` は Google Ads
指標 + キーワード文字列の複合、missing data は 0 点でなく weight 再正規化。`trend` は
`monthly_search_volumes` の最新 6 か月・前半3/後半3平均の symmetric change ratio。
Google Trends API は V1 未使用)。`site_relevance` は完全ローカルな rule-based 導出
(site profile `ai_business_automation` v1、外部 API・LLM なし。料金/比較/おすすめ等の
commercial intent 語は score に影響しない)。`affiliate_opportunity` はローカル
Affiliate Catalog の `match_terms` 照合による **供給側** 評価
(program 数 55% / percentage commission 35% / provider spread 10%。fixed commission
は provenance のみ、FX 換算なし。0 match は現 catalog 上の 0)。`originality` は
サイト内部の既存 Keyword / Article とのカニバリゼーション可能性の逆指標
(char bigram Dice + SequenceMatcher の max、Article タイトルは weight 0.80、
外部 API・LLM・embedding なし。空 corpus は 100 だが `corpus_available=false`)。
`competition_ease` は無料ツールで調べた Organic SEO Keyword Difficulty (0-100) の
手動 / CSV 投入で `ease = 100 - difficulty` (**外部 SEO API なし・追加費用 0 円**。
Google Ads competition は使わない)。これで Opportunity Score の 7 component が揃う。
Google Ads の credential (`GOOGLE_ADS_*`) は
**未設定でもアプリ・既存 API は起動する** (collector 呼び出し時のみ検証)。
キー名は [.env.example](.env.example)、詳細は [docs/development.md](docs/development.md)。

## REST API (Phase 1B)

```bash
# 起動
uv run uvicorn app.main:app --reload
```

- OpenAPI (Swagger UI): `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`
- ヘルスチェック: `GET /health`

prefix `/api/v1`。Application 例外は共通ハンドラで HTTP へ変換し、
レスポンスは `{"error": {"code": "...", "message": "..."}}` 形式で統一する
(`entity_not_found` → 404 / `duplicate_entity` → 409 / `invalid_status_transition` → 409 /
`incomplete_signal_set` → 409 / `provider_not_configured` → 503 /
`external_provider_data_error` → 502 / `external_provider_error` → 502)。

| Method | Path | 概要 | 成功 |
| --- | --- | --- | --- |
| POST | `/api/v1/keywords` | キーワード作成 | 201 |
| GET | `/api/v1/keywords` | 一覧 (`limit` 1..100 / `offset` >=0) | 200 |
| GET | `/api/v1/keywords/{keyword_id}` | 1 件取得 | 200 |
| PATCH | `/api/v1/keywords/{keyword_id}` | 部分更新 | 200 |
| DELETE | `/api/v1/keywords/{keyword_id}` | 削除 | 204 |
| PATCH | `/api/v1/keywords/{keyword_id}/status` | status 変更 | 200 |
| POST | `/api/v1/keywords/{keyword_id}/scores` | Opportunity Score 計算・保存 (manual) | 201 |
| GET | `/api/v1/keywords/{keyword_id}/scores/latest` | 最新スコア取得 | 200 |
| GET | `/api/v1/keywords/{keyword_id}/scores` | スコア履歴 (`limit` 1..100 / `offset` >=0) | 200 |
| POST | `/api/v1/keywords/{keyword_id}/scores/from-signals` | 最新 7 Signal からスコア作成 (不足時 409 `incomplete_signal_set`) | 201 |
| GET | `/api/v1/keywords/{keyword_id}/scores/{score_id}/signals` | スコアの provenance (使用 Signal 一覧) | 200 |
| POST | `/api/v1/keywords/{keyword_id}/signals` | Signal (根拠データ) 追加 | 201 |
| GET | `/api/v1/keywords/{keyword_id}/signals` | Signal 履歴 (`component` optional / `limit` 1..100 / `offset` >=0、新しい順) | 200 |
| GET | `/api/v1/keywords/{keyword_id}/signals/{component}/latest` | 指定 component の最新 Signal (`observed_at DESC, id DESC`) | 200 |
| POST | `/api/v1/keywords/{keyword_id}/signals/google-ads/search-demand` | Google Ads から `search_demand` Signal を収集 (body なし) | 201 |
| POST | `/api/v1/keywords/{keyword_id}/signals/google-ads/commercial-intent` | Google Ads + キーワード文字列から `commercial_intent` Signal を収集 (body なし) | 201 |
| POST | `/api/v1/keywords/{keyword_id}/signals/google-ads/trend` | Google Ads の `monthly_search_volumes` から `trend` Signal を収集 (body なし) | 201 |
| POST | `/api/v1/keywords/{keyword_id}/signals/site-relevance` | サイト profile から `site_relevance` Signal をローカル導出 (body なし・外部 API なし) | 201 |
| POST | `/api/v1/keywords/{keyword_id}/signals/affiliate-opportunity` | ローカル Affiliate Catalog から `affiliate_opportunity` Signal を導出 (body なし・供給側評価) | 201 |
| POST | `/api/v1/keywords/{keyword_id}/signals/originality` | サイト内部の既存 Keyword / Article から `originality` Signal を導出 (body なし・カニバリ逆指標) | 201 |
| POST | `/api/v1/keywords/{keyword_id}/signals/competition-ease/manual` | 手動投入の Organic SEO Keyword Difficulty (0-100) から `competition_ease` Signal を作成 (`ease = 100 - difficulty`・外部 API なし) | 201 |
| POST | `/api/v1/articles` | 記事作成 | 201 |
| GET | `/api/v1/articles` | 一覧 (`limit` 1..100 / `offset` >=0) | 200 |
| GET | `/api/v1/articles/{article_id}` | 1 件取得 | 200 |
| PATCH | `/api/v1/articles/{article_id}` | 部分更新 | 200 |
| DELETE | `/api/v1/articles/{article_id}` | 削除 | 204 |
| PATCH | `/api/v1/articles/{article_id}/status` | status 変更 | 200 |
| POST | `/api/v1/affiliate-programs` | アフィリエイト案件作成 (同一 `name`+`provider` は 409) | 201 |
| GET | `/api/v1/affiliate-programs` | 一覧 (`status` / `provider` / `category` filter、`limit` 1..100 / `offset` >=0) | 200 |
| GET | `/api/v1/affiliate-programs/{id}` | 1 件取得 | 200 |
| PATCH | `/api/v1/affiliate-programs/{id}` | 部分更新 | 200 |
| DELETE | `/api/v1/affiliate-programs/{id}` | 削除 | 204 |
