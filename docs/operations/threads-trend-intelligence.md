# Threads Trend Intelligence (T6.5)

BizFluxLab 自身の投稿の成績と、Threads 全体で伸びている投稿の形を **読むだけで** 記述する。
最初は記述と提案の材料だけで、**生成 (Luna) にも公開にも何も戻さない。**

## 段階 (roadmap)

| 段階 | 名前 | 状態 |
| --- | --- | --- |
| T6.5A | Own Performance Feature Store (自分の投稿の特徴と記述の基準) | 実装済み (active) |
| T6.5B | External Threads Observer Foundation (外の投稿を読むだけで観察する土台) | **完了** (2026-09-29、限りつき: 下の「T6.5B の締め」) |
| T6.5B.2 | Collection Strategy (1 日の観察の集め方・計画) | 実装済み・**本番では未実行** (active)。次は段階 1 の保存つきの試験 (人の許可) |
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
- `threads_trending_topics`: トピックの一覧の名前ごと (初めて・最後に見た時刻、見た回数、
  見本の投稿の数)。**一覧に出る = BizFluxLab に良い、とは決めない。** (一覧は「おすすめの
  トピック」(`topic_for_you`)。世の中のトレンドの順位ではない。下の「トピックの一覧の確認」)

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
| トピックの一覧 (おすすめのトピック) | 5 個 × 各 5 件 (`--follow-topics` で読むトピックの数を下げられる) |
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
  → `None`)・動画・外部リンクの有無 (今回の 5 件に無かった)・検索・トピックの一覧・
  トピック・カスタムフィード・アカウントの画面。
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

## 検索の画面の確認 (T6.5B、2026-09-28、dry-run だけ)

`observe_threads.py --search "生成AI" --limit-total 5 --headed --screenshots --dry-run
--show-posts --diagnose` (保存なし)。開いたのは `https://www.threads.com/search?q=生成AI&serp_type=default`
(「上位検索結果」のタブ)。

- 結果: `succeeded`、候補 8 = 受け入れ 5 + 上限で外した 3、勘定が合う。画面に写った投稿 8 件と
  候補 8 件が同じ順で一致 (まとまりの外の投稿なし・入れ子なし・形の崩れなし)。
- **投稿のまとまりの形は For You と同じ** (時刻のリンクの位置・指標のボタンの形・時刻 / 指標の
  アイコン / ボタンの数の印が一致)。検索のための読み方の変更は不要だった。
- 検索の画面の部品: 検索窓と「上位検索結果 / 最近 / プロフィール」のタブは、投稿のまとまりの
  **外**。本文に入らない。検索語 (「生成AI」) は本文の中の普通の文字 (強調の印は無い) で、
  そのまま残る。トピックの札 (例「› 生成AIパスポート」) は本文に入らない。
- 画面と照らしたもの (受け入れた 5 件): 候補の順・投稿者・permalink・時刻 (表示の「○分 /
  ○時間」と一致)・本文 (行の数まで)・分析用の本文 (5 件とも `text_clean`)・トピック・いいね・
  返信 (空は `None`)。
- 出てこなかったもの: 「meta.ai」の札、引用した投稿、投稿でない結果 (アカウントなど。既定の
  タブでは無かった)。**確かめていない。**
- 画面ごとの確認の状態は、全体の版とは別に記録する (`selectors.SURFACE_VERIFICATION`):
  `search` = `threads-search-verified-2026-09-28-v1`。実行の候補の勘定にも、出どころごとの
  `surface_selector_version` / `surface_verified` を残す。全体の `SELECTOR_VERSION` は
  `threads-web-verified-2026-09-28-v2` のまま。
- 残る限り: 確かめたのは 1 つの検索語・既定のタブ・スクロールなしの 1 画面だけ。「最近」
  「プロフィール」のタブ、スクロールした後の結果、ほかの検索語、投稿でない結果の混ざる画面は
  未確認。再投稿・共有・引用の数は読まない (変わらず)。

## トピックの一覧の確認 (T6.5B、2026-09-28、dry-run だけ)

`observe_threads.py --trending --follow-topics 1 --limit-total 5 --headed --screenshots --dry-run
--show-posts --diagnose` (保存なし)。

### どこにあるか (画面で確かめた)

