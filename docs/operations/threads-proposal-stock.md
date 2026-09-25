# Threads の投稿案の在庫の保守 (T6)

承認を待つ Threads の投稿案を、人の手間を減らしながら **ほどよく** 用意しておく仕組み。

```
在庫を数える → 作る必要があるか (理由つき) → 記事・切り口・リンクを選ぶ
→ T5.5 の弱い参考つき prompt → provider → 出力の検査 → awaiting_approval で保存
→ 既存の承認 digest (T4.2) → 人の判断 → 承認済みの queue → 常駐 worker の公開
→ 指標 → 学習 (T5) → 次の提案
```

**人の承認が防火壁である。** T6 は提案を用意するだけで、承認・却下・公開・既存の提案の
編集・承認の取り消し・承認依頼の送信はしない。保存は必ず `awaiting_approval`。承認依頼を
いつ・どう送るかは既存の digest が決める (通知窓 08:00–21:00 JST、約 60 分のまとめ、1 通 1〜5 件)。

## 使い方

```bash
# PLAN (既定。DB にもファイルにも書かない)
uv run python scripts/maintain_threads_proposal_stock.py

# 1 回だけ保守する (届いた生成結果の取り込み + 必要なら生成の依頼)
uv run python scripts/maintain_threads_proposal_stock.py --execute

# 保存した提案の学習の来歴を監査する (読むだけ)
uv run python scripts/audit_threads_proposal_guidance.py --proposal-id 12
```

常駐 worker に載せるときは `--maintain-proposal-stock` を付ける (既定では **無効**。
本番のタスクのプロファイルには入れていない)。

## 在庫の区分

1 件の提案は必ず 1 つの区分に入る (上から順に判定)。

| 区分 | 意味 | 使える在庫か |
| --- | --- | --- |
| `published` | 公開済み | いいえ |
| `rejected` / `superseded` | 却下 / 置き換え済み | いいえ |
| `stale` | 陳腐化 (記事の変更・整合性の問題) | いいえ (見直し) |
| `expired` | `expires_at` を過ぎた | いいえ (見直し) |
| `held` | 保留 | いいえ (見直し) |
| `scheduled` | 承認済み・`not_before` がまだ先 | **はい** |
| `approved_unpublished` | 承認済み・未公開 | **はい** |
| `requested` | 承認待ち・携帯の依頼が有効 | **はい** |
| `prepared` | 承認待ち・まだ依頼していない | **はい** |

目安の下限・上限は T4.2 と同じ計算 (日次の目安 × 在庫日数 = **3 と 15**)。**ノルマではない。**

## 作るかどうか (スコアは無い)

- 使える在庫が下限 (3) 未満 → 足りない分だけ。
- 下限以上・上限未満で、使える在庫が 1 つの記事/トピックに偏っている → 1 本だけ。
- それ以外 → 作らない。理由を必ず出す (例: 「usable stock 5 is within the advisory range
  3-15 and 3 topics are represented」)。
- 生成の依頼が答えを待っている間は、新しい依頼を出さない。
- **1 回の上限は 3 本** (`proposal_stock.max_new_proposals_per_cycle`。ポリシーでも 3 を
  超えられない)。1 記事につき 1 本。足りなければ次の回に評価し直す。

## 選び方 (決定的)

**記事**: 使える在庫を既に持つ記事は選ばない。(クールダウン 7 日の外か, 最後に使った
時刻が古い順, T5.5 のトピックの参考, 記事 ID) で並べ、この回の中ではまず違うトピック
から選ぶ。一度も使っていない記事が先頭に来るので、どの記事も永久に後回しにはならない。
WordPress の記事には触れない。

**切り口**: 使える在庫と最近 10 本の公開で **少ない** 切り口から。この回の中で同じ切り口を
2 回選ばない。同じ記事で 30 日以内に使った切り口は避ける。T5.5 の参考は同数のときの
並べ替えにだけ使う (多様性が学習より強い)。いまの本番は中立なので、編集上の多様性だけ。

**リンク**: 単体で価値のある投稿 (`none`) が基本。使える在庫と最近の公開でリンク付きの割合が
1/3 未満のときだけ、この回の 1 本を `article` にしてよい (prompt には目安として書くだけで、
検査はしない)。`note` はまだ作らない (システム全体で対応していないため)。

## 重複

- 既存: 同じ proposal hash、同じ記事の生きた提案と正規化して同じ本文 (T2)。
- T6 で追加: **別の記事でも**、生きている提案 (承認待ち・承認済み) と公開済みの本文を
  正規化して同じなら保存しない。理由つきでスキップし、通知はしない。
- 同じ記事 × 同じ切り口は 30 日間選ばない (計画の段階で抑える)。

## 見直し (自動では何もしない)

