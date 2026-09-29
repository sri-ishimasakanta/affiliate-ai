# Threads の Growth Post (T6.3.3)

アカウントそのものを紹介し、フォローを呼びかける投稿 (Growth Post) を **毎日 1 本まで** 用意する。
記事から作る通常の投稿に **足す** もので、通常の本数・在庫・計画・公開の規則は変えない。

**状態: 本番で有効 (2026-09-28 11:40 JST から、人の許可あり)。** migration `c4d2e8f1a9b3` は
2026-09-28 10:40 JST に適用済み。ランチャーの publish に `--maintain-growth-posts` を足し
(`965afbd`)、制御された再起動で worker (pid 19820) が読んだ。最初の Growth Post は提案 #25
(2026-09-28、`account_identity`、awaiting_approval。人の承認待ち)。

## 人の決定 (2026-09-28)

1. Growth Post は JST の 1 日に多くても 1 本。
2. 通常の記事の投稿の本数に **足す** (置き換えない・減らさない)。
3. 本文に自然に入れるもの: このアカウントが何をしているか / いまの短い目標 / フォロー・
   つながりのお願い / こちらからもフォローを返すこと。
4. 最初の目標は **フォロワー 100 人**。
5. 実際のフォロー返しは **人が手で行う** (このシステムはフォローしない。文章を作るだけ)。
6. トピック "AI Threads" は付けない。(T6.3.3b で変更: 次の Growth Post から、トピック
   "インサイト祭り" を付ける。下の記録)
7. 記事の URL は付けない (`link_mode=none`)。
8. 公開の前に **人の承認** が要る (通常の投稿と同じ)。
9. 1 日を逃しても、次の日にまとめて出さない (取り戻さない)。

## 方針 (`app/social/threads/growth.py`)

| 項目 | 値 |
|---|---|
| 1 日の本数 | `GROWTH_POST_TARGET_PER_JST_DAY = 1` (上限でもある) |
| 作ってよい時刻 | JST 07:00 から (その日の最初の保守で) |
| 公開してよい時間 | その日の 07:00〜24:00 (`not_before` / `expires_at`) のうち、公開窓 07:00〜23:00 の中。**記事の 120 分の間隔と 1 回 1 本の枠は使わない** (T6.3.3a の足し分の枠) |
| 目標 | `GROWTH_FOLLOWER_TARGET = 100` (人が決めた。自動で変えない) |
| 長さ | 本文 120〜280 字くらい (80〜320 字を外れたら書き直し)。Threads の上限 500 字は同じ。T6.3.1 の 280〜360 字は使わない |
| 絵文字 | 3 個まで |
| トピック | "インサイト祭り" (T6.3.3b から。`THREADS_GROWTH_TOPIC_TAG`、公開のときに付ける)。それより前に出た #25 はトピックなしのまま |
| リンク | なし (`link_mode=none`、URL・`{link}`・UTM・/go/ を書かない) |

値は版管理された定数 (秘密ではない)。`.env` には置かない。

## データ (migration `c4d2e8f1a9b3`)

- **記事から作る投稿**: `content_kind` の印なし (または `article`) + `source_article_id` あり。これまで通り。
- **Growth Post**: `learning_guidance_json.content_kind = "account_growth"` + `source_article_id = NULL`。
  印は必須 (本文から推測しない)。`angle` の列は `account_growth` (記事の切り口と混ざらない)。
  切り口 (`growth_angle`)・日付 (`date_jst`)・目標・フォロワーの観測・最近の Growth Post との比較は
  `learning_guidance_json.growth` に残す。`source_article_body_hash` にはアカウントの紹介の hash を
  入れる (紹介が変われば古い Growth Post は stale)。
- **それ以外** (印なしの NULL・記事ありの `account_growth`・未知の印): 公開しない。
- migration は `threads_post_proposals.source_article_id` と `threads_publications.source_article_id`
  を NULL にできるようにするだけ。外部キー・一意制約・CHECK・索引・既存の行は変えない。記事の必須は
  コードで守る。戻す (downgrade) ときに記事の無い行が 1 つでもあれば戻さない。
