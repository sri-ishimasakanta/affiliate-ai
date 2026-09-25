# Featured image 残り 21 記事の制作ブリーフ (W1.5A)

画像生成と組版をする人が **このまま使える** 指示書。仕様の根拠は
[`featured-image-pipeline.md`](featured-image-pipeline.md) (W1 の設計)、機械可読な入力は
[`featured-image-w1.5-manifest.json`](featured-image-w1.5-manifest.json)、確認と適用の段取りは
[`featured-image-w1.5-review-plan.md`](featured-image-w1.5-review-plan.md)。

この段階 (W1.5A) は **設計だけ**。画像はまだ作っていない。WordPress には何も書かない
(upload・`featured_media` の設定・post の変更・media 99 の削除・Cocoon の設定変更はしない)。

対象: article 1〜19・21・22 の 21 記事 (20 / 23 / 24 / 25 は W1.4 で適用済みなので除く)。

---

## 0. 共通の作り方 (W1.4 と同じ)

### 手順

1. **背景とモチーフだけ** を作る (文字なし)。画像生成でも、手でベクター (SVG) を描いてもよい。
2. 文字は **あとから組版** で重ねる (SVG / Pillow)。画像生成モデルに日本語を描かせない。
3. 1200 × 675 の PNG を master として書き出し、WebP に変換する。ファイル名は各記事の
   `planned_file` (例 `featured-7-ai-meeting-notes.webp`)。置き場所は
   `artifacts/featured-images/w1.5/batch-<N>/` (git 管理外)。
4. §7 の確認をしてから人に見せる (1 バッチずつ)。

### キャンバスと領域 (1200 × 675 px)

| 領域 | 範囲 (x, y, 幅, 高さ) | 置いてよいもの |
| --- | --- | --- |
| 外周マージン | 上下 60 / 左右 72 の内側だけを使う | 下端のアクセント帯 (装飾) を除き何も置かない |
| **ラベル予約域** | (0, 0, 560, 170) | 背景の色・模様と、モチーフから伸びる線や面の端だけ。**文字・顔・人型・主モチーフの中心は置かない** |
| 見出しの列 | (72, 230, 568, 385) | 見出しの印、見出し、補助語 (21 記事とも幅 568 のまま。広げる記事は無い) |
| モチーフの領域 | (660, 90, 468, 525) | 主モチーフ。記事ごとの箱と中心は各節の「構図」 |
| 下端のアクセント帯 | (0, 663, 1200, 12) | アクセント色のベタ帯 |

### 組版 (21 記事共通。W1.5B.1 で適用済みの試作 4 枚に合わせて較正)

| 要素 | 書体・太さ | サイズ | 色 | 位置 |
| --- | --- | --- | --- | --- |
| 見出しの印 | — | 128 × 18 のベタ | アクセント色 | (72, 322) |
| 見出し | Noto Sans JP **Bold**、palt 有効 | **92px** (21 記事とも。2 行) | `#12263F` | 左揃え、x = 72。1 行目の em box の上端 y = 367、行送り 92px (行間 1.0)。baseline は em box の上端 + 0.88em |
| 補助語 | Noto Sans JP **Bold** | 44px | `#12263F` | 見出しの最後の行の em box の下端から 14px 下が em box の上端、左揃え |
| 下端のアクセント帯 | — | 1200 × 18 | アクセント色 | (0, 657) (下端まで) |
| 背景 | — | — | `#F5F7FA` | 全面。ごく薄い幾何学模様を 4〜6% の濃さで入れてよい |

- 見出しはすべて **2 行・1 行あたり全角 5 字まで**。92px・palt で最も広い行は
  460px (「業務効率化」)、列は 568px。補助語は 44px で最も広いものが 528px (「単体・追加契約・組み込み」)。
  実測は NotoSansJP-VF 2.004 を fontTools で wght 700 に固定し、palt は GPOS の値 (2026-09-25)。
- 組版は `scripts/compose_featured_image.py` が行う (座標は manifest の `typesetting`。
  `app/wordpress/featured_image_batch.py` の `PRODUCTION_TYPESETTING` と同じ値でなければ止まる)。

#### 較正 (W1.5B.1)

適用済みの試作 4 枚 (20 / 23 / 24 / 25) の画像を測り、W1.5A の仕様から次のとおり変えた
(manifest の `typesetting_calibration` に測った値と理由を記録)。

| 項目 | W1.5A の仕様 (廃止) | 本番 (W1.5B.1 以降) | 試作 4 枚で測った範囲 |
| --- | --- | --- | --- |
| 見出しの印 | 64×8 at (72, 250) | 128×18 at (72, 322) | 128〜131 × 17〜20、x 63、y 297〜352 |
| 見出し | 104px、上端 282、行間 1.2 | 92px、上端 367、行間 1.0 (baseline 448 / 540) | 88〜99px、行送り 88〜92px |
| 補助語 | Noto Sans JP Medium 44px `#4A5B70`、間 28px | Bold 44px `#12263F`、間 14px (baseline 604) | 44〜61px、Bold、紺 |
| 下端の帯 | y 663、高さ 12 | y 657、高さ 18 | y 653〜658、高さ 17〜22 |

試作と違えたところ (わずか):

- x は W1 の左の安全余白 72 のまま (試作は 58〜63)。320×180 で約 3px、126×71 で約 1px の差。
- 補助語の em box の下端は 609 で、下の余白 615 の内側 (試作 23 は約 625 まで下がっていた)。

- サイズを記事ごとに変えない (一覧で並んだときに見出しの大きさがそろうように)。
- 文字に影・縁取り・グラデーションを付けない。
- 見出しの語は記事のタイトル全体ではなく、読んで得られることを短く示す語。商品名
  (HubSpot / Make / Notion) は **普通の書体の文字** として書くだけで、ロゴ風にしない。

### 画像生成に使う共通の文 (英語)

各記事の「画像生成の文」は、記事固有の文 + 次の共通の文 (`{ACCENT}` は記事のアクセント色)。
manifest の `generation_prompt` は、この 2 つをつないだ完成形。

- 付ける (positive): `flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color {ACCENT} with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`
- 付けない (negative): `text, letters, numbers, captions, watermark, logo, brand mark, product screenshot, user interface of a real product, photo, photorealistic, people, faces, hands, robot, humanoid, neon, glow, heavy gradient, rainbow colors, clutter, emoji, 3d render, stock photo`

生成した画像の **左半分と左上は空けておく**。左上 (0, 0, 560, 170) に主モチーフの中心が
来た画像は使わない。数字・文字らしき形が出た画像は、消すのではなく作り直す。

---

