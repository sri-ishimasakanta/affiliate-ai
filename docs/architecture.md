# アーキテクチャ

## レイヤと責務

Phase 1A で Keyword / Article に導入した層分離。

```
API 入出力 (将来の FastAPI)
        │  Pydantic schema
        ▼
   Service 層            ビジネスロジック
   app/services/         - Schema <-> モデルの対応付け
                         - 重複チェック / 存在チェック
                         - status 遷移ルールの適用
                         - commit / rollback (トランザクション境界)
        │  モデル属性名で呼び出し
        ▼
   Repository 層         DB アクセスのみ
   app/repositories/     - SQLAlchemy Session による CRUD
                         - flush は行うが commit はしない
                         - ビジネスルールを持たない
        │
        ▼
   SQLAlchemy モデル (app/models/)
```

- **SQLAlchemy モデルを API 入出力に直接使わない。** 外部境界は必ず
  `app/<domain>/schemas.py` の Pydantic モデルを経由する。
- **Repository は commit しない。** `flush()` で採番と制約違反の検知のみ行い、
  トランザクションの確定/破棄は Service が制御する。Service は
  `IntegrityError` を捕捉して `app/exceptions.py` の型へ変換する。
- 汎用 `BaseRepository` や CRUD フレームワーク、ワークフローエンジンは作らない。

### Schema フィールドとモデル属性の対応

スキーマは API 上の名前を採用し、既存モデル属性名との差分は Service が変換する。

| ドメイン | Schema フィールド | モデル属性 |
| --- | --- | --- |
| Keyword | `search_intent` | `Keyword.intent` |
| Article | `draft_content` | `Article.body` |
| Article | `wordpress_id` | `Article.wordpress_post_id` |

`category` / `opportunity_score` (Keyword)、`published_url` (Article) は
Phase 1A で追加したカラム (migration `5be5b19f3007`)。`opportunity_score` の
算出・保存は Phase 2A (下記「Opportunity Score V1」) で実装した。

## status 遷移ルール

定義は `app/services/status_transitions.py`。宣言的な遷移表 +
`ensure_transition_allowed()` のみ。**同一 status への変更は許可**し、
それ以外で表に無い遷移は `InvalidStatusTransitionError` を送出する。

### Keyword (`KeywordStatus`)

```
discovered ──▶ analyzed ──▶ selected ──▶ assigned
                     └────▶ rejected
```

| 現在 | 許可される遷移先 |
| --- | --- |
| `discovered` | `analyzed` |
| `analyzed` | `selected`, `rejected` |
| `selected` | `assigned` |
| `assigned` | (なし) |
| `rejected` | (なし) |

### Article (`ArticleStatus`)

```
idea ─▶ planned ─▶ drafting ─▶ review ─▶ approved ─▶ published ─▶ rewrite ─▶ review
                                                    │           │           │
                                                    └───────────┴───────────┴─▶ archived
```

| 現在 | 許可される遷移先 |
| --- | --- |
| `idea` | `planned` |
| `planned` | `drafting` |
| `drafting` | `review` |
| `review` | `approved` |
| `approved` | `published`, `archived` |
| `published` | `rewrite`, `archived` |
| `rewrite` | `review`, `archived` |
| `archived` | (なし) |

- `published` への遷移時、`published_at` が未設定なら現在時刻 (UTC) を設定する。
  それ以外の副作用は持たせない。

## Opportunity Score V1 (Phase 2A)

キーワードの「狙う価値」を **決定論的に** 数値化する仕組み。外部 SEO データの
取得は行わず、7 項目の入力値 (各 0〜100) から重み付き合計でスコアを出す。

純粋計算ロジックは `app/keyword/scoring.py`
(`OpportunityScoreInput` / `OpportunityScoreResult` /
`calculate_opportunity_score()`)。DB・FastAPI・Pydantic に依存しない。
strategy pattern や scoring framework は作らない。

### 7 項目と配点 (V1 weights)

重みは `app/keyword/scoring.py` の `OPPORTUNITY_SCORE_WEIGHTS` に集約 (合計 1.0)。

| コンポーネント | 重み | 意味 |
| --- | --- | --- |
| `search_demand` | 0.20 | 検索需要の大きさ |
| `commercial_intent` | 0.20 | 商用・購買寄りの検索意図 |
| `affiliate_opportunity` | 0.20 | アフィリエイト収益機会 |
| `competition_ease` | 0.15 | **競合の攻略しやすさ (100 に近いほど競合が弱い)** |
| `trend` | 0.10 | トレンドの伸び |
| `originality` | 0.10 | 自社が出せる独自性 |
| `site_relevance` | 0.05 | サイトテーマとの関連度 |

```
total = search_demand*0.20 + commercial_intent*0.20 + affiliate_opportunity*0.20
      + competition_ease*0.15 + trend*0.10 + originality*0.10 + site_relevance*0.05
```

Opportunity Score は 0〜100 (小数第 2 位)。`competition_ease` は「難易度」ではなく
「攻略しやすさ」の向きで統一しているため、**値が高いほど total も高くなる**。

### 履歴とキャッシュ

| 置き場所 | 役割 |
| --- | --- |
| `keyword_scores` テーブル (`KeywordScore`) | 計算の **履歴**。各コンポーネント値・`total_score`・`score_version`・`input_source`・`created_at` を保存。原則 immutable (`updated_at` なし)。`Keyword : KeywordScore = 1 : N`、Keyword 削除で CASCADE。 |
| `Keyword.opportunity_score` | **最新 total_score のキャッシュ値**。一覧・詳細で高速に参照するための非正規化。 |

`KeywordScoringService.score_keyword()` は次を **1 トランザクション** で行う
(失敗時 rollback):

1. Keyword 存在確認 (無ければ `EntityNotFoundError`)
2. 純粋関数でスコア計算
3. `KeywordScore` 履歴を追加
4. `Keyword.opportunity_score` を最新 `total_score` に更新
5. `Keyword.status == discovered` のときのみ `analyzed` へ変更
6. commit

DB 側でも各コンポーネントと `total_score` が 0〜100 であることを
`CheckConstraint`(`<col> BETWEEN 0 AND 100`、SQLite / PostgreSQL 共通) で保証し、
Pydantic (`KeywordScoreCreate`) でも `ge=0, le=100` を検証する。
`total_score` と `score_version` はクライアント入力を受け付けない
(`extra="forbid"`)。

### スコアと status の関係

- スコア作成成功時、`discovered` のキーワードのみ自動で `analyzed` へ遷移する。
- 既に `analyzed` / `selected` / `assigned` / `rejected` の場合は status を変えない。
- 再スコアリングは **どの `KeywordStatus` でも許可**する (履歴が 1 行増える)。
- **スコアによって `selected` / `rejected` へ自動遷移することはない。**
  選定・却下は人間の判断 (別 API) で行う。

### 将来拡張

- `input_source` は現在 `"manual"` 既定。`from-signals` 経由では `"signals"`。
  将来は `"collector"` / `"ai"` / `"imported"` 等を記録する。
- 重み変更時は `score_version` を上げ、過去履歴は元の version のまま残す。

## Keyword Signal と外部データ (Phase 2B-1)

### データフローと責務

```
External Provider (Google Ads / Google Trends / DataForSEO / SERP / ASP / manual ...)
        │  provider 固有の生データ
        ▼
   raw data (KeywordSignal.raw_data に JSON でそのまま保存)
        │
        ▼
   normalizer (provider ごと・component ごとの正規化。Phase 2B-2 以降で実装)
        │  0〜100 の normalized_value
        ▼
   KeywordSignal (component 値の「根拠」。immutable 履歴)
        │  7 component それぞれの最新 normalized_value
        ▼
   Opportunity Score (calculate_opportunity_score → KeywordScore + Keyword.opportunity_score)
```

**正規化ロジックは `KeywordSignal` にも Repository にも持たせない。**
今回は `normalized_value` (0〜100) を collector / client から受け取るだけ。
`search_volume → search_demand`、`CPC → commercial_intent`、`Trends → trend`、
`SERP → competition_ease` などの変換は Phase 2B-2 以降で provider 別 normalizer
として実装する。

### 3 層の役割の違い

| 層 | モデル | 役割 | 可変性 |
| --- | --- | --- | --- |
| 根拠・履歴 | `KeywordSignal` (`keyword_signals`) | component 値の観測データ。provider・raw_data・観測日時・対象期間を保持。1 keyword × 1 component に複数行 (再収集ごとに追記) | immutable (追記のみ) |
| 意思決定時点のスナップショット | `KeywordScore` (`keyword_scores`) | あるタイミングで実際に Opportunity Score 計算に使った 7 値と `total_score`。`KeywordScoreSignal` でどの Signal を使ったか追跡可能 | immutable (追記のみ) |
| 最新 score キャッシュ | `Keyword.opportunity_score` | 一覧・詳細で高速参照するための非正規化値。最新 `KeywordScore.total_score` と一致 | 上書き (mutable) |

- `KeywordSignal` : `KeywordScore` の関係は `KeywordScoreSignal` 中間テーブル
  (association) で表す。**1:1 FK にしない** — 将来「複数 Signal から 1 component を
  算出」する余地を残すため。同じ `(score, signal)` は重複不可。
- `score_keyword_from_latest_signals(keyword_id)` は 7 component すべての最新
  Signal が揃っている場合のみ `KeywordScore` を作成し、使った 7 件を
  `KeywordScoreSignal` で紐付ける。1 つでも欠けると `IncompleteSignalSetError`
  (HTTP 409 / `incomplete_signal_set`) を送出し、何も作成しない。
