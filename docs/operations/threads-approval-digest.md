# Threads 承認依頼のまとめ送りと在庫 (T4.2)

T4.2 は **自動公開のフェーズではない**。作ったのは、承認を 1 通のメールにまとめて
依頼する仕組みと、承認済みの提案を queue に貯める仕組みだけである。

- 投稿しない (自動公開はコードで無効のまま)
- WordPress に書かない、`/go/` に触れない、タスクスケジューラを変えない
- 承認依頼メールは、人が明示したとき (`--execute` / `--send-approval-digests`) だけ送る

## 承認は公開ではない

人の承認が意味するのは **「この文面は出してよい」** であって、「今すぐ出せ」ではない。

```
提案 (在庫) → 承認依頼 (digest) → 人の承認 → 承認済み queue → (T4.3) 公開の判定
```

携帯での承認もメールも、T3 の公開を呼ばない。承認されると `approved_at` が記録され、
提案は queue に入るだけである (テストで固定: 公開の行も試行も作られない)。

## 在庫の状態

提案の状態機械 (T2) はそのまま使い、本文も複製しない。在庫の状態は既存の値の
組み合わせで表す:

| 在庫での状態 | 判定 |
| --- | --- |
| 準備済み (依頼待ち) | `awaiting_approval` で、有効な承認依頼 (pending のセッション) が無い |
| 依頼中 | `awaiting_approval` で、pending のセッションがある |
| 承認済み | `approved` (`approved_at` あり) |
| 却下 | `rejected` |
| 保留 | `held_at` あり |
| stale / 置き換え | `stale` / `superseded`、または記事が変わった |
| 期限切れ | `expires_at` を過ぎた (消さない) |
| 公開済み | T3 の `threads_publications` に行がある |

在庫の目安 (助言のみ、ノルマでも上限でもない): 承認済みで未公開の提案が、1 日の
目安 (3–5 本) の 1–3 日分 = 3–15 件。15 件以上あるときは新しい依頼を **先送り**
する (消さない)。3 件を下回ると「提案を作り足す」ことを勧める。

提案は数百件も作らない。自動生成もしない (提案づくりは T2 の手動の流れのまま)。

## 承認の時刻 (権威ある記録)

| 列 | 意味 | 書かれる時 |
| --- | --- | --- |
| `threads_post_proposals.approved_at` | 人の承認がシステムに **受理された** 時刻 | 携帯の決定を取り込んで承認に遷移したとき |
| `threads_post_proposals.approval_request_sent_at` | 最新の承認依頼が **実際に届いた** 時刻 | メールの配送が `sent` のときだけ |
| `mobile_approval_sessions.request_sent_at` | そのセッションの依頼が届いた時刻 | 同上 |

`updated_at` からは推測しない (無関係な変更でも動くため)。T4.1 は queue の順序に
「携帯承認の decided_at、無ければ updated_at」を使っていたが、T4.2 から `approved_at`
だけを使う。

### 過去の行の埋め方 (migration `949edb90023f`)

権威ある既存の記録があるときだけ埋め、無ければ **NULL のまま**:

1. `mobile_approval_sessions.request_sent_at` ← そのセッションの配送記録が
   `outcome='sent'` のときの `finished_at` (SMTP が受け付けた後に刻まれる)
2. `threads_post_proposals.approval_request_sent_at` ← その提案のセッションで実際に
   届いた最新の `request_sent_at`
3. `threads_post_proposals.approved_at` ← `decision='approved'` かつ
   `state='synchronized'` のセッションの `decided_at`。これは決定をローカルへ受理した
   時刻で、T4.2 のコードが承認時に書く値と同じ意味を持つ

本番の提案 1 は、セッション 3 と配送 4 から `approved_at = 2026-09-23 18:07:09.831022 UTC`、
`approval_request_sent_at = 2026-09-23 18:06:05.311896 UTC` が埋まった。`updated_at` は
変わっていない。

## 承認の期限 (TTL)

**期限は、依頼を実際に送る操作の時刻から数える。** 提案を作った時刻からは数えない。

