# Threads の計測 (T4)

Threads に出した投稿を「読むだけ」で計測し、そこから **言えることだけ** を言う
ための仕組み。投稿・承認・公開はここには一切含まれない。

## 公式に存在する指標

Meta の Threads Insights API が返すのは投稿あたり次の 6 つだけである
(出典: developers.facebook.com / Threads API "Insights")。

| 指標 | 意味 | 注意 |
| --- | --- | --- |
| `views` | 投稿が再生・表示された回数 | 公式に **"in development"** と注記されている |
| `likes` | いいねの数 | |
| `replies` | 返信の数 | **返信の返信は含まれない** (公式の注記) |
| `reposts` | リポストの数 | |
| `quotes` | 引用の数 | |
| `shares` | 共有の数 | 公式に **"in development"** と注記されている |

**存在しない指標**: `reach` / `impressions` / `CTR` / `engagement rate`。
他の SNS の感覚で置き換えず、無いものは無いまま扱う。
`app/config/threads_measurement_policy.json` の `unsupported_metrics` に
明記してあり、テストで固定している。

その他の公式の制約:

- ユーザー指標の `since` / `until` は **2024-04-13 より前の日付では動かない**。
- `REPOST_FACADE` の投稿には空配列が返る。

## 「観測できなかった」と「0 だった」を混同しない

`threads_insight_snapshots` の指標列はすべて nullable で、取得できなかった指標は
**NULL のまま** 残す。取れなかった指標名は `missing_json` に記録する。
欠測を 0 で埋める処理はどこにも無い (テストで固定)。

## 公開直後の 0 は成績ではない

成熟度は **経過時間だけ** で決める。指標の値は見ない。

| 段階 | 経過時間 | 比較に使えるか |
| --- | --- | --- |
| `just_published` | 1 時間未満 | いいえ |
| `early_observation` | 24 時間未満 | いいえ |
| `initial_sample` | 72 時間未満 | いいえ |
| `mature_enough_for_comparison` | 72 時間以上 | はい |

公開 12 分後に views が 0 でも、それは `insufficient_age` という観察であって
「成績が悪い投稿」ではない。**低調さでアラートは出さない。**

## 総合スコアを作らない

名前の付いた観察 (`views_observed` / `no_interaction_yet` / `reply_activity` …) を
個別に立てるだけで、重み付けした一つの数字は作らない。
比率は `interactions_per_view` だけで、これは名前どおりの意味しか持たない
(`views` の定義が公式に "in development" である以上、他所の「エンゲージメント率」と
同じものだとは言えない)。分母が不明・0・母数不足のとき、および
**投稿がまだ若いとき** は `None` を返す (0.0 を返すと「効果ゼロ」と読まれてしまう)。

若さの条件は `interactions_per_view()` の必須引数として構造で守っている。
最初の実運用でこれが漏れており、公開 28 分後の投稿 (views 83 / 反応 0) が
`0.0` を出していた — 分母は足りていても、配信が一巡していなかった。

## 時刻と曜日は JST の壁時計で出す (T4.1)

保存している時刻は UTC のまま。公開時刻 (時)・曜日といった **人の生活時間で意味を
持つ派生値** だけを、`operations_policy.json` の `timezone` (Asia/Tokyo) で出す。
Threads 用に別のタイムゾーン設定は作らない。

T4 では UTC のまま時・曜日を出していた。2026-09-23 18:24 UTC の投稿は、実際には
JST で 09-24 (木) 03:24 なのに、「水曜 18 時」として集計されていた。

naive な保存値は UTC とみなしてから変換する。成熟度は絶対時間なので、変換の影響を
受けない。

## 比較は母数がそろってから

角度・リンク有無・長さ・公開時刻・曜日の各次元は、比較可能な投稿が
`minimum_mature_posts_per_dimension` (現在 3) 本そろうまで中央値を出さない。
件数だけを出して「まだ比較しない」と言う。

1 本しか無い段階で出る助言は `INSUFFICIENT_DATA` と
`COLLECT_MORE_OBSERVATIONS` だけである。**1 本の結果から切り口の優劣は言えない。**

## サイト側との突き合わせ

`link_mode=article` の投稿だけが UTM を持ち、GA4 と突き合わせられる。
突き合わせるのは「同じ UTM を持つセッションがあるか」までで、Threads の表示が
その訪問を生んだとは主張しない。

限界:

- GA4 の既定のレポートでは `utm_content` まで分解されないことがある。
- `link_mode=none` の投稿には UTM が無く、サイト側の帰属は **存在しない**。
- 同じ記事へ他経路からも流入するため、UTM 一致は相関であって因果ではない。

Threads の view と サイトのセッションは別の計測系である。片方がもう片方を
説明するとは言わない。

