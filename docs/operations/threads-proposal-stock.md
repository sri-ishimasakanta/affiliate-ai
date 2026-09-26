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

常駐 worker に載せるときは `--maintain-proposal-stock` を付ける (コードの既定では無効)。
本番では 2026-09-26 から `publish` プロファイルに入れて **有効** (下の「本番での有効化」)。

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

<!-- state-corrected: 2026-09-26 T7B: this line said the production DB was still at 33d93394f342; the live DB is at afc2f36bb3ca (head) -->

**済み (2026-09-25、T6 の本番の確認)**: 本番 DB は `afc2f36bb3ca` (head) にある。以下は
そのとき人の許可を得て行った手順の記録。当時の本番 DB は `33d93394f342` で、T6 は
migration 前でも安全に PLAN できるが、提案の保存と生成の依頼はしなかった。**以下は人の明示の許可を得てから行う。**

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

## T6.1 の強化

### 届いた答えを取り込むだけ (collect-only)

T6 の試行では、答えが届いた依頼を取り込みたいだけなのに、在庫がまだ下限より少ないため
通常の `--execute` が次の依頼まで出しうる状態だった (試行では submit を拒む仮の provider で
回避した)。いまは明示の collect-only がある:

```bash
# PLAN: 取り込める答えの数と、依頼を出さないことを表示する (何も書かない)
uv run python scripts/maintain_threads_proposal_stock.py --collect-only

# 届いた答えを取り込むだけ。新しい生成の依頼は出さない
uv run python scripts/maintain_threads_proposal_stock.py --collect-only --execute
```

**再送しない保証**: collect-only では、依頼を出す段階そのものに入らない
(`ThreadsProposalStockService.maintain(collect_only=True)` は provider の `submit` を
呼ぶ経路を持たない)。在庫が下限より少なくても、答えが 1 つも無くても同じ。PLAN は
`mode = collect_only (submission suppressed ...)`・`would request = 0` と表示する。
取り込みの検査・重複の判定・`awaiting_approval` での保存・1 回 3 本の上限・失敗の扱い・
通知は通常と同じ。`status.json` には `mode` が残る。オプションを付けない既定の動作は変わらない。

### 文体の警告は URL を数えない

「問いかけの数」「60 文字を超える文」「文の数」「丁寧語の連続」「絵文字」は **助言の警告** で、
URL の範囲を文章として数えない (`validators.style_analysis_text`)。URL の `?` は問いかけでは
なく、長い URL は長い文ではない。URL の直後の句読点 (`。` や末尾の `?`) は文章として数える。
**変えていないもの**: 公開される文字列、文字数の上限 (URL 込み)、リンク・ドメインの検査、
禁止表現 (本文そのものを見る)、重複の判定。既に保存された提案の警告は作り直さない。

### 承認ページは本文そのものを見せる

携帯の承認ページは Threads の提案で本文の枠が空になっていた (中継の許可リストが
`publish_text` を落とし、ページが C9 用の `inserted_paragraph` を描いていた)。修正後は
「投稿される本文」に保存済みの `content_text` をそのまま表示し、本文が無い・空のときは
承認ボタンを出さず、中継も承認を拒否する (fail closed)。中継 (mu-plugin) の再配置は
人の承認が要る作業で、2026-09-25 に行った (次の節) — 手順と記録は
`wordpress/mu-plugins/bizfluxlab-approval-relay.README.md` の「Redeploying after T6.1」。

### T6.1 本番デプロイの記録 (2026-09-25、人の許可あり)

