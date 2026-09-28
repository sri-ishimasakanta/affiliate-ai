# Threads Trend Intelligence (T6.5)

BizFluxLab 自身の投稿の成績と、Threads 全体で伸びている投稿の形を **読むだけで** 記述する。
最初は記述と提案の材料だけで、**生成 (Luna) にも公開にも何も戻さない。**

## 段階 (roadmap)

| 段階 | 名前 | 状態 |
| --- | --- | --- |
| T6.5A | Own Performance Feature Store (自分の投稿の特徴と記述の基準) | 実装済み (active) |
| T6.5B | External Threads Observer Foundation (外の投稿を読むだけで観察する土台) | 実装済み・selectors 確認済み・本番の migration 適用済み・保存つきの 5 件のパイロット済み (2026-09-28)・**ほかの画面は dry-run の確認待ち** (active) |
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

### 保存 (migration `2cfa0ccb2059`、2026-09-28 22:08 JST に本番へ適用済み)

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

- `observer/selectors.py`: 画面の読み方 (版つき)。2026-09-28 の読むだけのパイロットで本物の
  画面と照らして確かめた (`SELECTOR_VERSION = threads-web-verified-2026-09-28-v1`、
  `SELECTOR_VERIFIED = True`)。**確かめたのは For You の画面の一部だけ** (下の「パイロットの
  結果」)。
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

`--screenshots` のときだけ、出どころのページごとに、最初の状態と (スクロールしたなら) 最後の状態 (`-final`) を
ページ全体で撮る (投稿ごとには撮らない):
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

## 読むだけのパイロットの結果 (T6.5B、2026-09-28)

人の許可のもとで行った (Playwright 1.63.0 + Chromium 153.0.8010.12、専用のプロファイル、人が
画面でログイン、For You を 5 件、保存なし `--dry-run`、画面の保存あり)。

- 結果: `succeeded`、5 件を認識・捨てた 0 件。DB への保存 0。Threads への書き込み・社会的な
  操作 0。OpenAI の呼び出し 0。
- 下書きの selectors は **本物の画面と違っていた**:
  - 指標のアイコンの名前は `aria-label` ではなく svg の `title` 属性 (「いいね！」・返信・
    再投稿・シェアする)。数は同じボタンの中の `span[dir=auto]`。**0 のときは数が出ない (空)。**
  - 見出しの相対時刻 (「44分」) を包む `span[dir=auto]` と、ボタンの中の数の `span[dir=auto]`
    が本文に混ざっていた (下書きの parser の誤り。指標は読めずに `None` だったので、誤った
    値は出ていない)。
  - 投稿は後から描かれる (固定の待ち時間では足りない)。
- 直したこと (最小限): アイコンの名前を `title` 属性 / `<title>` / `aria-label` から読む。時刻を
  包む span とボタンの中の span を本文から外す。投稿のまとまりが出るまで待つ (最大 15 秒、
  出なくても parser が判定する)。画面の保存はページ全体で、最初の状態とスクロール後の状態。
  fail closed の規則 (まとまりが無い → `dom_unrecognized`、半分より多く崩れている → 実行を
  捨てる、読めない数 → `None`、OCR なし、表示回数を作らない) は変えていない。
- 画面と照らして確かめたもの (5 件すべて): For You の投稿のまとまり・本文 (行の数まで)・
  投稿者の公開の名前・permalink / 投稿のキー (時刻のリンクの `/@名前/post/コード`、名前が
  表示の投稿者と一致)・投稿時刻 (`time[datetime]`、表示の「○分」と一致)・トピック
  (「› note」「› インサイト祭り」)・いいねの数・返信の数・画像の有無 (プロフィール写真は
  数えない)。
- **確かめていないもの** (読まない / 未確認のまま): 再投稿の数 (画面に数は出るが、引用を
  含むかどうかが画面から分からない → `None`)・共有 (数が出ない → `None`)・引用 (別に出ない
  → `None`)・動画・外部リンクの有無 (今回の 5 件に無かった)・検索・トレンド・トピック・
  カスタムフィード・アカウントの画面。