- **migration の前の DB でもコードは安全**: Growth Post の保存だけが「migration 待ち」で止まる
  (何も書かない・何も呼ばない)。Project State はこれを「本番で未適用 (宣言済み)」として区別する
  (`PENDING_PRODUCTION_MIGRATIONS`。本番に適用したので今は空)。

## 1 日 1 本の守り方 (`ThreadsGrowthService`)

1. その日 (JST) の Growth Post の提案が 1 つでもあれば、状態を問わず作らない。
2. その日の生成の記録 (`data/threads-growth/<日付>.openai.json`) があれば、結果を問わず 2 度と
   呼ばない (呼ぶ前に記録を書く。途中で落ちても呼び直さない)。
3. 再起動しても同じ (記録は DB とファイルにある)。
4. 前の日の提案は `expires_at` を過ぎると公開されない (queue の `expired`、まとめ送りでも依頼しない)。
   **人の判断は書き換えない** (却下にしない)。今日の分が 2 本になることはない。
5. Growth の枠は、その日に Growth Post を公開したら次の Growth Post を `growth_daily_limit` で止める。
6. (T6.3.3a で変更) 記事の queue には並ばない。下の「足し分の公開の枠」を見る。記事の投稿が多い日でも、その日の
   Growth Post は出ない (取り戻さない)。

## フォロワー数

- 使うのは Threads のアカウントの指標 `followers_count` を **実際に読んだ記録** だけ
  (`data/threads-growth/followers.json`: 値・読んだ時刻・出どころ)。Luna には推測させない。
- worker が `--collect-insights` のときだけ、生成の直前に 1 回読む (読むだけ。1 日 1 回)。
- 新しい (6 時間以内) ときだけ「現在 X 人・あと Y 人」を書いてよい。古い・無いときは目標だけ
  (「現在」「あと N」を書いたら書き直し)。
- **目標に届いた記録** (100 人以上) があれば、新しい Growth Post を作らない (生成 0 回)。
  `follower_target_reached = true` を記録・Project State に出す。**次の目標は人が決める**
  (`GROWTH_FOLLOWER_TARGET` を変えるコミット)。200・500 などを自動で選ばない。

## 事実の境界

アカウントの紹介: 「AIを使って、収益メディアをどこまで自動化できるか。実際に作りながら検証・記録
しているアカウント。」 触れてよいのは、このプロジェクトに実際にあるもの (WordPress の記事サイト・
Threads への投稿・AI (Luna) による下書き・自動化・計測と分析・これからの収益化の実験)。
記事は渡さない。

決定的な検査 (`validate`): 500 字以内 / URL・`{link}`・ハッシュタグなし / 知らない人数を書かない
(目標と、新しい観測の値・残りだけ) / 収益・金額・報酬なし / 達成・突破などの実績なし / 家族・仕事
などの個人の話なし / 利用者・顧客の数なし / 絵文字 3 個まで / アカウントの紹介 (AI と自動化) ・
目標・フォローのお願いが入っている / 最近の Growth Post (7 件) と 3-gram の包含 0.5 以上なら書き直し。
違反は 1 回だけ書き直し、それでも違反なら保存しない (その日はもう呼ばない)。

**フォローのお願い・フォロバの言葉はこの種類だけに許す。** 記事の投稿の検査 (T6.3 の
「フォローを求めない」など) は変えない。

## 切り口

`account_identity` / `goal_progress` / `build_in_public` / `community` / `mutual_growth`。
日付の SHA-256 から決定的に選び、直前の Growth Post と同じ切り口なら次の切り口にする。

## 生成と記録