- レビューセッション (と capability) は、digest を送るその時にだけ作る
- 期限 = 送信の操作の時刻 + 24 時間 (中継の既定値。`ttl_hours` で設定)
- 夜のうちに作った提案は、朝に送られるまで期限を使わない
- メールが届かなければ、作ったセッションはすべて失効させ、提案は在庫に戻す
  (`approval_request_sent_at` は書かない)
- 期限が切れた pending のセッションは `expired` にし、提案は改めて依頼できる

提案ごとに独立して失効できる。digest の中の A が stale・期限切れになったら、
A のセッションだけを失効させ、B / C には触れない。

## まとめ送り (digest)

**1 通のメールに、通常 3–5 件の提案。** 1 件だけの 1 通もありうる。5 件を超える
ときも 1 通は 5 件までで、残りは在庫に残して次の機会に回す。

### いつ送るか

固定の時刻表 (08:00 / 09:00 / 10:00 …) は持たない。乱数も使わない。

- **通知窓 08:00–21:00 JST の外では送らない** (依頼も催促も)。夜の間も提案づくり・
  在庫の整理・queue の評価・指標の取得はしてよい
- **cooldown 60 分**: 前の digest から最低 60 分空ける
- **gather 60 分**: 最初に依頼の価値が生まれた提案は最大 60 分待ち、その間に増えた
  提案を同じ 1 通に入れる
- 5 件そろっていれば gather を待たない (cooldown だけ守る)
- gather の終わりが 21:00 を過ぎれば、翌 08:00 に繰り越す

例: 前回 10:00 に送信。13:05 / 13:22 / 13:41 に 3 件 → **14:05 に 1 通**。

朝 08:00 には、夜の間にできた提案を **評価し直してから** 選ぶ (期限切れ・stale・
重複・保留を除く)。

### 何を入れるか (名前の付いた規則だけ)

見送り (在庫には残る): `held` / `expired` / `stale` / `content_integrity` /
`already_requested` / `duplicate` (承認済み・公開済み・先に並んだ提案と同じ文面)

先送り (次の機会): `over_digest_limit` / `approved_stock_sufficient`

選ぶ順:

1. 期限 (`expires_at`) が近いもの
2. 1 通の中で記事と切り口がなるべく重ならないように
3. 作られた順

似た提案を黙って書き換えることはしない。見送った理由は digest の記録に残る。

### メールの中身

提案ごとに: 短い ID (`#12`)、記事、切り口、本文の短い抜粋、時期 (not_before /
expires_at があれば JST)、**その提案専用の** 確認リンク。

メールには一括承認のリンクも、ワンクリック承認のリンクも無い。

## 携帯での確認 (V1 の形と、その理由)

V1 は **1 通のメールに、提案ごとの確認リンクを並べる** 形にした。リンクを開くと
既存の確認ページ (C8.8) が 1 件分表示され、そこで承認・却下を明示的に選ぶ。

「1 つのリンクから、全件を 1 ページで見る」形は作っていない。理由:

- 中継 (WordPress の mu-plugin) は 1 セッション = 1 提案で作られている。1 ページで
  複数件を扱うには、**digest 単位の新しい capability** と新しいページ・経路が要り、
  中継の PHP を書き換えて本番に再配備する必要がある
- 確認用の cookie は 1 つ (`bfl_approval_review`) で、nonce はセッションごとに保存・
  照合される。同時に複数件を開くと、最後に開いたもの以外の決定は **安全に失敗する**
  (別の提案を誤って決めることはない)。1 ページ化にはこの設計も変える必要がある

安全を優先し、capability の設計は一切弱めていない。一覧ページが必要なら、別の
フェーズで中継の変更と再配備を人が承認してから行う。

## 決定は 1 件ずつ

- A を承認、B を却下、C は未決定 — それぞれ独立に記録される (テストで固定)
- 1 件の却下は digest 全体を無効にしない
- 一括承認は V1 に無い
- どの決定も `mobile_approval_events` に残る

## セキュリティ (C8.8 の性質はそのまま)