| 項目 | 内容 |
| --- | --- |
| コミット | `4fea211` (承認ページ) / `aea2bca` (URL と文体の警告) / `cff7a67` (collect-only) |
| デプロイ前 | 5239 tests passed、ruff・`alembic check` clean、スキーマ変更なし |
| 中継 | 人が XServer の 2 ファイルを PC へ退避 (`D:\Backups\affiliate-ai\relay-pre-t6.1\`、`09a4a39` と一致) してから HEAD の版に置き換えた |
| 中継の確認 (読むだけ) | 実在しないセッション ID の承認ページの枠を読み戻し、手元で描いた各版の script と比較。前: `09a4a39` と一致。後 (18:26 JST): HEAD と一致、`reviewModel`・「投稿される本文」あり、セキュリティヘッダーと `robots.txt` は変化なし。18:27:46 の worker の署名付き同期も正常 |
| 実物の携帯表示 | **確認済み (2026-09-26、人が実機で確認)**: 手動の在庫補充で作った提案 #8〜#10 の本物の承認依頼 (digest #3、18:39 JST 送信) を携帯のブラウザで開き、提案 #10 のレビュー画面で「投稿される本文」に本文が表示され、目立つ途切れが無く、承認・却下のボタンが通常どおり表示されることを確かめた (スクリーンショットはリポジトリに置かない)。デプロイの時点ではレビューのセッションが無かったため、表示と `review_text_missing` の拒否はテストでだけ確かめていた |
| worker の再起動 (1 回) | 旧 PID 12512 (14:02:41 起動)。タスクの停止は `cmd.exe` だけを止め、uv / python が残ったため、その 3 つの PID を止めた (18:28:49)。旧いロックが古くなる 18:42:46 を待って、タスクを 1 回だけ開始 (18:43:42)。新 PID 16200 が `reclaimed_stale=True` でロックを取得。18:40 の定期トリガーの起動は `already_running` で何もせず終了 |
| タスクの定義 | 変更なし: `run_threads_worker_task.cmd publish` (`--resident --collect-insights --sync-approvals --send-approval-digests --auto-publish`)、IgnoreNew、15 分ごと。`--maintain-proposal-stock` なし |
| 公開の規則 | 再起動後の最初の評価: `next_candidate=6 blockers=gap_not_elapsed`、次の評価 19:43:24 JST (#4 の実際の公開 17:43:24 から 120 分)。取り戻しの連続公開なし |
| collect-only | 本番で PLAN のみ: `mode = collect_only (submission suppressed)`、`would request = 0`、答えを待つ依頼 0 件。`--execute` は実行していない |
| 変えていないもの | 承認・却下 (#5 / #6 承認、#7 却下のまま)、公開 (4 件のまま)、DB スキーマ、C8、WordPress の本文、メール (送っていない)、`/go/` |

#6 は、常駐 worker が既存の承認と通常の規則 (19:43:24 JST 以降・公開窓の中・1 サイクル 1 件)
で公開する見込み。これはデプロイの操作ではない。

**worker の再起動の手順 (次回のため)**: `Stop-ScheduledTask` は起動した `cmd.exe` しか
止めない。子の `uv.exe` / `python.exe` (`run_threads_worker.py`) が残っていないかを確かめ、
残っていればその PID を止める。ロックは止めた worker が解放しないので、最後の heartbeat から
15 分経ってから `Start-ScheduledTask` を 1 回実行する (それより前に開始すると
`already_running` で終わる。定期トリガーでも 15 分以内に復帰する)。

### 本番での有効化

常駐 worker の在庫の保守 (`--maintain-proposal-stock`) は **有効 (2026-09-26 20:25 JST から)**。
有効にする前の条件 (承認ページの本文表示を本物の承認依頼で確かめる) を 2026-09-26 に満たし
(上の表)、人が有効化を承認して、ランチャーの変更と worker の再起動を人が行った。

| 項目 | 内容 |
| --- | --- |
| ランチャー | `publish` の `FLAGS` に `--maintain-proposal-stock` を追加 (人が編集) |
| 再起動 | 旧 PID 16200 → 新 PID 4840 (20:25:02 JST 起動、`reclaimed_stale=True`)。起動の記録の capabilities に `maintain_proposal_stock`、`can_publish=True` |
| 最初の保守 (20:25:06) | `usable=2 needs_generation=True requests_created=1 created=0 provider=manual`。次は 2026-09-27 02:25:03 JST |
| 依頼 | `10c423eb0b80f3e826b6` (記事 7、common_mistake、link article)。答え待ち |
| 公開 | 再起動で公開は起きていない (#9 は `gap_not_elapsed` のまま) |
| 変えていないもの | 自動公開 (有効)・公開窓・承認メールの窓・間隔 120 分・1 サイクル 1 件・下限 3 / 上限 15 / 1 回 3 件・保守の間隔 (360 分・最短 60 分)・タスクの定義・ポリシー |

**ログの `pending_requests` の意味**: 保守の 1 回の中で、答えの取り込みの後・新しい依頼の前に数えた値 (その回に出した依頼を含まない)。最初の保守の `pending_requests=0 requests_created=1` は「待っていた依頼は 0 件、この回に 1 件出した」で、回の後の待ちは 1 件。

**`status.json` の意味**: 最後の保守の時点の要約。保守の間に公開・承認があると古くなる。在庫の数は DB が正 (プロジェクトの状態の報告は `recorded` として扱う)。

**有効にする方法 (既存の仕組み。2026-09-26 にこの手順で有効にした)**: ランチャー `scripts/run_threads_worker_task.cmd` の
`publish` プロファイルの `FLAGS` に `--maintain-proposal-stock` を足し、常駐 worker を 1 回
再起動する (フラグは起動のときにだけ読まれる)。タスクの定義 (`publish` を渡すだけ)・ポリシー・
間隔は変えない。再起動は上の「worker の再起動の手順」に従う。

- ランチャーは cmd.exe が **実行中に** バイト位置で読み直すので、worker を止めてから書き換える
  (動いている間に書き換えると、再開した行がずれることがある)。
- 保守は起動直後に 1 回動き、その後は 6 時間ごと (queue が変われば前倒し、60 分より早くはしない)。
- provider は `manual` (`ManualFileProvider`、非同期)。保守がするのは `pending/<id>.prompt.txt`
  の依頼を書くことと、置かれた `response.json` を次の回に取り込むことだけ。**文章は自動で
  作られない** — 人 (または人が頼んだ作業) が依頼に答えるまで提案は増えない。答えの無い依頼は
  72 時間で `failed/` (stale) になり、その間は新しい依頼を出さない。

## 自動生成 (T6.2: OpenAI GPT-5.6 Luna)

**GPT-5.6 Luna を投稿案の主な生成器にする** (人の決定、2026-09-26)。実装は済み。
本番で有効にするのは **別の確認点** (人が API の契約・鍵・費用の上限を用意し、設定を変えて
worker を 1 回再起動する)。それまで本番は manual のまま。

```
在庫が下限より少ない → 生成の依頼 (manual と同じ prompt・同じファイル)
→ OpenAI Responses API (gpt-5.6-luna、reasoning medium、Structured Outputs の strict な JSON schema)
→ いつもの検査 + 自動生成の事実の検査 → awaiting_approval で保存
→ いつもの digest → 人の承認 / 却下 → いつもの自動公開の安全の条件
```

- **モデルの出力は信用しない入力である。** 決定的な検査が正: T2 の形・長さ (500 字)・切り口・
  リンク・文体・禁止表現・重複 (別の記事も含む)・T5.5 の来歴と指紋、に加えて自動生成にだけ
  `app/social/threads/fact_guard.py` (記事に無い価格・割合・単位つきの数、収益の主張、作られた
  体験、秘密らしい値) をかける。検査を通ったものだけが提案になる。
- **人の承認は必須のまま。** OpenAI の答えも、検査を通った案も、digest の送信も、承認ではない。
  provider は公開も承認も digest の送信もできない (コードを持たない。テストで固定)。
- 呼び方 (`app/services/threads_openai_provider.py`): `POST https://api.openai.com/v1/responses`
  (httpx。鍵は `Authorization` ヘッダーにだけ置く。`store: false`)。
- 通信の失敗 (timeout・接続・429・5xx) は最大 2 回まで再試行 (2 秒・4 秒、`Retry-After` は 30 秒
  まで守る)。400・401・403・404 は再試行しない。出力が JSON でない・拒否・未完了は再試行しない。
- 検査落ちは 1 回だけ書き直させる (検査の結果を短く渡す)。書き直しも通信の再試行は 2 回まで。
- 上限の後・鍵が無い・自動生成を止めたとき: その依頼は **普通の manual の依頼として残る**
  (`pending/<id>.request.json` と `.prompt.txt`。検査落ちの答えは `<id>.openai-rejected.json` へ
  外す)。人が `response.json` を置けば、次の保守が取り込む。worker は止まらない。
- 依頼ごとの記録 `pending/<id>.openai.json`: 呼び出しの回数・結果 (`generated` / `repaired` /
  `failed:<分類>` / `rejected_by_validation` / `misconfigured`)・モデル・usage (input / output /
  total tokens だけ)。鍵・認証ヘッダー・エラーの生の本文は残さない。呼ぶ **前** に書くので、
  同じ依頼を 2 度送らない (再起動をまたいでも)。

### 費用の上限

- 依頼は在庫が下限 (3) より少ないときだけ。1 回の保守で最大 3 件、1 記事 1 件。上限 15。
- 保守は 360 分ごと (queue が変われば前倒し、60 分より早くはしない)。heartbeat では呼ばない。
- 答え待ちの依頼があれば、新しい依頼 (= 新しい呼び出し) を出さない。
- 依頼 1 件あたり: 最初の呼び出し 1 回 + 通信の再試行 2 回 + 書き直し 1 回 (+ その再試行 2 回)。
  最大 6 回、普通は 1 回。
- 既にある manual の依頼 (例: `10c423eb0b80f3e826b6`) は、保守では自動で送らない。送るのは人が
  明示したときだけ (`ThreadsProposalStockService.generate_pending(<id>)`。これも冪等)。
- **API の費用は別** (OpenAI との契約。このリポジトリの「追加の実費 0 円」の既定から外れる人の
  決定)。OpenAI の管理画面で月の上限 (budget) を設定しておく。

### 設定と戻し方

`.env` (コミットしない) で選ぶ。ポリシーの `proposal_stock.provider` (コミット済み `manual`) より
環境変数が優先する:

```
THREADS_GENERATION_PROVIDER=openai
THREADS_GENERATION_MODEL=gpt-5.6-luna
THREADS_GENERATION_REASONING_EFFORT=medium
OPENAI_API_KEY=<人が用意する鍵>
```

設定は worker の起動のときに読まれるので、変えたら worker を 1 回再起動する (上の「worker の
再起動の手順」)。**戻し方**: `.env` の `THREADS_GENERATION_PROVIDER=manual` (または空) にして
worker を 1 回再起動する。依頼のファイルの形は同じなので、答え待ちの依頼はそのまま manual で
答えられる。

プロジェクトの状態の報告は `threads.generation` に provider・自動生成 (`enabled` / `disabled` /
`misconfigured`)・モデル・manual の答え待ちの数・最後の生成の時刻と結果を出す (鍵は読まない)。
