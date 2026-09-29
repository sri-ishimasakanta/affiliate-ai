# C10-2 Content Intelligence + Nightly Discovery + Next Article Orchestrator

C10-B (Content Intelligence)・C10-C (Nightly Analysis)・C10-D (Next Article Orchestrator) を 1 つに
まとめた単位。**どれも手元と保存済みのデータだけを読む** (Google Ads・WordPress・Threads・
OpenAI・メールに触れない)。Keyword・記事・記事の計画の依頼・Growth Action を **作らない**。
合成の点数は作らない (成分と理由を別々に持つ)。

```bash
uv run python scripts/run_nightly_analysis.py                              # PLAN の要約
uv run python scripts/run_nightly_analysis.py --section clusters|gaps|discovery|facts|refresh
uv run python scripts/run_nightly_analysis.py --section schedule           # タスクの定義 (登録しない)
uv run python scripts/run_nightly_analysis.py --section history
uv run python scripts/run_nightly_analysis.py --execute                    # 手元の 2 表だけ (migration 後)
uv run python scripts/plan_next_articles.py [--format json]                # 次の記事の計画 (読むだけ)
```

## search_demand の欠測 (C10-A の残り)

原因: Google Ads は推定の無い項目を proto3 の既定値 0 で返す。平均 0・月ごとの履歴が空でも
`search_demand = 0.0` が保存されていた (本番: keyword 28「生成AI 法人 導入」・29「AI 業務効率化
導入」)。V2 (`app/keyword/normalizers/search_demand.py`):

| 状態 | 条件 | 値 |
|---|---|---|
| observed | 平均 > 0 (または月の観測に > 0 がある) | V1 の式 |
| observed_zero | 平均 0 で、6 か月以上の月の履歴が全部 0 | 0.0 (本当の 0) |
| insufficient | 平均 0 で、月の履歴が 6 か月未満 | 作らない |
| missing | 平均が無い、または平均 0 + 履歴無し | 作らない |

これからの取り込みは欠測の signal を作らない (`skipped["search_demand"] =
"no_search_volume_evidence"`)。**保存済みの V1 の 0.0 は消さない** (履歴を壊さない) が、読む側
(`is_missing_search_demand`) で欠測として扱う: score は作らない (`IncompleteSignalSetError`)、
分析の証拠では `missing` (`stored_zero_is_missing`)。**本番への書き込みは不要** (0 行)。
2 つの keyword は score が無い (competition_ease・trend も無い) ので、既存の score への影響は無い。

## Topic Cluster (`app/content/clusters.py`、`topic-cluster/1`)

- 登録簿: `content_clusters.json` (A〜D、E は保留) と `content_portfolio.json` (計画・予備・既存の記事、
  E を含む) を 1 つにまとめる。E は `config_status=deferred` のまま、portfolio の記事を持つ。
- 識別: `cluster:<ID>` (設定の ID だけ。順位・signal の小さな動きで変わらない)。
  `identity_fingerprint` と、今の証拠の `evidence_fingerprint` を分ける。
- 所属: 設定の語 (正規化して一致) → 無ければ主題 (`intent_profile` の theme token) の重なりが
  ただ 1 つのクラスタで最大のとき。重ならない・同点は所属しない。
- 持つもの: 所属の語・記事 (役割 pillar / supporting、記事の役割)・商用 / 情報の内訳・役割の広がり・
  `coverage_state` (uncovered / partial / covered)・出どころ・時刻・版。

本番 (2026-09-30): 5 クラスタ。記事 25 件がすべていずれかに所属 (A 7・B 4・C 6・D 5・E 3)。
A・B・C covered、D・E partial。

## Content Gap (`app/content/gaps.py`、`content-gap/1`)

根拠を出せるものだけ: `no_article`、`missing_pillar`、`missing_<role>` (その役割になる語 —
keyword・計画・予備・発見の候補 — がクラスタにあり、同じ役割の公開の記事が無いとき。記事が
少ないだけでは作らない)、`weak_internal_linking` (最新の C6 の内部リンクの候補)、
`outdated_article` (事実が古い)、`search_index_gap` (URL Inspection で索引されていない系、古く
ない調べ)、`monetization_gap` (商用の役割の記事が supporting で、対象のプログラムがある)。
各 gap: クラスタ・根拠・今の広がり・求める役割・理由・鮮度・止める理由。

