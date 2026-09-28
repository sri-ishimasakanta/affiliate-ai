# Threads Trend Intelligence (T6.5)

BizFluxLab 自身の投稿の成績と、Threads 全体で伸びている投稿の形を **読むだけで** 記述する。
最初は記述と提案の材料だけで、**生成 (Luna) にも公開にも何も戻さない。**

## 段階 (roadmap)

| 段階 | 名前 | 状態 |
| --- | --- | --- |
| T6.5A | Own Performance Feature Store (自分の投稿の特徴と記述の基準) | 実装済み (active) |
| T6.5B | External Threads Observer Foundation (外の投稿を読むだけで観察する土台) | 実装済み・**手動の読むだけのパイロット待ち** (active) |
| T6.5C | Breakout Detector (伸びた候補の検出) | planned |
| T6.5D | Pattern Miner (繰り返し出る形の抽出) | planned |
| T6.5E | Velocity / Early Trend Detection (反応の速さ・早い兆し) | planned |
| T6.5F | Cross Validation (外の形 × 自分の結果) | planned |
| T6.5G | Strategy Recommendations (提案だけ) | planned |
| T6.5H | Controlled Feedback to Generation (人の判断のあと、制御された形で生成に戻す) | planned |

## 変わらない約束

- **Playwright を第一の候補** にする (Python の Playwright)。
- **読むだけ。** いいね・返信・フォロー・再投稿・引用・DM・投稿・アカウントの変更はしない。
  ブラウザの操作の型 (`observer/driver.py` の `ReadOnlyPage`) には、開く・決まった量の
  スクロール・HTML を読む・画面の保存しか無い。押す・書く方法は作っていない。
- **小さなパイロットから。** 件数に上限、スクロールの回数に上限 (無限にスクロールしない)。
- **自動の最適化はまだしない。** 少ない数の結果で生成・公開の設定を変えない。
- 外の投稿の **表示回数 (views / impressions) は見えないので、作らない・推測しない。**
- 夜の定期の観察 (Windows のスケジュール) は **まだ作らない。** 手動のパイロットが通ってから。
- 秘密 (パスワード・token・cookie) を記録・報告・git に残さない。

## T6.5A — 自分の投稿の特徴 (feature store)

`app/services/threads_feature_store.py` (読むだけ)。新しい表は作らない。公開済み
(`threads_publications.status = published`) の投稿ごとに 1 行を、既存の表から作り直す。

- 来歴: `publication_id`・`proposal_id`・`source_article_id`・`content_kind` (article /
  account_growth)・`topic_tag` (**コンテナ作成の記録に残った、実際に送った値**。記録の無い古い
  公開は `None` で、方針から埋めない)・`conversation_hook`・`angle`・`link_mode`・
  `published_at_jst` (Threads の時刻 → 手元の公開時刻の順)。
- **特徴** (`features`、`app/social/threads/features.py`、版 `t6.5-features-1`、`source=rule`):
  文字数・本文の長さ (URL を除く)・段落・文・疑問・絵文字・数字・金額の数・URL の有無・
  CTA の型 (`none` / `question` / `opinion_request` / `follow_connect` / `reply_request` /
  `other`)・構成の型 (`short_single_point` / `comparison` / `checklist_like` / `experience` /
  `goal_progress` / `informational` / `community`)・長さの区分・曜日と時 (JST)・メディア。
  すべて決定的な規則。AI (Luna) で作る特徴は今は無い (作るなら `source=model` と明記する)。
- **生の指標** (`metrics`): 最新の `observed` の観測の views・likes・replies・reposts・quotes・
  shares。観測が無ければ `None` (0 にしない)。
- **作った値** (`derived`): `engagement` (likes + replies + reposts + quotes + shares。どれかが
  欠ければ `None`)。生の指標とは分けて持つ。

基準 (`own_baselines`) は中央値と本数だけ: 種類 (article / Growth)・きっかけ・トピック・
リンクの有無・長さの区分・時・CTA・構成ごと。本数が 5 未満のまとまりは `small_sample`。
比べられるまとまりが 2 つ以上あるときだけ「observed higher median」「observed lower
median」を示し、足りなければ「insufficient sample」。**原因は主張しない。** 最新の観測なので
公開からの経過時間はそろっていない (同じ経過時間での比較は `scripts/analyze_threads_performance.py`)。

## T6.5B — 外の投稿の観察 (読むだけ)

### 保存 (migration `2cfa0ccb2059`、**本番には未適用**)

自分の提案・公開の表とは別の 4 つの表。

- `threads_observer_runs`: 1 回の観察 (状態 `succeeded` / `partial` / `failed` /
  `login_required` / `dom_unrecognized`、理由、上限、集めた数・捨てた数、collector と selector の
  版、画面の保存の場所)。
- `threads_external_posts`: 外の投稿ごとに 1 行 (`threads:<code>`)。投稿者の公開の名前・
  permalink・投稿時刻 (見えれば)・本文・hash・トピック (見えれば)・メディア・リンクの有無・
  特徴 (自分の投稿と同じ `features.extract`)。**最初に見た本文を残す。**
- `threads_external_observations`: 観測 (**積むだけ・上書きしない**)。いいね・返信・再投稿・
  共有 (見えれば。見えなければ NULL)。引用は画面に別に出ないので NULL。**表示回数の列は無い。**
  本文の hash を観測ごとに持つので、後で本文が変われば分かる。同じ投稿を 3 回見れば 3 行
  (T6.5E の速さの材料)。
- `threads_trending_topics`: トレンドのトピックの名前ごと (初めて・最後に見た時刻、見た回数、
  見本の投稿の数)。**トレンド = BizFluxLab に良い、とは決めない。**

