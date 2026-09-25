# Threads の学習 (T5)

実際の Threads の結果から「これまでの成熟した投稿で、どの切り口・長さ・時間帯が
どう見えたか」を **記述する** 仕組み。証拠が足りなければ「足りない」と言う。
それがこの仕組みの一番大事な出力である。

T5 は証拠と所見を出すだけで、提案の生成・並び順・公開・承認・通知には
**一切つながない** (提案の規則を変えるのは T5.5 以降)。

```bash
# 人が読む形 (読むだけ。DB にも Threads にも書かない)
uv run python scripts/report_threads_learning.py

# 機械可読な形も書き出す (T5.5 が読む)
uv run python scripts/report_threads_learning.py --json out/threads-learning.json

# 変化を比べる基準の時刻 (既定は 24 時間前) / 分析する時刻を変える
uv run python scripts/report_threads_learning.py --since 2026-09-20T00:00:00+09:00
uv run python scripts/report_threads_learning.py --as-of 2026-10-01T09:00:00+09:00
```

## T4 の計測の規則をそのまま使う

別の成熟の仕組みは作らない。値はすべて `app/config/threads_measurement_policy.json`。

| 規則 | 値 | 出どころ |
| --- | --- | --- |
| 成熟 (比較してよい) | 公開から 72 時間以降 | `maturity_hours.initial_sample` / `classify_maturity` |
| 値ごとの本数の下限 | 3 本 | `comparison.minimum_mature_posts_per_dimension` |
| 比率を出す views の下限 | 1 投稿 30 views | `comparison.minimum_views_for_ratio` |
| 長さの帯 | short ≤150 / medium ≤300 / long ≤500 | `length_buckets` / `length_bucket` |
| 時刻・曜日 | 運用タイムゾーン (Asia/Tokyo) の壁時計 | `operations_policy.json` の `timezone` |

T5 で足したのは `learning` の節だけである (代表の観測の窓、比較の閾値、時間帯の区切り)。
ポリシーの版は `t5.0`。

## 代表の観測 (canonical snapshot)

1 投稿は観測を何行も持つ。学習では **1 投稿につき 1 つだけ** を使う:

> 成熟の境界 (72h) から 24 時間以内 (72h 以上 96h 未満) に観測できた、最初の
> `observed` の snapshot。その窓の中に views の取れた snapshot があれば、その最初の
> ものを優先する。

- 同じ投稿を 2 回数えない。1 時間後の値と 7 日後の値を同じものとして並べない。
- 窓が閉じたあとの観測は代表を変えない。後から数字が揺れても所見は動かない。
- 72h を過ぎたのに窓の中の観測がまだ無い投稿は `awaiting_mature_observation`、
  窓が閉じても無かった投稿は `no_comparable_observation` として **数えるだけ**
  (例には入れない)。どちらも「比較できるデータの欠け」として報告に出る。
- `failed` / `empty` の観測は代表にしない。

常駐 worker は 24〜72h の投稿を 60 分ごとに観測するので、通常は 72h を過ぎてから
1 時間以内に代表の観測ができる。24 時間の窓は、worker が止まっていた日の分の余裕。

## 指標

使うのは保存済みの公式の指標だけ (`views` `likes` `replies` `reposts` `quotes` `shares`)。

- `interactions` = likes + replies + reposts + quotes + shares。**5 つとも観測できたとき
  だけ** 合計する。1 つでも欠けていれば `None` (欠けた分を 0 とみなさない)。
- `interaction_rate` = interactions ÷ views。views が欠測・0・30 未満なら出さない
  (0.0 にしない)。
- 生の件数 (合計と、観測できた本数) を常に率と並べて出す。
- 不透明な総合スコアは作らない。

**欠測は 0 ではない。** views が NULL の投稿は `missing.views` に、views=0 の投稿は
`zero_views` に数える。

## 証拠の状態

値ごと (たとえば angle=insight) と全体について、次の順に判定する。

