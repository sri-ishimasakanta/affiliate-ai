# 学習から投稿案の生成への弱い参考 (T5.5)

T5 の学習結果 (`threads-learning/1`) のうち **証拠の足りた所見だけ** を、これから作る
Threads 投稿案の prompt に **弱い参考** として入れる。自動の最適化ではない。

- 人がすべての提案を承認する。worker は承認済みの提案しか公開しない。
- 既存の提案・承認・承認済みの並び・公開の時刻には一切触れない。
- 証拠が足りなければ **中立** (いまの本番はこれ)。

## 使い方

```bash
# 1) prompt を出す。末尾に学習の参考と、次の手順に渡す値が出る
uv run python scripts/propose_threads_posts.py --article-id 21 --print-prompt

# 2) 生成結果を検査する (PLAN。保存しない)。1) が出した値をそのまま渡す
uv run python scripts/propose_threads_posts.py --article-id 21 --input out.json \
  --learning-as-of 2026-12-01T03:00:00+00:00 --guidance-fingerprint <64 桁>

# 3) 保存する (awaiting_approval。承認ではない)
uv run python scripts/propose_threads_posts.py --article-id 21 --input out.json \
  --learning-as-of 2026-12-01T03:00:00+00:00 --guidance-fingerprint <64 桁> --execute
```

`--guidance-fingerprint` を渡すと、prompt を作ったときと同じ参考であることを確かめる。
違えば拒否する (別の参考で書かれた案として記録しない)。渡さなければ保存はできるが、
来歴の `verified_against_prompt` は `false` になる。

## 参考の作り方 (`threads-generation-guidance/1`)

学習は計算し直さない。成熟・代表の観測・閾値・統計・ヒステリシスはすべて T5 のまま。
T5.5 が読むのは T5 の `findings` と `dimensions` だけである。

| 規則 | 内容 |
| --- | --- |
| 元になる所見 | `observed_difference` だけ。`no_clear_difference` と証拠不足は中立 |
| 強さ | `weak` だけ (V1)。スコア・重みは無い |
| prefer | どれかの所見で上、どの所見でも下でない値 |
| tentative | 支えている所見がすべて `weakening` (差が縮まりつつある) |
| de-emphasize | 下だった所見が `weakening` でなく、1 本を抜いても向きが変わらないときだけ。**禁止ではない** |
| 矛盾 | 同じ値が上でも下でもあれば、その値は中立 |
| 作れない値 | 生成器が作れない値 (将来の link_mode=note など)、またはそれと比べて得た参考は prompt に入れない (`actionable: false`) |

### 次元

| 次元 | 扱い |
| --- | --- |
| `angle` / `link_mode` / `length_band` | prompt に入れる (文面に効く) |
| `topic` / `daypart` / `weekday` | PLAN と来歴にだけ出す文脈の情報。**公開の時刻は worker が決め、T5.5 は変えない** |
| `trigger` | 使わない (診断用。manual / automatic で生成を変えない) |
| `hour` | 使わない (V1 では細かすぎる。daypart を使う) |
| `source_article` | 使わない (記事の中身そのものが交絡する。topic を使う) |

## prompt の中の位置

prompt は次の順に別々の節で並ぶ。学習の節が **いちばん弱い** と明記してある。

1. 立ち位置・文体・書いてはいけないこと (編集の規則)
2. 事実の扱い (記事本文が事実の境界)
3. 切り口・リンク
4. **多様性** (必ず守る): 依頼した切り口はすべて 1 本ずつ書く。リンクの有無と長さを
   全部そろえない。同じ構成を繰り返さない
5. **学習からの弱い参考**: 「事実の境界・文体・多様性の規則よりも弱い。矛盾したら
   この節を無視する」。中立なら「十分な証拠のある学習はまだ無い」とだけ書く。
   参考があるときも「優先した値以外の案も少なくとも 1 本は含める」
6. 出力形式、記事本文

内部の ID や分析の数字は prompt に入れない (PLAN には出す)。参考は検査 (errors) に
一切使わない。長さの上限などの制約は学習より常に上にある。

PLAN は、参考があるのに案が全部同じ値 (たとえば全部 medium) にそろったとき
`diversity:` として **知らせるだけ** (拒否しない。警告として保存もしない)。

## 来歴 (`threads_post_proposals.learning_guidance_json`)

保存した提案ごとに小さな来歴を残す。分析の中身は複写しない。

```json
{
  "schema": "threads-generation-guidance/1",
  "applied": false,
  "mode": "neutral",
  "fingerprint": "<sha256>",
  "as_of": "2026-12-01T03:00:00+00:00",
  "learning_schema": "threads-learning/1",
  "learning_policy_version": "t5.0",
  "evidence_status": "insufficient_sample",
  "preferences": [],
  "verified_against_prompt": true
}
```

- **指紋** は参考の中身 (元の版・証拠の状態・参考と根拠・中立の理由・安全策) の
  sha256。時刻は含まない (同じ証拠なら同じ指紋)。`as_of` は別に残す。
- 後から監査するときは、同じ `as_of` で参考を作り直して指紋を比べる。T5.1 の
  保証 (as_of の後の観測は見ない) により、後から入ったデータで結果は変わらない。
- T5.5 より前の提案は NULL のまま (書き換えない)。

### migration

列は 1 つ (`learning_guidance_json`、nullable JSON、migration `afc2f36bb3ca`)。
ORM では **deferred** にしてあり、通常の読み込みではこの列を読まない。だから
migration の前の DB でも、提案を読む処理 (常駐 worker・digest・queue) はそのまま動く。
提案の **保存** だけは列が必要で、無ければ何も書かずに止まる:

```bash
uv run alembic upgrade head
```
