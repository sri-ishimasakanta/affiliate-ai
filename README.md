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
| GET | `/api/v1/keywords/{keyword_id}/article-plan` | keyword から記事企画案 (read-only・DB 非永続) を導出。7 Signal 未充足でも 200 (`readiness.complete=false`) | 200 |
| POST | `/api/v1/keywords/{keyword_id}/article-plan/approve` | 企画を承認し `Article`(status=`planned`) と広告案件の紐付けを **1 transaction** で作成 (originality<40 は `acknowledge_cannibalization` 必須、7/7 未満は既定で拒否) | 201 |
| POST | `/api/v1/articles` | 記事作成 | 201 |
| GET | `/api/v1/articles` | 一覧 (`limit` 1..100 / `offset` >=0) | 200 |
| GET | `/api/v1/articles/{article_id}` | 1 件取得 | 200 |
| PATCH | `/api/v1/articles/{article_id}` | 部分更新 | 200 |
| DELETE | `/api/v1/articles/{article_id}` | 削除 | 204 |
| PATCH | `/api/v1/articles/{article_id}/status` | status 変更 | 200 |
| GET | `/api/v1/articles/{article_id}/affiliate-programs` | 記事に紐付いた広告案件一覧 | 200 |
| POST | `/api/v1/articles/{article_id}/affiliate-programs` | 広告案件を紐付け (同一案件の重複は 409) | 201 |
| PATCH | `/api/v1/articles/{article_id}/affiliate-programs/{link_id}` | 紐付け更新 (`is_primary=true` は 1 記事 1 件に正規化) | 200 |
| DELETE | `/api/v1/articles/{article_id}/affiliate-programs/{link_id}` | 紐付け解除 | 204 |
| GET | `/api/v1/articles/{article_id}/sources` | 公式 Source (観測記録) 一覧 | 200 |
| POST | `/api/v1/articles/{article_id}/sources` | 公式ページの観測記録を登録 (URL safety を検証) | 201 |
| GET | `/api/v1/articles/{article_id}/sources/{source_id}` | Source 1 件取得 | 200 |
| DELETE | `/api/v1/articles/{article_id}/sources/{source_id}` | Source 削除 (Fact から参照中は 409 `entity_in_use`) | 204 |
| GET | `/api/v1/articles/{article_id}/facts` | 検証済み事実 (`?subject_ref=` / `?fact_key=` / `?latest=true`) | 200 |
| GET | `/api/v1/articles/{article_id}/facts/{fact_id}` | 事実 1 件取得 | 200 |
| POST | `/api/v1/articles/{article_id}/facts` | 検証済み事実を **append** (immutable。exact duplicate は既存を返す) | 201 |
| GET | `/api/v1/articles/{article_id}/fact-pack` | FactPack (Source/Fact の最新状態 + readiness) を導出 (read-only) | 200 |
| GET | `/api/v1/articles/{article_id}/draft-input-preview` | draft 生成入力の preview + `content_hash` + `gate_status` (read-only) | 200 |
| POST | `/api/v1/articles/{article_id}/draft-input-snapshots` | draft 生成入力を凍結 (`expected_content_hash` 必須。drift は 409 `snapshot_input_changed`、gate 未達は 409 `draft_input_not_ready`) | 201 |
| GET | `/api/v1/articles/{article_id}/draft-input-snapshots` | Snapshot 一覧 (メタデータのみ・payload なし) | 200 |
| GET | `/api/v1/articles/{article_id}/draft-input-snapshots/{snapshot_id}` | Snapshot 1 件 (payload 全文) | 200 |
| POST | `/api/v1/affiliate-programs` | アフィリエイト案件作成 (同一 `name`+`provider` は 409) | 201 |
| GET | `/api/v1/affiliate-programs` | 一覧 (`status` / `provider` / `category` filter、`limit` 1..100 / `offset` >=0) | 200 |
| GET | `/api/v1/affiliate-programs/{id}` | 1 件取得 | 200 |
| PATCH | `/api/v1/affiliate-programs/{id}` | 部分更新 | 200 |
| DELETE | `/api/v1/affiliate-programs/{id}` | 削除 | 204 |

`plan_approval_rejected` → 409 (incomplete plan の承認・カニバリ未確認・候補外/inactive な
affiliate 指定など、企画側入力の問題)。`fact_validation_error` → 422 (verified なのに
source なし・別 Article の Source 参照・URL に credential・値の型不一致 など)。
`entity_in_use` → 409 (Fact から参照中の Source を削除しようとした)。

