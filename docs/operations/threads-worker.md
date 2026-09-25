# Threads 常駐 worker (T4.1 土台 / T4.2 承認のまとめ送り・読むだけの指標取得)

T4.2 の追加分 (在庫・承認の時刻・まとめ送り・queue 操作) は [threads-approval-digest.md](threads-approval-digest.md) にまとめた。

T4.1 は **自動公開のフェーズではない**。作ったのは、後の自動公開 (T4.3) が
きれいに乗るための土台だけである。

- 投稿しない。自動公開はコードで無効 (`AUTOMATIC_PUBLICATION_ENABLED = False`)
- 承認メールを送らない
- WordPress に書かない、`/go/` に触れない
- タスクスケジューラを作らない・変えない
- ネットワークに一切出ない (設定の状態は手元の設定値だけで判定する)

## 承認は公開ではない

人の承認が意味するのは **「この文面は出してよい」** であって、「今すぐ出せ」では
ない。承認済みの提案は queue に入るだけで、**いつ・どれを** 出すかは別の判定である。

携帯での承認もメールも、公開を引き起こさない。

## 運用ポリシー (`app/config/threads_operations_policy.json`)

| 項目 | 値 | 意味 |
| --- | --- | --- |
| タイムゾーン | (持たない) | `operations_policy.json` の `Asia/Tokyo` を使う |
| 公開窓 | 07:00–23:00 | この間なら公開の **資格が生まれうる** |
| 承認通知窓 | 08:00–21:00 | T4.2 で承認メールを送ってよい時間 |
| 公開間隔の目安 | 120 分 | 最後の **実際の** 公開から測る |
| 1 日の本数の目安 | 3–5 本 | **助言のみ** |
| heartbeat | 最大 5 分 | 生存確認とロック更新の間隔 |

窓は `[start, end)`。07:00 は公開可、06:59 と 23:00 は不可。

**07:00 は「07:00 に投稿する」ではない。** 07:00 から資格が生まれうるだけである。

### 1 日の本数は助言

3–5 本はノルマでも、健全性の条件でも、下限でも上限でもない。

- 3 本未満でも障害ではない
- 5 本を超えても誤りではない
- 編集上の理由があり、中身が十分に違い、間隔が自然なら、10 本前後の日もありうる

**日次の上限 (5 本で打ち止め) は作っていない。** ポリシーの読み込みは
`advisory: true` でなければ拒否する。

## 時刻の決まり方

固定の時刻表 (09:00 / 11:00 / 13:00 …) は持たない。
「人間らしく」見せるための乱数の遅延も入れない。時刻は状態から決まり、説明できる。

- 09:17 に出した → 次の資格は **11:17**。11:15 や 11:20 の枠に丸めない。
- 21:45 に出した → 23:45 は窓の外 → **翌 07:00** に繰り越す。夜中には出さない。
- 朝にまとめて出さない。最後の実際の公開から間隔を測るので、07:00 に 1 本出せば
  次はまた 09:00 以降になる。止まっていた間の分を取り返すことはしない。

時刻が変わるのは、実際の状態が変わるから:
承認された時刻、前回の実際の公開時刻、queue の中身、公開窓、切り口・話題の偏り、
不確定な公開の有無、(T4.2 以降) not_before / expires_at / 保留。

## worker の作り

作らないもの:

```
while True:
    run_everything()
    sleep(300)
```

作ったもの: 仕事ごとに **自分の次回時刻 (`next_run_at`) を持つ** 小さな scheduler。

- worker は、次に期限が来る仕事の時刻まで眠る。ただし heartbeat (5 分) を超えては
  眠らない。
- 起きたら **期限が来た仕事だけ** を動かす。
- 仕事の失敗は worker を止めない。その仕事だけを heartbeat 後に再試行する。

| 仕事 | 間隔 | やること |
| --- | --- | --- |
| `health` | 5 分 | 設定の状態 (disabled / misconfigured / ready) を見る |
| `queue_observation` | 5 分 (T4.2 で 10 分から短縮) | 提案と公開の状態の指紋を取る。変わっていたら公開評価とまとめ送りの評価を前倒し |
| `publication_evaluation` | **状態で決まる** | queue を評価し、次に評価すべき時刻ちょうどを返す |
| `insights_refresh` | 若い投稿 30 分 / 1–3 日 60 分 / 成熟後 6 時間 / 14 日で停止 | 期限が来た投稿を報告する。`--collect-insights` のときだけ読むだけで取得 (C8 と同じ経路) |
| `approval_notification_flush` | **状態で決まる** (cooldown / gather / 通知窓) | まとめ送りが期限かを評価する。`--send-approval-digests` のときだけ 1 通送る |