- 手動スコア (`POST /scores`) は従来どおり動作し、`KeywordScoreSignal` は 0 件でよい。

### "最新 Signal" の定義と Provider 混在ルール

component ごとの「最新 Signal」= **`observed_at` が最も新しい Signal。同一
`observed_at` なら `id` が最大**のもの。並び順・`get_latest` は一貫して
`ORDER BY observed_at DESC, id DESC` (`KeywordSignalRepository`)。

- `id` (= 挿入順) ではなく `observed_at` (= 観測日時) を基準にすることで、
  外部 Provider から過去データをバックフィルしても "最新" の意味が崩れない。
- **provider による固定優先順位はまだ導入しない。** `manual` / `google_ads` 等が
  混在しても「最も新しく観測された Signal」がそのまま採用される。
  Provider priority / override が必要になったら別 Phase で追加する。

### Google Ads search_demand collector (Phase 2B-2)

```
Google Ads Keyword Planning
        │  KeywordPlanIdeaService.GenerateKeywordHistoricalMetrics
        ▼
GoogleAdsKeywordMetricsProvider        app/keyword/providers/google_ads.py
  - Google Ads client 生成 (遅延 import)
  - request 組み立て (customer_id / geoTargetConstants / languageConstants /
    keyword_plan_network=GOOGLE_SEARCH。すべて Settings 経由・magic number なし)
  - SDK response -> 内部 DTO へ変換 (SDK enum/object を上位へ漏らさない)
        ▼
GoogleAdsKeywordMetrics DTO            dataclass (frozen)
  keyword / avg_monthly_searches / monthly_search_volumes[MonthlySearchVolume] /
  competition / competition_index / low_top_of_page_bid_micros /
  high_top_of_page_bid_micros
        ▼
SearchDemandNormalizer V1              app/keyword/normalizers/search_demand.py
  normalize_search_demand(avg_monthly_searches) -> 0〜100 (小数第 2 位)
  score = min(100.0, 20.0 * log10(avg_monthly_searches + 1))
  (DB/FastAPI/SDK 非依存・決定論的。DB 保存も Provider 呼び出しもしない)
        ▼
KeywordMetricsCollectionService       app/services/keyword_metrics_collection_service.py
  Keyword 存在確認 -> Provider 呼出 -> metrics 検証 -> normalize ->
  KeywordSignal 作成 (1 トランザクション / 失敗時 rollback)
        ▼
KeywordSignal
  component = search_demand
  provider  = google_ads
  normalized_value = Normalizer 結果
  observed_at = 収集を実施した UTC 日時
  period_start / period_end = monthly_search_volumes の最古月〜最新月 (算出できれば)
  source_reference = "google-ads:keyword-plan-idea:historical-metrics"
  raw_data = Google Ads 指標 (primitive) + normalizer metadata
```

Google Ads collector が今フェーズで生成してよい Signal は **`search_demand` のみ**。

#### raw_data に保存する Google Ads 生指標

| raw field | 意味 | 今フェーズの使い道 |
| --- | --- | --- |
| `avg_monthly_searches` | 平均月間検索数 | `search_demand` の正規化元 |
| `monthly_search_volumes[]` (`year` / `month` 1–12 / `monthly_searches`) | 月別検索数 | `period_start`/`period_end` の算出。将来の季節性・trend 補助 |
| `competition` (`LOW`/`MEDIUM`/`HIGH`/…) | 広告オークションの競争度 | 保存のみ (下記注意) |
| `competition_index` (0–100) | 同上の指数 | 保存のみ |
| `low_top_of_page_bid_micros` / `high_top_of_page_bid_micros` | ページ上部掲載入札の下限/上限 (micros) | 保存のみ。次 Phase の Commercial Intent Normalizer で使用 |
| `geo_target_id` / `language_id` | 収集時のターゲティング | 再現性のため保存 |
| `normalizer` (`{"name","version"}`) | 使用した normalizer | 数式変更時の追跡用 (Signal にカラム追加はしない) |

SDK の enum / message オブジェクトはそのまま入れず、`int` / `str` / `None` /
`list` / `dict` の **primitive JSON 値へ明示変換**してから保存する。

#### 広告 competition ≠ SEO competition_ease

**Google Ads の `competition` / `competition_index` は広告主間の入札競争度**であり、
Opportunity Score の `competition_ease` (= organic SEO の攻略しやすさ) とは
**別物。自動的に `competition_ease` Signal として登録してはいけない。**
広告 competition は将来 `commercial_intent` の補助 Signal 等に使う可能性はあるが、
`competition_ease` は SERP 分析など別ソースから正規化する。

#### CPC (`*_bid_micros`) の扱い

`low_top_of_page_bid_micros` / `high_top_of_page_bid_micros` は raw_data に保存する
のみ。今フェーズでは `commercial_intent` Signal を自動生成しない。次 Phase で
CPC・広告 competition・クエリ文字列・affiliate 案件等を組み合わせた
Commercial Intent Normalizer を設計する。

#### 設定と起動

Google Ads の credential (`GOOGLE_ADS_*`) は **未設定でもアプリ・既存 API
(`/health`・Keyword/Article/Signal/Score) は起動・動作する**。credential は
collector を実際に呼ぶ (`build_request_params` / `_build_client`) タイミングで
のみ検証し、不足時は `ProviderNotConfiguredError` (HTTP 503 /
`provider_not_configured`) を返す。geo=2392(日本) / language=1005(日本語) は
Settings の default。

### Google Ads commercial_intent collector (Phase 2B-3)

Google Ads historical metrics と **キーワード文字列そのもの** から
`commercial_intent` (0〜100) の Signal を自動生成する。V1 の構成は実際に取得した
30 キーワードの Google Ads データを比較して決めたもの。

純粋計算は `app/keyword/normalizers/commercial_intent.py`
(`classify_query_intent` / `normalize_cpc_score` / `normalize_ad_competition_score` /
`score_commercial_intent` / `calculate_commercial_intent` / `CommercialIntentResult`)。
DB・FastAPI・Google Ads SDK 非依存・決定論的。

```
commercial_intent = query_intent_score*0.60 + cpc_score*0.30 + ad_competition*0.10
```

| サブスコア | 重み | 元データ | 算出 |
| --- | --- | --- | --- |
| Query Intent Score | 0.60 | keyword 文字列 (Google Ads 非依存) | 下表のルール。複数該当は最高 score を採用。非該当は `generic`/40 |
| Low CPC Score | 0.30 | Google Ads `low_top_of_page_bid_micros` | `low_bid(JPY)=micros/1_000_000`、`100*(1-exp(-low_bid/250))`、0〜100、小数第 2 位 |
| Ad Competition Score | 0.10 | Google Ads `competition_index` (0〜100) | そのまま採用 (妥当性検証のみ) |

**Query Intent ルール (V1)**

| 該当語 | type | score |
| --- | --- | --- |
| 料金 / 価格 / 費用 | `price` | 95 |
| 比較 | `compare` | 90 |
| おすすめ / ランキング | `recommend` | 90 |
| 導入 / 法人向け | `b2b` | 85 |
| ツール | `tool` | 65 |
| 無料 | `free` | 45 |
| (非該当の一般 query) | `generic` | 40 |
| 使い方 | `how_to` | 20 |
| とは | `informational` | 10 |

- **250 は JPY calibration 定数** (`CPC_CALIBRATION_JPY`)。V1 は日本市場・JPY の
  Google Ads アカウント前提 (`currency_assumption = "JPY"`)。概ね
  100 円→約 33 / 250 円→約 63 / 400 円→約 80 / 700 円→約 94。
- **`high_top_of_page_bid_micros` は V1 では score に使わない** (実データで外れ値が
  大きく score が不安定になったため)。raw_data には保存する。
- **`competition_index` は広告オークションの競争度**であり、Opportunity Score の
  `competition_ease` (organic SEO の攻略しやすさ) とは別物。**competition_ease へ
  流用しない。** `competition` enum からの補完も V1 では行わない。

#### missing data の扱い (0 点にしない)

Google Ads が一部 keyword で CPC / `competition_index` を返さないことがある。
その場合 **欠測を 0 点で減点せず**、利用できた weight だけで再正規化する:

```
commercial_intent = Σ(value * weight) / Σ(利用できた weight)
```

| 揃った要素 | available_weight (= `evidence_coverage`) |
| --- | --- |
| query のみ | 0.60 |
| query + competition | 0.70 |
| query + CPC | 0.90 |
| query + CPC + competition | 1.00 |

query intent は keyword から常に得られるため available_weight は通常 0.60 以上
(0 になる場合は防御的に `ValueError`)。`market_evidence_available` は CPC または
`competition_index` のどちらかがあれば `true`、両方欠測なら `false`。

#### KeywordSignal への保存

`KeywordMetricsCollectionService.collect_google_ads_commercial_intent(keyword_id)`
が担当 (1 トランザクション / 失敗時 rollback)。Google Ads がその keyword の行を
返さない場合は `ExternalProviderDataError` (search_demand と同方針、502)。