### 仕組み

- `observer/selectors.py`: 画面の読み方 (版つき)。**まだ本物の画面で確かめていない下書き**
  (`SELECTOR_VERSION = threads-web-draft-2026-09-28`、`SELECTOR_VERIFIED = False`)。
- `observer/parser.py`: 保存した HTML から決定的に取り出す (標準ライブラリの `html.parser`)。
  期待する形が無ければ **ページ全体を `dom_unrecognized` として止める** (fail closed)。半分より
  多くの投稿の形が崩れていても止める。形の違う要素を、いいね等として読み替えない。数が読め
  なければ `None` (0 にしない)。OCR は使わない。
- `observer/collector.py`: 出どころ (for_you / trending_topic / search / custom_feed /
  known_account) ごとの上限と、1 回の合計の上限。スクロールは最大 3 回。ログインの画面なら
  `login_required` で止まる。画面の形が違えば `dom_unrecognized` で止まり、**その実行で集めた
  ものは全部捨てる** (実行の行だけを理由つきで残す)。
- `observer/driver.py`: Playwright は実際に観察するときだけ読み込む (試験では使わない)。
  開ける URL は Threads の読む画面だけ (`/login`・`/intent`・`/compose`・`/accounts` や、ほかの
  host は開かない)。

### 上限 (パイロットの既定)

| 出どころ | 上限 |
| --- | --- |
| For You | 20 |
| 検索 (1 語ごと) | 10 |
| トレンドのトピック | 5 個 × 各 5 件 |
| カスタムフィード (1 つごと) | 10 |
| 知っているアカウント (1 つごと) | 10 |
| 1 回の合計 | 60 |
| スクロール | 最大 3 回 / ページ |

`--limit-total` は上限を **下げる** ことだけができる。

### ブラウザのプロファイルとログイン

- 専用の持続するプロファイル: `data/threads-observer/browser-profile` (**git に入らない**、
  `.gitignore`)。cookie・session はここにだけあり、リポジトリにも報告にも出さない。
- ログインは `--login` で開いた画面で **人が** 行う。パスワードを CLI に渡さない。CAPTCHA・
  確認の画面を自動で越えない。
- 観察の途中でログインが要ると分かったら、何も保存せずに `login_required` で止まる
  (終了コード 3)。

### 画面の保存 (任意)

`--screenshots` のときだけ、出どころのページごとに 1 枚 (投稿ごとには撮らない):
`artifacts/threads-observer/YYYY-MM-DD/HHMMSS/for_you.png`・`trends.png`・`search-ai.png`
など。監査・調べもの用で、**git に入らない。** アカウントの設定の画面などは開かない。

### 伸びた候補 (breakout) の土台

`app/social/threads/trends.py`。同じ投稿者の **ほかの** 投稿 (その投稿自身は入れない) の
いいね・返信の中央値を基準にする。ほかの投稿が 5 本以上あるときだけ比を出す
(`like_breakout_ratio` / `reply_breakout_ratio`)。例: 過去 8 本の中央値 9、新しい投稿のいいね
71 → 7.89 倍 → `candidate_breakout` (**候補であって、原因ではない**)。フォロワー数は見えて
いないので使わない。

## CLI

```
uv run python scripts/analyze_threads_trends.py          # 読むだけ (DB に書かない)
uv run python scripts/analyze_threads_trends.py --json
uv run python scripts/observe_threads.py --login          # 人がログインする (観察しない)
uv run python scripts/observe_threads.py --for-you --headed --screenshots --dry-run
```

- `analyze_threads_trends.py`: 外 (伸びた候補・いいね / 返信の多い投稿・繰り返し出たトピック・
  特徴の数え上げ) と自分 (まとまりごとの中央値と本数・observed higher / lower median・
  small sample) を出す。観察の表が無い DB では「外の観察は利用できない」と出す。
- `observe_threads.py`: 手動のパイロット用。**定期の実行には登録しない。** 観察の表が無い DB では
  ブラウザを開かずに止まる (`--dry-run` は保存しない)。

## 手動の読むだけのパイロット (次の確認点)

1. 人が、Playwright と Chromium を入れることを許可する
   (`uv add playwright` → `uv run playwright install chromium`)。
2. 人が `observe_threads.py --login` で専用のプロファイルに画面からログインする。
3. 保存なしで小さく試す: `observe_threads.py --for-you --limit-total 5 --headed --screenshots
   --dry-run`。取れた件数・捨てた件数・画面と照らし、selectors を本物の画面に合わせる。
   合ったら `SELECTOR_VERSION` を上げ、`SELECTOR_VERIFIED = True` にする。
4. 人の許可のもとで、観察の表の migration (`2cfa0ccb2059`) を本番に適用してから、保存つきで
   小さく試す。
5. 夜の定期の観察は、パイロットが通ってから別に決める。

## してはいけないこと

- 読むだけ。**いいね・返信・フォロー・再投稿・DM の自動化はしない。**
- 観察のために自分のアカウントで操作しない (見るだけ)。
- 少ない数の結果で、生成・公開の設定を自動で変えない。Luna には何も戻さない (T6.5H まで)。
- 「best」「winning」「guaranteed」のような言い方をしない。「observed higher median」
  「candidate pattern」「insufficient sample」と言う。
- 秘密 (token・cookie・パスワード) を記録や報告に残さない。

## 前提

T6.4 (日本語の表示) 完了。T6.3.3b (Growth のトピック "インサイト祭り") の本番の確認は、
次の自然な Growth Post で行う (T6.5 とは別に進む。T6.5 の作業は Growth に触れない)。