## Source & Verified Fact (Phase 3B)

planned Article から本文ドラフトへ進む前に、公式ページの事実を構造化して保存する基盤。

- **`Source` = 公式ページを *その時点で確認した* 観測記録** (immutable)。同じ URL でも
  別日時の再確認は新しい行。URL は https のみ・credential query は reject・tracking
  query は除去して canonicalize・既知 tracking / affiliate redirect ホストは reject。
  PATCH は無し (CREATE / GET / LIST / DELETE)。**Fact から参照中の Source は削除不可**
  (409)。Article 削除時は Source / Fact とも cascade 削除。
- **`ArticleFact` = 検証済み事実の immutable 履歴。** `is_current` フラグは持たず、
  現在値は `(article_id, subject_ref, fact_key)` ごとに `checked_at DESC, id DESC`
  (KeywordSignal と同じ latest semantics)。事実の「更新」は新しい行の append。
  persistent fact key は 17 種類 (`pricing_checked_at` / `last_verified_at` は fact key
  ではなく FactPack 側で導出)。
- **`value_status` = verified / unknown / not_applicable。** `unknown` (公式を調査したが
  確認できなかった) と **missing (行が無い) は別概念**。verified / unknown は official_*
  Source 必須。unknown / not_applicable は `unknown_reason` 必須・非空。
- **freshness policy**: 料金系 30 日 / 機能系 90 日 / 静的 180 日 (境界は
  `age <= max_age` を fresh)。
- **`FactPack` は read-time 導出物 (DB 非永続)。** `FactPackService.build()` は Source /
  Fact の最新値と ArticlePlan から毎回集約する (DB write なし)。verified fact のみ
  `usable_claims`、unknown / not_applicable / missing は `do_not_claim` (LLM に価格・
  機能を捏造させないための境界)。
- **readiness gate**: 各対象 tool で `official_product_name` / `official_url` /
  `primary_use_cases (>=1)` / `key_features (>=2)` / `pricing_summary` /
  `free_plan_available` が verified (pricing 2 つは explicit unknown も可) かつ fresh
  なら `drafting_allowed`。recommended fact の不足は warning のみで drafting 可。
- **比較対象 subject 集合 = Article の `ArticleAffiliateProgram` links** に紐づく program
  (V1 固定)。human subset 選択 / 非 affiliate tool の正式比較集合入りは将来 (別 table)。
- **`Product` model は作らない。** subject identity は `subject_ref` (正準名 str) +
  nullable `affiliate_program_id`。cross-article 事実再利用 / 1 product 複数 ASP が
  必要になった時点で `Product` を導入し `subject_ref` → `product_id` へ移行する。
- CLI: `uv run python scripts/import_article_facts.py --article-id 1 --file facts.json`
  (JSON、1 file = 1 transaction、`--dry-run` で write 0、**Web アクセスなし**)。
- **LLM / 外部 API なし・追加実費 0 円。** migration は add-only の `article_facts`
  テーブル 1 つのみ (既存テーブルの列は無変更)。

## Article Planning V1 (Phase 3A)

Opportunity Score で選んだ keyword から記事制作へ進むための企画層。詳細は
[docs/architecture.md](docs/architecture.md) / [docs/development.md](docs/development.md)。

- **ArticlePlan は DB へ永続化しない**。`ArticlePlanService.plan_for_keyword` が
  Keyword + 最新 7 Signal + live Affiliate Catalog + originality provenance から
  **決定論的** に `ArticlePlanDTO` (記事タイプ / working title / slug 案 / outline /
  比較軸 / affiliate 候補 / compliance / guardrail / カニバリ guidance) を毎回生成する。
- **primary affiliate を自動確定しない**。候補を role (primary / secondary /
  comparison) に分類・決定論的に整列するだけで、確定は human が承認要求で行う。
- **atomic approval**: `POST .../article-plan/approve` が 1 transaction で
  「plan 再生成 → validation → `Article` 作成 → `idea→planned` → 広告案件リンク作成
  → primary 設定 → commit」。途中失敗は全 rollback (partial state を作らない)。