公開評価は、worker が 5 分おきに起きていても、**自分の時刻 (例: 11:17) まで走らない**。

## queue の評価

`app/social/threads/queue.py` (pure)。不透明な「次に出すべき投稿スコア」は作らない。
名前の付いた事実と理由だけで判定し、並べる。

候補ごとの理由 (1 つだけ付く):

| 理由 | 意味 |
| --- | --- |
| `eligible` | 出してよい |
| `not_approved` | まだ承認されていない |
| `already_published` | 既に出した (二度と出さない) |
| `stale` | 元記事が変わった・非公開になった |
| `content_integrity` | 文字数の不整合など、中身の整合性が崩れている |
| `uncertain_publication` | この提案の公開試行が不確定 |
| `held` | 人が保留にしている (T4.2) |
| `not_before` | `not_before` より前 (T4.2) |
| `expired` | `expires_at` を過ぎた。消さない (T4.2) |

queue 全体を止めるブロッカー:

| ブロッカー | 人が直す必要があるか |
| --- | --- |
| `threads_disabled` | いいえ (意図した停止) |
| `threads_misconfigured` | **はい** |
| `uncertain_publication` | **はい** (T3 の照合が必要) |
| `gap_not_elapsed` | いいえ (待つだけ) |
| `outside_publication_window` | いいえ (待つだけ) |
| `no_eligible_candidate` | いいえ |
| `automatic_publication_disabled` | いいえ (T4.1 では常に付く) |

提案ごとの中身の判定は T3 の `ThreadsPublicationService.assess()` を使う。
T3 の `plan()` も同じ `assess()` を使うように切り出したので、判定は 1 か所にしかない。

### 並び順と安定性

並び順は辞書式の比較で、スコアではない。資格のある候補だけが並ぶので、
「次に優先」が安全の条件を飛ばすことは構造上ない:

0. 人が「次に優先」を指定したか (T4.2、指定した順)
1. 直前の公開と同じ記事か (弱い信号)
2. 直前の公開と同じ切り口か (弱い信号)
3. 承認が古い順 (編集上の順序。T4.2 から権威ある `approved_at` だけを使う)
4. proposal id

何も変わらなければ、順序も変わらない。

### 弱い信号で永遠に後回しにしない

弱い信号 (同じ記事・同じ切り口) は並び順を少し後ろにするだけで、**候補から外さない**。
しかも承認から `starvation_guard_hours` (48 時間) を過ぎた候補には効かない。

公開を止められるのは強いブロッカーだけ: 間隔、公開窓、stale、不確定な公開、
中身の不整合、(T4.2 以降) 保留・not_before・expires_at。

### 若い指標を順位の根拠にしない

T4.1 の並び順は指標を **一切使わない**。20 分で views 0 の投稿は失敗ではなく、
30 分で views 100 の投稿は似た投稿を連発してよい理由ではない。

評価結果は `evidence_state` を明示する: 比較可能な投稿 (72 時間以上・観測あり) が
3 本未満なら `insufficient_evidence`、それ以上なら `usable`。`usable` になっても、
T4.1 では順序は変わらない (テストで固定)。

## 1 回に 1 本

自動公開はまだ無効だが、worker は次の不変条件の上に作ってある:

**1 回の評価サイクルで公開してよいのは最大 1 本。**

- queue 評価が返すのは `next_candidate` の 1 件だけ。複数を返す API は無い。
- worker は 1 サイクルで 2 件目の公開が報告されたら、その場で止まる。
- 1 本出したら状態が変わる (間隔が始まる) ので、次の候補は評価し直す。
- 資格のある候補をまとめて出すループは存在しない。

T4.1 では `publisher` を worker に渡すと起動自体を拒否する。

## T3 の不確定な公開は絶対

公開の状態が `creating` / `container_created` / `publishing` / `uncertain` のもの、
または `reconciliation_required` のものが 1 件でもあれば、`uncertain_publication` が
hard blocker として立つ。`publish_threads_post.py --reconcile` で照合するまで、
次の候補は 1 件も進まない。T3 の二重投稿防止 (一意制約・状態機械・media id) は
そのまま。

## ロック

C8 と同じ `OperationsLockService` を、別のロック名 `threads_worker` で使う。
C8 のパイプラインのロックとは独立している。