- Web で見えるトピックの一覧は、**検索の最初の画面** (`/search`、語なし) の上の横並びの札
  (ハンドメイド・AI・インスタで繋がろう・旅行・転職…、30 前後)。各札は
  `/search?q=<語>&serp_type=search_nullstate_topic_for_you` への **リンク** (押す操作は要らない。
  リンク先を開くだけで読める)。
- **Threads はこれを「トレンド」と表示していない** (「トレンド」「話題」の文字も見出しも無い)。
  `serp_type` のとおり、このアカウント向けの「おすすめのトピック」。世の中の順位として扱わない。
  保存するときの種類は `topic_for_you`。
- 同じ画面の下は「フォローのおすすめ」(アカウントの紹介。フォローのボタンつき)。投稿では
  ないので、投稿として読まない (投稿の時刻のリンクが無い → `dom_unrecognized`)。
- 下書きの読み方は、`serp_type=tags` のリンクを一覧として読んでいた。この画面ではそれは
  **左のメニューのコミュニティ** (AI Threads・Business Threads) だけなので、メニューを一覧と
  読み違えるところだった。直した: 一覧の候補は `serp_type=search_nullstate_topic` のリンクだけ。
- 下書きの collector は、トピックのページの URL を `serp_type=tags` で作り直していた。直した:
  一覧の **リンクそのもの** を開く。

### Stage A: 一覧

- 候補 30 = 受け入れ 5 + 上限で外した 25 (同じ語・名前の無い札は 0)。勘定が合う。
- 受け入れた 5 件: 1 ハンドメイド・2 AI・3 インスタで繋がろう・4 旅行・5 転職 (画面の順と一致)。
  名前・語 (`q`)・`serp_type`・画面の順を記録する。**件数・分類は画面に出ていない** (作らない)。
- 検索窓・「フォローのおすすめ」・アカウント・左のメニューは、一覧に入らない。
- 候補の結果と理由: `accepted` / `filtered_over_limit` / `duplicate_topic` / `malformed_topic`
  (投稿の候補とは別に数える: `candidate_accounting.topic_list`)。
- 版: `trending_list` = `threads-topic-list-verified-2026-09-28-v1`。

### Stage B: 1 つのトピックの投稿

- 選び方 (決まった規則): 受け入れた一覧の上から、投稿が 1 件以上読めた最初のトピック。
  → 「ハンドメイド」(1 番目)。開いたのは一覧のリンク先 (そのトピックの語の検索結果、
  「上位検索結果」のタブ)。
- 候補 5 = 受け入れ 5。勘定が合う。画面に写った投稿 5 件と候補 5 件が同じ順で一致。
- 投稿のまとまりの形は For You・検索と同じ (共通の読み方をそのまま使う)。
- 画面と照らしたもの: 投稿者・permalink・時刻・本文 (行の数まで。「1/2」の印は 2 件で本文に
  入らない)・分析用の本文 (5 件とも `text_clean`)・投稿のトピック・いいね・返信・画像。
- **ページのトピック ≠ 投稿のトピック**: ページは「ハンドメイド」だが、投稿のトピックは
  その投稿のまとまりに出ているものだけ (5 件: なし・なし・はじめましてThreads・インサイト祭り・
  インサイト祭り)。ページのトピックは、出どころの語 (`source_query`) としてだけ残る。
- 投稿のまとまりが 1 つも無いトピックのページは、画面の形の違いと区別できないので fail closed
  で実行を止める (次のトピックへ黙って進まない)。
- 版: `trending_topic` = `threads-topic-posts-verified-2026-09-28-v1`。
- 出てこなかったもの: 「meta.ai」の札、引用した投稿。

### 保存の形 (本番には書いていない)

`threads_trending_topics` (名前・種類・初めて / 最後に見た時刻・見た回数・見本の投稿の数・
最後の実行) で表せる。一覧の順・リンク・語は実行の `artifacts_json.candidate_accounting.topic_list`
に残る (列は増やさない)。投稿は `threads_external_posts` / `threads_external_observations` に別に
入る。migration は要らない。

### 残る限り

- 確かめたのは 1 回の一覧と 1 つのトピック (上から 1 番目) だけ。ほかのトピックのページ・
  スクロールした後・「最近」のタブは未確認。
- 一覧は **このアカウント向け** で、世の中の「トレンド」ではない。本物のトレンド (順位) の
  画面は、Web のこのアカウントでは見つからなかった。

## カスタムフィードの確認 (T6.5B、2026-09-29): このアカウントでは使えない

