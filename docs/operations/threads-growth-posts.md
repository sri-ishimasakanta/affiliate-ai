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
6. トピック "AI Threads" は付けない (`topic_tag` を送らない)。
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
| トピック | なし (T6.3.2 の方針で `account_growth` → `None`) |
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