T4.2 で **回収を原子的にした**。T4.1 までは「読んで、古ければ書く」で、古いロックの
直後に 2 つの worker がほぼ同時に起動すると、どちらも回収できたと信じられた。
いまは条件付き UPDATE (compare-and-swap) 1 文で回収し、書けた行数が 1 のときだけ
取得できたとみなす。負けた方は `lost_race` で何もせずに終わる。

所有者の証明: 取得ごとに乱数の `owner_token` を発行する (列を追加)。heartbeat と
解放は token が一致したときだけ効く。T4.1 の worker は所有者を示さずに解放しており、
所有権を失った worker が新しい所有者のロックを解放できた。これも直した。
heartbeat が所有権の喪失を返したら、worker はその場で止まる (終了コード 5)。

外に作用するフラグ (`--collect-insights` / `--send-approval-digests`) を付けたときは
`--once` でもロックを取る。`plan_threads_approval_digest.py --execute` も同じロックを
取るので、2 つのプロセスが同時に digest を送ることはない。

- 2 つ目の worker は取得に失敗し、**何もせずに** 終わる (終了コード 4)。
  1 つ目のロックを解放しない。
- ロックは毎サイクル heartbeat される。
- プロセスが死んだら、15 分 (heartbeat の 3 倍) を過ぎたロックを次の worker が回収する。
  回収したことは戻り値で分かる (黙って奪わない)。
- 既定の 1 回評価はロックを取らない (DB に何も書かない)。

## CLI

```bash
# 既定: 1 回だけ評価して状態を出す (ロックも取らない。DB に何も書かない)
uv run python scripts/run_threads_worker.py
```

```bash
# 常駐 PLAN ループ (ロック行だけを書く)
uv run python scripts/run_threads_worker.py --resident
```

```bash
# 読むだけの指標取得を有効にする (T4.2)
uv run python scripts/run_threads_worker.py --resident --collect-insights
```

```bash
# 承認依頼のまとめ送りを有効にする (T4.2。本番では人の確認を経てから)
uv run python scripts/run_threads_worker.py --resident --send-approval-digests
```

`--execute` は無い。どのモードでも、Threads への書き込み・WordPress・`/go/`・
スケジューラの変更は 0 件。承認依頼メールは `--send-approval-digests` を付けたとき
だけ送られ、Threads の指標は `--collect-insights` を付けたときだけ読む。

## Windows タスクスケジューラ (設計のみ・未登録)

**T4.1 ではタスクを作らない。** 将来 (T4.3) の設計:

役割: 常駐 worker を **起動し、落ちたら立ち上げ直す** こと。
「決まった時刻に投稿する cron」ではない。投稿の時刻は worker が状態から決める。

- ランチャー: C8 と同じ流儀で `scripts\run_threads_worker_task.cmd` を用意する
  (ASCII のみ、`/TR` に入れ子の引用符を作らない、`cd /d "%~dp0.."`、
  `AFFILIATE_AI_UV` / `AFFILIATE_AI_LOG_DIR` で上書き可能)。
  中身は `uv run python scripts\run_threads_worker.py --resident` だけ。
- 秘密情報をタスク定義にもランチャーにも入れない。資格情報は既存の `.env` 読み込みで
  プロセス内で解決する。
- トリガー: ログオン時 + 起動時。加えて、落ちた worker を拾うための周期トリガー
  (例: 15 分おき)。worker が動いていれば、2 つ目はロックで即座に終わるだけ。
- `StartWhenAvailable` は使ってよい。worker はまとめ出しをしない
  (間隔は最後の実際の公開から測る) ので、遅れて起動しても取り返しは起きない。
- 「新しいインスタンスを開始しない」(`MultipleInstancesPolicy=IgnoreNew`) に加えて、
  DB ロックで二重起動を防ぐ (タスクスケジューラの外から起動された場合にも効く)。
- ログは C8 と分ける: `D:\Logs\affiliate-ai\threads-worker.log`。
- 実行レベルは C8 と同じ `LIMITED`。

## 後のフェーズに回したもの

**T4.2 — 済み** (在庫・承認の時刻・まとめ送り・queue 操作・読むだけの指標取得・
原子的なロック回収)。詳細は [threads-approval-digest.md](threads-approval-digest.md)。

**T4.3 — 実装済み・本番では無効**。自動公開 (1 回に 1 本、ゲート付き)、書き込み経路での
120 分の間隔、人による間隔の上書き (理由を記録)、携帯での決定の取り込み、ランチャと
タスク登録の計画。詳細と、本番で有効にする前の確認事項は
[threads-autopublish.md](threads-autopublish.md)。
