# Threads 自動公開 (T4.3: 実装済み・本番では無効)

T4.3 で、承認済み queue から自動で公開する経路を実装した。**本番では無効のまま**
コミットしてある。有効にするのは、下の「本番で有効にする前に」をすべて人が確認して
からである。

2026-09-25 06:30–08:50 JST ごろ、Meta 側で Threads API のアクセスが止められていた
(`HTTP 400 / Graph code 200 / "API access blocked."`)。08:58 JST には回復している
(`check_threads_connection.py` が `reachable = True`、指標の取り込みも成功)。

## 公開まで進む条件 (すべて必要)

| # | 条件 | どこで決まるか |
| --- | --- | --- |
| 1 | ポリシー `automatic_publication.enabled` が `true` | `app/config/threads_operations_policy.json` (**コミット済みは `false`**) |
| 2 | worker が `--auto-publish` で起動されている | CLI / ランチャの `publish` プロファイル |
| 3 | worker のロックを持っている | `ThreadsWorkerLock` (原子的な回収、所有者の証明) |
| 4 | Threads の設定が `ready` | `ThreadsService.describe()` |
| 5 | queue の評価にブロッカーが無い | 公開窓・間隔・不確定な公開・候補なし・保留・not_before・expires_at |
| 6 | 選ばれた候補が T3 の `plan()` を通る | 承認・stale・中身の整合性・二重投稿・間隔・他の公開の不確定 |
| 7 | 読むだけの事前確認 `GET /me` が通る | API が止められていれば **コンテナを作る前に** 止まる |

1 つでも欠ければ公開しない。フラグだけ、ポリシーだけ、では何も起きない。

## 1 回に 1 本

- 1 回の評価で公開を試みるのは **最大 1 件**。資格のある候補が 5 件あっても 1 件。
- 公開の直前に、ロックの中で queue を評価し直す (古い評価で出さない)。
- 公開すると間隔が始まるので、次の評価はちょうど 120 分後に来る。
- worker が止まっていた間の分を取り返さない (朝のまとめ出しは無い)。
- worker の骨組みは、1 サイクルで 2 件目の公開が報告されたらその場で止まる。

## 書き込み経路の安全条件 (手動にも効く)

T3 の `ThreadsPublicationService.plan()` / `publish()` に次を入れた。
`publish_threads_post.py --execute` で人が公開するときにも同じように効く。

- **前回の実際の公開から 120 分空いていなければ公開しない。**
- 人が理由を付けて明示したときだけ、**間隔だけ** を上書きできる:

  ```bash
  uv run python scripts/publish_threads_post.py --proposal-id 3 --execute --override-gap --override-reason "..."
  ```

  理由と時刻は `threads_publications.gap_override_reason / gap_override_at` に残る。
  フラグだけ・理由だけはどちらも拒否する。
- **自動の公開は間隔を上書きできない** (T3 がそれを拒否する)。
- **他の公開が不確定・照合待ちなら、どの提案も公開しない** (上書きもできない)。
  `publish_threads_post.py --reconcile` で照合するまで止まる。
- 誰が公開を始めたかを `threads_publications.trigger` (`manual` / `automatic`) に残す。
  既存の公開 1 は `manual`。

## 失敗したとき

| 状況 | 何が起きるか | 知らせ方 |
| --- | --- | --- |
| 事前確認が失敗 (API が止められている・token 切れ等) | コンテナを作らずに止まる。次の評価でまた確認する | その場で `IMPORT_FAILURE` (権限・token は error、一時的な失敗は warning) |
| コンテナ作成で失敗 | 外には何も出ていない。T3 が `failed` にし、再試行してよい | その場で `IMPORT_FAILURE` |
| 公開の応答を取りこぼした | T3 が `uncertain` にする。**queue 全体が止まる**。照合が要る | その場で `AUTOMATION_HEALTH` error |
| 公開の途中で 10 分以上止まった | queue 全体が止まる | 日次の監視で `AUTOMATION_HEALTH` error |
| 読み戻しで文面が違う | `reconciliation_required`。queue 全体が止まる | その場で `AUTOMATION_HEALTH` error |

