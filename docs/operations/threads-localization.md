# Threads の日本語の表示 (T6.4)

人に見せる Threads の画面・メール・報告を日本語にする。**表示だけ。** 生成・承認・公開・queue・
スケジューラ・トピック・Growth Post・学習の振る舞いは変えない。

## 仕組み: 内部の値はそのまま、表示の層で日本語にする

- 状態・理由の ID・enum・ログの event 名は英語のまま保存する (`awaiting_approval`・
  `gap_not_elapsed`・`growth_daily_limit`・`sentence_too_long` など)。機械・ログ・将来の自動化は
  これを読む。
- 人に見せるときだけ、`app/social/threads/labels_ja.py` が `内部の値 → 日本語の見出し (+ 説明)` に
  変える。表は決定的。知らない値は捨てず、内部の値を括弧で添える (例: `要確認（needs_human_check）`)。
- **警告**: 生成のコードが書く英語の警告は、このリポジトリの **決まった形の文** (定型)。
  `labels_ja` の目録がそれぞれを ID・見出し・数値入りの説明に対応づける (例:
  `1 sentence(s) exceed 60 characters; prefer shorter ones` → `sentence_too_long` →
  「長い文があります / 60文字を超える文が1文あります。短くすると読みやすくなります。」)。
  試験が実際の生成のコードを動かして、出てくる警告がすべて目録にあることを確かめる (定型が変われば
  落ちる)。目録に無い警告は「システム警告 / 詳細: 元の文」として残す (黙って落とさない)。
  保存している警告 (`warnings_json`) は英語のまま。
- **時刻**: 人に見せる時刻は JST (`2026-09-28 13:55 JST`)。保存している時刻 (UTC) は変えない。
- **秘密**: 表示するのは状態と理由だけ。token・鍵・認証のヘッダは通らない。

## 変えた表示

| 面 | 内容 |
|---|---|
| 承認のまとめ送りメール | 件名「【Threads承認】投稿案N件の確認をお願いします」。1 件ごとに 投稿種別・状態・元記事・切り口・会話フック・目標 (Growth)・リンク・トピック・文字数・本文・注意 (日本語の警告)。承認は今まで通りページ上の操作だけ (メールに承認のボタンは置かない) |
| 携帯のレビューページ (snapshot) | 既存の欄 (切り口・リンク・警告) を日本語の値にした (中継の許可リストにある鍵なので、**今の中継でもすぐ効く**)。内部の値は `angle_raw`・`link_mode_raw`・`warnings_raw` に残す。新しい欄: `post_kind_label`・`status_label`・`source_article_label`・`goal_label`・`hook_label`・`topic_label`・`warning_details` |
| 中継 (WordPress の mu-plugin) | 許可リストに上の新しい欄を足し、ページに 投稿種別・状態・元記事・目標・会話フック・トピック・文字数 (「文字」付き)・警告の見出しを出す。新しい欄の無い古い snapshot も今まで通り描く。**本番の WordPress への配備は別の確認点** |
| 公開の失敗のアラート | コンテナ作成・公開の失敗を「事前確認の失敗」と書いていた誤りを直した。何が起きたか・投稿されたか・自動で再試行するか・対応が要るか の 4 行 + 技術の識別子。Topic が断られたら「自動的に Topic なしで再投稿はしません」。Growth の失敗は「Growth Post だけを止め、通常投稿は続けます」 |
| アラートのメール本文 | 見出しを日本語、発生時刻を JST |
| 週次の運用レポート | 見出しを日本語 (節の名前は英語を括弧で残す)。**Threads の節を足した**: 通常投稿・Growth Post・合計・承認待ち・公開失敗・要確認・今週公開した投稿の最新の指標 (合計)・フォロワーの目標と観測・対応が必要なこと・来週も自動で続くこと。成績の結論は書かない (T6.5) |
| 日次のインシデントメール | 見出しと件名を日本語 |
| 今日の Threads 運用 | `scripts/report_threads_daily.py` (読むだけ・メールは送らない)。毎日メールで送るかは別に人が決める |
| Growth のフォロワー数 | 読んだ結果の理由を残す (`success`・`not_requested`・`permission_denied`・`api_error`・`metric_unavailable`・`missing_field`・`stale`)。status・ログ (`follower_read=`)・Project State・報告に日本語で出す。呼び出しは増やさず、取れなくても Growth は目標だけの文面で進む |