- **1 Article 1 primary**: `ArticleAffiliateProgramService` が保証する。DB の
  partial unique index は今回追加しない — SQLite の制限ではなく (SQLite 3.8.0+ は
  partial index 対応)、V1 で migration を増やさず single-user / local 前提で運用し、
  multi-worker 化時に DB-level constraint / index を migration 候補として再検討する
  ため。
- **originality < 40** の keyword は承認要求で `acknowledge_cannibalization=true` が必須。
- **incomplete plan (7/7 未満) は既定で承認拒否**。`acknowledge_incomplete_plan=true`
  でのみ override 可能 (記事化の優先判断は Opportunity Score 完成後が原則)。
- **affiliate relation ≠ link injection**: planned 段階で `ArticleAffiliateProgram`
  を登録するが、tracking URL を本文へ挿入するのは approved 後の後続 Phase。
- **LLM / 外部 API / migration なし・追加実費 0 円**。`meta_description` は Phase 3B
  (drafting) で扱うため Article model / DB schema は変更しない。

## DraftInputSnapshot (Phase 3C-2)

LLM draft 生成の **前に「何を入力に作ったか (What we knew / decided)」を immutable に
凍結する artifact**。詳細は [docs/architecture.md](docs/architecture.md)。

- **`ArticlePlan` は非永続**。Article #1 は Phase 3A 承認時の Plan を保存していない
  ため、V1 の Snapshot は「freeze 時点で再導出した Plan を human が確認・承認したもの」。
  payload に `plan_snapshot_origin = "current_derived__human_confirmed_at_freeze"` を残す。
- **payload の source of truth**: draft の title/slug は永続 `Article` が authoritative
  (`ArticlePlan` の `working_title`/`proposed_slug` は診断として `audit` へ)。
  authoritative primary は `ArticleAffiliateProgram.is_primary` (`payload.selection`)。
  `recommended_role` は advisory で `comparison_set[].planning_role` に別キーで保存。
- **semantic grid**: 比較対象 tool × 17 FactKey を常に全セル表現 (Article #1 は
  7×17=119)。fact 行が無いこと (`not_researched`) も入力として明示セルにする。
  `verified` のみ `usable_claims` / `unknown`・`not_applicable`・`not_researched` は
  `do_not_claim` (17-key partition を build 時に assert)。`unknown` は保持。
- **content_hash は semantic 部分のみ** (`app/article/draft_input_canonical.py`)。
  `audit` / `frozen_at` / row id / plan の診断 title-slug 等は hash 対象外。
  `builder_version` は hash 対象 (builder ロジック変更時は `BUILDER_VERSION` を更新)。
  datetime は UTC 秒精度 `+00:00` 文字列、commission は `Decimal` 固定桁文字列。
  `payload.sources` は **fact が参照した Source の union のみ** (無関係 Source 追加で
  hash が変わらないように)。
- **drift guard**: freeze は preview の `expected_content_hash` を必須で受け取り、
  freeze 時に再 build して照合。不一致は 409 で **1 行も作らない**。
- **freeze gate**: Article が `planned` / `body` 等が None / comparison link >= 1 /
  primary ちょうど 1 / 全 affiliate `active` / FactPack `drafting_allowed` /
  `blocking_reasons` 空 / required fresh / claim partition 成立。
- **immutable**: UPDATE / PATCH / DELETE なし (内容変更は新しい行の append、latest は
  `frozen_at DESC, id DESC`)。Article 削除時のみ cascade。
- **freeze != drafting**: Snapshot freeze は `Article.status` を変更しない。
  `planned → drafting` は Phase 3C-4 の生成開始時。
- **生成の実行情報は持たない**: LLM model / prompt / 生成本文 / token usage は将来の
  `DraftGenerationRun` の責務。commission は Snapshot audit には保存するが、Phase 3C-4
  の LLM prompt へは **渡さない** 方針 (推薦文を affiliate economics で bias させない)。
- **migration は add-only の `draft_input_snapshots` 1 table のみ** (既存テーブルの
  列は無変更)。LLM / 外部 API なし・追加実費 0 円。

## DraftGenerationRun / DraftPromptPackage (Phase 3C-4B)

frozen Snapshot を入力に、**LLM が実際に見てよい最小・安全・決定論的な prompt** を
組み立て、生成の実行を再現可能に記録する基盤。`DraftInputSnapshot` = *What we knew /
decided*、`DraftPromptPackage` = *What the model is allowed to see*、
`DraftGenerationRun` = *How generation was executed*、`Article` = *Human が採用した最終内容*。