| 状態 | 条件 |
| --- | --- |
| `insufficient_sample` | 成熟した投稿が 3 本未満 |
| `insufficient_comparable_data` | 代表の観測があり、views と相互作用 5 つが揃った投稿が 3 本未満 |
| `insufficient_views` | そのうち views が 30 以上の投稿が 3 本未満 |
| `sufficient_evidence` | 上のどれにも当たらない |

母数が足りないうちは中央値も比率も出さない (T4 と同じ)。件数と合計だけを示す。

## 所見 (finding)

同じ次元の中で `sufficient_evidence` の値が 2 つ以上あるときだけ、値どうしを
成熟した投稿の **interaction_rate の中央値** で比べる。

- 中央値の差 (高い方を基準にした相対差) が 0.2 以上で、かつ **どちらの群のどの 1 本を
  抜いても向きが変わらない** ときだけ `observed_difference`。外れ値 1 本では所見に
  ならない。
- 一度立った所見は、差が 0.1 を下回るか向きが変わるまで保つ (`weakening: true`)。
  新しい証拠が 1 本入るたびに所見が点滅しないため。所見は、代表の観測が入った順に
  最初から作り直すので、同じ観測からは常に同じ結果になる。
- それ以外は `no_clear_difference` (証拠はあるが、はっきりした差は見えない)。
- views の多い 1 本が本数の不足を埋めることはない。若い投稿は結論を動かさない。
- `trigger` (manual / automatic) は **診断用** で、比べない。

所見の文は「現在の成熟した標本では、A の方が B より中央値の interaction rate が
高かった」という記述だけで、本数・値・期間・不確かさを必ず添える。相関であって、
その次元が結果を生んだとは言わない。「最適」「勝ち」「常に良い」とは書かない。

## 次元

| 次元 | 値 | 備考 |
| --- | --- | --- |
| `angle` | insight / common_mistake / comparison / question / beginner_tip | 未知の値もそのまま出る |
| `source_article` | 記事 ID (ラベルは記事タイトル) | |
| `topic` | 記事の keyword | 無ければ `unknown` |
| `link_mode` | none / article | 未知の値 (将来の note など) もそのまま出る |
| `length_band` | short / medium / long | T4 の帯 |
| `hour` | 00〜23 (運用タイムゾーン) | |
| `daypart` | morning 05-10 / midday 10-14 / afternoon 14-18 / evening 18-22 / night 22-05 | |
| `weekday` | Mon〜Sun (運用タイムゾーン) | |
| `trigger` | manual / automatic | 診断用。比べない |

## 変化 (changes)

基準の時刻 (既定は 24 時間前) の結果を、**同じ観測から作り直して** 今と比べる。
新しい成熟した例も、所見の変化も、証拠の十分さの変化も無ければ
`no material change` と出す。

履歴のための表は持たない。観測は append-only で書き換えないので、どの時点の結果も
同じ入力から決定的に作り直せる (スキーマの変更なし)。

## 機械可読な出力 (`threads-learning/1`)

`--json` で書き出す。主なキー:

- `schema_version` `policy_version` `generated_at` `generated_at_local` `local_timezone`
- `thresholds` `canonical_snapshot_rule`
- `counts` (total / mature / immature / comparable / missing_comparable_data …)
- `evidence_status` `evidence_reasons` `comparable_dimensions`
- `dimensions.<name>.values[]` (status / reasons / counts / missing / views /
  interactions / metric_totals / interaction_rate / outliers / window / publication_ids)
- `findings[]` (id / dimension / kind / higher / lower / values / relative_difference /
  leave_one_out_consistent / weakening / since / statement / uncertainty)
- `publications[]` (1 投稿の扱いと代表の観測。若い投稿の数字は出さない)
- `changes` `caveats` `side_effects`

token・URL・メディア ID は含まない。T5.5 はこの形を読むだけで、T5 の側に生成の
規則は置かない。