見出しの例: 状態 (承認待ち・承認済み・却下・公開済み・失敗・保留・期限切れ・結果確認中)、
種類 (通常記事・Growth Post)、会話フック (質問・選択・経験・意見・なし)、Growth の切り口
(アカウント紹介・目標・進捗・制作過程・交流・相互成長)、指標 (表示数・いいね・返信・再投稿・
引用・シェア・フォロワー数)、保留の理由 (次の通常投稿まで待機中・現在は公開時間外・あなたの承認を
待っています・本日の Growth Post はすでに公開済み・公開期限を過ぎました・公開結果を確認できない
ため自動再送を停止しています)。

## 変えないもの

ログの event 名・理由の ID・保存する値、Luna の prompt、記事・Growth の検査、承認の規則、自動公開、
120 分の間隔、Growth の足し分の枠、公開窓、在庫の下限、Growth の 1 日 1 本、トピック・リンクの方針、
スケジューラ、フォロワーの目標、OpenAI のモデル、学習。

## 本番への展開 (人の確認点)

1. **worker の再起動** (制御された再起動): 承認のまとめ送りのメール・アラート・Growth の理由・
   携帯の snapshot の日本語の値 (切り口・リンク・警告) が効く。
2. **中継の配備 (WordPress)**: `wordpress/mu-plugins/bizfluxlab-approval-relay/lib-core.php` を本番に
   配備すると、携帯のページに 投稿種別・状態・目標・会話フック・トピックの欄が出る。これは WordPress
   への書き込みなので、人の許可のもとで別に行う (配備前は、既存の欄だけが日本語になる)。
3. 週次のメールは次の週次の実行から日本語になる (スケジュールは同じ)。

## 本番への展開の記録 (2026-09-28、人の許可あり)

**worker (手順 1): 済み。**

| 項目 | 内容 |
|---|---|
| 停止 | 15:07:09〜15:07:15 JST。pid 21540 の親子 (14592 → 11560 → 20768 → 21540) だけ。タスクの定義は同じ |
| 復帰 | 15:10 の起動は `already_running` (ロックがまだ新しい)。15:25:02 に自然に復帰 (pid 16164、ロックを回収、health ready) |
| capabilities | collect_insights・sync_approvals・send_approval_digests・maintain_proposal_stock・maintain_growth_posts、can_publish=True |
| 起動の直後 | 記事の枠は `gap_not_elapsed` (次は 17:03:01)・Growth は `growth_daily_limit`・Growth の保守は 0 回。記事の在庫の保守がいつも通り提案 #27 を作った (在庫が 2 だった) |
| 日本語の表示 | 本番のコードで #27 の snapshot とまとめ送りを描いた (読むだけ): 件名「【Threads承認】投稿案1件の確認をお願いします」、投稿種別・状態・元記事・切り口・会話フック・リンク・トピック・文字数・注意。#27 の警告 (`quality: the requested hook is choice…`) は「会話フックを確認してください / 「選択」型ですが、選ぶ候補がありません…」 |
| そのほか | 週次の Threads の節・今日のまとめ・失敗のアラートの文・フォロワー数の理由の表は本番のデータで描けた。Project State `--strict` ok |
| 注意 | 前の worker (pid 21540、13:55 起動) は、14:55 の最初のまとめ送りで T6.4 の表示の一部 (件名など) を遅延の import で読んでいた (誤りは出ていない)。この再起動で揃った |

**中継 (手順 2、WordPress): 人のアップロード待ち。** このリポジトリの手順は、人が XServer に
ファイルを上げる方法 (配備の道具や資格情報はリポジトリに無い)。本番の今の `lib-core.php` は
T6.1 の版 (`08d5ed12…`) で、表示されるページの script が一致する (読むだけで確認)。

- 戻す用の写し: `D:\Backups\affiliate-ai\relay-pre-t6.4\lib-core.php` (`08d5ed12…`)
elay-pre-t6.4\lib-core.php` (`08d5ed12…`)
- 上げるファイル: `D:\Backups\affiliate-ai\relay-t6.4-upload\lib-core.php` (`3f0799dc…`、PHP の構文 ok)
elay-t6.4-upload\lib-core.php` (`3f0799dc…`、PHP の構文 ok)
- 置き場所: `wp-content/mu-plugins/bizfluxlab-approval-relay/lib-core.php` (この 1 つだけ。
  `bizfluxlab-approval-relay.php` は変わらない。`tests/` は上げない)
- 上げた後の確認 (読むだけ): 任意の session id の `/bfl-approval/<32桁>` を取り、script が
  `3f0799dc…` の版の描画と一致すること・安全のヘッダが同じこと・worker の次の同期が通ること。
  携帯では #27 (または次の) 承認のリンクを開いて、投稿種別・状態・元記事・会話フック・トピック・
  警告の欄を見る (承認・却下は人がいつも通り判断する)。