Growth 専用の prompt (記事なし) → Luna (既存の OpenAI client。再送 2 回まで) → 構造化出力
(`angle` = 今日の切り口、`link_mode` = `none` の固定の enum、`body`) → 検査 → 提案
(`awaiting_approval`)。手動の答えへの切り替えは無い (鍵が無ければ呼ばずに止まる)。
記録は T6.3.1a と同じ形 (`generation_attempts=1`・`model_calls`・`repair_calls`・呼び出しごとの
出力・検査の理由の ID (`growth_*`)・token) で、`content_kind=account_growth` を持つ。記事の生成の
記録 (`data/threads-generation`) とは別の場所。

## 記事の投稿から分けるもの

- 記事の在庫 (下限 3・上限 15)・計画 (切り口・リンクの割合・記事の間隔)・最近の話題の重なり・
  学習 (T5.5) には Growth Post を数えない。
- 3〜5 本の目安 (`daily_activity`) は記事の投稿だけで数える。worker の状態には
  `article_posts_today` / `growth_posts_today` / `total_posts_today` を分けて出す。
- 承認のまとめ送り: Growth Post は記事の在庫の目安で先延ばしにしない (その日のうちに期限が来る)。
  1 通の上限 (5 件) は同じ。メールに「投稿種別: Growth Post」「目標: フォロワー100人」を出す。
  携帯のレビューページは表示する鍵を限っているので、題の欄に「投稿種別: Growth Post / 目標:
  フォロワー100人」を出す (日本語の画面の作り直しは T6.4)。
- 会話のきっかけの集計 (T6.3) では `account_growth` として別に数える。

## worker

新しい subsystem `account_growth_maintenance` (1 時間ごとに状態を見る。期限が来ていなければ
何も呼ばない)。`--maintain-growth-posts` で起動したときだけ動く (**既定は動かない**。今の
ランチャーには無い)。ログ: `event=account_growth_maintenance date=... due=... active_proposal=...
published_today=... created=... model_calls=... target=100 target_reached=... reason=...`。

手で確かめる: `uv run python scripts/maintain_threads_growth_post.py` (PLAN。書かない・呼ばない)。

## 本番への展開 (人の確認点)

前提: T6.3.2 の固定トピックが本番の通常の投稿で確かめられていること (Threads のアプリで
"AI Threads" が付いて見える)。T6.3.2 が本番で失敗していたら、先にそれを直す。

1. 人の許可を得る (本番のスキーマを変え、worker を再起動する)。
2. worker を止める (タスクを止め、worker の親子のプロセスだけを止める)。
3. DB を退避する (`affiliate_ai.db` のコピー)。
4. `uv run alembic upgrade head` → `alembic current` が `c4d2e8f1a9b3 (head)`・`alembic check` が
   clean・`PRAGMA integrity_check` が ok・`PRAGMA foreign_key_check` が空・既存の行が同じ。
5. `PENDING_PRODUCTION_MIGRATIONS` から `c4d2e8f1a9b3` を消すコミット (適用済みの記録)。
6. worker を **`--maintain-growth-posts` なし** で戻す (ロックの自然な回収)。T6.3.2 の確認が
   まだなら、ここで次の通常の投稿を確かめる。
7. `scripts/maintain_threads_growth_post.py` (PLAN) で、今日の判断 (due・理由) を読む。
8. 人が有効にすると決めたら、ランチャー (`scripts/run_threads_worker_task.cmd`) に
   `--maintain-growth-posts` を足して、もう一度制御された再起動をする。
9. 次の 07:00 以降の保守で 1 本だけ用意される → 通知の時間帯 (08:00〜) のまとめ送り →
   携帯で承認・却下 → 既存の queue が間隔と公開窓の中で公開 (トピックなし・リンクなし) →
   人がアプリで見て、フォロー返しを手で行う。

## 本番の migration の記録 (2026-09-28、人の許可あり)