| フィールド | 値 |
| --- | --- |
| `component` | `commercial_intent` |
| `provider` | `google_ads` (keyword-derived intent + Google Ads 市場指標の複合。合成の内訳は raw_data に持つ) |
| `normalized_value` | `commercial_intent` (0〜100) |
| `observed_at` | 収集を実施した UTC 日時 |
| `period_start` / `period_end` | `monthly_search_volumes` の最古〜最新月 (search_demand と同じ) |
| `source_reference` | `google-ads:keyword-plan-idea:historical-metrics` (search_demand と同じ) |
| `raw_data` | `query_intent_type` / `query_intent_score` / `cpc_score` / `ad_competition_score` / `query_intent_weight` / `cpc_weight` / `ad_competition_weight` / `available_weight` / `evidence_coverage` / `market_evidence_available` / `low_top_of_page_bid_micros` / `high_top_of_page_bid_micros` / `competition` / `competition_index` / `geo_target_id` / `language_id` / `normalizer_version` / `currency_assumption` / `normalizer{name,version}` |

API: `POST /api/v1/keywords/{id}/signals/google-ads/commercial-intent` (body なし →
201)。例外マッピングは search_demand と同じ (`ProviderNotConfiguredError`→503 /
`ExternalProviderError`→502 / `ExternalProviderDataError`→502 / Keyword 無し→404)。
新規例外・新規 provider 名は追加しない。`scores/from-signals` は不変更で、
`commercial_intent` の最新 Signal が自然に latest 収集の一部として使われる。

複数 component をまとめて収集する bulk endpoint / scheduler は追加しない
(search_demand と commercial_intent は独立エンドポイント。Google Ads 1 回取得で
両方生成する最適化は将来フェーズ)。

### Google Ads trend collector (Phase 2B-4)

**既存の Google Ads historical metrics の `monthly_search_volumes` だけ** を使い、
`trend` (0〜100) の Signal を自動生成する。**Google Trends API / pytrends は
V1 では使わない。**

純粋計算は `app/keyword/normalizers/trend.py`
(`calculate_trend` / `trend_from_monthly_searches` / `prepare_monthly_series` /
`TrendResult`)。DB・FastAPI・Google Ads SDK 非依存・決定論的。

trend は「検索需要が最近伸びているか / 落ちているか」= **方向と勢いだけ** を表す。

| 値 | 意味 |
| --- | --- |
| 0 | 強い下降傾向 |
| 50 | 横ばい |
| 100 | 強い上昇傾向 |

**検索ボリュームの絶対量は評価しない** (それは `search_demand` の担当)。
`[100,100,100,...]` は検索数の大小に関わらず trend ≈ 50。

```
year, month 昇順にソートした「有効月」の最新 6 か月を使用:
    previous_3   = mean(1〜3 番目)          # 6 か月前〜4 か月前
    recent_3     = mean(4〜6 番目)          # 直近 3 か月
    change_ratio = (recent_3 - previous_3) / max((recent_3 + previous_3) / 2, 1.0)
                   を [-1.0, +1.0] に clamp
    trend_score  = round(clamp0_100(50 + 50 * change_ratio), 2)
```

例: `100→100` = 50 / `100→150` = 70 / `150→100` = 30 / `0→100` = 100 (clamp) /
`100→0` = 0 (clamp) / `0→0` = 50。

**なぜこの式か**: 絶対量は `search_demand` に任せ、trend は方向と勢いだけを見る。
直近 1 か月 vs 前月ではノイズが大きいため 3 か月平均同士で比較する。symmetric
percent change (分母 = 平均、下限 1.0) なので previous_3 が 0 でもゼロ除算せず、
増加と減少を対称に扱える。

#### monthly data の扱い

- **最低 6 か月の有効データが必要**。`monthly_searches` が `None` の月は除外。
  除外後 6 未満なら `ExternalProviderDataError` (502)。
- `monthly_searches` が負値の月があれば `ExternalProviderDataError` 相当
  (provider data error) として扱う。新規例外は追加しない。
- 7 か月以上あれば **最新 6 か月のみ** を trend 計算に使う。
- `period_start` / `period_end` は `search_demand` / `commercial_intent` と同じく
  **利用可能な monthly data 全体の最古〜最新月** (`_period_from_volumes` を再利用)。

#### KeywordSignal への保存

`KeywordMetricsCollectionService.collect_google_ads_trend(keyword_id)` が担当
(1 トランザクション / 失敗時 rollback / `provider = google_ads` /
`source_reference` は Google Ads historical metrics と共通)。

| raw_data キー | 内容 |
| --- | --- |
| `previous_3_average` / `recent_3_average` | 前半 3 / 後半 3 か月平均 (小数第 2 位) |
| `change_ratio` | symmetric 変化率 (-1.0〜1.0) |
| `months_used` | trend 計算に使った月数 (V1 は常に 6) |
| `available_months` | フィルタ後の有効月の総数 |
| `monthly_search_volumes[]` (`year` / `month` / `monthly_searches`) | 計算に使った最新 6 か月 (年月昇順) |
| `geo_target_id` / `language_id` | 収集時ターゲティング |
| `normalizer_version` / `normalizer{name,version}` | 使用 normalizer |

API: `POST /api/v1/keywords/{id}/signals/google-ads/trend` (body なし → 201)。
例外マッピングは search-demand / commercial-intent と同じ
(`ProviderNotConfiguredError`→503 / `ExternalProviderError`→502 /
`ExternalProviderDataError`→502 / Keyword 無し→404)。新規例外・新規 provider 名なし。

`search_demand` / `commercial_intent` / `trend` の各エンドポイントは個別に Google Ads
API を呼ぶ (Phase 2B-4 では許容)。3 component 同時収集や Google Ads call 集約は
後の最適化フェーズ。

### Site Relevance signal (Phase 2B-5)

keyword が現在のサイトテーマ (**AI・生成AI・業務効率化・業務自動化**) にどの程度
関連するかを 0〜100 で評価する。**完全ローカル・決定論的・rule-based。外部 API /
LLM API / Google Ads を一切使わない。DB schema 変更なし。**

純粋計算は `app/keyword/normalizers/site_relevance.py`
(`calculate_site_relevance` / `normalize_keyword` / `SiteRelevanceResult`)。
Google Ads 由来ではないので Metrics Collection Service には置かず、
`KeywordSignalService.derive_site_relevance(keyword_id)` が Signal 化する。

site_relevance が見るのは **「このサイトのテーマとして適切か」だけ**。検索需要
(`search_demand`) / 購買意図 (`commercial_intent`) / 需要の増減 (`trend`) / SEO 競合
(`competition_ease`) / 案件有無 (`affiliate_opportunity`) は評価しない。
**料金・比較・おすすめ・ランキング・無料・使い方・とは・導入 等の commercial intent 語は
加点しない** (`"ChatGPT 料金"` と `"ChatGPT 使い方"` は原則同じ site_relevance)。

#### Site Profile V1 (`ai_business_automation` v1)

関連語を意味グループに分け、group ごとに base score を持つ (vocabulary は
normalizer 内の定数に集約。計算ロジックとは疎結合)。

| topic group | base | 主な語 |
| --- | --- | --- |
| `CORE_THEME` | 80 | AI / 生成AI / 人工知能 / ChatGPT / Claude / Gemini / Copilot / LLM / RPA / 自動化 / 業務自動化 / 自動作成 |
| `RELEVANT_TOOL` | 75 | Notion / Zapier / Make / n8n / Power Automate / UiPath |
| `BUSINESS_PRODUCTIVITY` | 70 | 業務効率化 / 生産性(向上) / バックオフィス / 営業・経理・人事効率化 / DX |
| `ADJACENT_USE_CASE` | 60 | 議事録 / 文字起こし / 要約 / OCR / ワークフロー / ノーコード / チャットボット / 文書作成 / メール自動化 / データ入力 |
| (どれも match しない) | 20 | unknown / general |

- **business context 語** (法人 / 企業 / 業務 / 社内 / 会社 / チーム / 会議 / 営業 /
  経理 / 人事 / バックオフィス / カスタマーサポート / 業界) があれば `+10`。
- **out-of-scope 語** (レシピ / 料理 / 観光 / 旅行 / ゲーム / 占い / 芸能 / スポーツ /
  ダイエット / 恋愛) は、topic group が 1 つも無い場合のみ 0 点判定に使う。

#### V1 formula

```
matched topic group あり:
    base_score             = matched groups の base score の最大値
    multi_group_bonus      = 10 if 異なる matched group >= 2 else 0
    business_context_bonus = 10 if business context 語あり else 0
    site_relevance         = clamp(base_score + multi_group_bonus + business_context_bonus, 0, 100)

matched topic group なし:
    out-of-scope 語あり -> 0   (明確なサイトテーマ外)
    それ以外            -> 20  (unknown / general)

round(., 2)
```

例: `AI 議事録 おすすめ` = 80 + 10 = **90** / `Notion AI 料金` = 80 + 10 = **90** /
`生成AI 法人 導入` = 80 + 10(context) = **90** / `AI 業務効率化` = 80 + 10 + 10 = **100** /
`Zapier 料金` = **75** / `議事録` = **60** / `鶏肉 レシピ` = **0** / `AI 旅行` = **80**
(topic があるので out-of-scope でも 0 にしない)。

#### keyword normalization

`normalize_keyword()` (pure): Unicode **NFKC** → `casefold` → 前後空白除去 →
連続空白の単一化。全角英数字・全角スペース・半角カナの差を吸収する。
`AI` / `Make` / `RPA` 等の ASCII 語は英数字境界で照合し、`maker` の `make` や別英単語中の
`ai` を誤検知しない (日本語を含む語は実質 substring 一致)。