- GET は決定しない。決定は明示的な POST だけ
- CSRF / 確認用セッション (cookie + nonce) はそのまま。nonce はセッションごと
- capability は 256 bit。DB と中継には digest だけを保存し、生の値はメール本文にしか無い
- 1 回限り・期限付き。失効・期限切れの capability では決定できない
- セキュリティヘッダ、noindex / robots.txt の除外もそのまま
- 中継の秘密と runtime の秘密は別のまま。秘密の値はログに出さない

中継の PHP テスト (74 件) は、T4.2 で「nonce をセッションごとに保存・照合する」ことを
追加で固定した。中継のコード自体は変えていないので、再配備は不要。

## 人の queue 操作

```bash
uv run python scripts/manage_threads_queue.py show 12
```

```bash
uv run python scripts/manage_threads_queue.py hold 12 --reason "数字を確認してから"
```

```bash
uv run python scripts/manage_threads_queue.py release 12
```

```bash
uv run python scripts/manage_threads_queue.py prefer 12 --reason "今日の話題に合う"
```

```bash
uv run python scripts/manage_threads_queue.py timing 12 --not-before 2026-10-01T09:00 --expires-at 2026-10-03T21:00 --reason "イベント期間"
```

時刻にタイムゾーンが無ければ Asia/Tokyo とみなす。保存は UTC。

- **hold**: 選ばれなくする (理由が必須)。release で戻る
- **prefer**: 並び順の先頭に来るだけ。却下・stale・期限切れ・not_before・中身の
  不整合・T3 の不確定・公開窓・間隔・公開済み の **どれも飛ばさない**。優先した
  提案が止まっていれば理由を示し、他の候補を通常どおり評価する
- **timing**: `not_before` より前は資格が無い。`expires_at` を過ぎたら資格が無い
  (消さない)。常緑の提案には付けない。古いというだけで期限にはならない
- どの操作も `threads_queue_control_events` に誰が・いつ・なぜを残す

## 指標の取得 (常駐 worker と C8 の関係)

T4.2 で、常駐 worker が **読むだけ** で指標を取得できるようになった
(`--collect-insights`)。取得は C8 と同じ `ThreadsInsightsService.collect()` を通る。

- 欠測は NULL のまま、失敗は失敗の観測として残る (C8 と同じ)
- 低い数字ではアラートを出さない (C8 と同じ)
- **どちらかが 10 分以内に観測した投稿は、もう一方が取りに行かない**
  (`min_observation_spacing_minutes`)
- 若い投稿は 30 分、1–3 日目は 60 分、成熟後は 6 時間おき、14 日で停止
- C8 の日次取り込み (`import_threads_insights`) は **残す**。worker が止まっていても、
  少なくとも 1 日 1 回は観測される安全網になる

## 2026-09-24 11:06 UTC の 2 本目の観測

C8 の日次タスク (`\affiliate-ai-operations-daily`) の実行で説明がつく:

- タスクの前回実行時刻: `2026/09/24 20:06:22` (JST) = 11:06:22 UTC
- operations run 6: `trigger=scheduler`、`started_at = 2026-09-24 11:06:30.364012`
- 観測 2 の `observed_at = 2026-09-24 11:06:30.364012` — run の開始時刻と **完全に一致**
  (runner は同じ `now` を各ステップに渡す)
- その run の `import_threads_insights` ステップ: `imported=1`

06:30 の予定時刻に PC が動いておらず、`StartWhenAvailable` で 20:06 に実行された
ものと考えられる。未知の収集経路は無い。

## 本番パイロットの前に人が確認すること

T4.2 の実装中、本番では digest を送っていない (レビューセッションも作っていない)。
パイロットの手順は最終報告に書いた。送る前に:

1. 在庫に依頼の価値がある提案があること (いまは 0 件。まず提案を作る)
2. 通知窓 (08:00–21:00 JST) の中であること
3. PLAN の出力で、選ばれる提案・期限・メールが 1 通であることを確認すること
4. 常駐 worker を動かしていないこと (動いていれば `--execute` はロックで止まる)