| 項目 | 内容 |
|---|---|
| worker の停止 | 10:39:32〜10:39:38 JST (pid 19820 と親子のプロセスだけ。タスクの定義は同じ) |
| 退避 | `D:\Backups\affiliate-ai\affiliate_ai.before-c4d2e8f1a9b3.20260928-014003.db` (UTC の時刻、8,126,464 bytes、SHA-256 `ca34cdd5337b77bcba0c463426758cf4f5dfa474f735d2bb19da16eb3b1b9ce8`、integrity ok・foreign key 問題なし・`afc2f36bb3ca`) |
| 適用 | `uv run alembic upgrade head` → `c4d2e8f1a9b3 (head)`、`alembic check` clean |
| 確認 | integrity ok、foreign key 問題なし、54 の表のうち `alembic_version` 以外の 53 の表の行数と中身の hash が同じ、索引と制約が同じ、`source_article_id` が NULL の行は 0 (Growth Post は 0) |
| Growth Post | 有効にしていない (ランチャーに `--maintain-growth-posts` なし) |

## 本番での有効化の記録 (2026-09-28、人の許可あり)

| 項目 | 内容 |
|---|---|
| T6.3.2 | 完了 (API の受け入れと人の目の確認)。Growth Post の前提を満たした |
| ランチャー | publish の flags に `--maintain-growth-posts` (`965afbd`)。タスクの定義・間隔は同じ |
| 起動の記録 | 起動の行の capabilities に `maintain_growth_posts` が出ていなかった (固定の一覧) のを直した (`b6e839f`) |
| 再起動 | 11:19:41〜11:19:47 JST に pid 2572 の親子だけを止め、11:40:02 に自然に復帰 (pid 19820、ロックを回収) |
| 最初の保守 | 11:40:35 `event=account_growth_maintenance due=True created=25 model_calls=1`。呼び出し 1 回・書き直し 0 回・939 tokens |
| フォロワー数 | 観測の記録なし (`followers.json` が無い)。目標だけの文面。読めなかった理由は記録に残らない (後で直す) |
| 提案 #25 | `account_growth`・記事なし・link none・トピックなし・187 字・絵文字 1・`awaiting_approval` (人の承認待ち。07:00〜24:00 JST だけ公開の資格) |
| 1 日 1 本 | 同じ日の 2 回目の判断は「今日の Growth Post がある (提案 25)」で呼ばない |
| 記事への影響 | 記事の在庫 (usable 3) に数えない。記事の保守・トピック・目安は同じ |

## 今後

目標に届いたら、人が次の目標を決める。T6.4 (日本語の承認・報告メール)、T6.5 (成績の分析) で
Growth Post の見せ方と振り返りを扱う。

## 足し分の公開の枠 (T6.3.3a)

T6.3.3 の最初の実装は、Growth Post を記事と同じ queue に並べていた (承認の順・120 分の間隔・
1 回 1 本)。2026-09-28 の提案 #25 は 12:45 に承認されたが、先に承認された記事 #23・#24 の後ろで、
それぞれ 120 分を待つ並び (15:02・17:02・19:02) になり、記事の承認が増えれば当日中に出られずに
期限が来うる。また、Growth の公開が記事の間隔の起点になり、Growth の不確定な公開が記事の
queue を止める作りだった。人の決定: **Growth Post は記事の本数と間隔を使わない足し分。**

- **記事の枠は変えない**: 公開窓・120 分の間隔・1 回 1 本・承認の順・3〜5 本の目安・トピック
  "AI Threads"・失敗と照合の扱いは同じ。記事の queue と間隔の起点 (`latest_gap_basis`) は
  **記事の公開だけ** を見る。
- **Growth の枠** (`evaluate_growth_lane`、`ThreadsAutoPublisher.publish_growth_one`): 承認済み・
  今日 (JST) の分・`not_before` 以降で `expires_at` より前・公開窓の中・今日まだ Growth を出して
  いない・**どの** 公開も不確定でない。記事の間隔は見ない。出した後も記事の間隔の起点にならない。