本番: 20 件 (weak_internal_linking 5、missing_implementation 3、missing_how_to 2、
missing_comparison 2、missing_category_landing 2、missing_pricing 1、missing_informational 1、
missing_pillar 1 (D の pillar「法人向け 生成AI」)、outdated_article 1、search_index_gap 1 (記事 11、
discovered_not_indexed)、monetization_gap 1)。

## Continuous Keyword Discovery (`app/content/discovery.py`、`keyword-discovery/1`)

出所: 検索クエリ (保存済みの Search Console の行)・portfolio の計画 / 予備・クラスタの設定の語・
人が書いた種 (`app/config/discovery_seeds.json`)。新しい外の提供元は足さない。
候補は **Keyword にしない**。同じ語 (正規化: NFKC・casefold・空白を除く) は 1 つ。既にある
Keyword と同じ語は `existing_keyword`、記事と同じ語・同じ意図 (`compare_profiles`) は
`existing_article` / `overlaps_article` (前の絞り込みで除く。見えるように残す)。質問文は除く。
Google Ads の値が無い候補は `refresh_needs = ["google_ads"]`。

保存 (`content_discovery_candidates`、migration `c1d0e233e180`): 語の鍵で一意、最初と最後に
見た時刻・見た回数 (日が変わったときだけ増える)・出所・根拠・クラスタ・重なりの状態・取り直しの
要る出所。状態: tracked / suppressed / promoted (後で Keyword になった) / dismissed (人が外した。
戻さない)。

本番 (PLAN): 観測 68 語 → 新しい候補 26、既にある keyword 36、記事と重なる 6。

## Content Type Strategy (`app/content/content_types.py`)

8 つの役割: comparison / roundup / how_to / practical_workflow / implementation / pricing /
informational / category_landing。practical_workflow と implementation は既存の `how_to` の
テンプレートを使う (新しい型は作らない)。選び方: 語の印 (既存の `classify_article_type` +
導入・構築・連携・移行 → implementation、活用・ワークフロー → practical_workflow) → 無ければ、
head term でクラスタに landing が無い → category_landing → commercial_intent ≥ 60 かつ対象の
プログラムあり → roundup → commercial_intent が無い → undetermined (人が見る) → informational。
理由を残す。クラスタに同じ役割の記事があれば理由に書く (重なりは次の記事で確かめる)。

## 再利用できる SaaS の事実 (`app/content/facts.py`、`saas-facts/1`)

既存の記事ごとの事実 (`article_facts` 535 行・対象 21) を **対象ごと** の見方にまとめる (新しい表は
作らない)。鍵ごとに最新の値・どの記事のどの出典か・鮮度 (料金 30 日・機能 90 日・静的 180 日)・
記事ごとの値の食い違い。`unknown` は値にしない。古い事実は `stale` (最新として使わない)。
準備度: ready / stale / partial / missing。外から集める仕組みは作らない (`fact_research_plan` に
並べるだけ)。本番: ready 10、partial 9、stale 2。

## Nightly Analysis (`app/services/nightly_analysis_service.py`)

発見の全体 → 前の絞り込み (重なり・記事のある語を除く) → 約 80 の分析の候補 (予算。埋めない) →
手元・保存済みの証拠 (C10-A の成分) → 取り直しの要るものの分類 → Content Intelligence → 重なりの
除外 → 次の記事の候補 → 既存の Growth Action (create_new_article) と同じ識別 → C9-A の選び方・
まとめ。人に 80 件を見せない (Growth の受け箱・まとめは今までどおり少数)。

- 選び方 (説明できる順、1 つの点数は作らない): gap を埋める既存の keyword → 既存の keyword →
  gap を埋める発見の語 → 検索の表示がある発見の語 → その他。同じ段では設定の優先度 → 語の鍵。
  予算を超えたものは「予算の外」として理由つきで後回し。
- 手に入れ方の段: A 手元 (site_relevance など) / B 保存済みの外 (Google Ads・GSC・URL Inspection)
  / C 取り直しが要る。C は提供元ごとの計画 (`refresh_plan`) にまとめる。Google Ads は一括の
  呼び出し 1 回あたり 1,000 件。**夜の分析は呼ばない** (人が承認して別に実行する)。