#### Signal への保存

`KeywordSignalService.derive_site_relevance(keyword_id)`
(Keyword 存在確認 → 純粋 normalizer → `KeywordSignal` 作成、Service が commit /
失敗時 rollback)。再実行すると新しい Signal を追記する (immutable history 維持)。

| フィールド | 値 |
| --- | --- |
| `component` | `site_relevance` |
| `provider` | `site_profile` (外部 provider ではなくローカル profile 由来) |
| `source_reference` | `site-profile:ai-business-automation:v1` |
| `observed_at` | 計算時 UTC |
| `period_start` / `period_end` | **`null`** (時系列データではない静的評価) |
| `raw_data` | `base_score` / `matched_groups` / `matched_terms` / `business_context_terms` / `out_of_scope_terms` / `multi_group_bonus` / `business_context_bonus` / `profile_name` / `profile_version` / `normalizer_version` / `normalizer{name,version}` (実際に match した語のみ。全 vocabulary はコピーしない) |

API: `POST /api/v1/keywords/{id}/signals/site-relevance` (body なし → 201)。
Google Ads namespace には入れない。外部 provider を使わないので 502 / 503 は新設せず、
Keyword 無しの `EntityNotFoundError` (404) のみ。

将来的に semantic relevance / Search Console データ / 既存記事 embedding / 複数サイト
profile / DB 管理 profile へ拡張しうるが **V1 では未実装** (YAGNI)。

### Google Trends に備えた方針

- 公式 Google Trends API は限定 Alpha のため、`trend` component の Signal を
  生成する **TrendProvider は将来差し替え可能な構造** とする方針のみ定める
  (非公式ライブラリ → 公式 API への移行を想定)。
- Provider インターフェースそのものを今フェーズで先行実装はしない。
  現状は `provider="google_trends"` の `KeywordSignal` を受け取れれば十分。

## Application 例外 (`app/exceptions.py`)

| 例外 | 用途 |
| --- | --- |
| `ApplicationError` | 基底クラス |
| `EntityNotFoundError` | 指定 ID のエンティティが無い / 存在しない `keyword_id` 指定 / Keyword は在るが `KeywordScore` 履歴・`KeywordSignal` が無い / `KeywordScore` が指定 Keyword に属さない (すべて HTTP 404 / `entity_not_found`) |
| `DuplicateEntityError` | `keyword` 重複、`slug` 重複 (事前チェック + `IntegrityError` 変換) (409 / `duplicate_entity`) |
| `InvalidStatusTransitionError` | 許可されていない status 遷移 (409 / `invalid_status_transition`) |
| `IncompleteSignalSetError` | `from-signals` で 7 component の最新 Signal が揃っていない。`keyword_id` と `missing_components` を保持 (409 / `incomplete_signal_set`) |
| `ProviderNotConfiguredError` | 外部 Provider の credential / 設定が未設定 (運用上の構成エラー)。`provider` を保持。message に credential 値は含めない (**503** / `provider_not_configured`) |
| `ExternalProviderDataError` | 外部 Provider から有効なデータ (指標) が得られなかった。0 点 Signal を作らせないための型 (**502** / `external_provider_data_error`) |
| `ExternalProviderError` | 外部 Provider API 呼び出しの失敗 (通信・認証・SDK 内部エラー)。元例外は `__cause__` にのみ保持し、SDK 詳細・credential は HTTP へ露出しない (**502** / `external_provider_error`) |

Phase 2B-1 で `IncompleteSignalSetError`、Phase 2B-2 で
`ProviderNotConfiguredError` / `ExternalProviderDataError` / `ExternalProviderError`
を追加。credential/config エラー (503) と Provider データ/通信エラー (502) を区別する。
Repository/DB・外部 SDK 由来の低レベル例外は Service / Provider 層で上記へ変換し、
上位・ログへ秘密情報を漏らさない。

## Affiliate Catalog (Phase 2B-6A)

`affiliate_opportunity` component を意味のある実データから採点できるようにするための
**カタログ管理基盤**。今フェーズでは **Signal の採点は実装しない** (normalizer /
`KeywordSignal` 生成は Phase 2B-6B)。

- **カタログの投入源は手動 / ローカル CSV のみ。ASP API 連携・スクレイピングは行わない。**
- `AffiliateProgram` に add-only で 2 列追加 (migration `abfa2f774ff4`):

  | 列 | 型 | 用途 |
  | --- | --- | --- |
  | `match_terms` | `sa.JSON` (nullable) | keyword と案件を関連付けるための明示的な検索語群 (`["議事録", "AI 議事録", "文字起こし", ...]`)。2B-6B のマッチングで使用。URL / secret を入れる用途ではない。更新時は Service が list 全体を assign する (`MutableList` は使わない) |
  | `currency` | `String(3)` (nullable) | `commission_value` の通貨 (ISO 4217、3 文字大文字)。V1 運用は原則 JPY。既存レコード互換のため nullable、DB では JPY 固定にしない |

- 今回追加しない列 (2B-6B で未使用): `is_recurring` / `epc` / `conversion_rate` /
  `approval_rate` / `cookie_days` / `advertiser`、および `keyword_affiliate_programs`
  中間テーブル。将来の migration 候補として残す。
- **層構成は Keyword / Article と同一**: Schema (`app/affiliate/schemas.py`) → Service
  (`AffiliateProgramService`、transaction 所有・commit/rollback) → Repository
  (`AffiliateProgramRepository`、`flush` のみ) → モデル。
- **重複ポリシー**: 同一 `name` + `provider` は `DuplicateEntityError` (409)。
  勝手な upsert で既存案件を上書きしない。CSV importer では重複行を **skip**。
- **`commission_type` は自由文字列のまま** (既存データ互換)。DB enum 化・
  CheckConstraint は追加しない。新規入力では `fixed` / `percentage` を推奨。
- **`tracking_url` の扱い**: model / catalog CRUD API では通常フィールドとして
  read/write 可。ただし将来 `affiliate_opportunity` の `KeywordSignal.raw_data`・
  アプリログ・例外メッセージには **含めない** (affiliate/account ID を埋め込みうるため)。
  CSV importer も 1 行エラー時にセル値・`tracking_url` 全文を出力しない。
- API: `/api/v1/affiliate-programs` の CRUD。`GET` は `status` / `provider` /
  `category` で filter 可能 (keyword→match_terms のマッチング API は 2B-6B)。
- **将来拡張**: EPC / CVR / 承認率 / 継続報酬 (`is_recurring`) 等は列追加後に
  `affiliate_opportunity` formula へ組み込みうるが、V1 では未対応。

## Affiliate Opportunity signal (Phase 2B-6B)

keyword に対して **現在の active Affiliate Catalog にどれだけ収益化案件があり、
どの程度儲かるか (供給側)** を 0〜100 で評価する。**検索者の購買意図
(`commercial_intent`) とは別物。** 外部 API / LLM / ASP API / スクレイピングなし。
完全ローカル・決定論的。DB schema 変更なし。

- 純粋計算: `app/keyword/normalizers/affiliate_opportunity.py`
  (`calculate_affiliate_opportunity` / `AffiliateOpportunityResult`)。
- 照合ルール: `app/keyword/affiliate_matching.py` に集約
  (`normalize_for_match` = `site_relevance.normalize_keyword` を再利用 /
  `term_matches` / `match_programs`)。**分析 CLI
  (`scripts/analyze_affiliate_opportunities.py`) と production が同一 helper を
  import して matching semantics を共有** (CLI の private 関数を production から
  import しない)。
- 導出: `KeywordSignalService.derive_affiliate_opportunity(keyword_id)`。
  active AffiliateProgram を read-only 取得 → `match_programs` → 純粋 normalizer →
  Signal 作成、Service が commit / 失敗時 rollback。**catalog は変更しない。**
  再実行で新 Signal を追記 (immutable history 維持)。時系列でないため
  `period_start` / `period_end` は None。

### V1 formula

| sub-score | weight | 0〜100 正規化 | データ源 |
| --- | --- | --- | --- |
| `program_match_score` | **0.55** | `n == 0 → 0`、`n > 0 → 100*(1 - exp(-n / 4.0))` (4.0 = V1 calibration。案件が増えるほど限界効用が逓減。1→22.12 / 3→52.76 / 7→82.62 / 10→91.79) | matched active program 数 `n` |
| `commission_score` | **0.35** | **percentage commission のみ**: `min(100, pct * 2.5)` (10→25 / 25→62.5 / 40→100)。matched 内の最大値を採用。`commission_type` は strip + casefold で照合。`commission_value < 0` は不正値として score に使わない | active matched の percentage 案件 |
| `provider_spread_score` | **0.10** | `matched == 0 → 0`、それ以外 `min(100, p * 40)` (1→40 / 2→80 / 3+→100)。`p` = 非空 provider の distinct 数 | **弱い補助指標** (`provider="direct"` が複数案件をまとめるため weight は 0.10 のみ) |

```
matched_program_count == 0:
    affiliate_opportunity = 0.0
    market_evidence_available = false

matched > 0:
    present = program_match(always) + provider_spread(matched>0) + commission(valid percentage があるときだけ)
    available_weight = Σ(present の元 weight)      # 全部揃えば 1.00 / commission 欠測なら 0.65
    affiliate_opportunity = round(clamp(Σ(score*weight) / available_weight, 0, 100), 2)
    market_evidence_available = true
```