## 1. 系統と見分け方

似た記事は、**① モチーフの形 → ② 見出しの語 → ③ 色の明るさ** の順で見分ける
(pipeline §2.5.1 と同じ)。色だけで見分けさせない (グレースケールでも区別できること)。

| 系統 | アクセント | 記事 | 共通の小さな印 |
| --- | --- | --- | --- |
| AI・業務効率化 (ツール総論) | `#0D9488` teal | 1 (+ 試作 25) | a set of different tool-category tiles |
| 議事録・文字起こし | `#0284C7` sky | 2 / 6 / 7 / 8 / 9 | audio waveform + document lines |
| 議事録・文字起こし (文字起こし寄り) | `#0369A1` deep sky | 3 / 12 | audio waveform turning into text lines |
| CRM・SFA | `#EA580C` orange | 4 / 5 / 14 | funnel / deal pipeline / contact card |
| プロジェクト・タスク管理 | `#DB2777` rose (new in W1.5; confirm at batch review) | 13 / 15 | board columns / timeline bars / task table |
| RPA・自動化 | `#16A34A` green | 10 / 11 / 16 / 17 / 18 | Make: round app modules on a connected line / RPA: desktop window with a loop arrow |
| AIエージェント・生成AI | `#4F46E5` indigo | 19 (+ 試作 24) | panels showing how the AI is used |
| ガイドライン・ガバナンス | `#475569` slate | 21 / 22 | document + check (guideline) / cycle + flag (governance) |

同じ系統の中の形の違い (いちばん混ざりやすい組):

| 組 | 形の違い |
| --- | --- |
| 2 ↔ 6 | 2 = 立てた 2 枚の議事録カード (用途の印つき) / 6 = 1 枚の比較表 (行と列) |
| 2 ↔ 7 | 2 = 並んだ 2 枚 / 7 = 中心の文書から 3 つの節点に線が伸びる概念図 |
| 8 ↔ 9 | 8 = ∞ と砂時計の 2 枚のカード / 9 = 席のアイコンと切り替えスイッチつきの見積もりシート |
| 8 ↔ 12 | 8 = 2 枚のカード (sky) / 12 = クリップボードのチェックリストと注意の印 (deep sky) |
| 3 ↔ 2 | 3 = 波形がテキストの行に変わる変換 (deep sky) / 2 = 出来上がった議事録カード (sky) |
| 4 ↔ 14 | 4 = 離れた 2 枚のパネル / 14 = 重なった 2 つの領域 (ベン図) |
| 5 ↔ 23 | 5 = オレンジの段差カード + 離れた 1 回限りの札 / 試作 23 = 青い段差カードと席だけ |
| 10 ↔ 18 | 10 = 曲線でつながる丸いモジュール / 18 = 上り階段とデスクトップの窓 |
| 11 ↔ 5 | 11 = カードの中にモジュールの鎖と時計 / 5 = 席と漏斗の印と 1 回限りの札 |
| 16 ↔ 17 | 16 = 同じ窓のタイルが漏斗を通って 2 つに絞られる / 17 = 5 列の比較の格子 |
| 16 ↔ 1 | 16 = 緑・同じ窓のタイル・漏斗 / 1 = ティール・違う種類の印のタイル・漏斗なし |
| 6 ↔ 17 | 6 = 2 列の表 (sky) / 17 = 5 列の格子と鍵・硬貨の印 (green) |
| 13 ↔ 15 | 13 = カンバンとタイムラインの 2 枚のボード / 15 = 1 枚のデータベースの表 |
| 21 ↔ 22 | 21 = 見出しつきの文書とチェック / 22 = 5 つの矢印の輪と旗 (盾を使わない) |
| 19 ↔ 24 | 19 = 横に並ぶ 3 枚のパネル / 試作 24 = 中心から線が伸びる概念図 |

---

## 2. バッチ 1 — 議事録 (5 枚)

並べる順: 7 → 2 → 6 → 8 → 9。同じ 2 ツールを扱う最大の系統で、見分けがいちばん難しいので最初に確かめる。

### article 7 — AI議事録｜意味・種類・選び方の基礎知識

| 項目 | 内容 |
| --- | --- |
| article_id / type | 7 / `category_landing` |
| URL | https://bizfluxlab.com/ai-meeting-notes/ |
| slug | `ai-meeting-notes` |
| **見出し (2 行, 92px)** | **「AI議事録」 / 「とは」** (実測 365px / 167px / 列 568px) |
| 予備 | 「AI議事録入門」 / 「種類と観点」 (使う前に実測し直す) |
| 補助語 (44px) | 種類と選ぶ観点 (実測 308px) |
| アクセント色 | `#0284C7` (議事録・文字起こし) |
| ファイル名 | `featured-7-ai-meeting-notes.webp` |
| alt | AI議事録の仕組みを表す概念図 |
| media の title | AI議事録｜意味・種類・選び方の基礎知識 アイキャッチ |

**モチーフ**: A central document node with a waveform header, linked by thin lines to three satellite nodes: a microphone (capture), an uploaded audio file (import) and a checklist (tasks extracted). Concept-map style that mirrors the article's three processing stages.

**構図**: モチーフは (690, 120) 〜 (1128, 590) に
収め、中心を (909, 355) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'what AI meeting notes are' (the category entry), calmer than the product articles.

**見分ける相手**: 24: pilot 24 is an indigo map with calendar/document/table/message; this is sky with waveform, microphone and file; 2: 2 is two parallel cards

**避けるもの (この記事で特に)**: microphone held by a hand; meeting room with people; tool logos

**画像生成の文**: `a concept map: one central rounded document node with a small audio waveform on top, linked by thin lines to three smaller round nodes containing a microphone, an audio file with an arrow, and a short checklist; sky blue #0284C7 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #0284C7 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: Headline mirrors pilot 24 ('AIエージェント/とは') on purpose: category-landing articles share the 'とは' pattern.

### article 2 — AI議事録おすすめ｜選び方と目的別の比較

| 項目 | 内容 |
| --- | --- |
| article_id / type | 2 / `recommendation_roundup` |
| URL | https://bizfluxlab.com/ai-meeting-notes-tools/ |
| slug | `ai-meeting-notes-tools` |
| **見出し (2 行, 92px)** | **「AI議事録」 / 「の選び方」** (実測 365px / 364px / 列 568px) |
| 予備 | 「AI議事録を選ぶ」 / 「目的別に選ぶ」 (使う前に実測し直す) |
| 補助語 (44px) | 目的別の比較 (実測 264px) |
| アクセント色 | `#0284C7` (議事録・文字起こし) |
| ファイル名 | `featured-2-ai-meeting-notes-tools.webp` |
| alt | AI議事録の選び方を表す2枚の議事録カードの図 |
| media の title | AI議事録おすすめ｜選び方と目的別の比較 アイキャッチ |

