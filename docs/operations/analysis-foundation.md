# C10-A Analysis Foundation

C10-B (Content Intelligence) と C10-C (Nightly Analysis、1 日 1 回・約 80 候補、厳密な件数では
ない) が使う分析の土台。**分析は外に問い合わせない** (保存済みのデータだけを読む)。外のデータが
古い・無いときは「外の取り込みが要る」と印を付け、提供元ごとにまとめて計画する。

```bash
uv run python scripts/analyze_signal_health.py                         # 出所の一覧 (読むだけ)
uv run python scripts/analyze_signal_health.py --section all [--format json]
uv run python scripts/rederive_commercial_intent.py                    # PLAN (書かない)
uv run python scripts/rederive_commercial_intent.py --execute --expect-rederive N --expect-rescore M
```

## 1. commercial_intent (Google Ads)

既存の決定論的な signal (Phase 2B-3、`app/keyword/normalizers/commercial_intent.py`) を V2 に
した。重み (query intent 0.60 / CPC 0.30 / 広告の競争 0.10) と JPY の calibration
(`CPC_CALIBRATION_JPY=250`) は V1 のまま。新しい表も新しい component も作らない
(`keyword_signals` の component `commercial_intent`)。

| 項目 | V2 の規則 |
|---|---|
| 入札の証拠 | `low_top_of_page_bid_micros` (high は記録と整合の確認だけ) |
| 入札 0 | Google Ads は推定の無い項目を proto3 の既定値 0 で返す → **欠測** (0 点で減点しない) |
| 広告の競争 | `competition_index`。competition が `UNSPECIFIED` / `UNKNOWN` / 無しなら既定値 → **欠測**。LOW / MEDIUM / HIGH なら 0 でも実際の値 |
| 外れ値 | 入札が 20,000 JPY を超えたら上限で打ち切る (`bid_outlier_capped`)。high < low は `bid_range_inverted` の印だけ |
| 欠測 | 使えた重みだけで割り直す (V1 と同じ)。query intent は keyword から常に得られる。`market_evidence_state` = available / partial / missing |
| 範囲 | 0〜100 (丸めは小数第 2 位) |
| 検索量 | **入力にしない** (検索量が多いだけで上がらない) |
| SEO の難しさ | 広告の競争は organic の難しさではない (`competition_ease` は人の入力の KD だけ) |
| 記録 | `raw_data`: サブスコア・重み・`evidence_coverage`・`market_evidence_state`・`quality_flags`・生の Google Ads の値・`normalizer {name, version: v2}` |

導き直し (`CommercialIntentRederiveService`): 最新の commercial_intent の `raw_data` にある
保存済みの値だけを使う (Google Ads を呼ばない)。行は追記だけ (元の観測の時刻のまま、
`rederived_from_signal_id`)。値の変わった既存 score だけを付け直す (7 成分がそろうもの)。
期待値が PLAN と違えば何も書かない。2 回目は `already_current`。

本番 (2026-09-30): 保存済みの値がある 30 keyword を V2 にした (追記 30 行、付け直し 0。変わった
値は 3 件: 業務効率化 ツール 比較 54.0→90.0、生成AI 法人 導入 51.0→85.0、AI 業務効率化 導入
51.0→85.0。どれも入札 0・competition UNSPECIFIED)。12 keyword は保存済みの値が無い (取り込みが
要る: 1 回の一括の呼び出しで足りる。人が `run_keyword_analysis.py` で行う)。backup:
`D:/Backups/affiliate-ai/affiliate_ai.pre-c10a-ci-backfill.20260929T161709Z.db`。

## 2. URL / index state

URL Inspection は既に本番で使っている (`app/search_console/url_inspection_client.py`、週ごとの
運用の `check_indexability`、`inspect_on_weekly: true`、既存の read-only scope)。**新しい呼び出しの
形は足していない。**

- 正規化した状態 (`app/seo/index_state.py`): indexed / crawled_not_indexed /
  discovered_not_indexed / not_indexed (unknown to Google) / excluded / blocked / error / unknown。
  Google の生の値 (`verdict`・`coverageState`・`robotsTxtState`・`indexingState`・
  `pageFetchState`・`lastCrawlTime`) は `raw_status` に別に持つ。**unknown は「索引されていない」
  ではない。**
- 週ごとの確認は、生の値も `operations_step_runs.result_json` に残す (URL を含む値・他のサイトの
  参照は残さない)。
- 読み方 (`IndexStateService`): 記事ごとに **URL Inspection をした最新の確認**。C9 までは最新の
  実行 (日ごとの確認は `GSC_UNKNOWN` だけ) を読んでいたので、ほとんどの日に index が
  `insufficient` になり、週ごとに usable ↔ insufficient を行き来していた (直した)。
  調べてから 8 日 (`source_policy.json` の `url_inspection.stale_after_days`) を過ぎたら `stale`。