読むだけで確かめた (For You の画面の DOM 全体、隠れた要素を含む。押していない)。

- **自分で作ったフィードへのリンクは無い。** 左のメニューの「他のフィード」にあるのは
  組み込みのフィードだけ: おすすめ (`/for_you`)・フォロー中 (`/following/`)・保存済み
  (`/saved/`)・「いいね！」済み (`/liked/`)。取り込み済みの画面の構造 (266 個のフィードの
  リンク) もすべて組み込み。
- 「表示を増やす」「編集」は **ボタン** (リンクではない)。この段階では押す操作を使わないので、
  その先は開いていない (もしその先に自分で作ったフィードがあっても、ここでは見えない)。
- 組み込みのフィードを、自分で作ったフィードの代わりにはしない (別の画面。未確認)。
- 記録: `selectors.SURFACE_AVAILABILITY["custom_feed"]` = 使えない (2026-09-29)。
  `custom_feed_url` の `/custom_feed/<id>` は **下書きの形** (画面で確かめていない)。
  フィードのリンクを組み込み / 自分で作ったものの候補に分ける読み方 (`parse_feed_links`)
  を足した。自分で作ったフィードができたら、そのリンクで dry-run して確かめる。
- **確かめていない画面は保存しない**: `observe_threads.py` は、保存するときに、計画した
  すべての画面が確認済み (`SURFACE_VERIFICATION`) でなければ、ブラウザを開かずに止まる
  (`--dry-run` は使える)。今の確認済み: for_you・search・trending_list (おすすめのトピックの
  一覧)・trending_topic (そのトピックの投稿)。未確認: custom_feed・known_account。
- 出どころ (`source_type=custom_feed`)・フィードの名前 / ID (`source_query`)・投稿のトピックは
  別のもの。フィードの名前を投稿のトピック・本文に入れない (試験あり)。

## 知っているアカウントの確認 (T6.5B、2026-09-29、dry-run だけ)

- 選び方 (決まった規則): 最後に確かめた検索の dry-run (生成AI) で最初に出た公開の投稿者
  (BizFluxLab 以外)。人気・フォロワー数では選んでいない。報告では名前を伏せる。
- `observe_threads.py --account <名前> --limit-total 5 --headed --screenshots --dry-run
  --show-posts --diagnose` (保存なし)。`https://www.threads.com/@<名前>` をそのまま開いた
  (転送なし・公開・押す操作なし)。既定のタブは「スレッド」(返信・メディア・再投稿のタブは
  確かめていない)。
- 結果: `succeeded`、候補 8 = 受け入れ 5 + 上限で外した 3、勘定が合う。画面に写った投稿 8 件と
  候補 8 件が同じ順で一致 (最後の 2 件は「1/2」「2/2」の続きの投稿)。
- **投稿のまとまりの形は For You・検索と同じ** (時刻 / 指標のアイコン / ボタンの数の印が一致・
  入れ子なし)。共通の読み方をそのまま使う。
- **プロフィールの部品はまとまりの外**: 表示名・名前・自己紹介・プロフィールのトピックの札
  (例「朝活」、`serp_type=tags` のリンク)・フォロワー数と閲覧数・フォロー / メッセージの
  ボタン・タブ。本文にも投稿のトピックにも入らない。プロフィールの札と同じ名前のトピックが
  投稿にあっても、投稿のトピックはその投稿のまとまりに出ているものだけ。
- 画面と照らしたもの (受け入れた 5 件): 候補の順・投稿者・permalink・時刻・本文 (行の数まで)・
  分析用の本文 (5 件とも `text_clean`)・投稿のトピック (2 件)・いいね・返信 (空は `None`)。
- 見ているアカウント (`source_type=known_account`、`source_query=<名前>`) と、まとまりに出て
  いる投稿者は **別のもの** (今回はすべて本人。再投稿・引用では違いうる。書き換えない。試験あり)。
- 出てこなかったもの: 固定した投稿の札、再投稿の見出し、引用した投稿、「meta.ai」の札。
- 投稿者の基準 (breakout) には、同じ投稿者の **ほかの** 投稿が 5 件以上いる。この画面は、
  同じ投稿者の投稿を並べて読めるので、後で同じ投稿者を繰り返し読むのに向いている。ただし
  今回は保存していない (基準はまだ無い。閾値は変えない)。
