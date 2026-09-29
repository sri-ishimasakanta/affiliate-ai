# Threads の成績の分析と生成への補助の参考 (T6.5、読むだけ)

保存済みの観測 (`threads_insight_snapshots`、追記だけの履歴) から、投稿ごとの成績・比べる相手
(cohort)・まとまり (segment)・生成への補助の参考 (feedback) を作る。
[成績の診断](threads-performance-diagnostic.md) (T6.1) の「経過時間をそろえる」規則をそのまま
使う。診断の出力 (`reports/threads_performance_diagnostic_latest.*`) は変えない。

```bash
uv run python scripts/analyze_threads_performance.py --analysis
uv run python scripts/analyze_threads_performance.py --analysis --format json
uv run python scripts/analyze_threads_performance.py --analysis --lane regular --angle insight
uv run python scripts/analyze_threads_performance.py --analysis --topic "AI Threads" --min-age-hours 24
uv run python scripts/analyze_threads_performance.py --analysis --feedback --format json
uv run python scripts/analyze_threads_performance.py --analysis --as-of 2026-09-29T15:00:00+09:00
```

`--analysis` は **標準出力だけ** に出す。DB に書かない・Threads に問い合わせない・ファイルを
書かない。絞り込み (`--lane/--topic/--angle/--min-age-hours`) は表示だけで、分析は全体で作る。

## 入力

| 値 | どこから |
|---|---|
| lane (`regular` / `growth` / `unknown`) | 提案の種類 (`content_kind`: 記事の投稿 / `account_growth`) |
| トピック | コンテナ作成の試行に記録した `topic_tag`。記録なし = `unknown`、記録ありで値なし = `none` |
| 元 (`article` / `non_article`) | 公開の `source_article_id` |
| 切り口・長さ | 提案の `angle`・文字数 (長さの区切りは `threads_measurement_policy.json`) |
| 会話のきっかけ | 提案の来歴 `generation_brief.conversation_hook` (T6.3 より前は `legacy`) |
| 公開時刻 | `ThreadsPublicationService.gap_basis` (Threads の `remote_timestamp` を優先) |

## 指標

- 生の値 (最新の累積): views / likes / replies / reposts / quotes / shares。
- total engagement = likes + replies + reposts + quotes + shares (**5 つとも観測済みのときだけ**)。
- amplification = reposts + quotes + shares。
- engagement / view、conversation rate (replies / views)、amplification rate
  (amplification / views): **views が 1 以上のときだけ計算**。views が比率の下限
  (`comparison.minimum_views_for_ratio`、いまは 30) 未満なら `rate_reliable=false` で比べない。
- 欠測は **0 にしない** (`null`)。views が 0 / 不明でも 0 で割らない。
- 最初の観測・最新の観測・その間の伸び (`growth_delta`)・最新の観測からの経過 (`insight_age_hours`)・
  完全さ (`no_insight` / `partial` / `complete`)。

## 比べ方 (cohort)

- **経過時間の違う投稿を累積値で比べない。** 比べるのはチェックポイント (1h / 3h / 6h / 12h /
  24h / 72h) の値だけ。観測は近い本物の 1 件 (補間しない、許容幅は T6.1 と同じ)。
- **Growth と通常の投稿は混ぜない** (lane ごとに別の cohort)。
- 比べるチェックポイントはデータで決める: 比べられる本数で届く **いちばん強い証拠の段階** の中で、
  いちばん遅いチェックポイント。下限に届くものが無ければ `null` (`insufficient_data`)。
- 成分は別々: reach (views)・engagement・conversation・amplification・engagement_rate。
  **総合点は作らない。**
- 位置はほかの投稿の中の順位 (midrank、0〜1) と四分位の名前。**勝ち負けの固定の閾値は無い。**
- 証拠の段階 (`threads_observation_policy.json` の `evidence_thresholds`):
  5 本未満 `insufficient_data` / 5〜9 `hypothesis` / 10〜29 `preliminary` / 30 以上 `descriptive`。
  投稿の `status` は、比べられた成分のうちいちばん強い段階 (成分ごとの段階は `components` にある)。

## まとまり (segment)