- 数が空 (0 のときの表示) は `None` のまま。0 と決めつけない。
- 本物の入れ子をもとにした合成の fixture:
  `tests/fixtures/threads_observer/for_you_verified_2026-09-28.html` (文字・名前・コード・
  URL はすべて合成)。
- 画面の保存は `artifacts/threads-observer/2026-09-28/` (git に入らない)。自分のアカウントの
  左のメニューとアイコンが写るので、外に出さない。

### Windows での Chromium の置き場所

`uv run playwright install chromium` の既定の置き場所 (`%LOCALAPPDATA%\ms-playwright`) では、
この PC の Windows が Chromium を起動しなかった ("side-by-side configuration is incorrect"、
入れ直しても同じ。同じファイルを別の場所に置くと起動する)。そこで、Playwright の標準の
環境変数で、git に入らない `data/playwright-browsers/` を使う:

```
PLAYWRIGHT_BROWSERS_PATH=D:/Projects/affiliate-ai/data/playwright-browsers
```

(`uv run playwright install chromium` をこの環境変数つきで実行しても同じ場所に入る。)
コードは変えていない。

## 本番の migration と保存つきのパイロット (T6.5B、2026-09-28)

人の許可のもとで行った。worker は止めていない・再起動していない (worker はこの表を使わない)。

- backup: `D:\Backups\affiliate-ai\affiliate_ai.before-2cfa0ccb2059.20260928-220811.db`
  (8,237,056 bytes、SHA-256 `7192f9a18873411d9c1d0ccf234498737c82a3023b3d672e35a78411ca99d3c6`、
  integrity ok、FK の違反なし、revision `c4d2e8f1a9b3`)。
- `uv run alembic upgrade head` → `2cfa0ccb2059 (head)`。`alembic check` は差なし。integrity ok、
  FK の違反なし。既存の 54 の表の行の数は前後で同じ (3,696 行)。既存の表・索引の定義は変わって
  いない。足されたのは観察の 4 つの表と 3 つの索引だけ (はじめは 0 行)。
- `PENDING_PRODUCTION_MIGRATIONS` から `2cfa0ccb2059` を消した (Project State: DB は head、観察の
  表あり、未適用の migration なし)。
- 保存つきの観察を 1 回だけ (For You、最大 5 件、画面の表示あり、画面の保存あり):
  run #1 `succeeded`、5 件を認識・保存 (投稿 5・観測 5)・捨てた 0、スクロール 1 回、
  `threads-web-verified-2026-09-28-v1` / `t6.5b-collector-2`。
- 画面と照らした (5 件すべて): 投稿者・permalink (名前とキーが一致)・投稿時刻 (表示の
  「○分 / ○時間」と一致)・トピック (5 件とも無し)・いいね・返信 (空の表示は `None`)・画像。
  **指標の食い違いは無し。** 再投稿・共有・引用は `None`。表示回数の列は無い。
- 特徴は決定的に作り直せる (5 件とも保存値と同じ)。観測の行は変わらない。
- 外の分析: 投稿 5・観測 5・投稿者 5・基準を作れた投稿者 0 (伸びた候補なし。少ない数)。

### パイロットで見つかった問題 (T6.5B.1 で直した。下の「観察の質の強化」)

1. **本文に画面の部品が混ざる**: 続きの投稿の印「1/2」(`\xa01/2`) が本文の末尾に入った
   (1 件)。「meta.ai」の青い札が本文の先頭に入った (2 件)。前の dry-run の 1 件でも「1/2」が
   入っていた (そのときは行の数だけを照らしていて見落とした)。指標・投稿者・時刻・トピックには
   影響なし。本文の長さ・数字の数などの特徴には影響する (例: 「1/2」で数字の数が 2)。
   保存した本文は「最初に見た本文を残す」ので、後の観測では直らない。
2. **For You の 5 番目の投稿が保存されなかった**: スクロール後の画面では changna… と
   hi.yumama… の間に別の投稿 (「1/2」の印つき) があるが、保存されず、捨てた数にも入って
   いない。(原因は T6.5B.1 で確認: 描いた後の差し込み。) 集めた 5 件の値は正しいが、
   「画面の上から 5 件」とは限らない。