- 版: `known_account` = `threads-known-account-verified-2026-09-29-v1`。これで保存の門
  (確かめた画面だけ保存する) を通れるのは for_you・search・trending_list・trending_topic・
  known_account。custom_feed は通れない。

## T6.5B の締め (2026-09-29)

外の画面の確認 (T6.5B) は **完了**。ただし、次の限りつき (すべての画面を確かめたわけではない)。

| 状態 | 画面 |
| --- | --- |
| 確認済み | for_you・search (既定の「上位検索結果」)・topic_for_you の一覧・topic_for_you の投稿・known_account (「スレッド」のタブ) |
| このアカウントでは使えない | custom_feed (リンクだけで見つかる自分で作ったフィードが無い) |
| 未確認 | 世の中のトレンド (global_trending)・検索の「最近」「プロフィール」のタブ・プロフィールの「返信」「メディア」「再投稿」のタブ・再投稿 / 共有 / 引用の数 |

言葉は分けて使う: `for_you` / `search` / `topic_for_you` / `known_account` / `custom_feed` /
`global_trending`。**`topic_for_you` はこのアカウント向けの一覧で、世の中のトレンド・Threads 全体の
人気・順位ではない。** (T6.5B.2 から、トピックの投稿の `source_type` の値も `topic_for_you`。
本番にこの値の行はまだ無かったので、書き換えは起きていない。)

## T6.5B.2 — 観察の集め方 (Collection Strategy)

5 件の確認は **確かめるためだけ** だった。分析には、もっと多くの投稿が要る。ただし
**安全は数より先**: 数を満たすために fail closed を弱めない・足りない分を埋めない。

### 方針 (`app/config/threads_observation_policy.json`、`threads-observation-policy-1`)

`.env` には置かない (git で管理する方針のファイル)。

- 1 日 (JST) の **重複を除いた投稿の数**: 最低の目安 50・通常の目安 90・上限 100
  (目標であって約束ではない)。
- 通常の割り当て (段階 3): for_you 25・search 20・topic_for_you 15・known_account 30 = 90。
- ページの上限: 1 ページのスクロール 3 回・1 ページで受け入れる投稿 25 件・1 回の実行で開く
  ページ 15・検索の語 5 個 / 日・トピックのページ 3 / 日・知っているアカウント 3 人 / 日。
- 計画に入れないもの: custom_feed (使えない)・global_trending (未確認)。
  topic_for_you を global_trending の代わりにしない。
- 方針は読み込み時に確かめる (`planning.validate_policy`): 段階ごとの合計が段階の上限と
  上限 100 を超えない・1 ページの予算が 25 以下・通常の割り当てが目安 90 と同じ・再観測を
  重複を除いた数に入れない。

### 検索の語 (`threads-search-queries-1`)

語の一覧: 生成AI・AI自動化・AI副業・ブログ・Threads運用。1 日に 4 語 × 5 件。
**JST の日付から決まる順** で回す (日付の通し番号 mod 語の数から 4 つ。毎日 1 つずつずれ、
5 日でどの語も 1 日ずつ休む)。乱数は使わない。観測にはどの語で見つけたか (`source_query`) が残る。
語の増減は方針のファイルで行う (版を上げる)。

### おすすめのトピック (topic_for_you)

見つけるためだけに使う。一覧を読み、**一覧の順** に、投稿が読めた最初の 3 トピック × 5 件。
一覧の順位・トピック・`source_type=topic_for_you` を残す。**ページのトピックを投稿のトピックに
写さない。** モデルがトピックを選ぶことはしない。

### 知っているアカウント (基準を作るための読み取り)

目的は、投稿者の基準 (同じ投稿者の **ほかの** 投稿 5 件以上) を使えるようにすること。
3 人 × 最大 10 件 = 30 件 / 日。

- 候補は、すでに観察した公開の投稿者だけ (有名人を決め打ちしない。自分は除く)。
- 順 (決まった規則): 基準がまだ足りない投稿者が先 → 最初に見つけた出どころの近さ
  (search → topic_for_you → for_you) → 最初に見た時刻 → 名前。**いいねの多さでは選ばない。**
- 候補が足りなければ、その分の予算は使わない (ほかの出どころに回さない)。

### 投稿者の一覧 (`services/threads_author_pool.py`、読むだけ・新しい表なし)