`held` / `expired` / `stale` と、14 日を超えて承認待ちの `prepared` を PLAN に「re-review」
として出す。消さない。古いというだけで却下しない。期限の無い (evergreen) 提案は失効しない。

## provider

生成の本文は **人が外部で作る** のが既定 (このリポジトリの方針: 追加の実費 0 円、secret を
増やさない)。T6 はその境界の外側を自動化する。

| provider | 状態 | すること |
| --- | --- | --- |
| `manual` (既定) | 有効 | `data/threads-generation/pending/<id>.prompt.txt` を書く。人がそれを外部で実行し、`pending/<id>.response.json` に JSON を置く。次の保守が取り込む |
| `disabled` | 無効 | 何も呼ばない (自動 LLM の場所) |

ファイル: `pending/` (待ち)、`done/` (取り込み済み・作った提案の ID)、`failed/` (理由つき。
自動では再試行しない)、`status.json` (最後の保守の要約。T7 が読む)。`data/` は git の管理外。

自動の LLM provider を有効にするには: 実費と secret の扱いを人が決め、
`ThreadsProposalGenerationProvider` (submit / pending / collect / complete) を実装し、
ポリシーの `proposal_stock.provider` で選ぶ。出力は必ず同じ検査と `awaiting_approval` を通る。

## 失敗

| 失敗 | 結果 | 通知 |
| --- | --- | --- |
| 出力が壊れている・切り口が合わない・検査を通らない | その依頼を `failed/` へ。提案は作らない | warning (`threads_proposal_stock:invalid_output`) |
| 重複だけ | `failed/` へ | なし |
| 保存の失敗 (DB) | 何も承認されない。依頼は残り、次の回に再試行 | error (`threads_proposal_stock:save_failed`) |
| provider の失敗 | 依頼を出さない | warning (`threads_proposal_stock:provider_failed`) |
| 72 時間答えが無い | `failed/` へ (stale)。次の回に必要なら新しく依頼 | warning (`threads_proposal_stock:request_unanswered`) |
| 生成結果はあるが migration 前 | 取り込まずに待つ | warning (`threads_proposal_stock:migration_required`) |

通知は既存の `OperationsAlertService` (同じ fingerprint は cooldown の間 1 回だけ)。
どの失敗も queue・承認・公開には触れない。

## 常駐 worker

- 仕事の名前は `proposal_stock_maintenance`。既定で無効 (`--maintain-proposal-stock` で有効)。
- 6 時間ごと (`interval_minutes: 360`)。queue の中身が変わったら前倒しするが、前回から
  60 分 (`min_interval_minutes`) より早くはしない。
- 起動直後に 1 回だけ動く。止まっていた間の分をまとめて作らない (1 回 3 本の上限と、
  答え待ちの依頼があれば出さない規則がそのまま効く)。
- 新しい提案ができたら、digest の評価を前倒しする (送るかどうかは T4.2 が決める)。
- 生成は夜でも動いてよい (公開しないため)。通知窓と公開窓はそのまま。T5.5 の時間帯の
  所見で公開の時刻を決めることはしない。

## 本番の migration と最初の運用 (チェックリスト)

本番 DB は `33d93394f342` のまま。T6 は migration 前でも安全に PLAN できるが、提案の保存と
生成の依頼はしない。**以下は人の明示の許可を得てから行う。**

1. 許可を得る (この手順は本番のスキーマを変える)。
2. DB を退避する: `affiliate_ai.db` をコピーする (worker が書いていない瞬間に)。
3. `uv run alembic upgrade head`
4. 確認:
   - `uv run alembic current` → `afc2f36bb3ca (head)`
   - `uv run alembic check` → No new upgrade operations detected
   - `PRAGMA integrity_check` → ok、`PRAGMA foreign_key_check` → 何も出ない
   - `threads_post_proposals` に `learning_guidance_json` がある
5. 常駐 worker の状態を確かめる (ログに ERROR が無い・heartbeat が新しい)。列は deferred
   なので、再起動は必須ではない。
6. `uv run python scripts/maintain_threads_proposal_stock.py` (PLAN) で
   `migration = ... present; saving allowed` と、計画された依頼を確かめる。
7. 最初の 1 回は手で: `--execute` → `data/threads-generation/pending/*.prompt.txt` を外部で
   実行 → `*.response.json` を置く → もう一度 `--execute` → 提案が `awaiting_approval` で
   保存されたことと、`audit_threads_proposal_guidance.py` が `match` を返すことを確かめる。
8. 承認 digest が通知窓の中で届き、個別に承認・却下できることを確かめる。
9. 問題が無ければ、worker に `--maintain-proposal-stock` を足すかを人が決める
   (タスクのプロファイルの変更は別の判断)。