- **いつ出るか**: 公開の評価のたびに、記事の枠 (今まで通り) の後で Growth の枠を見る。
  - 記事を出した評価の中で続けて出す (`article_companion`、組の記事の公開 id を記録)
  - 承認の取り込みで評価が前倒しされたとき (`post_approval_heartbeat`、承認から 15 分以内)
  - ふだんの評価 (`normal_heartbeat`)。承認済みの今日の Growth が時刻だけを待っていれば、評価の
    次の時刻をその時刻 (公開窓の開く時刻・`not_before`) か 30 分後に早める
- **1 回の評価の枠**: 記事は 1 回 1 本 (今まで通り)。Growth は記事の枠の外で、自分の枠で 1 回
  1 本まで (worker の核で別に数え、超えたら止める)。1 日 1 本は別に守る。
- **失敗を分ける**: Growth の公開の失敗・不確定は記録・アラートし、Growth の枠だけを止める
  (照合まで Growth を出さない。送り直さない)。**記事の queue は止めない。** 記事の公開の不確定は
  今まで通り記事の queue を止め、Growth も出さない。
- **記録**: Growth の公開の試行 (`create_container` / `publish_container`) に `lane=account_growth`・
  `growth_trigger`・`growth_date_jst`・`paired_article_publication_id`・`article_gap_applies=false`・
  トピック (なし) を残す。ログの `event=publication_evaluation` に `growth_candidate`・`growth`・
  `growth_trigger` か `growth_blockers`・`growth_publication`。worker の状態に `growth_lane`。
- 取り戻さない・人の承認・トピックなし・リンクなしは同じ。**T6.3.3 は、本番でこの枠を確かめる
  までは完了にしない。**

### 本番での確認 (T6.3.3a の API の確認、2026-09-28)

`d81a577` を読んだ worker (pid 21540、13:27 に人の許可のもとで止め、13:55:02 に自然に復帰。
ロックを回収) の最初の公開の評価で、承認済みの Growth Post #25 が足し分の枠で出た。

| 項目 | 内容 |
|---|---|
| 公開 | 提案 #25 → 公開 20 (2026-09-28 13:55:39 JST、media `18089278694679631`、https://www.threads.com/@bizfluxlab/post/Dd0X-I1mmy5) |
| きっかけ | `normal_heartbeat` (組の記事なし)。記事の枠は同じ評価で `gap_not_elapsed` のまま |
| 記録 | `lane=account_growth`・`growth_date_jst=2026-09-28`・`article_gap_applies=false`・`topic_tag_sent=false`・リンクなし |
| 本文 | 承認された 187 字そのまま (hash 同じ、読み戻しで一致)。URL・`#`・"AI Threads" なし |
| 記事の間隔 | 前後で同じ: 起点は公開 19 (13:02:25)・次は #23・15:02:25 から。Growth は起点にならない |
| 1 日 1 本 | 次の評価 (13:56:05) は `growth_daily_limit`。Growth の保守は「今日の分がある」で呼ばない |
| 今日の本数 | 記事 4・Growth 1・合計 5 (3〜5 本の目安は記事の 4 本だけ) |
| 人の目の確認 | **まだ** (Threads のアプリで本文・トピックなし・リンクなし・ふつうの投稿として見えるか) |

**T6.3.3 は本番の API の確認まで済み。人の目の確認の後に完了にする。**

**人の目の確認 (2026-09-28)**: 人が Threads のアプリで公開 20 を確かめた (承認した本文・
"AI Threads" のトピックなし・リンクなし・ふつうの単独の投稿)。**T6.3.3 は完了。**

## Growth Post のトピック (T6.3.3b)

人の決定 (2026-09-28): **次の Growth Post から、トピック "インサイト祭り" を付ける。**
記事の投稿は "AI Threads" のまま。note のトピック ("note") は note を始めるときに決める (今は作らない)。