公開の名前・最初に見た時刻と出どころ・出どころの一覧・投稿の数・観測の数・最後に見た時刻・
知っているアカウントの画面で読んだことがあるか・基準の標本の数 (いいねが見える投稿)・
新しい投稿に基準が付くか (5 件以上)・持っている投稿のどれにも基準が付くか (6 件から。それまで
`bootstrap_needed`)。**フォロワー数・自己紹介などは持たない** (そのために読みに行かない)。

### 重複と出どころ (provenance)

- 1 日の件数は重複を除いた投稿の数。同じ投稿が for_you・search・topic_for_you・known_account の
  どれで見えても 1 つ。
- 見つけた出どころは全部残る: 投稿は `threads_external_posts` に 1 行 (最初の本文を残す)、観測は
  出どころごとに `threads_external_observations` に積む (`source_type`・`source_query`・`run_id`・
  時刻・その時の数)。**表を変える必要は無い** (migration なし)。
- 注意: 観測の行は「その時の数」と「出どころ」を兼ねる。同じ実行の中で別の出どころから見えた
  行は、速さ (velocity) の計算では同じ時点として扱う (実行ごとにまとめる)。

### 足りないとき・止まるとき (実行の仕組み `observer/orchestrator.py`)

- 出どころが予算より少なく返した → その数のまま。ほかを増やさない・スクロールを増やさない。
  例: 目安 90 で安全に読めたのが 72 → 結果は 72。
- 重複で数が減った → 減ったまま。
- アカウントに行けない / トピックのページが空 / 画面の形が違う / 勘定が合わない → その手順の
  投稿を捨てて (fail closed) 次の手順へ。実行は `partial`。
- ログインが要る → そこで全体を止め、何も残さない。
- 確かめていない画面の手順は実行しない。受け入れた合計 (重複を含む) が段階の上限を超えない
  ように各手順を切る (重複を除いた数も上限を超えない)。
- **この段階では本番で実行しない** (偽のページの試験だけ)。

### 計画のコマンド (読むだけ)

```
uv run python scripts/plan_threads_observation.py              # 段階 3 (通常)
uv run python scripts/plan_threads_observation.py --stage 1    # 段階 1 (試験)
uv run python scripts/plan_threads_observation.py --date 2026-09-30 --json
```

ブラウザを開かない・Threads に問い合わせない・DB に書かない (投稿者の候補は読むだけ)・
スケジュールを作らない。

**実行 (T6.5B.3)**: `--execute` に `--dry-run` (保存しない) か `--store` (本番の DB に保存) の
どちらかを必ず付ける (既定の書き込みは無い)。ブラウザを開く前に確かめること: 段階 (保存は
方針の今の段階の次まで)・すべての画面が確認済み・custom_feed / global_trending が無い・計画の数が
段階の上限と 100 以内・ページ / スクロール / 語 / アカウントの上限・専用のプロファイル・
`PLAYWRIGHT_BROWSERS_PATH`・`--store` なら DB の revision と観察の表。どれかが合わなければ
ブラウザを開かない (終了コード 3)。実行後、ログイン / 画面の形 / 勘定で止まった手順があれば
保存しない (終了コード 4)。保存した実行の `artifacts_json` には、計画 (`observation_plan`)・
手順ごとの結果 (`orchestration`)・出どころ (`provenance`) が残る。

```
uv run python scripts/plan_threads_observation.py --stage 1 --execute --dry-run --headed --screenshots --show-posts
uv run python scripts/plan_threads_observation.py --stage 1 --execute --store --headed --screenshots --show-posts
```

### 同じ投稿の再観測 (設計だけ)

重複を除いた新しい投稿の予算とは **別の予算** (将来 10〜20 件 / 日)。再観測は重複を除いた数に
入れない (`repeat_snapshot`)。今は無効 (予算 0)。どの投稿を読み直すかは T6.5C / T6.5E で決める。

### 記述の証拠の目安 (原因の証拠ではない)

| 目安 | 扱い |
| --- | --- |
| 外の投稿の使える数 < 30 | 傾向を判断できない (insufficient evidence) |
| まとまりの本数 < 10 | 形として述べない (insufficient sample) |
| まとまりの本数 ≥ 10 | 候補の形 (candidate pattern) だけ |
| まとまりの本数 ≥ 30 | より強い記述ができる (それでも原因ではない) |
| 投稿者の基準 | 同じ投稿者の **ほかの** 投稿 5 件以上 (変えない) |