3. 外の分析の文字の出力に「少ない数 (insufficient evidence)」の明示が無い (本数は出ている)。

## 観察の質の強化 (T6.5B.1、2026-09-28)

上の 3 つの問題を、For You の **dry-run だけ** で調べて直した (本番の観察の記録は書き換えて
いない。保存つきの観察もしていない)。調べるときは、For You を読むだけの診断 (保存なし・
押さない) で、画面の構造を文字を伏せて保存した (`artifacts/threads-observer/`、git に入らない)。

### 原因 (画面で確かめた)

1. **「1/2」**: 続きの投稿の印は、本文の `span[dir=auto]` の **中の** `div`
   (`div > div > [span 数, div > span 区切り, span 数]`) だった。下書きの parser は本文の span の
   文字を丸ごと取っていたので、印も入った。7 回の診断の読みで、古い取り方では 40 件の本文に
   印が入り、新しい取り方では 0 件。
2. **「meta.ai」**: 画面では本文の上の、アイコンつきの青い札。**DOM の形は見られなかった**
   (診断 7 回・約 50 件の中に無かった)。dry-run で 1 件あり、保存前の本文の先頭に入っていた。
3. **保存されなかった 5 番目の投稿**: Threads は、描いた **後で**、すでに描いた投稿の **間に**
   投稿を差し込む (最初の読みで 4 件 → 2 秒後に 14 件。すでにあった 2 件の間に 4 件が入った、
   など)。run 1 の collector は、差し込まれる前の画面を読んで 5 件をそろえたので、後から
   changna… と hi.yumama… の間に入った投稿は **読んでいない** (捨てたのではない)。入れ子の
   まとまり・まとまりの外の投稿・形の崩れは、診断では 1 件も無かった。さらに、読みと読みの間
   だけ現れて消えた投稿も 1 回見た (画面の保存にだけ写った)。

### 直したこと

- **本文 (DOM の段階)**: 本文の span の中の「数 / 数」だけの `div` (続きの投稿の印) は本文に
  入れない。書いた人の「1/2の確率」は本文の文字の span の中にあるので残る (試験あり)。
- **引用した投稿**: dry-run で、外側の投稿の返信の数に **引用した投稿の返信の数** が入っていた
  (外側 ♡5 💬2、引用 ♡5 💬8 → 読んだ値 5 / 8)。まとまりの中の別のまとまり (引用) の要素は、
  外側の投稿の指標・本文・メディア・トピック・時刻・リンクに使わない。**run 1 の 5 件には引用が
  無かった (画面で確認)** ので、保存した値は影響を受けていない。直した後の画面ではまだ引用の
  投稿を見ていない (試験で確かめた)。
- **候補の勘定** (`t6.5b-card-accounting-1`): 読んだ候補の 1 つずつに、結果 1 つと理由を付ける。
  - 結果: `accepted` / `malformed` / `duplicate` / `filtered` / `unsupported` /
    `other_explicit_reason`。
  - 理由 (安定した ID): `accepted`・`malformed_no_permalink`・`malformed_empty_body`・
    `duplicate_in_frame`・`filtered_over_limit`・`unsupported_nested_card`・
    `unsupported_outside_container`・`late_inserted_after_read`。
  - **候補の数 = 結果ごとの数の合計**。さらに、投稿の時刻のリンクから独立に数えたキーが、
    すべて勘定に入っていなければ、実行は `accounting_mismatch` (投稿は保存しない。状態の列が
    24 文字なので `candidate_accounting_mismatch` ではなくこの名前)。
  - 仮想化で画面から外れた投稿は、勘定済みのまま数える (`virtualized_out`)。
  - 勘定は実行の `artifacts_json.candidate_accounting` に残す (列は増やしていない)。
- **読む時機**: 並びが 2 回続けて同じになるまで読む (1 秒ごと、最大 3 回)。画面を保存するときは
  **撮った直後に読み**、その読みを使う (画面と読んだものを同じ時点にそろえる)。最後にもう一度
  読み、読んだ投稿より上に後から差し込まれた投稿を `late_inserted_after_read` として数える
  (読んでいないので保存しない。実行は `partial`)。