- **missing commission は 0 点にせず weight 再正規化** (`commercial_intent` V1 と同方式)。
- **fixed commission は V1 score に使わない** — 現 catalog に USD fixed があり、
  JPY / USD を公平に比較する calibration がまだ無いため。**FX 換算はしない。**
  fixed の内訳は `raw_data.fixed_commissions` に provenance として残す。
- `evidence_coverage` は V1 では `available_weight` と同値。

### 0 match の意味 (重要)

0 match は **「市場に affiliate 案件が存在しない」ではない**。
**「現在の active Affiliate Catalog に、この keyword へ直接 match する案件が無い」**
という意味。`ChatGPT` / `RPA` 等が 0 match でも、それを理由に別製品へ
`match_terms` を足さない。catalog coverage の改善は別作業であり、
このスコアリングは catalog completeness を保証しない。

### Signal provenance

`provider = affiliate_catalog` / `source_reference = affiliate-catalog:local:v1` /
`observed_at = 計算時 UTC` / `period_start = period_end = None`。

`raw_data` (JSON。**`tracking_url` / `landing_page_url` / affiliate ID / credential /
ASP account 情報は絶対に含めない**):
`program_match_score` / `commission_score` / `provider_spread_score` /
`program_match_weight` (0.55) / `commission_weight` (0.35) /
`provider_spread_weight` (0.10) / `available_weight` / `evidence_coverage` /
`market_evidence_available` / `matched_program_count` / `matched_program_ids` /
`matched_program_names` / `matched_terms` / `distinct_provider_count` /
`active_providers` / `percentage_commissions[{program_id,name,value}]` /
`fixed_commissions[{program_id,name,value,currency}]` (provenance のみ) /
`catalog_size` / `active_catalog_size` / `normalizer_version` /
`normalizer{name,version}`。

API: `POST /api/v1/keywords/{id}/signals/affiliate-opportunity` (body なし → 201)。
ローカル catalog のため 502 / 503 は追加しない。Keyword 無しの 404 のみ。

## Originality signal (Phase 2B-7)

`originality` は **サイト内部のカニバリゼーション可能性の逆指標**。この keyword で
記事を作ったとき、既存の内部 Keyword / Article と検索意図がどれだけ重複しないか
(= どれだけ新しいコンテンツ機会か) を 0〜100 で表す。

- 高い → 内部重複が少ない / 低い → 既存 Keyword・Article と非常に近い。
- **Google 検索結果上の外部競合 (`competition_ease`) とは別物。** search_demand /
  commercial_intent / site_relevance / affiliate_opportunity とも責務が異なる。
- **外部 API / LLM / embedding / vector DB / 追加 pip dependency なし。** 決定論的。
  semantic similarity (言い換え・表記揺れの吸収) は V2 以降。

### similarity

`app/keyword/text_similarity.py`（pure、標準ライブラリのみ）:

```
similarity = max( character_bigram_dice , difflib.SequenceMatcher(autojunk=False).ratio )
```

- 入力は「NFKC → casefold → 空白正規化」(既存 `site_relevance.normalize_keyword`)
  した文字列から **さらに空白を除去** したもの（`"AI 議事録" → "ai議事録"`）。日本語は
  空白分割されないため文字 bigram を主尺度にする。**token Jaccard / TF-IDF / trigram
  は使わない。**
- bigram は **set 方式**の Sørensen–Dice（`2*|A∩B| / (|A|+|B|)`）。keyword は短く
  同一 bigram の反復が稀なため multiset 重み付けはしない。
- **commercial suffix（料金 / 比較 / おすすめ / 使い方 / 無料 / 導入 / とは …）を
  削除してから比較する処理はしない。** `"ChatGPT 料金"` と `"ChatGPT 使い方"` を
  1.0（完全重複）にしないため。intent の価値評価は `commercial_intent` の責務。
- **intent_adjustment は V1 では実装しない**（`raw_data.intent_adjustment_applied = false`）。

### 比較対象 corpus（status フィルタ）

| 種別 | 含める status | candidate kind | evidence weight |
| --- | --- | --- | --- |
| 既存 Keyword 文字列 | `analyzed` / `selected` / `assigned`（`discovered` / `rejected` 除外） | `keyword` | 1.00 |
| Article の担当 Keyword 文字列（`article.keyword_id` 経由、JOIN） | Article: `approved` / `published` / `rewrite`（`idea`/`planned`/`drafting`/`review`/`archived` 除外） | `article_keyword` | 1.00 |
| Article タイトル | 同上 | `article_title` | **0.80**（keyword 一致より弱い証拠） |

- **`Article.body` 全文は使わない**（過剰・重い・provenance 制約）。
- **current keyword 自身は id で除外**（`Keyword.id != current`）。exact 一致による
  originality 0 の自己汚染を禁止。
- **current keyword に紐づく Article も比較対象から除外**（再スコアリングであって
  新規カニバリではない）。除外したことは `raw_data.self_article_exists` に残す。
- `Keyword.search_intent` は V1 formula に使わない（nullable / 自由文字列 / 品質未保証 /
  `commercial_intent` との責務重複回避）。V2 候補。

### V1 formula

```
eligible candidates == 0:
    originality = 100.0
    corpus_available = false
    evidence_coverage = 0.0
    max_similarity = 0.0
    # 意味: 「現在の対象内部 corpus にカニバリ対象が存在しない」。
    #       「十分な比較データから独創性 100 と証明された」ではない。

eligible candidates > 0:
    for each candidate:
        raw_similarity     = max(char_bigram_dice, SequenceMatcher ratio)   # 0..1
        effective_similarity = clamp(raw_similarity * evidence_weight, 0, 1)
    max_similarity = max(effective_similarity)          # title のみ一致なら上限 0.80
    originality = round(clamp(100 * (1 - max_similarity), 0, 100), 2)
    corpus_available = true
    evidence_coverage = 1.0
```

- Keyword candidate との正規化後完全一致 → `effective = 1.0` → **originality 0.0**。
- Article タイトルのみの完全一致 → `effective = 0.80` → **originality 20.0**（意図的仕様）。
- 同率 max の tie-break: `effective DESC → kind priority (keyword < article_keyword <
  article_title) → id ASC`（決定論的）。

### Signal provenance

`provider = internal_corpus` / `source_reference = internal-corpus:v1` /
`observed_at = 計算時 UTC` / `period_start = period_end = None`。再導出で新 Signal を
追記（immutable history 維持）。

`raw_data`（JSON。**`Article.body` 全文 / `meta_description` / `published_url` /
WordPress URL / credential / 個人情報は保存しない**。most similar は Keyword: id + text、
Article: id + title まで）:
`corpus_available` / `evidence_coverage` / `candidates_count` /
`keyword_candidates_count` / `article_keyword_candidates_count` /
`article_title_candidates_count` / `keyword_total` / `article_total` /
`corpus_size_total`（= keyword_total + article_total、フィルタ前の総件数）/
`max_similarity` / `raw_similarity` / `bigram_dice` / `sequence_matcher` /
`most_similar_kind` / `most_similar_keyword_id` / `most_similar_keyword_text` /
`most_similar_article_id` / `most_similar_article_title` /
`similarity_method` (`"char_bigram_dice|sequencematcher_max"`) / `ngram_size` (2) /
`title_evidence_weight` (0.80) / `status_filter` /
`self_excluded_keyword_id` / `self_article_exists` /
`intent_adjustment_applied` (false) / `normalizer_version` / `normalizer{name,version}`。

API: `POST /api/v1/keywords/{id}/signals/originality`（body なし → 201）。empty corpus
でも `201` + `normalized_value = 100.0` + `corpus_available = false`。Keyword 無しの
404 のみ（ローカル DB 計算のため 502 / 503 なし）。

### performance / migration

- **DB migration なし**（`KeywordSignal` 既存 schema。fingerprint / embedding 列や
  similarity cache は不要）。
- **V1 は Python 全件比較で十分**（keyword は短く、corpus 数千〜1 万件レンジまで
  SELECT が支配的）。アクティブ keyword が概ね 1 万件超 + 高頻度バルク再計算に
  なった段階で precomputed fingerprint / FTS (`pg_trgm` / SQLite FTS5) / 近似最近傍を
  検討。**今回は実装しない。**

## Competition Ease signal (Phase 2B-8)

`competition_ease` は **Google Organic SEO の攻略しやすさ**（100 に近い = 競合が弱い /
0 に近い = 競合が強い）。**外部 SEO API を一切使わず、追加実費 0 円**。ユーザーが
無料ツール等で確認した Organic SEO Keyword Difficulty を manual / CSV で投入し、
システムが検証 → 計算 → Signal 生成 → DB 保存 → Opportunity Score 利用 まで自動化する。

### exact formula

入力は **Organic SEO Keyword Difficulty (0 = easy 〜 100 = hard)** のみ。

```
competition_ease = round(clamp(100 - keyword_difficulty, 0, 100), 2)
```

例: difficulty 0 → 100 / 10 → 90 / 30 → 70 / 50 → 50 / 80 → 20 / 100 → 0 /
32.45 → 67.55。

- **Google Ads の `competition` / `competition_index` は絶対に使わない**
  （広告オークションの競争度であり Organic SEO の Keyword Difficulty ではない）。
  既存 Google Ads provider にも変更なし。
- 別スケール（0-10 / low-medium-high / Google Ads competition index 等）は勝手に
  変換せず受け付けない。`raw_data.difficulty_scale = "0_easy_100_hard"` を必ず保存。