自分の投稿の「中央値が高く / 低く観測されたまとまり」も、10 本以上のまとまりだけで比べる
(以前は 5 本)。Luna の生成は自動で変えない。

### 段階 (rollout)

| 段階 | 内容 | 状態 |
| --- | --- | --- |
| 0 | 5 件の画面の確認 | **完了** |
| 1 | 複数の画面の保存つきの試験 (約 20〜30 件。割り当て 26 件: for_you 8・search 2×4・topic_for_you 1×5・known_account 1×5) | **完了** (2026-09-29、run 2、下の「T6.5B.3」) |
| 2 | 50 件 / 日 | **人の許可が要る** (まだ動かしていない) |
| 3 | 80〜90 件 / 日 (通常) | 計画 |
| 4 | 上限 100 件まで (理由があるとき) | 計画 |

段階は 1 つずつ。前の段階の保存した値の質を確かめてから次へ。検証からいきなり自動の
100 件 / 日にはしない。

### 見込み (約束ではない)

| 1 日 | 14 日 | 30 日 |
| --- | --- | --- |
| 80 件 | 約 1,120 件の機会 | 約 2,400 件 |
| 90 件 | 約 1,260 件 | 約 2,700 件 |

実際の重複を除いた数は、重複・開けないページ・fail closed で止まった実行・形の崩れた投稿の
分だけ少なくなる。足りない値を作らない。

### まだしないこと

Windows のスケジュールは作らない (既存の 3 つのタスクも変えない)。夜の自動の観察はしない。
20 / 50 / 90 / 100 件の本番の観察はしない。T6.5C (伸びた候補の検出) は、意味のある数と
投稿者の基準がたまってから。

## T6.5B.3a — 投稿の本文とメディアの読み方の直し (2026-09-29)

Stage 1 の最初の dry-run (保存なし) で、画面と照らして 4 つの形の誤りが見つかった。DOM で
形を確かめてから、**文字ではなく形で** 直した (collector `t6.5b-collector-4`、selector
`threads-web-verified-2026-09-29-v3`)。migration なし。過去の実行 (run 1) は書き換えない。

| 誤り | 画面の形 (確認した DOM) | 直し方 |
| --- | --- | --- |
| 「ピン留め済み」が本文の 1 行目に入った | 見出し (投稿者・時刻) より **上** の行の `span[dir=auto]` | 本文は、その投稿の時刻のリンクより **後** の文字だけ |
| 「他1件を見る」が本文に入った | 指標の列 (いいね等) より **下** の行 (小さなアイコン + `span[dir=auto]`) | 本文は、最初の指標のボタンより **前** の文字だけ |
| 閲覧数のカード (閲覧数・99万・30日・日付) が本文に入った | 本物の `<button>` の中 (`role` 無し) | `<button>` の中は本文ではない (`role="button"` と同じ) |
| GIF のスタンプで `media_type=image` になった | 本文の `span[dir=auto]` の中の小さな `<img>` | 添付のメディアは `<picture>`・その投稿の `/post/<code>/media` のリンク・画像を開くボタンの中の画像だけ。本文の中の画像・外のリンクの見出しの画像・`<button>` の中の画像は添付ではない (診断の数だけ) |

さらに、「meta.ai」の札の形が分かった: `<a role="link" href="/@meta.ai">` にアイコン (svg) と
文字 `meta.ai`。この形だけを本文から除く (DOM の段階)。書いた人の `@meta.ai` の言及 (アイコン
なし) や、本文の中の文字の「meta.ai」は残る。過去の行のために `threads-body-normalizer-1` は
そのまま残す。

書いた人が本文に「ピン留め済み」「他1件を見る」と書いても、本文の範囲の中なので残る (試験あり)。
メディアの判定の出どころは `media_diagnostics` (`media_detection_source`・
`inline_media_ignored_count`・`embedded_card_images_ignored`・`link_preview_images_ignored`) で
見える (表示だけ。DB には入れない)。取り込み済みの画面の構造 456 件で確かめ直した: 本文が
変わったのは 13 件 (すべて上の形)、`image` → `none` は 12 投稿 (本文の中の画像・外のリンクの
見出し・埋め込みのカード)。`<picture>` / `/media` のリンクの画像はすべて `image` のまま。

## T6.5B.3 — 段階 1 の保存つきの試験 (2026-09-29)