**モチーフ**: Two meeting-note document cards standing side by side, each topped with a short waveform strip. The left card has a small headset glyph (online meetings), the right card a small magnifier over list lines (finding and sharing). A check badge sits at the foot of each card: two options, each fitting a purpose.

**構図**: モチーフは (690, 150) 〜 (1128, 570) に
収め、中心を (909, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'how to choose an AI meeting-notes tool' (roundup by purpose).

**見分ける相手**: 6: 6 is a comparison table with rows; this is two separate standing cards with purpose glyphs; 7: 7 is a node map around one central document

**避けるもの (この記事で特に)**: Krisp or Fireflies.ai logos or screens; video-call grids with faces

**画像生成の文**: `two tall document cards standing side by side, each card topped by a short audio waveform strip and filled with blank grey lines; the left card has a small headset glyph, the right card a small magnifying glass over list lines; a round checkmark badge at the bottom of each card; sky blue #0284C7 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #0284C7 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article covers Krisp and Fireflies.ai only; the two cards reflect 'two options' without naming them.

### article 6 — AI議事録比較｜違い・料金・選び方

| 項目 | 内容 |
| --- | --- |
| article_id / type | 6 / `comparison_listicle` |
| URL | https://bizfluxlab.com/ai-meeting-notes-comparison/ |
| slug | `ai-meeting-notes-comparison` |
| **見出し (2 行, 92px)** | **「AI議事録」 / 「違いを比較」** (実測 365px / 455px / 列 568px) |
| 予備 | 「2つの違い」 / 「違いの見方」 (使う前に実測し直す) |
| 補助語 (44px) | 無料範囲・料金・機能 (実測 440px) |
| アクセント色 | `#0284C7` (議事録・文字起こし) |
| ファイル名 | `featured-6-ai-meeting-notes-comparison.webp` |
| alt | AI議事録の違いを比較する表の図 |
| media の title | AI議事録比較｜違い・料金・選び方 アイキャッチ |

**モチーフ**: A blank comparison table: two tall columns separated by a centre divider, five rows. Each cell holds a check, a dash or a short bar of different length; the two column headers are small waveform glyphs (no names).

**構図**: モチーフは (700, 130) 〜 (1120, 580) に
収め、中心を (910, 355) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'the differences between AI meeting-notes tools, axis by axis'.

**見分ける相手**: 2: 2 is two standing cards with purpose glyphs; this is one table with rows; 17: 17 is a wider 5-column matrix in green

**避けるもの (この記事で特に)**: Krisp or Fireflies.ai logos; letters or numbers in the table

**画像生成の文**: `a clean blank comparison table with two tall columns separated by a thin centre divider and five rows; cells contain checkmarks, short dashes and small horizontal bars of different lengths; each column header is a small audio waveform glyph; sky blue #0284C7 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #0284C7 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article compares only two tools on five axes (free range, price band, annual discount, features, uses).

### article 8 — AI議事録無料｜プラン別の料金と選び方

| 項目 | 内容 |
| --- | --- |
| article_id / type | 8 / `pricing` |
| URL | https://bizfluxlab.com/ai-meeting-notes-free/ |
| slug | `ai-meeting-notes-free` |
| **見出し (2 行, 92px)** | **「AI議事録」 / 「無料の範囲」** (実測 365px / 459px / 列 568px) |
| 予備 | 「無料で使う」 / 「無料プラン」 (使う前に実測し直す) |
| 補助語 (44px) | 無料プランとトライアル (実測 484px) |
| アクセント色 | `#0284C7` (議事録・文字起こし) |
| ファイル名 | `featured-8-ai-meeting-notes-free.webp` |
| alt | AI議事録の無料プランと無料トライアルの違いを表す図 |
| media の title | AI議事録無料｜プラン別の料金と選び方 アイキャッチ |

**モチーフ**: Two cards side by side: the left card carries an infinity loop (free with no time limit), the right card an hourglass above a short strip of blank calendar squares (a time-limited trial). A thin bracket spans both cards. No numbers.

**構図**: モチーフは (690, 160) 〜 (1128, 560) に
収め、中心を (909, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'what you can do for free with AI meeting notes' (free plan vs trial).

**見分ける相手**: 12: 12 is a clipboard checklist with a caution mark; this is two cards (infinity vs hourglass); 9: 9 is an estimate sheet with seat icons and a toggle

**避けるもの (この記事で特に)**: the word FREE; price tags with numbers; day counts

**画像生成の文**: `two rounded cards side by side; the left card shows a bold infinity loop symbol, the right card shows an hourglass above a short row of blank calendar squares; a thin bracket line under both cards; no numbers; sky blue #0284C7 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #0284C7 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article's core is 'Free forever' vs a 7-day trial; the day count must not appear (it can change).

### article 9 — AI議事録料金｜プラン別の費用と選び方

| 項目 | 内容 |
| --- | --- |
| article_id / type | 9 / `pricing` |
| URL | https://bizfluxlab.com/ai-meeting-notes-pricing/ |
| slug | `ai-meeting-notes-pricing` |
| **見出し (2 行, 92px)** | **「AI議事録」 / 「料金の見方」** (実測 365px / 459px / 列 568px) |
| 予備 | 「料金の見方」 / 「費用の見積もり」 (使う前に実測し直す) |
| 補助語 (44px) | 人数・月払い・年払い (実測 440px) |
| アクセント色 | `#0284C7` (議事録・文字起こし) |
| ファイル名 | `featured-9-ai-meeting-notes-pricing.webp` |
| alt | AI議事録の料金の見方を表す見積もりシートの図 |
| media の title | AI議事録料金｜プラン別の費用と選び方 アイキャッチ |

**モチーフ**: A small stack of seat icons feeding into an estimate sheet (blank lines with a waveform glyph in its header); above the sheet a two-position toggle switch (monthly / annual billing); the sheet's last line is a dotted outline (an upper plan without a published price).

**構図**: モチーフは (690, 130) 〜 (1128, 580) に
収め、中心を (909, 355) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'how to read AI meeting-notes pricing' (per seat, monthly vs annual).

**見分ける相手**: 8: 8 is two cards (infinity vs hourglass); 23: pilot 23 is three stepped blue cards; this is one estimate sheet with a toggle

**避けるもの (この記事で特に)**: $, ¥, amounts; calculator keypad with digits; tool logos

**画像生成の文**: `an estimate sheet with blank grey lines and a small audio waveform glyph in its header, a small stack of simple seat icons on its left connected by an arrow, a two-position toggle switch above the sheet, the last line of the sheet drawn as a dotted outline; no numbers or currency; sky blue #0284C7 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #0284C7 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article stresses per-seat billing, monthly vs annual amounts and whether upper plans publish a price.

---

## 3. バッチ 2 — 文字起こし + プロジェクト・タスク管理 (4 枚)

並べる順: 3 → 12 → 13 → 15。バッチ 1 の隣で sky の系統を仕上げ、新しい rose のアクセントを 2 記事だけで試す。

### article 3 — 文字起こしAIおすすめ｜選び方と目的別の比較

| 項目 | 内容 |
| --- | --- |
| article_id / type | 3 / `recommendation_roundup` |
| URL | https://bizfluxlab.com/ai-transcription-tools/ |
| slug | `ai-transcription-tools` |
| **見出し (2 行, 92px)** | **「文字起こし」 / 「AIの選び方」** (実測 434px / 453px / 列 568px) |
| 予備 | 「文字起こしAI」 / 「音声をテキストに」 (使う前に実測し直す) |
| 補助語 (44px) | 言語とファイル対応 (実測 396px) |
| アクセント色 | `#0369A1` (議事録・文字起こし (文字起こし寄り)) |
| ファイル名 | `featured-3-ai-transcription-tools.webp` |
| alt | 文字起こしAIの選び方を表す音声波形がテキストになる図 |
| media の title | 文字起こしAIおすすめ｜選び方と目的別の比較 アイキャッチ |

**モチーフ**: A wide audio waveform band enters from the left of the motif area, passes through a slim converter bar and leaves on the right as horizontal text lines. Below it, two small parallel panels: an audio-file card with an upload arrow, and a globe (languages).

**構図**: モチーフは (680, 140) 〜 (1128, 580) に
収め、中心を (904, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'choosing a transcription AI' (input-focused: audio files and languages), not meeting minutes.

**見分ける相手**: 2: 2 shows finished meeting-note cards (output); this shows audio becoming text (conversion) and uses the deeper sky; 12: 12 is a clipboard checklist with a caution mark

**避けるもの (この記事で特に)**: Krisp or Fireflies.ai logos or screens; microphone with a person; letters inside the text lines

**画像生成の文**: `a wide audio waveform band flowing from left to right through a slim vertical converter bar and turning into neat horizontal grey placeholder lines like a transcript; below it two small rounded panels side by side, one an audio file card with an upward upload arrow, the other a simple globe; deep sky blue #0369A1 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #0369A1 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article compares by supported languages, audio-file handling and price; the file card and globe reflect the first two.

### article 12 — 文字起こし無料｜要点と実務上の注意点

| 項目 | 内容 |
| --- | --- |
| article_id / type | 12 / `informational` |
| URL | https://bizfluxlab.com/ai-transcription-free/ |
| slug | `ai-transcription-free` |
| **見出し (2 行, 92px)** | **「無料で」 / 「文字起こし」** (実測 267px / 434px / 列 568px) |
| 予備 | 「無料の条件」 / 「無料と試用」 (使う前に実測し直す) |
| 補助語 (44px) | 無料プランと試用の違い (実測 484px) |
| アクセント色 | `#0369A1` (議事録・文字起こし (文字起こし寄り)) |
| ファイル名 | `featured-12-ai-transcription-free.webp` |
| alt | 無料で文字起こしする際の注意点を表すチェックリストの図 |
| media の title | 文字起こし無料｜要点と実務上の注意点 アイキャッチ |

**モチーフ**: A clipboard checklist with three rows (two checked, one open), a small waveform at the clipboard top, and a caution triangle next to a small hourglass (a trial that ends).

**構図**: モチーフは (720, 130) 〜 (1128, 580) に
収め、中心を (924, 355) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'points to check before transcribing for free' (informational, cautious).

**見分ける相手**: 8: 8 is two cards (infinity vs hourglass); this is a clipboard checklist; 3: 3 is a waveform turning into text lines

**避けるもの (この記事で特に)**: the word FREE; day counts; tool logos

**画像生成の文**: `a clipboard with a three-row checklist, two rows checked and one row open, a small audio waveform glyph at the top of the clipboard, beside it a small caution triangle and a small hourglass; deep sky blue #0369A1 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #0369A1 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article is neutral ('not recommending a service'); the caution mark keeps the card informational.

### article 13 — プロジェクト管理ツールおすすめ｜選び方と目的別の比較

| 項目 | 内容 |
| --- | --- |
| article_id / type | 13 / `recommendation_roundup` |
| URL | https://bizfluxlab.com/project-management-tools/ |
| slug | `project-management-tools` |
| **見出し (2 行, 92px)** | **「チーム進捗」 / 「の見える化」** (実測 443px / 442px / 列 568px) |
| 予備 | 「進捗を1か所に」 / 「プロジェクト管理」 (使う前に実測し直す) |
| 補助語 (44px) | プロジェクト管理ツール (実測 484px) |
| アクセント色 | `#DB2777` (プロジェクト・タスク管理) |
| ファイル名 | `featured-13-project-management-tools.webp` |
| alt | プロジェクトの進捗管理を表すボードとタイムラインの図 |
| media の title | プロジェクト管理ツールおすすめ｜選び方と目的別の比較 アイキャッチ |

**モチーフ**: Two parallel boards: the left a kanban of three columns with blank cards (one card moving across with an arrow), the right a timeline of staggered horizontal bars with a small dashboard gauge.

**構図**: モチーフは (680, 160) 〜 (1128, 560) に
収め、中心を (904, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'making team progress visible' (project-management roundup).

**見分ける相手**: 15: 15 is a single database table with checkboxes; 1: 1 is a grid of mixed category tiles in teal

**避けるもの (この記事で特に)**: ClickUp or monday.com logos or colour stripes; people at desks

**画像生成の文**: `two rounded boards side by side; the left board shows three kanban columns with small blank cards and one card moving to the next column with an arrow; the right board shows staggered horizontal timeline bars and a small semicircle gauge; rose #DB2777 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #DB2777 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article covers ClickUp and monday.com; board + timeline reflect 'tasks and dashboards' without names. Rose #DB2777 is a new family accent; confirm it at the batch 2 review.

### article 15 — Notionタスク管理｜要点と実務上の注意点

| 項目 | 内容 |
| --- | --- |
| article_id / type | 15 / `informational` |
| URL | https://bizfluxlab.com/notion-task-management/ |
| slug | `notion-task-management` |
| **見出し (2 行, 92px)** | **「Notionで」 / 「タスク管理」** (実測 393px / 437px / 列 568px) |
| 予備 | 「DBでタスク管理」 / 「タスクをDBで」 (使う前に実測し直す) |
| 補助語 (44px) | データベースで表す (実測 396px) |
| アクセント色 | `#DB2777` (プロジェクト・タスク管理) |
| ファイル名 | `featured-15-notion-task-management.webp` |
| alt | Notionのデータベースでタスクを管理する表の図 |
| media の title | Notionタスク管理｜要点と実務上の注意点 アイキャッチ |

**モチーフ**: A database table of five rows: a checkbox column, a column of blank status pills and a column of short bars; two rows checked; a small caution mark at the table's lower edge (free-plan conditions).

**構図**: モチーフは (700, 140) 〜 (1128, 580) に
収め、中心を (914, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'task management in Notion via databases' (informational).

**見分ける相手**: 13: 13 is two boards (kanban + timeline); this is one table; 12: 12 is a clipboard checklist in deep sky

**避けるもの (この記事で特に)**: Notion 'N' cube logo; Notion page screenshot; letters in the table

**画像生成の文**: `a clean database table with five rows, the first column of checkboxes with two checked, a second column of small blank rounded status pills, a third column of short grey bars, and a small caution triangle at the lower right edge of the table; rose #DB2777 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #DB2777 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: 'Notion' is plain type in the headline only.

---

## 4. バッチ 3 — CRM・SFA + ツール総論 (4 枚)

並べる順: 1 → 4 → 5 → 14。article 1 は HubSpot と Pipedrive も扱うので、CRM のカードと試作 25 の隣で確かめる。

### article 1 — 業務効率化ツールおすすめ｜選び方と目的別に比較

| 項目 | 内容 |
| --- | --- |
| article_id / type | 1 / `recommendation_roundup` |
| URL | https://bizfluxlab.com/%e6%a5%ad%e5%8b%99%e5%8a%b9%e7%8e%87%e5%8c%96-%e3%83%84%e3%83%bc%e3%83%ab-%e3%81%8a%e3%81%99%e3%81%99%e3%82%81-roundup/ |
| slug | `業務効率化-ツール-おすすめ-roundup` |
| **見出し (2 行, 92px)** | **「業務効率化」 / 「ツール選び」** (実測 460px / 448px / 列 568px) |
| 予備 | 「目的別に選ぶ」 / 「ツールの絞り方」 (使う前に実測し直す) |
| 補助語 (44px) | 用途別に候補を絞る (実測 396px) |
| アクセント色 | `#0D9488` (AI・業務効率化 (ツール総論)) |
| ファイル名 | `featured-1-business-efficiency-tools-roundup.webp` |
| alt | 業務効率化ツールを目的別に選ぶことを表すツールカテゴリのタイル図 |
| media の title | 業務効率化ツールおすすめ｜選び方と目的別に比較 アイキャッチ |

**モチーフ**: A 3x2 grid of rounded tiles, each holding a different simple tool-category glyph (calendar, checklist, kanban columns, funnel, connected blocks, clock). Two tiles are lifted and ringed in teal with a small check: narrowing many categories down to a few candidates.

**構図**: モチーフは (680, 150) 〜 (1128, 570) に
収め、中心を (904, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'choose work-efficiency tools by purpose' (a multi-category roundup), not a single product.

**見分ける相手**: 25: pilot 25 is a two-lane workflow; this is a static grid of tiles; 16: 16 filters identical desktop windows through a funnel; this grid mixes different category glyphs and has no funnel

**避けるもの (この記事で特に)**: Make, HubSpot, ClickUp, monday.com, Pipedrive, Reclaim.ai or Todoist logos or screens; more than 6 tiles

**画像生成の文**: `a neat 3 by 2 grid of six rounded square tiles, each tile with a different simple glyph (a calendar, a checklist, three kanban columns, a funnel, two connected blocks, a clock dial without numbers); two of the tiles are slightly raised with a teal #0D9488 ring and a small checkmark badge; placed on the right half of the canvas, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #0D9488 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article compares 7 tools across CRM, project, task, iPaaS and AI scheduling; the category glyphs stand for that spread without naming any product. DB article_type is null; treated as recommendation_roundup from the title and structure. WordPress stores the Japanese slug percent-encoded; the apply manifest must use the slug exactly as the REST API returns it (see the review plan).

### article 4 — CRMおすすめ｜選び方と目的別の比較

| 項目 | 内容 |
| --- | --- |
| article_id / type | 4 / `recommendation_roundup` |
| URL | https://bizfluxlab.com/crm-tools/ |
| slug | `crm-tools` |
| **見出し (2 行, 92px)** | **「CRMの」 / 「選び方」** (実測 292px / 273px / 列 568px) |
| 予備 | 「CRMを選ぶ」 / 「CRM選び」 (使う前に実測し直す) |
| 補助語 (44px) | 無料範囲と有料プラン (実測 440px) |
| アクセント色 | `#EA580C` (CRM・SFA) |
| ファイル名 | `featured-4-crm-tools.webp` |
| alt | CRMの選び方を表す顧客管理と商談パイプラインの図 |
| media の title | CRMおすすめ｜選び方と目的別の比較 アイキャッチ |

**モチーフ**: Two parallel panels: the left shows a central contact card linked to four small plain contact circles (customer information in one place); the right shows a deal pipeline of three columns with small cards moving to the right.

**構図**: モチーフは (680, 160) 〜 (1128, 560) に
収め、中心を (904, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'how to choose a CRM' (roundup), orange CRM family.

**見分ける相手**: 14: 14 is two overlapping areas (Venn); this is two separate panels; 5: 5 is stepped plan cards with a detached one-time tag

**避けるもの (この記事で特に)**: HubSpot or Pipedrive logos or screens; handshake; business people

**画像生成の文**: `two rounded panels side by side; the left panel shows one contact card in the centre linked by thin lines to four small plain circles; the right panel shows three narrow pipeline columns with small blank cards and a right-pointing arrow; orange #EA580C accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #EA580C with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article covers HubSpot and Pipedrive: a customer platform and a sales pipeline; the two panels mirror that difference without names.

### article 5 — HubSpot料金｜プラン別の費用と選び方

| 項目 | 内容 |
| --- | --- |
| article_id / type | 5 / `pricing` |
| URL | https://bizfluxlab.com/hubspot-pricing/ |
| slug | `hubspot-pricing` |
| **見出し (2 行, 92px)** | **「HubSpot」 / 「プラン選び」** (実測 400px / 434px / 列 568px) |
| 予備 | 「HubSpot料金」 / 「プランと費用」 (使う前に実測し直す) |
| 補助語 (44px) | 月額と初期費用 (実測 308px) |
| アクセント色 | `#EA580C` (CRM・SFA) |
| ファイル名 | `featured-5-hubspot-pricing.webp` |
| alt | HubSpotの料金プランと初期費用を表すプランカードの図 |
| media の title | HubSpot料金｜プラン別の費用と選び方 アイキャッチ |

**モチーフ**: Three stepped plan cards (each a little taller) with seat icons 1, 2 and 4 on top and a small funnel glyph on each card header; beside the two taller cards a small detached tag with a single flag, joined by a dotted line (a one-time onboarding cost). No amounts.

**構図**: モチーフは (670, 170) 〜 (1128, 570) に
収め、中心を (899, 380) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'HubSpot plans and costs'; the detached tag is the article's point (a one-time onboarding fee on upper plans).

**見分ける相手**: 23: pilot 23 has blue cards and no tag; this is orange with the detached one-time tag; 11: 11 cards contain module chains and clocks

**避けるもの (この記事で特に)**: HubSpot sprocket logo or orange brand shapes; $, ¥, amounts, monthly-fee wording; calendar dates

**画像生成の文**: `three blank plan cards in a row, each slightly taller than the previous, with small seat icons on top of each card and a tiny funnel glyph in each card header; next to the two tallest cards a small detached tag with a single flag connected by a dotted line; no numbers; orange #EA580C accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #EA580C with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: 'HubSpot' is plain type in the headline (no logo lettering). Prices are 'as of 2026-09' and must never appear in the image.

### article 14 — CRM SFA違い｜どこが違うのかを整理

| 項目 | 内容 |
| --- | --- |
| article_id / type | 14 / `comparison_listicle` |
| URL | https://bizfluxlab.com/crm-sfa-difference/ |
| slug | `crm-sfa-difference` |
| **見出し (2 行, 92px)** | **「CRMとSFA」 / 「の違い」** (実測 448px / 274px / 列 568px) |
| 予備 | 「範囲の違い」 / 「CRMとSFA」 (使う前に実測し直す) |
| 補助語 (44px) | 扱う範囲で読み解く (実測 396px) |
| アクセント色 | `#EA580C` (CRM・SFA) |
| ファイル名 | `featured-14-crm-sfa-difference.webp` |
| alt | CRMとSFAの扱う範囲の違いを表す重なった2つの領域の図 |
| media の title | CRM SFA違い｜どこが違うのかを整理 アイキャッチ |

**モチーフ**: Two overlapping rounded areas (a Venn shape): the larger area holds a contact card and a mail glyph (the whole customer relationship), the smaller one a three-stage pipeline arrow (the sales process); the overlap is tinted orange.

**構図**: モチーフは (680, 150) 〜 (1128, 570) に
収め、中心を (904, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'the difference between CRM and SFA is scope'.

**見分ける相手**: 4: 4 is two separate panels; this is one overlapping shape

**避けるもの (この記事で特に)**: the letters CRM or SFA drawn by the model; HubSpot or Pipedrive logos

**画像生成の文**: `two overlapping rounded soft areas like a Venn diagram, the larger area containing a small contact card and an envelope glyph, the smaller area containing a three-stage pipeline arrow, the overlapping region tinted orange #EA580C; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #EA580C with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article reads the difference from what each product officially covers, so overlap (not opposition) is the right shape.

---

## 5. バッチ 4 — RPA・自動化 (Make + RPA) (5 枚)

並べる順: 10 → 11 → 16 → 17 → 18。1 色のアクセントに 2 種類の印 (Make の丸いモジュール / RPA のデスクトップの窓)。まとめて確かめる。

### article 10 — Make使い方｜手順と注意点をわかりやすく解説

| 項目 | 内容 |
| --- | --- |
| article_id / type | 10 / `how_to` |
| URL | https://bizfluxlab.com/make-how-to/ |
| slug | `make-how-to` |
| **見出し (2 行, 92px)** | **「Make」 / 「最初の手順」** (実測 242px / 459px / 列 568px) |
| 予備 | 「Makeの始め方」 / 「シナリオを作る」 (使う前に実測し直す) |
| 補助語 (44px) | シナリオ作りの流れ (実測 396px) |
| アクセント色 | `#16A34A` (RPA・自動化) |
| ファイル名 | `featured-10-make-how-to.webp` |
| alt | Makeのシナリオを作る手順を表すフロー図 |
| media の title | Make使い方｜手順と注意点をわかりやすく解説 アイキャッチ |

**モチーフ**: Three round app modules joined by a curved connecting line (a scenario), each with a small step marker of one, two and three dots beneath it; a small plug glyph on the first link and an arrow into a final check.

**構図**: モチーフは (680, 170) 〜 (1128, 550) に
収め、中心を (904, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'the first steps in Make' (how-to), not pricing.

**見分ける相手**: 18: 18 is an ascending staircase with a desktop window; this is round modules on a curved line; 11: 11 is stepped plan cards

**避けるもの (この記事で特に)**: Make logo (purple dots); Make editor screenshot; digits as step numbers; robot

**画像生成の文**: `three round app modules connected by a smooth curved line from left to right, a small plug glyph on the first connection, under each module a step marker made of one, two and three small dots, the line ending in a round checkmark; green #16A34A accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #16A34A with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: Step markers use dots, not digits, so no numeric text is generated.

### article 11 — Make料金｜プラン別の費用と選び方

| 項目 | 内容 |
| --- | --- |
| article_id / type | 11 / `pricing` |
| URL | https://bizfluxlab.com/make-pricing/ |
| slug | `make-pricing` |
| **見出し (2 行, 92px)** | **「Make」 / 「料金と上限」** (実測 242px / 445px / 列 568px) |
| 予備 | 「Make料金」 / 「プランの上限」 (使う前に実測し直す) |
| 補助語 (44px) | プランごとの制限 (実測 352px) |
| アクセント色 | `#16A34A` (RPA・自動化) |
| ファイル名 | `featured-11-make-pricing.webp` |
| alt | Makeの料金プランごとの上限を表すプランカードの図 |
| media の title | Make料金｜プラン別の費用と選び方 アイキャッチ |

**モチーフ**: Three stepped plan cards; inside each a chain of round modules that grows longer (2, 3, 5 modules: more active scenarios) and a small clock dial whose hand sweeps a shorter interval on higher cards. No amounts.

**構図**: モチーフは (670, 160) 〜 (1128, 570) に
収め、中心を (899, 370) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'Make's plans and their limits' (scenario count and run interval per plan).

**見分ける相手**: 5: 5 is orange cards with seats and a detached tag; 10: 10 is a single curved module flow

**避けるもの (この記事で特に)**: Make logo; $ or amounts; credit numbers

**画像生成の文**: `three blank plan cards in a row, each slightly taller than the previous; inside each card a short chain of small round modules that gets longer on each card, and a small clock dial without numbers; no numbers or currency; green #16A34A accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #16A34A with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article's point is that active scenarios, run interval and run time change per plan, not just the price.

### article 16 — RPAおすすめ｜選び方と目的別の比較

| 項目 | 内容 |
| --- | --- |
| article_id / type | 16 / `recommendation_roundup` |
| URL | https://bizfluxlab.com/rpa-tools/ |
| slug | `rpa-tools` |
| **見出し (2 行, 92px)** | **「RPA製品」 / 「の選び方」** (実測 367px / 364px / 列 568px) |
| 予備 | 「RPAを選ぶ」 / 「候補の絞り方」 (使う前に実測し直す) |
| 補助語 (44px) | 料金の公開状況も整理 (実測 440px) |
| アクセント色 | `#16A34A` (RPA・自動化) |
| ファイル名 | `featured-16-rpa-tools.webp` |
| alt | RPA製品の候補を絞り込む流れを表す図 |
| media の title | RPAおすすめ｜選び方と目的別の比較 アイキャッチ |

**モチーフ**: Six small blank desktop-window tiles (each with a tiny loop arrow: a repeated task) in two rows pass through a funnel into two raised tiles with checks. Some tiles carry a small blank tag (price shown), others a small envelope (price on inquiry).

**構図**: モチーフは (670, 140) 〜 (1128, 580) に
収め、中心を (899, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'how to shortlist RPA products' (roundup), including which publish prices.

**見分ける相手**: 1: 1 is a teal grid of mixed category glyphs; this is identical green window tiles through a funnel; 17: 17 is a matrix table

**避けるもの (この記事で特に)**: robot or robotic arm; UiPath, Automation Anywhere, WinActor, BizRobo!, Power Automate or RoboTANGO logos

**画像生成の文**: `six small blank desktop window tiles in two rows, each with a tiny circular loop arrow, flowing through a simple funnel into two raised tiles with checkmark badges; some tiles have a small blank tag, others a small envelope glyph; green #16A34A accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #16A34A with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article is explicitly 'not a ranking'; the funnel narrows by conditions instead of showing a winner.

### article 17 — RPA比較｜違い・料金・選び方

| 項目 | 内容 |
| --- | --- |
| article_id / type | 17 / `comparison_listicle` |
| URL | https://bizfluxlab.com/rpa-comparison/ |
| slug | `rpa-comparison` |
| **見出し (2 行, 92px)** | **「RPA」 / 「違いの見方」** (実測 183px / 458px / 列 568px) |
| 予備 | 「RPAの違い」 / 「課金の違い」 (使う前に実測し直す) |
| 補助語 (44px) | 課金単位とライセンス (実測 440px) |
| アクセント色 | `#16A34A` (RPA・自動化) |
| ファイル名 | `featured-17-rpa-comparison.webp` |
| alt | RPA製品の課金とライセンスの違いを表す比較表の図 |
| media の title | RPA比較｜違い・料金・選び方 アイキャッチ |

**モチーフ**: A wide comparison matrix: five columns headed by small blank desktop-window glyphs, four rows marked on the left by a key (license) and a coin stack (billing unit); cells hold filled circles, hollow circles, checks and dashes.

**構図**: モチーフは (670, 140) 〜 (1128, 580) に
収め、中心を (899, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'how RPA products differ' (billing unit and license type).

**見分ける相手**: 6: 6 is a two-column sky table; this is a five-column green matrix with key and coin markers; 16: 16 is a funnel

**避けるもの (この記事で特に)**: robot; currency symbols or amounts; product logos

**画像生成の文**: `a wide clean comparison matrix with five columns and four rows; each column header is a small blank desktop window glyph; the rows are marked on the left by a small key glyph and a small coin stack glyph; cells contain filled circles, hollow circles, checkmarks and dashes; green #16A34A accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #16A34A with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article stresses that amounts are not comparable (units, currency and period differ), so the matrix uses symbols only.

### article 18 — RPA導入｜手順と注意点をわかりやすく解説

| 項目 | 内容 |
| --- | --- |
| article_id / type | 18 / `how_to` |
| URL | https://bizfluxlab.com/rpa-implementation/ |
| slug | `rpa-implementation` |
| **見出し (2 行, 92px)** | **「RPA導入」 / 「の進め方」** (実測 367px / 366px / 列 568px) |
| 予備 | 「導入の順序」 / 「RPA導入手順」 (使う前に実測し直す) |
| 補助語 (44px) | 対象業務の絞り込みから (実測 484px) |
| アクセント色 | `#16A34A` (RPA・自動化) |
| ファイル名 | `featured-18-rpa-implementation.webp` |
| alt | RPA導入の進め方を表す階段状の手順図 |
| media の title | RPA導入｜手順と注意点をわかりやすく解説 アイキャッチ |

**モチーフ**: An ascending staircase of four steps (left to right) with step markers of one to four dots: a single document on the first step (one target task), a fork of two small paths mid-way (attended: a seat icon / unattended: a moon), and a desktop window with a loop arrow and a check on the top step.

**構図**: モチーフは (680, 130) 〜 (1128, 580) に
収め、中心を (904, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'the order for rolling out RPA' (how-to).

**見分ける相手**: 10: 10 is round modules on a curved line; this is a staircase with a desktop window

**避けるもの (この記事で特に)**: robot; digits as step numbers; product logos; office workers

**画像生成の文**: `an ascending staircase of four flat steps from left to right, small dot markers on each step; a single document on the first step, a small fork into two paths with a seat icon and a crescent moon on the middle steps, a blank desktop window with a circular loop arrow and a checkmark on the top step; green #16A34A accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #16A34A with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article's first decisions are: one target task, attended vs unattended, and the billing unit.

---

## 6. バッチ 5 — 生成AI・ガイドライン・ガバナンス (3 枚)

並べる順: 19 → 21 → 22。試作 24 (indigo の概念図) と試作 20 (slate の管理画面 + 盾) の隣で確かめる。

### article 19 — 生成AIツールおすすめ｜選び方と目的別の比較

| 項目 | 内容 |
| --- | --- |
| article_id / type | 19 / `recommendation_roundup` |
| URL | https://bizfluxlab.com/generative-ai-tools/ |
| slug | `generative-ai-tools` |
| **見出し (2 行, 92px)** | **「生成AIの」 / 「選び方」** (実測 364px / 273px / 列 568px) |
| 予備 | 「使う形で選ぶ」 / 「生成AIを選ぶ」 (使う前に実測し直す) |
| 補助語 (44px) | 単体・追加契約・組み込み (実測 528px) |
| アクセント色 | `#4F46E5` (AIエージェント・生成AI) |
| ファイル名 | `featured-19-generative-ai-tools.webp` |
| alt | 生成AIツールの使う形の違いを表す3つのパネルの図 |
| media の title | 生成AIツールおすすめ｜選び方と目的別の比較 アイキャッチ |

**モチーフ**: Three parallel panels showing how the AI is used: a standalone chat panel with two speech bubbles; a plug-in block slotting into a larger suite block (added to an existing contract); a document with a small sparkle embedded in it (AI inside another tool).

**構図**: モチーフは (670, 170) 〜 (1128, 550) に
収め、中心を (899, 360) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'choosing generative AI tools by the form they take'.

**見分ける相手**: 24: pilot 24 is an indigo concept map (one centre node); this is three separate panels in a row

**避けるもの (この記事で特に)**: ChatGPT, Claude, Gemini, Copilot or Notion logos or chat screens; robot; brain illustration; sparkle used as a logo imitation

**画像生成の文**: `three rounded panels in a row: the first with two simple speech bubbles, the second with a small block plugging into a larger block, the third with a document page with a small four-point sparkle inside it; indigo #4F46E5 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #4F46E5 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article's frame is 'free range / add-on to an existing contract / built into another tool'.

### article 21 — 生成AIガイドライン｜要点と実務上の注意点

| 項目 | 内容 |
| --- | --- |
| article_id / type | 21 / `informational` |
| URL | https://bizfluxlab.com/generative-ai-guidelines/ |
| slug | `generative-ai-guidelines` |
| **見出し (2 行, 92px)** | **「指針の」 / 「読み方」** (実測 275px / 274px / 列 568px) |
| 予備 | 「ガイドライン」 / 「文書の構成」 (使う前に実測し直す) |
| 補助語 (44px) | AI事業者ガイドライン (実測 439px) |
| アクセント色 | `#475569` (ガイドライン・ガバナンス) |
| ファイル名 | `featured-21-generative-ai-guidelines.webp` |
| alt | AI事業者ガイドラインの構成を表す文書とチェックリストの図 |
| media の title | 生成AIガイドライン｜要点と実務上の注意点 アイキャッチ |

**モチーフ**: A main document with tab markers on its edge (the main part) and a thinner binder behind it (the appendix); on the page three larger dots (basic principles) above ten short blank lines with small ticks; a small shield in the lower corner of the page.

**構図**: モチーフは (720, 110) 〜 (1128, 590) に
収め、中心を (924, 350) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'how to read the AI guideline document' (structure, principles).

**見分ける相手**: 22: 22 is a cycle with a flag; this is a document; 20: pilot 20 is an admin panel with a lock; this is a paper document

**避けるもの (この記事で特に)**: government emblem or ministry logo; Japanese flag; gavel; letters on the page

**画像生成の文**: `a main document page with coloured tab markers on its right edge and a thinner binder behind it; on the page three larger dots above ten short blank lines each with a small tick; a small shield in the lower corner of the page; slate #475569 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #475569 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article covers the MIC/METI 'AI事業者ガイドライン' (v1.2): 3 basic principles, 10 common guidelines, main part vs appendix. The sublabel names the document; the headline is a short hook.

### article 22 — AIガバナンス｜要点と実務上の注意点

| 項目 | 内容 |
| --- | --- |
| article_id / type | 22 / `informational` |
| URL | https://bizfluxlab.com/ai-governance/ |
| slug | `ai-governance` |
| **見出し (2 行, 92px)** | **「ガバナンス」 / 「の回し方」** (実測 434px / 355px / 列 568px) |
| 予備 | 「AIガバナンス」 / 「体制と運用」 (使う前に実測し直す) |
| 補助語 (44px) | AIの体制と運用モデル (実測 439px) |
| アクセント色 | `#475569` (ガイドライン・ガバナンス) |
| ファイル名 | `featured-22-ai-governance.webp` |
| alt | AIガバナンスの運用サイクルを表す循環図 |
| media の title | AIガバナンス｜要点と実務上の注意点 アイキャッチ |

**モチーフ**: A circular loop of five arrow segments (the stages) around a central flag on a target (goals aligned with business goals); beside the loop a thin chain of three linked nodes (the value chain).

**構図**: モチーフは (700, 120) 〜 (1128, 590) に
収め、中心を (914, 355) 付近に置く。見出しの列 (72, 230, 568, 385) と
ラベル予約域 (0, 0, 560, 170) には何も置かない。

**一覧での役割**: Reads as 'how to run AI governance' (structure and a repeating operating cycle).

**見分ける相手**: 21: 21 is a document with ticks; this is a loop with a flag; 20: pilot 20 uses a shield and lock; this card deliberately has no shield

**避けるもの (この記事で特に)**: shield or lock (reserved for pilot 20 and article 21); gavel; people in a meeting; letters

**画像生成の文**: `a circular loop made of five curved arrow segments around a small flag planted on a target in the centre, and beside the loop a thin chain of three linked round nodes; slate #475569 accent; placed on the right half, flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color #475569 with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`

**メモ**: The article covers risk-based approach, agile governance, five stages, goals aligned with management and the value chain.

---

## 7. できあがった画像の確認 (人に見せる前、1 バッチずつ)

1. **縮小して読めるか**: 320×180 (PC 一覧)・160×90 (関連記事)・**126×71 (スマホ一覧)**・
   120×68 (前後ナビ) に縮小して、見出しが読めること。
2. **ラベルとの重なり**: 縮小した画像の左上に、カテゴリラベルの大きさの箱
   (PC: 62×22 / スマホ: 58×17) を重ねて、文字・顔・主モチーフの中心が隠れないこと。
3. **OGP のトリミング**: 1200×630 と 1200×600 に上下を切って、見出しとモチーフが欠けないこと。
4. **並べたときの印象**: バッチの画像を一覧の順に並べ、統一感があること。§1 の組が
   **形だけで** 見分けられること (グレースケールにしても区別できるか)。試作 4 枚とも並べる。
5. **禁止事項**: ロゴ・製品画面・金額・日付・擬似文字・人物写真・ロボットが無いこと。
6. **文字の役割**: 見出しが一覧のタイトル (画像の横に出る) の繰り返しになっていないこと。
7. **ファイル**: 1200×675、WebP、0 バイトでない、ファイル名が `planned_file` と一致。