### 純粋 normalizer

`app/keyword/normalizers/competition_ease.py`（`calculate_competition_ease` /
`CompetitionEaseResult`。DB / SQLAlchemy / FastAPI 非依存、外部通信なし）。

validation: `keyword_difficulty` は required / numeric / finite / `0 <= v <= 100`。
**NaN・Infinity・負値・>100・bool は reject**（bool は int のサブクラスだが numeric
として受け付けない）。有効な Difficulty がある場合 `evidence_available = true` /
`evidence_coverage = 1.0`。

### 手動 evidence schema

`app/keyword/schemas.py` の `CompetitionEaseManualCreate`:
`keyword_difficulty: float`（0〜100、bool 拒否）/ `source_name: str`（必須・strip・
空白のみ不可）/ `source_reference: str | None`（任意。**credential / API key /
account ID / tracking parameter を入れない**）/ `observed_at: datetime | None`。

### Service

`KeywordSignalService.derive_competition_ease_manual(keyword_id, payload)`:
Keyword 存在確認 → validation → 純粋 normalizer → raw_data → Signal 作成、
Service が commit / 失敗時 rollback。

- `component = competition_ease` / `provider = manual_keyword_difficulty`。
- `source_reference` = 入力があればその安全な値、無ければ `manual-keyword-difficulty:v1`。
- `observed_at` = 入力があればそれ、無ければ生成時 UTC（**Keyword Difficulty は
  時間で変化しうるため provenance として重要**）。`period_start = period_end = None`。
- 再投入で新 Signal を追記（immutable history 維持）。

### 同一 source 運用

異なる SEO ツールは Keyword Difficulty の算出方法が異なるため、**同じ分析 batch では
原則同じ source を使う**。システムは `source_name` を provenance に保存するが、
**V1 では source 間の補正は実装しない**。→ **異なる source の Difficulty を絶対値として
直接比較することには注意**（同一 keyword でツール A の 40 とツール B の 40 は等価でない）。

### raw_data provenance

`keyword_difficulty` / `competition_ease` / `difficulty_scale` (`"0_easy_100_hard"`) /
`source_name` / `evidence_available` (true) / `evidence_coverage` (1.0) /
`collection_method` (`"manual"`) / `normalizer_version` / `normalizer{name,version}`。

**保存禁止**: credential / API key / password / account ID / **Google Ads competition** /
tracking parameter / 個人情報。

### API / CSV

- `POST /api/v1/keywords/{id}/signals/competition-ease/manual`（body 必須 →
  `CompetitionEaseManualCreate`）。成功 201。Keyword 無し → 404。validation → 422。
  **外部通信しないので 502 / 503 は追加しない**。新規 provider error クラスもなし。
- `scripts/import_competition_ease.py`（`--file` 必須 / `--dry-run` / `--force`）—
  複数 keyword の Difficulty を 0 円でまとめて投入。CSV 列
  `keyword` / `keyword_difficulty` / `source_name`（必須）、`source_reference` /
  `observed_at`（任意）。**Keyword は勝手に作成しない**（既存 `Keyword.keyword` と
  exact lookup、無ければ invalid）。既存 Keyword の status / category / signals は変更しない。
- **idempotency**: 同一 CSV 内の keyword 重複は invalid。最新 competition_ease Signal が
  `provider=manual_keyword_difficulty` かつ `(keyword_difficulty, source_name,
  source_reference)` が今回と一致する場合は default で **skip**（`--force` で同値でも
  新 history を追加）。誤再実行で同じ Signal が大量に増えるのを防ぐ。

### コストポリシー

**V1 は追加実費ゼロを優先。** このフェーズの normalizer / Service / API / CLI / tests
から外部 HTTP request を一切送信しない。DataForSEO / 有料 SEO API / SERP API /
LLM API / embedding API 依存を追加しない。自動有料 API request は存在しない。
有料 provider（Search Console 由来データ / optional paid SEO provider）は
**将来の optional extension** であり `competition_ease` の必須依存ではない。将来
automatic provider を足す場合も、この manual route と同じ `component` /
`raw_data` 契約に載せる。

## Keyword Analysis Workflow (Phase 2C-1)

Phase 2B までに個別実装した 7 component の Signal 生成 / スコアリングを、
**追加実費ゼロ** で実運用できる一連のフローにまとめる。

```
keywords
  → (Phase A) auto 6 signals
        search_demand / commercial_intent / trend   ← Google Ads Historical Metrics を
                                                       全 keyword 分 1 回だけ bulk fetch
        site_relevance / affiliate_opportunity / originality ← local (既存 derive_*)
  → (Phase B) competition_ease Difficulty template を CSV 出力
  → (Phase C) ユーザーが無料ツールで Difficulty を調べて記入し
              scripts/import_competition_ease.py で投入 (別ツール、workflow 外)
  → (Phase D) 7/7 揃った keyword を score_keyword_from_latest_signals で採点
  → (Phase E) Opportunity Score ランキングを CSV 出力
```

- **business logic は `app/services/keyword_analysis_service.py` (`KeywordAnalysisService`)。**
  CLI (`scripts/run_keyword_analysis.py`) は orchestration の入口に留める。
- **各 component の formula / normalizer / `scoring.py` は再実装しない**（既存を呼ぶだけ）。

### Google Ads bulk (1 fetch → 3 signal)

`KeywordMetricsCollectionService.collect_google_ads_signals_bulk(requests)` を追加。
`requests` は `(keyword_id, 作成したい component 集合)` のリスト。

- **全 keyword の text を 1 回の `fetch_historical_metrics(...)` で取得**（keyword ごとに
  3 回、30 keyword で 90 回…のような呼び出しをしない）。必要な keyword が 0 なら
  provider を呼ばない。
- 1 回取得した `GoogleAdsKeywordMetrics` から、既存の `normalize_search_demand` /
  `calculate_commercial_intent` / `calculate_trend` と既存 `_build_*_raw_data` を
  **そのまま再利用**して 3 Signal を導出（計算式コピーなし）。`source_reference` /
  エラー判定（no_metrics / no_avg_monthly_searches / insufficient_monthly_volumes）も
  単体 collector と同一。keyword ごとに commit するので 1 keyword の失敗が他を
  巻き添えにしない。**fetch 自体の失敗（未設定 / 通信エラー）は bulk 全体の失敗**として
  呼び出し側へ伝播する。
- **既存の単体 collector（`collect_google_ads_search_demand` 等）は無変更。**
- **keyword ↔ 応答行の照合 (`_match_metrics`)**: Google Ads は Historical Metrics
  応答で CJK keyword を分かち書きし直して返すことがある（`AI 議事録 おすすめ` →
  `ai 議事 録 おすすめ`）。照合は (1) NFKC + casefold + 連続空白正規化の完全一致 →
  (2) 完全一致しない場合のみ空白を全除去した compact key の一致、の順で行い、
  fuzzy match はしない。同じ key に複数行が該当したら曖昧として割り当てない。
  単一応答 fallback は単体 collector のみで有効（bulk では誤配布を避けるため無効）。

### rerun policy

- immutable history は維持（既存 Signal を書き換え・削除しない）。
- **default**: component の最新 Signal が存在すれば **再利用（skip）**。誤再実行で
  同じ Signal が大量に積み上がるのを防ぐ。GA component が全 keyword で揃っていれば
  Google Ads API も呼ばない。
- **`--refresh`**: 6 auto component をすべて再生成（新しい history 行）。
  `site_relevance` / `affiliate_opportunity` / `originality` は内部 profile / catalog /
  corpus の変更で値が変わるため、更新を取り込む明示手段。
- スコアも同様: default は最新 `KeywordScore` があれば reuse、`--refresh` で再採点。
- cache framework は導入しない（`KeywordSignalRepository.get_latest` の有無判定のみ）。

### readiness / ranking

- `readiness(keyword_id)` は read-only。7 component の latest Signal 有無から
  `present` / `missing` / `complete` を返す。
- ranking CSV 列: `keyword` / `keyword_id` / 7 component の latest 値 /
  `opportunity_score` / `analysis_status`（`complete` / `incomplete`）/
  `missing_components`。**complete かつ採点済みのみ score あり**、incomplete は空欄。
- 並び順（決定論的）: complete（score あり）は `opportunity_score DESC` → `keyword ASC`、
  その後に incomplete を `keyword ASC`。ranking の `opportunity_score` は
  `Keyword.opportunity_score` cache と一致する。

### competition_ease template (Phase B)

`--export-competition-template PATH` は **competition_ease Signal が無い keyword のみ**を、
`scripts/import_competition_ease.py` が読むのと**同一のヘッダ**
（`keyword,keyword_difficulty,source_name,source_reference,observed_at`）で出力する。
`keyword_difficulty` は空欄、`source_name` は `--competition-source` 共通値または空欄。
credential / URL は勝手に埋めない。importer のロジックはコピーしない。

### dry-run / cost safety

- **`--dry-run` は完全な no-side-effect preview**: DB write なし / Signal・Score 作成なし /
  commit なし / **Google Ads API も呼ばない**。何を実行する予定かだけ表示。
- この workflow（normalizer / Service / CLI / tests）から **DataForSEO / 有料 SEO API /
  SERP API / LLM API / embedding API / scraper を一切呼ばず、`requests` / `httpx` も
  import しない**。使うのは既存 Google Ads API（無料機能）/ local DB / affiliate catalog /
  manual competition_ease のみ。**追加実費ゼロ**。