- 決め方: `app/social/threads/topic.py` の `THREADS_GROWTH_TOPIC_TAG = "インサイト祭り"`
  (版管理された定数。`.env` ではない)。`account_growth` → "インサイト祭り"、`article` →
  "AI Threads"、未知の種類 → 公開しない。Luna はトピックを選ばない。
- **前へ進むだけ**: 公開済みの #25 (公開 20、トピックなし) と、その行・本文・hash・試行の記録・
  snapshot は変えない。承認済みで未公開の Growth Post があれば、公開のときに付く。
- トピックはメタデータ: 本文に `#インサイト祭り` も「インサイト祭り」も足さない。本文・hash・
  文字数・link_mode (none)・URL (なし)・目標 (100 人)・Growth の prompt は同じ。
- 公開の要求: Growth のコンテナ作成に `topic_tag=インサイト祭り`。記事は `topic_tag=AI Threads`。
  ほかの違いは無い。記録 (`create_container` の試行) に `content_kind`・`topic_tag`・
  `topic_tag_sent`、失敗なら HTTP の status と API のコード、成功なら media id。
- **失敗しても閉じる**: "インサイト祭り" を API が 4xx で断ったら、トピックなしで出し直さない・
  別のトピックに変えない。公開の行は `failed` + `reconciliation_required` になり、アラート
  (「Threads 側で Topic が受け付けられませんでした / Growth Post だけを止め、通常投稿は続けます」)。
  **止まるのは Growth の枠だけ** (T6.3.3a のとおり)。記事の投稿は続く。一時的な失敗の再試行は
  同じトピックで送る。
- 足し分の枠 (記事の 120 分の間隔を使わない・1 日 1 本・取り戻さない・承認が要る) は変えない。
- 承認の表示: 「投稿種別: Growth Post / 目標: フォロワー100人 / リンク: なし / トピック:
  インサイト祭り」。T6.4 の中継はすでに `topic_label` を表示するので、**WordPress の配備は要らない**。
- Project State: `threads.topic.growth_topic_tag = インサイト祭り`、
  `growth_topic_production_acceptance = pending_canary` (今のトピックで送った Growth の作成が
  受け入れられたら `observed`)。本番の確認点: worker の再起動の後の、次の自然な Growth Post。

### 本番の切り替え (T6.3.3b、2026-09-28、人の許可あり)