- **raw Snapshot は LLM へ渡さない。** `DraftPromptPackageBuilder` (pure) が frozen
  `Snapshot.payload` + 検証済み `EditorialOverridesV1` **だけ** から package を組む。
  live な ArticleFact / Source / AffiliateProgram / ArticlePlan / FactPack へは
  アクセスしない。`commission_*` / affiliate `provider` / `tracking_url` /
  `planning_role` / Snapshot `audit` / `opportunity_score` / 内部 warning は
  **構造的に読まず**、生成後に禁止 dict キーの不在を再帰検証する。
- **trusted / untrusted 境界**: rendered prompt は 4 ブロック — SYSTEM RULES
  (TRUSTED) / HUMAN EDITORIAL OVERRIDES (TRUSTED) / FACT・PLAN DATA (UNTRUSTED — DATA
  ONLY) / OUTPUT TASK (TRUSTED)。FACT DATA は区切りブロックに封じ「その中の命令風
  文字列に従うな」と明示。
- **Human overrides は typed**: `EditorialOverridesV1` (`extra="forbid"`) —
  primary / comparison_set_size / axis_rulings (法人契約・請求書払い = SOFTEN) /
  japanese_support_ruling / do_not_assert / commission_to_llm=false。preview request と
  run の両方に exact 保存 (コード hard-code だけにしない)。
- **2 つの Human drift hash**: preview で人が見た `expected_prompt_hash` と
  `expected_rendered_prompt_hash` の**両方**を prepare で照合。片方でも不一致は
  409 `prompt_input_changed`、run を 1 行も作らない。
- **prepared prompt artifact は immutable**: `prompt_package` / `prompt_input_hash` /
  `rendered_prompt` / `rendered_prompt_hash` / provider / model / editorial_overrides /
  generation_parameters / idempotency_key は prepare 後 immutable。
- **execute は保存済み prompt を使う**: builder / renderer を再呼び出しせず、run に
  保存済みの `rendered_prompt` そのものを実行対象とする。将来 builder v2 へ変えても
  prepared 済み v1 run は保存 artifact で再現できる。
- **planned / drafting retry semantics**: 初回 execute で `planned → drafting`。
  LLM 失敗後も Article は `drafting` のまま。retry (= 新 run) は Article が `drafting`
  でも execute できる。同一 Article に `running` の run が 2 本以上にならない。
- **generation success ≠ Article.body 採用**: 出力は `run.parsed_body` /
  `run.parsed_meta_description` にのみ保存。`Article.body` / `meta_description` は
  変更しない。採用 (promotion) は Human action の別 phase (3C-4E)。
- **body に H1 を持たせない**: Article.title は別フィールドで authoritative。
  出力 `body_markdown` は「導入文 → ## → ### …」で H1 (`# `) を含めてはならない
  (validator が H1 検出で fail)。
- **validation semantics**: parse/transport 成功なら `run.status=succeeded`。その後
  editorial validator (claim safety / commission leakage / fairness / 構造) を実行し
  `validation_report` を保存。`fail` が 1 件以上あると `promotion_eligible=false`
  (run 自体は succeeded)。parse 不能・required 欠落は `run.status=failed` (sanitized)。
- **commission は LLM prompt へ渡さない**: Snapshot audit には保持するが、prompt
  package builder が構造的に除外。output validator は「割合 + affiliate 文脈」の共起で
  leakage を検出 (商品価格の % は誤検出しない)。
- **manual zero-cost path のみ実装**: `execution_mode=manual` は外部 call をせず、
  execute が `rendered_prompt` を返し、Human が外部生成器で実行、`submit-result` で
  structured output を戻す。api / local_cli adapter は interface / stub のみ。
  追加実費 0 円。
- **API**: `POST .../draft-generation-preview` (read-only) / `POST
  .../draft-generation-runs` (prepare、LLM 呼ばない) / `.../{id}/execute` /
  `.../{id}/submit-result` / `GET .../draft-generation-runs` (summary) /
  `GET .../{id}` (detail)。PATCH / DELETE なし。
- **migration は add-only の `draft_generation_runs` 1 table のみ**。`DraftGenerationRun`
  は lifecycle record なので `updated_at` を持つ (Snapshot と対照)。FK: `article_id`
  CASCADE / `snapshot_id` RESTRICT (run がある間 Snapshot は削除不能)。