- 1 keyword の失敗で batch を止めない。keyword ごとに success / incomplete / failed を区別。
  secret を例外メッセージ・出力に入れない。

## Article Planning V1 (Phase 3A)

Opportunity Score で選んだ 1 keyword から記事制作へ進むための企画層。**migration なし・
LLM / 外部 API なし・追加実費 0 円。**

### ArticlePlan は導出物 (DB 非永続)

`ArticlePlanService.plan_for_keyword(keyword_id)` は **read-only / 決定論**。
`Keyword` + 最新 7 `KeywordSignal` + `AffiliateProgramRepository.list_active()` +
originality provenance から `ArticlePlanDTO` を毎回生成する。

- 純粋ロジックは `app/article/planning.py` (DB / SQLAlchemy / FastAPI / 外部 API 非依存):
  記事タイプ判定 / working title / slug 案 / 想定読者 / search intent summary /
  primary・secondary goals / outline (`PlanSection`) / 比較軸 / CTA 方針 /
  compliance checklist / quality guardrails / 出典要件 / カニバリ guidance。
- **記事タイプ**は keyword の明示 intent marker から決定論的に判定する。優先順位は
  `how_to (使い方/導入…) > comparison_listicle (比較/違い…) >
  recommendation_roundup (おすすめ/ランキング…) > category_landing (とは/種類…)`。
  marker が無ければ `article_type=None` + warning で human review を要求する
  (unknown で誤魔化さない)。
- **表示用テーマ / タイトル**は `planning.display_text` で正規化する。NFKC のみ
  (casefold しない) で、日本語が絡む token 境界の空白は詰め、ASCII/Latin 英数字
  どうしの境界の空白だけ残す (`業務効率化 ツール おすすめ` → `業務効率化ツールおすすめ`、
  `ChatGPT Plus 料金` → `ChatGPT Plus料金`)。**slug 生成 (`suggest_slug`) は別系統**で、
  token separator の `-` はそのまま (`業務効率化-ツール-おすすめ-roundup`)。
- **比較軸**は catalog で確認できないもの (料金 / 無料プラン / 連携 / AI 機能等) を
  `future_research_required` と明示する。推測値では埋めない。
- **affiliate 候補**は snapshot ではなく live active catalog に対し
  `affiliate_matching.match_programs` を再実行して作る。
- **catalog drift**: `affiliate_opportunity` Signal の `matched_program_ids` (生成時点
  snapshot) と live 結果を比較する。snapshot の有無を `catalog_snapshot_available` で
  区別する:
  - Signal 不在 / `raw_data` が dict でない / `matched_program_ids` キーが list でない
    → `catalog_snapshot_available=false`、`catalog_drift=false` (判定不可)、
    `catalog_snapshot_unavailable` warning。
  - `matched_program_ids` が list (空 `[]` を含む) → `catalog_snapshot_available=true`。
    `[]` は「生成時点で 0 件マッチ」という有効な snapshot として live と比較する
    (`snapshot=[]` かつ `live=[1,2]` → `catalog_drift=true`)。
  - `catalog_drift = catalog_snapshot_available and sorted(snapshot) != sorted(live)`
    (順序は無視)。**snapshot unavailable ≠ catalog drift。**
- **primary affiliate を自動確定しない**。候補は `percentage commission DESC → name
  ASC → id ASC` で決定論的に整列し、commission データ有無で
  `primary_candidate / secondary_candidate / comparison_candidate` に分類するだけ。
  確定は human が承認要求で行う。
- `tracking_url` / credential / ASP account は DTO に一切含めない。

### atomic approval

`ArticlePlanService.approve(keyword_id, ArticlePlanApproveRequest)` は 1 transaction:

1. current plan を再生成、2. validation (下記)、3. `Article` 作成
(`title` / `slug` は request 指定、`body=None`)、4. `idea → planned`、
5. 選択された広告案件を `ArticleAffiliateProgram` として作成、6. primary 設定、7. commit。

途中で失敗したら `rollback` し、Article だけ残る / link が一部だけ残る等の partial
state を作らない。plan DTO 自体は保存しない。

validation (書き込み前に全て実施):

- **incomplete plan (7/7 未満) は既定で拒否** (`plan_approval_rejected` → 409)。
  `acknowledge_incomplete_plan=true` でのみ override 可能
  (記事化の優先判断は Opportunity Score 完成後が原則)。
- **originality < 40** の keyword は `acknowledge_cannibalization=true` が必須。
- 同一 keyword に **非 archived な Article が既にあれば** `duplicate_entity` → 409
  (1 クリック二重実行で Article が 2 件できない)。archived は再企画を妨げない。
- `slug` がグローバルに使用済みなら `duplicate_entity` → 409。既存 Article は上書きしない。
- request の primary / secondary program は **approval 直前に再生成した
  `affiliate_candidates` (= active かつ matched) に含まれるものだけ許可**。
  primary が secondary list にも含まれる / secondary が重複 / 候補外 → `plan_approval_rejected`。

### ArticleAffiliateProgram の primary ルール

- **「1 記事につき `is_primary=True` は最大 1 件」を `ArticleAffiliateProgramService`
  で保証する。** 新しい link を primary にする際は同一 `article_id` の既存 primary を
  `False` に降格してから `True` にする (同一 transaction)。`set_primary` / `update_link`
  も同様。
- **DB の partial unique index (`WHERE is_primary`) は今回追加しない。** 理由は
  SQLite の機能制限ではなく (SQLite 3.8.0+ は partial index をサポートする)、
  (1) V1 で migration を増やさない、(2) single-user / local workflow 前提、
  (3) 1 Article 1 primary は Service 層で保証する、から。V1 では **高並列
  transaction の race は DB レベルでは防げない**ため、multi-worker 化時に
  DB-level の partial unique constraint / index を migration 候補として再検討する。
- 中間モデルの CRUD 自体は program status を問わない (planned 段階での relation 登録は可)。
  active 限定の強制は approval フローの責務。

### affiliate relation ≠ link injection

planned 段階で `ArticleAffiliateProgram` を登録することは許可する。ただし **tracking URL
を `Article.body` へ挿入する actual link injection は Phase 3A では行わない**。
本文 review / approved 後の後続 Phase の責務。

### 責務分離 (将来 LLM を使う場合)

- LLM が決めてよい: 文章表現・見出し文言・要約案・meta_description ドラフト。
- system が固定 (LLM に上書きさせない): keyword / affiliate 候補 / source facts /
  article intent / outline 要件 / compliance / publication approval / slug。
- **Phase 3A は LLM API を呼ばない。** `meta_description` は Phase 3B (drafting) で扱う
  ため Article model / DB schema は変更しない。

## Source & Verified Fact / FactPack (Phase 3B)

planned Article から本文ドラフトへ進む前に、外部世界の事実 (料金・機能・無料プラン等)
を公式ページ由来で構造化保存する。ArticlePlan と違い **事実は永続化する** (時間で変わり、
確認に人手コストがかかるため「毎回再生成できる derived data」ではない)。
**LLM / 外部 API なし・追加実費 0 円。migration は add-only の `article_facts` 1 テーブルのみ。**

### Source (immutable observation)

既存 `sources` テーブルを **無変更で再利用** (`source_type` に `official_product` /
`official_pricing` / `official_docs` / `official_help` / `official_announcement` /
`secondary` の値を入れる。native ENUM は使わない)。

- Source は「公式ページそのもの」ではなく「そのページを *その時点で確認した* 観測記録」。
  同じ URL でも別日時の再確認は新しい Source 行 → historical provenance を壊さない。
- **immutable**: `SourceService` は update / PATCH を提供しない (CREATE / GET / LIST /
  DELETE)。
- **URL safety** (`app/article/source_url_safety.py`): https のみ / userinfo 禁止 /
  credential query (`token` `api_key` `secret` `password` …) は **reject** (除去保存
  しない) / tracking query (`utm_*` `ref` `aff` `partner` `clickid` `gclid` …) は
  除去して canonicalize (fragment も落とす) / 既知 tracking・redirect ホスト
  (`pxf.io` `partnerstack.com` redirect `impact.com` `bit.ly` `track.*` …) と現在の
  `AffiliateProgram.tracking_url` のホストは reject。公式ドメインの最終判断は human。
- **delete guard**: `ArticleFact` から参照されている Source の削除は `entity_in_use`
  (409)。Fact を cascade で消して provenance を壊さない。ただし **Article 削除時は
  ORM cascade (`Article.facts` / `Article.sources` の `delete-orphan`) で Fact / Source
  とも削除される**。

### ArticleFact (immutable fact history)

新規 `article_facts` テーブル。`updated_at` は持たない (immutable)。

- **現在値 = `(article_id, subject_ref, fact_key)` ごとに `checked_at DESC, id DESC`**。
  `is_current` フラグは **持たない** (current flag との二重管理を避ける。KeywordSignal と
  同じ latest semantics)。事実の「更新」は新しい行の append。exact duplicate
  (article, subject, key, checked_at, status, value, source) は skip。
- **17 の persistent fact key** (`app/article/fact_keys.py` の `FactKey`)。
  `pricing_checked_at` / `last_verified_at` は fact key ではなく **FactPack 側で導出**
  (pricing 系 fact の最新 checked_at / verified fact の最大 checked_at)。