`c47bcd6` を読んだ worker: 16:27:11〜16:27:17 JST に pid 16164 の親子だけを止め、16:55:02 に
自然に復帰 (pid 9288、ロックを回収)。16:25 の承認のまとめ送り (#27) が終わってから止めた。
読み込んだ方針: 記事 → "AI Threads"、Growth → "インサイト祭り"、未知の種類 → 公開しない。
今日は #25 がすでに出ているので `growth_daily_limit` (Growth の生成 0 回・公開 0 件)。
**本番での受け入れはまだ** (`growth_topic_production_acceptance = pending_canary`)。確認は
2026-09-29 以降の、次の自然な Growth Post (人の承認の後に Growth の枠で公開) で行う。

## Growth の多様さと確かさ・日付のトピック (T6.3.3c)

実装と試験だけ (2026-09-29)。worker は再起動していない。本番の切り替えは別の確認点。

### 1 日の目的

**JST の 1 日に、検査を通った Growth Post の提案を 1 本** (1 回の呼び出し、ではない)。
前 (T6.3.3): 1 回の生成 + 書き直し 1 回まで。通らなければその日は終わり (2026-09-29 は
最近の #25 と似すぎて (0.543) 提案なし)。後 (T6.3.3c):

1. 資格 (07:00 JST から、今日の提案・公開がまだ無い) とフォロワーの目標 (100 人に届いたら
   止まる) を見る
2. その日の記録 (`data/threads-growth/<日付>.openai.json`) から、使った呼び出しと書き方を読む
3. 事実のそろった書き方だけを、決まった順に並べる
4. 候補を作り、検査する。通れば `awaiting_approval` の提案を 1 本作って止まる
5. 落ちたら、落ちた理由で次を決める (下)。**呼び出しは 1 日に多くても 4 回**
   (`MAX_GROWTH_MODEL_CALLS_PER_DAY`、最初・書き直し・別の書き方を合わせて)。
   上限か書き方が尽きたら、提案なしで止まる (`growth_generation_exhausted`)

提案は 1 日 1 本だけ (選ばせるための複数の提案は作らない)。公開も 1 日 1 本のまま、人の承認が
要るまま。**必ず 1 本できる、という約束ではない** (検査は弱めない。似ている度合いの上限 0.5 も
同じ)。

### 書き方 (`app/social/threads/growth_strategy.py`、`threads-growth-strategy-1`)

| 部品 | 値 |
| --- | --- |
| family | account_identity・goal_progress・build_in_public・behind_the_scenes・lesson_learned・failure_improvement・experiment・community_question・principle・next_step・milestone・mutual_growth |
| hook | question・experience・progress・observation・opinion・lesson・challenge・direct_statement |
| CTA | follow_connect・mutual_growth・question・experience_share・soft_connection・none |
| structure | single_short_point・two_paragraph・progress_then_invite・lesson_then_question・observation_then_connection・question_then_context |

family ごとに使える hook・CTA・structure を決めてある。書き方の印 (signature) =
`family+hook+cta+structure`。

- **事実の要る family は、事実があるときだけ**: goal_progress は観測したフォロワー数、
  behind_the_scenes・lesson_learned・failure_improvement・experiment・next_step・milestone は
  `app/config/threads_growth_facts.json` の、その日に有効な文 (人が書く。公開してよい文だけ。
  URL・path・秘密の言葉があれば読み込みで止まる)。milestone の事実はまだ無い (使わない)。
  多様さのために出来事を作らない。
- 目標の人数を必ず書くのは account_identity・goal_progress・milestone・mutual_growth だけ。
  ほかの family は、何をしているアカウントか (AI と自動化) で伝える。結び (CTA) は書き方どおりか
  を見る (question なら「？」、フォローのお願いなら「フォロー」「つながり」)。question の hook
  は最初の段落に「？」。アカウントの紹介・事実の境界 (お金・実績・人数・個人の話)・リンク・
  絵文字・長さ・似ている度合いは、どの書き方でも同じ検査。
- 「フォロワー100人を目指しています」から書き始めない。必ずフォローを返す・全員に返信する、とは
  約束しない (フォロー・返信は人が手で行う)。

### 順と、落ちたときの次

順は **JST の日付・方針の版・最近の履歴・使える書き方・試みの番号** だけで決まる (SHA-256。
乱数なし)。最近 7 本の Growth Post の書き方を見て、前の日と同じ family・前の日と同じ
family+CTA・最近と同じ signature・前の日と同じ hook を後ろに回す (選べるものが無ければ使う)。

| 落ちた理由 (分類) | 次 |
| --- | --- |
| `validation_similarity` (最近の Growth Post に似すぎた) | 言い換えはしない。**まだ試していない family** で最初から書く。指示には新しい方向 (family の意図・書き出し・組み立て・結び) を書く。family が尽きたら、同じ family で hook・CTA・structure の 2 つ以上が違う書き方 |
| `validation_format`・`validation_fact`・`validation_hook` | 同じ書き方で 1 回だけ書き直す (検査の理由を渡す)。それでも落ちたら別の書き方 |
| `provider_auth` (認証・設定) | その日は止める (残りの呼び出しを使わない) |
| `provider_transient` (時間切れ・通信・429・5xx) | その回は止め、次の点検 (1 時間ごと) に同じ書き方で続ける (上限の中で) |
| `strategy_exhausted`・`model_call_budget_exhausted` | 提案なしで止める (`growth_generation_exhausted`) |
| `follower_target_reached` | 呼ばない (次の目標は人が決める) |

呼び出しは、呼ぶ **前に** 記録に書く (落ちても数は戻らない)。再起動の後も、記録から使った数と
試した書き方を読み、残りの上限の中で続ける。今日の提案があれば作らない。T6.3.3c より前の形の
記録 (書き方の版が無い) がある日は呼び直さない: **2026-09-29 の記録
(`rejected_by_validation`・0.543) はそのまま**。

### 記録

- 日の記録: 呼び出しごとに `call_index`・`attempt_index`・`purpose` (initial / repair /
  strategy_retry)・書き方・検査の結果・`failure_class`・似ている度合い (最大・比べた提案・上限)・
  `retry_direction`・`next_action`。その日の `outcome` (stored / growth_generation_exhausted /
  provider_auth) と `exhaustion_reason`。
- 提案 (`learning_guidance_json.growth`): 書き方・`strategy_policy_version`・
  `model_call_index`・`attempt_index`。migration なし。
- `scripts/report_threads_growth_reliability.py` (読むだけ): 生成した日・提案ができた日・
  提案なしの日・呼び出し・候補・似すぎ・書き直し・別の書き方・提案 1 本あたりの呼び出し・
  成功の割合、と最近の family / hook / CTA / structure の分布・同じ書き方の繰り返し・最近の
  最大の類似。**記述だけ** (フォロワー・表示回数の原因は言わない。生成に戻さない)。
- 将来 (設計だけ): 外の傾向 (T6.5)・自分の投稿の成績・書き方の履歴 → 書き方の相談役。
  **いまはつながない** (T6.3.3c の順は決まった規則だけ)。

### Growth のトピック (日付の方針)

**公開の JST の日付で決める** (`app/social/threads/topic.py`、UTC の日付では決めない)。

| 種類 | 公開の日 (JST) | トピック |
| --- | --- | --- |
| article | いつも | AI Threads |
| account_growth | 2026-10-04 まで (その日を含む) | インサイト祭り |
| account_growth | 2026-10-05 00:00 から | なし |
| 未知の種類 | — | 公開しない (fail closed) |

- 10/4 23:59:59 JST → インサイト祭り、10/5 00:00:00 JST → なし (試験あり)。ただし Growth の
  公開は公開の窓 (07:00〜23:00) の中だけなので、実際には 10/4 は 22:59 までに出る。
- 10/4 までの公開は `topic_tag=インサイト祭り` を 1 回だけ送る。断られたら、トピックなしで出し
  直さない (Growth の枠だけ止まり、記事の枠は続く。T6.3.3b のまま)。
- 10/5 からは、**方針として** `topic_tag` を送らない。公開の記録に `topic_decision =
  policy_no_topic` (断られて外した、ではない。失敗も出し直しも無い)。
- 10/5 の後に別のトピックを自動で選ばない (`GROWTH_TOPIC_AUTO_REPLACEMENT = False`)。次は人が
  成績とフォロワーを見て決める。
- トピックは公開のときのメタデータだけ: 本文・提案の hash・文字数・prompt・似ている度合いの計算・
  link_mode・目標は変わらない。本文に「インサイト祭り」を足さない (prompt もトピックの言葉を
  書かせない)。
- 承認の画面・メール: 10/4 までの Growth Post は「トピック: インサイト祭り」、10/5 からは
  「トピック: なし」 (提案の公開してよい日で決める。WordPress の中継の変更は要らない)。
- 過去の行は変えない: #25 (トピックなし) も、トピック付きで公開した行も、そのまま。

T6.3.3b (Threads が `topic_tag=インサイト祭り` を受け入れたか) と T6.3.3c (多様さと確かさの
仕組みで検査を通った提案ができたか) の本番の確認は、別々に数える。

**1 日 1 本の検査を通った提案を目指し、似すぎたら別の書き方で書く。検査は弱めない。**