- collector `t6.5b-collector-3`、selector `threads-web-verified-2026-09-28-v2`。

### 件数の上限の意味

`--limit-total N` = **読んだ候補の流れ** (読んだ時点の画面の上からの順、決まった回数の
スクロールまで) の中から、受け入れた投稿を最大 N 件。**「画面に表示された最初の N 件」では
ない** (後から差し込まれる投稿があるため、それは約束できない)。読んだ候補の数・受け入れた数・
それ以外の数と理由・候補の順を、実行ごとに残す。

### 元の記録と分析用の本文 (run 1 を含む)

- 保存した本文・hash・保存した特徴・指標・実行の状態は **書き換えない** (証拠)。
- 分析の層で、決まった規則 (`threads-body-normalizer-1`、`observer/normalize.py`) で
  分析用の本文を作り、特徴を作り直す。`raw_body_hash`・`normalized_body_hash`・
  `normalization_version`・`normalization_flags` を付ける。規則は 2 つだけ:
  本文の末尾の「NBSP + 数/数」(古い collector の印) と、本文の先頭の `meta.ai` の札。
  **本文が本当に `meta.ai` で始まる投稿も除かれる** (残る危険。印で分かる)。
- run 1: `collection_status=succeeded`・`post_collection_quality=partial`・
  `text_quality=normalized_with_known_ui_chrome`・`candidate_completeness=not_guaranteed`。
  3 件の本文を分析用に直した (「1/2」で数字の数 2 → 0、「meta.ai」の札を除いた。ほかの文字は
  そのまま)。

### 質の印

投稿ごと: `text_clean` / `ui_chrome_removed` / `text_contaminated`、`metrics_verified` /
`metrics_unverified`、`partial_metrics`、`selector_verified`、`candidate_accounting_complete` /
`candidate_accounting_not_guaranteed`。**`text_contaminated` の投稿は特徴の形の数え上げから
外す** (いいね・返信の数の分析には使う)。

### 少ない数

外の観測で、特徴の数え上げに使える投稿が 30 件未満、または基準を作れた投稿者が 0 人なら、
分析の出力の先頭に出す:

「外部投稿の観測数が少ないため、現時点では傾向を判断できません。
以下は観測値の一覧であり、伸びる要因を示す証拠ではありません。」

(標本の数・基準の有無・候補の数は、そのまま出す。)

### dry-run の確認 (For You、5 件、保存なし)

直した後の For You の dry-run を画面と照らした (最後の 2 回は、画面を撮った直後の読みを使う
形)。最後の回: 候補 6 = 受け入れ 5 + 上限で外した 1、勘定が合う、画面に写った投稿 6 件と
候補 6 件が同じ順で一致。本文に「1/2」「1/3」「1/4」の印が入らない (画面では 3 件に印あり)。
投稿者・permalink・時刻・トピック・いいね・返信 (空は `None`) が画面と一致。再投稿の見出し
つきの投稿も正しく読めた。**DB への書き込みは 0。**

## 次の確認点: ほかの画面の dry-run (保存しない)

それぞれ **dry-run だけ・5 件以下・画面と照らす** (`--show-posts --diagnose`)。確かめた画面ごとに
selectors の確認の状態を記録してから、保存に使う。

- A. 検索: `observe_threads.py --search "<語>" --limit-total 5 --headed --screenshots --dry-run
  --show-posts --diagnose`。
- B. トレンドのトピック: まずトピックの一覧の画面だけを確かめる。そのあと 1 つのトピックから
  5 件以下。
- C. カスタムフィード: `--custom-feed <id> --limit-total 5 ... --dry-run`。
- D. 知っているアカウント: `--account <名前> --limit-total 5 ... --dry-run`。

引用した投稿を含む画面と、「meta.ai」の札の DOM の形は、どの画面でも出てきたら照らす。
夜の定期の観察・自動の提案は、まだ作らない。

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