切り口・会話のきっかけ・長さ・トピック・元・時間帯・曜日・経過の段階・lane ごとに、本数 (n)、
成分ごとの値の中央値、順位の中央値、**同じ cohort のほかの投稿** の順位の中央値を出す。
向き (`higher_than_cohort` / `lower_than_cohort`) は、まとまりとほかの投稿の両方が下限以上で、
1 本ずつ抜いても向きが変わらないときだけ言う (0 が多いと順位が 0.5 からずれるので、固定の 0.5
ではなく、ほかの投稿と比べる)。

## 参考 (`threads-performance-feedback/1`)

- `currently_supported_patterns` (上) / `weak_patterns` (下) / `insufficient_evidence_patterns` /
  `context_observations` / `growth_observations`。
- prompt に入れるのは **生成器が作れる値** (切り口・会話のきっかけ・長さ) だけ。トピック・時間帯・
  曜日・`legacy` などは `context` (記録するが prompt には入れない)。
- 文は「観測された傾向。原因ではない」と段階 (仮説 / 暫定 / 記述) を必ず付ける。本文は入れない
  (特徴だけ)。
- 証拠が無い・読めない・失敗 → **中立** (prompt に何も足さない。生成は止めない)。
- 指紋 (`fingerprint`) は内容の sha256。Growth の最新の値は入れない (観測のたびに揺れないように)。

### 生成へのつなぎ

`ThreadsProposalService(performance_feedback_provider=...)` を渡したときだけ使う。渡さなければ
prompt は T6.5 より前と同じ。使うときは T5.5 の学習の参考の後に次の節が付く:

- 事実・記事の根拠・文体の規則が優先。これは補助の参考で決まりではない。
- いつもの多様さを保つ (新しい書き方も試す)。直近 1〜2 本の切り口・話題を避ける既存の方針はそのまま。

使った参考 (中立を含む) は提案の来歴 `learning_guidance_json.performance_feedback` に
`schema/mode/fingerprint/as_of/checkpoint/patterns` として残る (列は増やさない)。

## worker

subsystem `performance_feedback_evaluation` (読むだけ・メモリに持つだけ)。

| 設定 (`subsystems.performance_feedback_evaluation`) | 既定 |
|---|---|
| `enabled` | `true` |
| `interval_minutes` | 360 (5 分の heartbeat ごとには作り直さない) |
| `min_interval_minutes` | 60 (観測を取り込んだ直後の前倒しの最短間隔) |
| `use_in_generation` | **`false`** (在庫の生成には渡さない。記録だけ) |

`insights_refresh` が新しい観測を取り込んだ (`imported`) ときだけ前倒しする。ログの行は
`mode / checkpoint / cohort_n / supported / weak / changed / used_in_generation`。
方針のファイルには節を足していない (既定の値で動く)。生成に使うのは運用の判断で
`use_in_generation: true` にしてから (worker の再起動が要る)。

## 限界

- 記述だけ: 特徴と成績の関連で、原因ではない。本数が少ない間は仮説。
- トピックの記録は新しい投稿だけ (古い投稿は `unknown`)。
- 比率は views が下限以上の投稿だけ (いまは少ない)。

## 生成の依頼への固定 (prompt を作った時点の参考)

在庫の生成の依頼 (`GenerationRequest.performance_feedback`、依頼の JSON の中。列は増やさない) に、
prompt に入れた参考をそのまま固定する (`threads-performance-feedback-frozen/1`):
`used_in_generation`・`schema`・`fingerprint`・`mode`・`evaluated_at`・`checkpoint`・`evidence`・
`content` (参考の中身)。指紋は `content` の正規の JSON の sha256 (評価の時刻は入らない。Growth の
最新の値も入らない)。参考を使わない依頼は `{"used_in_generation": false}` だけ (指紋は求めない)。

保存の時 (`ThreadsProposalService.persist(frozen_feedback=..., request_prompt=...)`):

- 固定した中身から指紋を計算し直して、固定した指紋と一致すること。
- 実際に provider へ渡した prompt の参考の節が、その中身から作ったものと同じこと
  (中立・使っていないなら、参考の節が無いこと)。
- 合えば **固定した参考** を来歴に残す (`frozen_at_prompt: true`)。今の参考に差し替えない。
- 合わなければ `performance feedback mismatch` で保存しない (fail closed)。

この項目の無い古い依頼は、前と同じ経路で取り込める。