T6.5B.3a の直しのあと、段階 1 の計画 (26 件、6 ページまで、スクロール 15 回まで) を **dry-run で
最後まで** 動かし (保存なし、JST 10:55)、画面と照らしてから、**1 回だけ** 保存つきで動かした
(JST 11:04、run 2)。**保存は 1 回だけ**: 再試行・2 回目・予算の追加はしていない。

| 手順 | 画面 | 予算 | 取った | 新しい投稿 | 状態 |
| --- | --- | --- | --- | --- | --- |
| 1 | for_you | 8 | 8 | 8 | succeeded |
| 2 | search「ブログ」 | 4 | 4 | 4 | succeeded |
| 3 | search「Threads運用」 | 4 | 4 | 4 | succeeded |
| 4 | topic_for_you「ハンドメイド」(一覧の 1 番目) | 5 | 5 | 5 | succeeded |
| 5 | known_account (投稿者の一覧の 1 人) | 5 | 5 | 5 | succeeded |

- 保存: run 2 = `succeeded`・26 件・捨てた 0 件・collector `t6.5b-collector-4`・selector
  `threads-web-verified-2026-09-29-v3`。投稿 26 行・観測 26 行・おすすめのトピック 1 行が増えた。
  ほかの表・schema・revision (`2cfa0ccb2059`) は変わっていない。
- 重複: 同じ投稿の重複 0 (手順の間の重複も 0)。出どころ (provenance) は run の
  `artifacts_json` に全部残る。
- run 1 は変わっていない (観測 5 行の指紋が前と同じ、投稿 1〜5 の `last_seen_at` も同じ)。
- 本文の質: 26 件すべて `text_clean`。画面の部品 (ピン留め済み・他N件を見る・閲覧数のカード・
  meta.ai の札・N/M の印) の混ざりは 0。
- メディア: image 8・none 15・video 3。`<picture>` で判定、本文の中の画像での誤判定は 0。
- 指標: 20 件 `complete`、6 件 `partial_metrics` (画面でいいね / 返信の数が空の投稿)。
- 画面との照合: 26 件のうち 22 件を画面で確かめ、食い違いは 0。残り 4 件 (For You の 7・8 番目、
  トピックの 4・5 番目) は全体のスクリーンショットで **描かれていなかった** (画面の外の投稿を
  Threads が描かない。後ろが白い)。値は他と同じ形で読めていて食い違いの証拠は無いが、目では
  確かめていない。次の段階の前に、スクロールごとの画面の撮り方を考える。
- ログインの要求・画面の形の崩れ・上限の問題・社会的な操作 (いいね・返信・フォロー等): なし。

### 分析 (記述だけ)

`scripts/analyze_threads_trends.py`: 使える投稿 31 件 (run 1 の 5 件 + run 2 の 26 件) で最低の
目安 30 件は越えた → `descriptive_only`。まとまりごと: 10 件以上は `candidate_pattern`、30 件以上
(`has_url=False` 31 件) だけ `stronger_descriptive`。投稿者の基準を作れたのは 1 人 (知っている
アカウントの 6 件)。その人の固定の投稿が **返信** で基準の 16 倍 (基準の中央値 1.0、ほかの 5 件) の
伸びた候補。いいねの基準は数のある投稿が 3 件で足りない (`insufficient_author_baseline`)。
CLI の 1 行の表示はいいねだけを出すので「None 倍」と出る (値は JSON で正しい。表示の直しは別の作業)。
原因の証拠ではない。生成・公開には何も戻さない。

### 段階 2 (50 件 / 日) の前に

段階 2 は **人の許可が要る**。方針のファイルの `rollout.current_stage` は 0 のまま (1 にすると
段階 2 の保存が通るようになる)。許可するときに、人が 1 にする。スケジュールは作らない。

## 次の確認点: ほかの画面の dry-run (保存しない)

それぞれ **dry-run だけ・5 件以下・画面と照らす** (`--show-posts --diagnose`)。確かめた画面ごとに
selectors の確認の状態を記録してから、保存に使う。

- A. 検索: 確認済み (上の「検索の画面の確認」)。保存つきで使うのは、人の許可のあと。
- B. トピックの一覧: 確認済み (上の「トピックの一覧の確認」)。
- C. カスタムフィード: このアカウントでは使えない (上の「カスタムフィードの確認」)。
- D. 知っているアカウント: 確認済み (上の「知っているアカウントの確認」)。

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