## 使い方

```bash
# 何を見に行くかだけ (Meta には 1 度も問い合わせない)
uv run python scripts/import_threads_insights.py

# 実際に読んで観測を 1 行積む
uv run python scripts/import_threads_insights.py --execute

# これまでの観測から言えることだけを出す
uv run python scripts/import_threads_insights.py --report
```

C8 の日次/週次 run には `import_threads_insights` ステップとして入っている。
このステップは **他のどのステップにも依存しない** (`STEP_DEPENDENCIES` に無い)。
失敗しても SEO/収益の取り込みや候補評価を巻き込まない。高頻度タスクは増やさない。

## アラートにすること / しないこと

出すのは「計測そのものが壊れているとき」だけ:

- 資格情報が使えない (`threads_auth` / `threads_permission` / 未設定) — 即時、error
- 一時的な失敗 (`threads_rate_limit` / `threads_server` / `threads_timeout`) が 3 回続いた
  — warning (1 回では出さない)
- 想定外の応答 (`threads_response`) — 即時、warning。原因は断定せず、理由をそのまま見せる
- 公開済み投稿が見つからない (`threads_not_found` = HTTP 404) — warning
- 公開中の文面が承認された文面と一致しない — error

### 失敗の分類 (2026-09-25 に修正)

Graph API は token 切れや権限の喪失の多くを **HTTP 400** で返し、種類は本文の
`code` で示す。以前は HTTP status だけで分類していたため、2026-09-25 06:30 の
日次実行で返った `HTTP 400 / code 200 / "API access blocked."` (API アクセスが
アカウント単位で止められた) が「想定外の応答」になり、さらにそれが
「公開済み投稿が読めない (削除・非公開・ID の不整合)」という **誤った警告** になった。

いまは文書化された code を HTTP status より先に見る
(出典: Graph API "Handling Errors"):

| code / subcode | 分類 |
| --- | --- |
| 190, 102 / subcode 458, 459, 460, 463, 464, 467 | `threads_auth` |
| 3, 10, 200–299, 368 | `threads_permission` |
| 4, 17, 341 | `threads_rate_limit` |
| 1, 2 | `threads_server` |
| (code なし) HTTP 404 | `threads_not_found` |
| それ以外の 4xx・JSON でない・形が違う・通信の失敗 | `threads_response` |

「投稿が読めない」と言うのは `threads_not_found` のときだけ。警告は弱めていない
(権限の喪失は warning から error に上がった)。

`run_threads_worker.py` は、最後の **成功した** 観測に加えて、最後の **試み** が失敗して
いればそれを `LAST ATTEMPT FAILED` として表示する。

**出さないもの**: views が少ない、いいねが 0、反応が無い。
これらは運用の障害ではない。

設定の状態は 3 つに分けて扱う (T4.1)。T4 では「未設定」をひとまとめにしており、
有効なのに token が無い環境まで静かに成功扱いにしていた。これは実際の不具合だった。

| 状態 | 条件 | C8 ステップ | アラート |
| --- | --- | --- | --- |
| `disabled` | `THREADS_ENABLED=false` | 成功 (no-op) | 出さない |
| `misconfigured` | 有効なのに user id / token が欠けている・不正 | **失敗** | `IMPORT_FAILURE` を即時 |
| `ready` | 有効で設定がそろっている | 通常どおり取り込む | 故障時のみ |

`disabled` を skip にしないのは、skip だと run が partial に落ち、使っていないだけで
毎日「異常」メールが飛ぶため。アラートには問題のある設定の **名前** だけを出し、
値は出さない。

## アクセストークンの期限

公式の仕様 (Threads API / Long-Lived Tokens):

- 長期トークンの有効期間は **60 日**。
- 更新は `GET /refresh_access_token` (`grant_type=th_refresh_token`)。
- 更新できるのは **発行から 24 時間以上たっていて、まだ失効していない** トークンだけ。
- 更新するとそこから改めて 60 日。
- **60 日以内に更新しなかったトークンは完全に失効し、もう更新できない。**

**トークンのメタデータ (有効期限など) を単独で読む公式エンドポイントは
Threads には無い** (Facebook の `debug_token` に相当するものが用意されていない)。
残り日数が分かるのは、交換・更新の応答に含まれる `expires_in` の時点だけである。
したがって期限監視は自動化していない。**無い API を推測で実装しない。**

運用としては、失効すれば取り込みが `threads_auth` で失敗し、その場で error
アラートが出る。そこで人が更新する。トークンは `.env` にのみ置き、DB にも
ログにも例外にも出さない。prefix も出さない。

## 学習 (T5)

成熟した投稿の比較は `docs/operations/threads-learning.md` (T5) が担当する。
この文書の成熟・本数・views の規則をそのまま使う。