「その場で」は、worker が自動公開を試みた直後に `OperationsAlertService` で記録・通知
することを指す (C8 と同じ仕組み)。同じ問題は fingerprint で 1 行にまとまり、cooldown
中は再通知しない。C8 の日次の監視も同じ公開の状態を見るので、worker が止まって
いても翌朝には必ず知らされる。**成績 (views の少なさ等) ではアラートを出さない。**

事前確認の失敗の分類は、2026-09-25 に直した Graph エラーの分類
(`docs/operations/threads-measurement.md`) をそのまま使う。

## 携帯での決定の取り込み

worker を `--sync-approvals` で起動すると、5 分おきに中継から決定を取り込む
(既存の `MobileApprovalService.sync(execute=True)` をそのまま使う)。取り込めたら公開の
評価を前倒しする。承認は queue に入るだけで、ここからは公開しない。

## dry run (いつでも安全)

```bash
uv run python scripts/run_threads_worker.py
```

既定の 1 回評価は、**自動公開が有効だったら何が起きるか** を表示する
(`automatic publication dry run`)。ゲートの状態、次の候補、止めている理由を出す。
ネットワークにも DB にも触れない。

## ランチャとタスクスケジューラ (準備のみ・未登録)

`scripts/run_threads_worker_task.cmd` (ASCII のみ、C8 と同じ流儀)。プロファイル:

| プロファイル | フラグ | 投稿 | メール |
| --- | --- | --- | --- |
| `observe` (既定) | `--collect-insights --sync-approvals` | しない | しない |
| `operate` | + `--send-approval-digests` | しない | 承認依頼の digest |
| `publish` | + `--auto-publish` | ポリシーが有効なときだけ | 承認依頼の digest |

登録コマンドは表示するだけで、登録はしない:

```bash
uv run python scripts/plan_threads_worker_schedule.py --profile observe
```

タスクは 15 分おきの **復帰用** トリガーで、投稿の時刻表ではない。worker が動いて
いれば 2 つ目はロックで即座に終わる (終了コード 4)。登録後に、タスクスケジューラの
画面で「スケジュールされた時刻に開始できなかった場合、すぐに実行する」と
「既に実行中の場合は新しいインスタンスを開始しない」を設定する
(`schtasks /Create` では設定できない)。ログは `threads-worker.log` で C8 と分かれる。

## 最初の自動公開 (人が明示して 1 回だけ)

1 本目は常駐させずに、1 サイクルだけ動かして確かめる:

1. 公開窓 (07:00–23:00 JST) の中で、dry run が意図どおりの候補を示すことを確認する:

   ```bash
   uv run python scripts/run_threads_worker.py
   ```

2. `threads_operations_policy.json` の `automatic_publication.enabled` を `true` にする
   (人がレビューしてコミットする)。
3. 1 サイクルだけ動かす (ロックを取り、公開は最大 1 本):

   ```bash
   uv run python scripts/run_threads_worker.py --once --auto-publish
   ```

4. 結果を確かめる (読み戻しの一致・trigger=automatic・次の評価が 120 分後):

   ```bash
   uv run python scripts/check_threads_post.py --publication-id <id>
   ```

5. 常駐に進むまでは `automatic_publication.enabled` を `false` に戻してよい。

## 本番で有効にする前に (人が確認すること)

1. Meta の API アクセスが戻っていること:
   `uv run python scripts/check_threads_connection.py` が `reachable = True`
2. 承認済みの提案が queue にあり、dry run が意図どおりの候補を示すこと
3. まず `observe` プロファイルでタスクを登録し、数日問題が無いこと
4. `threads_operations_policy.json` の `automatic_publication.enabled` を `true` にする
   変更を、人がレビューしてコミットすること
5. `publish` プロファイルに切り替えること
