# 成長の証拠と次の行動の候補 (C9、`growth-analysis/1`、読むだけ)

既存の計測 (SEO C6・収益 C7・計測 C5/C8・Threads T6.5・キーワード) を、記事・キーワードごとの
**成長の証拠** にまとめ、観測できる状況を型に分け、次の行動の **候補** を作る。
**行動はしない** (候補を作るまで)。DB・WordPress・Threads・メール・外の API に書かない・
問い合わせない。ファイルも書かない。

```bash
uv run python scripts/analyze_growth_opportunities.py
uv run python scripts/analyze_growth_opportunities.py --format json
uv run python scripts/analyze_growth_opportunities.py --action-type review_affiliate_placement
uv run python scripts/analyze_growth_opportunities.py --article-id 10 --format json
uv run python scripts/analyze_growth_opportunities.py --evidence-state hypothesis --monetized-only
uv run python scripts/analyze_growth_opportunities.py --min-age-days 30 --keyword-id 12
```

出力: 1 Summary / 2 Evidence (記事ごとの出所の状態) / 3 Action candidates / 4 Data quality。

## 構成

| 層 | 役目 |
|---|---|
| `GrowthEvidenceService` (`app/services/growth_evidence_service.py`) | 既存のサービスから証拠を集める (`read_only_session`、最後に rollback) |
| `app/growth/analysis.py` (pure) | 出所ごとの成熟度・全体の段階・型・行動・優先の成分・指紋 |
| `GrowthOpportunityService` (`app/services/growth_opportunity_service.py`) | 証拠 → 候補・要約・警告 (`evaluate_growth_opportunities(now)`) |
| `scripts/analyze_growth_opportunities.py` | 表・JSON・絞り込み (標準出力だけ) |

集計は作り直さない:

| 出所 | 使うもの |
|---|---|
| 鮮度 | `collect_source_freshness` + `evaluate_source_refresh` (網羅範囲で判定。活動の日付では判定しない) |
| SEO / GA4 と SEO の候補 | `SeoImprovementCandidateService.evaluate` (C6 の成熟度・閾値のまま) |
| クリック・収益の候補 | `RevenueOptimizationCandidateService.evaluate` (C7、`AffiliateCleanClickService`) |
| データの質・プログラム単位の報酬 | `ArticleMeasurementReportService.build` |
| 索引 | 保存済みの `operations_step_runs[check_indexability]` (live の検査・URL Inspection はしない) |
| Threads | `ThreadsPerformanceAnalysisService.report` (T6.5。通常の投稿だけを記事に結ぶ) |
| キーワード | `KeywordScoreRepository.get_latest` (スコアの年齢は `created_at`) |
| Growth の枠 | `ThreadsGrowthService.plan` (読むだけ) |

## 証拠の形

`GrowthEvidence(subject_type, subject_id, article_id, keyword_id, article, sources, existing_candidates)`。
`sources` は `seo / ga4 / affiliate / commission / keyword / threads / index` の
`SourceEvidence(source, state, reason, provenance, data_through, coverage_through, observed_at,
metrics, maturity)`。欠測は `None` (0 にしない)。

- `article`: 状態・公開日・経過日数・最後の内容の更新 (`wordpress_content_update_runs` の成功)・
  種類・収益化の形・収益化済みか・アフィリエイトのリンクの有無。
- `affiliate.metrics`: `clean_clicks` (信頼できる計測開始より後)、`raw_clicks`、
  `excluded_instrumentation_clicks`、`trusted_measurement_start_at`。
- `commission.metrics`: `attribution_scope` と `article_level_revenue: null` (記事の収益にしない)。
  プログラム単位の報酬はサイトの側 (`program_commissions`) にそのまま置く。

## 成熟度

出所ごとの状態 (`unavailable` / `stale` / `insufficient` / `usable`) は **各出所の既存の規則**:

- SEO: C6 の `sufficient_search_sample` だけが `usable`。取り込みが古ければ `stale`。
- GA4: 未設定・未取り込みは `unavailable`。C6 の `sufficient_engagement_sample` だけが `usable`。
- アフィリエイト: C7 の `measurable` だけが `usable`。追跡が無ければ `unavailable`。
- Threads: T6.5 の評価で `insufficient_data` より強い投稿があれば `usable` (段階はそのまま)。
- キーワード: 最新のスコアが 60 日より古ければ `stale`。
- 索引: `GSC_UNKNOWN` (URL Inspection なし) は `insufficient`。

全体の段階 (読者の行動の出所 SEO・GA4・アフィリエイト・Threads のうち `usable` の数):
0 → `insufficient_data` / 1 → `hypothesis` / 2 → `preliminary` / 3 以上 → `descriptive`。
構造だけから分かる候補は `structural`。Threads の本数の閾値をほかの出所に流用しない。

## 型 (因果を言わない)

`search_visibility_opportunity` / `content_refresh_candidate` / `monetization_opportunity` /
`affiliate_interest_signal` / `threads_repromotion_candidate` /
`threads_alternative_angle_candidate` / `new_content_opportunity` / `insufficient_evidence`
(+ サイトの `data_quality_issue`・`growth_lane_due`)。

- C6/C7 の候補はそのまま型と行動に写す (閾値を作り直さない)。
- `affiliate_interest_signal`: 信頼できるクリックが 1 件以上。
- `threads_repromotion_candidate`: 公開済みで、通常の投稿が無い、または最後の投稿から 14 日以上。
- `threads_alternative_angle_candidate`: 少なくとも 1 本が 24h を過ぎ、まだ試していない切り口が
  ある。この記事の投稿が同じ経過時間の中で下半分のときだけ行動の証拠、それ以外は構造の話。
- `new_content_opportunity`: スコアのある記事の無いキーワード (C6 の将来の記事の候補も)。
- `insufficient_evidence`: 読者の行動の出所が 1 つも使えない → `wait_for_more_data`。
- 公開前の記事には候補を出さない (証拠の行だけ)。

## 行動の候補

`GrowthActionCandidate`: `action_type`・対象の ID・`patterns`・`evidence` (元のエンジンと理由)・
`rationale` (「試す価値のある候補で、効果は保証しない」)・`evidence_state`・`prerequisites`・
`blockers` (開いている提案・古いデータ・追跡の設定は人の作業)・`freshness`・`effort`・
`reversible`・`requires_human_approval`・`external_write_required` (wordpress / threads)。

優先の成分 (別々に持つ・1 つの点数にしない):

| 成分 | 決め方 |
|---|---|
| `evidence_strength` | 段階そのもの (`insufficient_data < structural < hypothesis < preliminary < descriptive`) |
| `potential_opportunity` | 元のエンジンの優先度。キーワードは記事の無いキーワードの中の相対の位置 |
| `recency_urgency` | 古いデータ・データの質 > 鮮度 > 公開 30 日以内の記事の紹介 |
| `effort` | 行動の種類 (記事の新規は high、見出し・リンク・配置は low) |
| `monetization_relevance` | 収益化済みで信頼できるクリックあり > アフィリエイトの記事 > 補助の記事 |

並べ方: 証拠の強さ → 機会 → 収益との関係 → 手間 (少ない方) → 急ぎ。キーワードの機会スコアを
総合点に使わない。

## worker との境界

`GrowthOpportunityService.evaluate_growth_opportunities(now)` が `fingerprint` (候補の正規の JSON
の sha256) と `next_evaluation_at` (24 時間後。取り込みは 1 日 1 回) を返す。**このバッチでは
worker に登録しない** (heartbeat ごとに評価しない設計)。候補を自動で実行しない。

## 限界

- 記述だけ。観測された状況と、試す価値のある行動の候補 (原因・効果は言わない)。
- クリックは `/go/` の記録 (UA・IP は持たない)。除外は信頼できる計測開始の時刻だけ。
- 報酬は Make からプログラム単位でしか届かない (記事への帰属には追跡の設定の変更が要る。
  このバッチではしない)。