- C9 の証拠の `index` の出所もこれを使う。**本番で 1 回だけ、index を指紋に含む
  `review_internal_links` の 22 候補が新しい版になる** (状態が正しく分かったため。レビュー・通知は
  0 件だったので人の側の影響は無い。以後は行き来しない)。

## 3. 出所の状態の共通の形

`app/analysis/sources.py` (`SourceStatus`) と `app/services/source_health_service.py`。

| 状態 | 意味 |
|---|---|
| fresh | 届いていて新しい |
| waiting | 窓・遅れの目安の中でまだ届いていない (C9-C の観測) |
| stale | 古い (古い値を最新として使わない) |
| missing | 設定が無い・一度も無い (0 ではない) |
| insufficient | 取り込めたが行がまだ無い |
| provider_error | 最新の取り込みが失敗した |

使えるのは fresh と waiting だけ。保存済みの外のデータ (`access=cached`) が stale / missing /
provider_error なら `needs_external_refresh` (分析はそれを呼ばない)。C9 の証拠の鮮度
(fresh / stale / unavailable) は `legacy_state` として同じ判定のまま (運用の
`evaluate_source_refresh`)。C9 の証拠と C9-C の観測は、この 1 か所を使う。

| 出所 | 遅れの目安 | 古さの境 | 数字の置き場所 |
|---|---|---|---|
| search_console | 3 日 | 取り込み 48h / 網羅 3 日 + 遅れ | `operations_policy.json` imports |
| ga4 | 2 日 | 取り込み 48h / 網羅 3 日 + 遅れ | `operations_policy.json` imports |
| affiliate_clicks | 1 日 (取り込んだ日の前の日まで) | 取り込み 48h | imports + `source_policy.json` |
| make_commissions | — | 取り込み 72h / 網羅 3 日 | `operations_policy.json` imports |
| google_ads | 0 | 45 日 | `source_policy.json` |
| threads_insights | 0 | 48h | `source_policy.json` |
| url_inspection | 0 | 8 日 (週ごと + 1 日) | `source_policy.json` |

## 4. 帰属の準備度 (C11 のため)

`app/revenue/attribution_readiness.py` と `AttributionReadinessService`。**帰属はしない**
(比例配分・最後のクリックの仮定・収益の分割をしない。鍵が無ければ記事に配らない)。

| データ | 段階 | 結ぶ鍵 |
|---|---|---|
| 信頼できる計測開始より後のクリック | A_direct | click.token → link target → article / program |
| リンク先の無いクリック | D_unattributed | 無し (合成の試験など) |
| 計測開始より前のクリック | 除く | 計測の試験の期間 |
| Make の成果 | B_provider | 提供元だけ (取り込みはプログラムで絞らない。クリックの参照は保存しない) |
| 取り込みの無い提供元の成果 | D_unattributed | データ無し |
| 成果 → 記事 | D_unattributed | 鍵が無い (SubID / clickref / 注文の鍵が無い) |

本番 (2026-09-30): 19 プログラムのうち追跡の URL があるのは 1 (Make)。クリック 50 (計測開始より
後 21、すべてリンク先あり)。**そのうち 13 は同じ秒に並ぶ束** (計測の試験らしい。印を付けるだけで
除かない。除くかは人の判断)。成果の行は 0 (取り込み 15 回、行 0)。決定的に記事まで結べる成果の
プログラムは 0。

C11 に要る変更 (いずれも人の判断。C10-A では変えない): ASP が成果に戻す 1 クリックごとの参照
(SubID / clickref) を選ぶ (ASP の規約の確認が先)、`/go/` か追跡の URL に参照を足す (公開の
href の契約が変わる)、クリックに参照を保存し成果の取り込みで読む、成果の取り込みをプログラムで
絞る (いまは最初に走ったプログラムに全部付く)。過去の成果は記事に戻せない。

## 5. 分析の証拠の共通の形 (C10-B / C10-C)

`app/analysis/evidence_contract.py` と `AnalysisEvidenceService`。候補ごとに成分を別々に持つ
(`composite_score` は常に `None`):

- keyword: search_demand / commercial_intent / trend (`google_ads`, cached)、site_relevance /
  originality / affiliate_opportunity (local)、competition_ease (manual)。
- 記事: index_state (url_inspection, cached)、attribution_readiness (clicks)。

成分ごとに state (usable / stale / missing / insufficient)・値・提供元・観測の時刻・版・印・
`needs_external_refresh`。`refresh_plan` は外の取り直しを **提供元ごとにまとめる** (Google Ads は
1 回の一括の呼び出し)。夜の分析が 80 候補 × 全提供元を呼ぶ形にしない。

## 残り (後の段階)

- 12 keyword の Google Ads の値の取り込み (既存の一括の呼び出し。人が実行)。
- 成果の取り込みのプログラムの付け方 (C11)。
- C6 の評価に索引の状態を渡す (`INDEXING_FOLLOWUP` は運用の流れから出ない。C10-B/C で検討)。
- search_demand も、平均 0・月の履歴無しを 0.0 として保存している (欠測の扱いは C10-B で。
  今回は search_demand を変えない)。