- `list[str]` fact は NFKC → trim → 空除外 → 重複除去 (順序保持)。内容は casefold しない。
- **`value_status`** (`verified` / `unknown` / `not_applicable`):
  - `verified`: `fact_value` 非 null / official_* source 必須 / `unknown_reason` は null /
    型が fact key 宣言型 (str / bool / list[str]) と一致。
  - `unknown` (公式を調査したが確認できなかった): `fact_value` null / official_* source
    必須 / `unknown_reason` 必須・非空。
  - `not_applicable`: `fact_value` null / `unknown_reason` (説明) 必須 / source は optional。
  - **missing (行が無い) ≠ unknown。** missing は「未調査」、unknown は「調査済み・確認不能」。
- `pricing_summary` に通貨文字は **要求しない** (「要問い合わせ」「Contact sales」も
  verified として許可。数字を system / LLM で補完しない)。
- `subject_ref`: `affiliate_program_id` 指定時は `AffiliateProgram.name` と正規化一致
  (`affiliate_matching.normalize_for_match`)。`affiliate_program_id` が null なら
  非 affiliate comparison tool を明示的に許可 (nonblank / length のみ検証)。

### freshness policy (`app/article/fact_freshness.py`)

料金系 (`pricing_summary` / `free_plan_available` / `free_trial_available` /
`business_plan_available`) **30 日** / 機能系 **90 日** / 静的
(`official_product_name` / `official_url` / `category` / `target_users` /
`japanese_language_support` / `japan_business_support`) **180 日**。境界は
`now - checked_at <= max_age` を fresh。`now` は Service に inject 可能 (テスト決定論)。
将来 `Settings` 化を検討 (今回は module 定数)。

### FactPack (read-time 導出)

`FactPackService.build(article_id)` は **DB write 禁止**。Source / Fact の最新値と
ArticlePlan から毎回集約する。

- **比較対象 subject 集合 = Article の `ArticleAffiliateProgram` links** に紐づく program
  (V1 固定)。human が比較対象 subset を選ぶ機能 / 非 affiliate tool を正式比較集合に
  含める機能は将来 (別 table)。**known limitation。**
- 各 fact key: verified → `usable_claims` / unknown・not_applicable → `do_not_claim` +
  `missing_facts` (reason: `unknown` / `not_applicable`) / 行なし → `missing_facts`
  (reason: `not_researched`)。not_applicable は比較表で「対象外」表示できるよう status
  を保持。
- **readiness gate**: 各対象 tool で required fact
  (`official_product_name` / `official_url` / `primary_use_cases >=1` /
  `key_features >=2`) が verified、`pricing_summary` / `free_plan_available` が
  verified **または explicit unknown** (official source + reason)、かつ全て fresh なら
  その tool は ok。全 tool ok かつ subject が 1 つ以上あれば `drafting_allowed`。
  recommended fact (target_users / ai_features / integrations …) の不足は `warnings`
  のみで drafting 可。
- LLM 境界: verified 事実だけを「使ってよい事実」として渡し、unknown / missing は
  「言及禁止」。価格は `pricing_summary` 文字列をそのまま引用させ再構成させない
  (Phase 3C)。

### Product model を作らない理由

`Make` (product) と `Make` affiliate program は概念的に別だが、V1 の要件 (1 記事・7 tool)
に対して `Product` model + migration + `AffiliateProgram.product_id` + 既存 catalog の
backfill は過剰。identity は `article_facts.subject_ref` (正準名 str) で持ち、
`affiliate_program_id` nullable で紐付ける。**cross-article 事実再利用 / 1 product 複数
ASP / tool 別名解決が必要になった時点で `Product` を導入し `subject_ref` → `product_id`
へ移行する** (既知の負債)。

### Phase 3C 再現性の負債

Fact が immutable でも、Phase 3C で **どの Fact id を使って draft したか** を保存しないと
draft 再現性はない。加えて ArticlePlan は現在 **非永続** なので、human が承認した
outline / comparison_axes / CTA strategy / cannibalization guidance が再生成時に変わり
得る。Phase 3C 開始前に `DraftInputSnapshot` (approved plan snapshot + used fact ids +
affiliate selection + timestamp) を freeze する設計を行う。**Phase 3B-2 では実装しない。**

## DraftInputSnapshot (Phase 3C-2)

LLM draft を生成する前に「**その draft が何を入力に作られたか (What we knew / decided)**」
を immutable に凍結する artifact。`app/models/draft_input_snapshot.py`。

### 目的と ArticlePlan の歴史的制約

Keyword Signal / ArticlePlan / AffiliateProgram / primary selection / ArticleFact latest /
Source / freshness / claim policy は時間とともに変化する。Snapshot はある時点の入力を
外部 join なしで再現・監査できるようにする。

現行 `ArticlePlan` は **非永続** で、Article #1 は Phase 3A 承認時の Plan を保存して
いない。したがって V1 の Snapshot は「**過去に承認された厳密な Plan**」ではなく
「**freeze 時点で再導出した Plan を human が確認・承認したもの**」。payload に
`plan_snapshot_origin = "current_derived__human_confirmed_at_freeze"` を必ず残す。
将来 Article では drafting 前の Snapshot freeze を標準 workflow にする。

### payload の source of truth

- **draft の title / slug は永続 `Article` が authoritative。** `ArticlePlan` の
  `working_title` / `proposed_slug` (Article #1 では collision 回避で `-roundup-2`) は
  planning 診断であり、`payload.audit.plan` にのみ入れる。`payload.article` には
  実 `Article.title` / `Article.slug` を入れる。
- **authoritative primary は `ArticleAffiliateProgram.is_primary`** (`payload.selection`)。
  `ArticlePlan` / `FactPack` の `recommended_role` (`primary_candidate` 等) は advisory で
  `payload.comparison_set[].planning_role` として別キーで保存する。LLM drafting が
  実 primary として扱うのは `selection` / `is_primary` のみ。
- **commission / provider は Snapshot audit context として保存してよいが、Phase 3C-4 の
  LLM prompt builder では commission を LLM へ渡さない**。human が既に primary を決めて
  おり、推薦文を affiliate economics で bias させないため。

### semantic grid と missing の扱い

比較対象 tool × 17 FactKey を **常に全セル** 表現する (Article #1 なら 7 × 17 = 119)。
各セルの `state` は `verified` / `unknown` / `not_applicable` / `not_researched` の 4 値で、
**fact 行が無いこと (`not_researched`) も drafting 入力**として明示セルにする
(missing を Snapshot から消さない)。`claim_allowed = (state == "verified")`。
各 tool で `usable_claims ∪ do_not_claim = 17 FactKey` / 交わり空、を build 時に assert
する (崩れたら `DraftInputNotReadyError`)。`unknown` は `do_not_claim` のまま保持。

### semantic hash boundary と drift guard

`content_hash` は「保存 payload をそのまま hash」ではなく、
`app/article/draft_input_canonical.py` の `semantic_payload_for_hash()` で **意味的入力
だけ** を取り出してから SHA-256 する。非意味的値 (built_at 等) は payload の
`"audit"` サブツリーに集約し hash から除外する。除外: `audit` / `frozen_at` / row id /
`created_at` / plan の working_title・proposed_slug・slug_available・診断 readiness・
opportunity_score。含める: `builder_version` (builder ロジックが意味的に変わったら
`BUILDER_VERSION` を更新)、article title/slug、keyword、drafting 用 plan フィールド、
comparison_set、selection、119 cell + provenance + claim boundary、参照された Source の
union、policy、freeze に意味を持つ readiness/freshness。datetime は UTC 秒精度
`+00:00` 文字列に正規化し、同一 instant は offset に依らず同一 hash。commission は
`Decimal` 由来の固定桁文字列 (`"35.0000"`)。

`payload.sources` は **present fact が実際に参照した `source_id` の union のみ**
(全 Article Source ではない)。draft 入力に使っていない Source を追加しても hash が
変わらないようにするため。

**freeze の drift guard**: `POST .../draft-input-snapshots` は preview で human が見た
`expected_content_hash` を必須で受け取り、freeze 時にその場で再 build して照合。
不一致なら 409 `SnapshotInputChangedError` で **1 行も作らない** (human が見ていない
入力を凍結しない)。

### immutable / freeze != drafting

`DraftInputSnapshot` は UPDATE / PATCH / DELETE を持たない (内容変更は新しい行の
append、latest = `frozen_at DESC, id DESC`、`is_current` flag なし)。Article 削除時のみ
cascade。**Snapshot freeze は `Article.status` を変更しない**。`planned → drafting` は
Phase 3C-4 の生成開始時に行う。

### DraftGenerationRun との分離

`DraftInputSnapshot` に LLM model / provider / prompt / 生成本文 / token usage /
生成 status を入れない。それらは将来の `DraftGenerationRun` (別 model、`snapshot_id`
を参照) の責務。**What we knew/decided** と **How generation ran** を分離する。

## ArticleMetric のクリック指標 (将来方針)

- 現在の `clicks` は **Search Console のクリック数** として扱う想定。
- 将来の ASP 分析では別カラム `affiliate_clicks` を追加し、指標を分離する:

  ```
  CTR           = clicks / impressions              (検索結果のクリック率)
  Affiliate CVR = conversions / affiliate_clicks    (アフィリエイトの成約率)
  ```

- これらの比率は保存せず、プロパティ / Service で算出する
  (`ArticleMetric.ctr` / `conversion_rate` と同方針)。
- **`affiliate_clicks` の migration は今回追加しない。** 追加は ASP 連携フェーズで行う。