- 記録 (`nightly_analysis_runs`): `run_key = nightly:<日付 (Asia/Tokyo)>`、状態・数・取り直しの
  計画・次の記事の要約・失敗の理由・やり直しの回数。同じ日の 2 回目は前の結果を返す。失敗・
  止まった (6 時間) 実行はやり直せる。
- 定期の実行: 毎日 03:30 JST の専用のタスク (`affiliate-ai-nightly-analysis`、
  `scripts/run_nightly_analysis_task.cmd`)。Threads の常駐 worker とは別。**登録は人の判断**
  (`--section schedule` がコマンドを出す。コードは登録しない)。

本番 (PLAN、2026-09-30): 全体 85 → 前の絞り込み 42 → 対象 43 → 分析 43 (予算 80 に届かない。
埋めない) → 次の記事: Growth のレビュー待ち 11、signal が足りない 4、Keyword への昇格が要る 26、
重なりで止める 2。取り直し: Google Ads 38 語 (保存済みの値が無い keyword 12 + 発見の語 26) を
一括の呼び出し 1 回 — **PENDING HUMAN**。

## Next Article Orchestrator (`app/content/next_article.py`)

候補ごと: 話題・クラスタ・記事の役割 (種類)・クラスタの中の役割 (pillar / supporting)・埋める
gap・収益の役割 (affiliate / supporting)・理由・証拠 (成分ごと)・止める理由・鮮度・重なり・外の
取り直し・引き渡しの準備度。**記事は作らない。**

重なり (1 つでも当たれば `blocked`): 同じ正規化した語の記事 / 計画中の記事 / 開いている記事の
計画の依頼、既存の意図の重なりの規則 (`compare_profiles`)、同じクラスタで同じ役割 (landing /
roundup / comparison) の記事が同じ主題。本番: 「業務効率化 ツール 比較」(記事 1 と同じ選ぶ意図)、
「生成AI ツール 比較」(記事 19) を止める。

引き渡し: `ready_for_growth_review` (既存の Growth Action create_new_article と同じ識別
`create_new_article:keyword:keyword:<id>`。人がレビュー → 承認 → C9-B の記事の計画の依頼 → 既存の
記事の計画の承認 → 記事) / `needs_signals` (competition_ease などが足りない) /
`needs_keyword_promotion` (発見の語: 人が Keyword にし、Google Ads の一括の取り直しを承認) /
`blocked`。夜の分析と Growth Action で同じ候補を別々に管理しない。

## C6 と索引の状態

C6 (`SeoImprovementCandidateService.evaluate(indexability=...)`) に、保存済みの索引の状態
(`IndexStateService.as_c6_indexability`、URL Inspection をした最新の確認) を渡す (運用の SEO の
段・C9 の証拠・変換の中の C6)。外に問い合わせない。調べていない・古い (8 日) 記事は `GSC_UNKNOWN`
(分からないと言う)。C6 の `inspection_max_age_days` を 7 → 8 にして、source policy と揃えた (週ごとの
確認が少し遅れても消えて戻る揺れを作らない)。本番で確かめた: Growth の候補の新しい版は 0
(再観測 68)。記事 11 (discovered_not_indexed) は公開から 14 日 (2026-10-06) を過ぎると
`INDEXING_FOLLOWUP` (情報、`investigate_data_quality`) になりうる。

## C10-D の適用の経路 (DEFERRED)

- 本文の書き換え (text_edit): WordPress へは既存の本文の更新 (`{"content": ...}`) と同じ書き込みの
  形で済むが、適用の段 (`ChangeApplicationService._check_local_gates`) は内部リンク 1 本の挿入だけを
  通す。本文の書き換えの提案・古さの判定・確認の画面も要る。
- メタディスクリプション (抜粋): 既存の投稿の抜粋を更新する経路が無い (更新の payload は本文だけ)。
  **新しい WordPress の書き込みの形** になる (人の判断の対象)。
- どちらも C10-2 の本体 (読むだけの分析と計画) を止めないように、C10-E へ残した (理由は roadmap)。

## 疑わしいクリックの束 (C10-A の印)

同じ秒に並ぶクリック 13 件は、印を付けるだけのまま (除かない)。計測の試験の印・計測の出所・
決定的な除く規則は、ソースからも保存済みのデータからも証明できない (クリックの行は token と時刻
だけ) ので、C11 へ残す。
