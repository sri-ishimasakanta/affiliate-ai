# Featured image 試作 4 枚の制作ブリーフ (W1.3)

画像生成と組版をする人が **このまま使える** 指示書。仕様の根拠は
[`featured-image-pipeline.md`](featured-image-pipeline.md)、機械可読な入力は
[`featured-image-pilot-manifest.json`](featured-image-pilot-manifest.json)。

この段階の成果物は **ローカルの画像ファイル 4 枚だけ**。WordPress への登録はしない
(人が 4 枚を承認してから、別の手順で 1 記事ずつ設定する)。

---

## 0. 共通の作り方

### 手順

1. **背景とモチーフだけ** を作る (文字なし)。画像生成でも、手でベクター (SVG) を描いてもよい。
2. 文字は **あとから組版** で重ねる (SVG / Pillow / デザインツール)。画像生成モデルに
   日本語を描かせない。
3. 1200 × 675 の PNG で書き出す (ファイル名は `featured-<article_id>-<slug>.png`)。
4. §5 の確認をしてから人に見せる。

### キャンバスと領域 (1200 × 675 px)

| 領域 | 範囲 (x, y, 幅, 高さ) | 置いてよいもの |
| --- | --- | --- |
| 外周マージン | 上下 60 / 左右 72 の内側だけを使う | 下端のアクセント帯 (装飾) を除き何も置かない |
| **ラベル予約域** | (0, 0, 560, 170) | 背景の色・模様と、モチーフから伸びる線や面の端だけ。**文字・顔・人型・主モチーフの中心は置かない** |
| 見出しの列 | (72, 230, 568, 385) ※記事ごとに幅の調整あり | 見出しの印、見出し、補助語 |
| モチーフの領域 | (660, 90, 468, 525) | 主モチーフ。中心はおおよそ (900, 350) |
| 下端のアクセント帯 | (0, 663, 1200, 12) | アクセント色のベタ帯 (OGP で上下が切れても困らない装飾) |

### 組版 (共通)

| 要素 | 書体・太さ | サイズ | 色 | 位置 |
| --- | --- | --- | --- | --- |
| 見出しの印 | — | 64 × 8 のベタ | アクセント色 | (72, 250) |
| 見出し | Noto Sans JP **Bold**、プロポーショナル詰め (palt) を有効 | **96〜112px** (記事ごとに指定。90px 未満は禁止) | `#12263F` | 左揃え、上端 y = 282。行間 1.2 |
| 補助語 | Noto Sans JP Medium | 44px | `#4A5B70` | 見出しの下 28px、左揃え |
| 背景 | — | — | `#F5F7FA` | 全面。ごく薄い幾何学模様を 4〜6% の濃さで入れてよい |

- 本文中の「実測幅」は Noto Sans JP (NotoSansJP-VF) の Bold (wght 700)、palt なしで測った値
  (2026-09-25、Pillow)。palt を有効にすると同じか狭くなる。
- 見出しは、指定の幅に **実測で** 収まること。収まらないときはサイズを 96px まで下げ、
  それでも収まらなければ 2 行にする (1 行は列の幅に収まる長さ)。それでも無理なら予備の見出しを使う。
- 文字に影・縁取り・グラデーションを付けない。

### 画像生成に使う共通の文 (英語)

モチーフの生成に画像モデルを使う場合、次を毎回付ける。

- 付ける (positive): `flat vector illustration, clean minimal business infographic style, soft light grey background #F5F7FA, generous empty space on the left half and in the top-left corner, single accent color {ACCENT} with navy #12263F details, simple geometric shapes, subtle soft shadows, crisp edges, 16:9`
- 付けない (negative): `text, letters, numbers, captions, watermark, logo, brand mark, product screenshot, user interface of a real product, photo, photorealistic, people, faces, hands, robot, humanoid, neon, glow, heavy gradient, rainbow colors, clutter, emoji, 3d render, stock photo`

生成した画像の **左半分と左上は空けておく** (文字はあとから重ねる)。左上 (0, 0, 560, 170)
に主モチーフの中心が来た画像は使わない。

---

## 1. article 25 — AI業務効率化｜要点と実務上の注意点

| 項目 | 内容 |
| --- | --- |
| article_id | 25 |
| title | AI業務効率化｜要点と実務上の注意点 |
| URL | https://bizfluxlab.com/ai-business-efficiency/ |
| **見出し (第一候補)** | **AI業務効率化** |
| 予備 | 「AIに任せる範囲」/「進め方と注意点」 |
| 補助語 | 要点と注意点 |
| アクセント色 | ティール `#0D9488` |

**モチーフの要約**: 左から右へ流れる角丸のツールのブロックが、途中で **2 本のレーンに
分かれる**。上のレーンは小さな歯車 / 光の印 (AI に任せる作業)、下のレーンの終点は
チェックマーク (人が確かめる)。記事の「AI に担わせる役割は 2 つに分かれる」を、特定の
製品を示さずに表す。

**構図**: 見出しの列を (72, 230, 628, 385) に広げ、モチーフは (720, 150) 〜 (1128, 560) に
収める。分かれ目の点を中心 (920, 350) 付近に置く。ブロックは 4〜5 個まで。流れの始まりの
線が見出しの列に少しかかる程度はよい (文字とは重ねない)。見出しは 1 行、**100px**
(実測幅 598px / 列 628px)。

**避けるもの**: Make・ChatGPT などのロゴや画面、ロボット、人物写真、未来都市風の演出、
矢印だらけの複雑な図 (ブロック 6 個以上)。

**画像生成の文**: `a left-to-right workflow of 4 rounded tool blocks connected by lines that splits into two lanes; the upper lane ends in a small gear-and-spark symbol, the lower lane ends in a checkmark; teal #0D9488 accent; placed on the right half of the canvas` + 共通の文 (`{ACCENT}` = `#0D9488`)。

**組版の注意**: 見出し「AI業務効率化」は英字 2 文字を含むので、palt を有効にして
字間を詰めすぎないこと。補助語「要点と注意点」は見出しの下。

---

## 2. article 23 — ChatGPT法人プラン｜プラン別の料金と選び方

| 項目 | 内容 |
| --- | --- |
| article_id | 23 |
| title | ChatGPT法人プラン｜プラン別の料金と選び方 |
| URL | https://bizfluxlab.com/chatgpt-business-plans/ |
| **見出し (第一候補)** | **法人プラン比較** |
| 予備 | 「法人プランの選び方」/「プランと人数」 |
| 補助語 | ChatGPT (欧文のまま、補助語の位置に小さく) |
| アクセント色 | ブルー `#2563EB` (明るめ) |

**モチーフの要約**: **横一列に並ぶ 3 枚のプランカード** (左から右へ少しずつ高くなる段差)。
各カードの上部に、シート (利用者) を表す小さな人型アイコンが 1 → 3 → 5 個と並ぶ。
カードには金額・通貨・プラン名を描かない。**「並べて見比べる」比較の構図** にする。

**構図**: 3 枚は (670, 180) 〜 (1128, 560) に等間隔で横並び。真ん中のカードの中心が
(900, 380) 付近。**Enterprise (article 20) と見分けがつくように、横に広がる・並列の形** に
する (1 枚だけの大きな面にしない)。見出しは **2 行「法人プラン」/「比較」、104px**
(1 行目の実測幅 520px / 列 568px。1 行「法人プラン比較」は 96px でも 672px で入らない)。

**避けるもの**: 金額・$・¥・「月額」などの数字や単位、OpenAI のロゴ・ChatGPT の画面、
盾・鍵 (Enterprise の印と混ざる)、握手やスーツの人物。

**画像生成の文**: `three flat pricing-tier cards side by side in a horizontal row, each slightly taller than the previous, with 1, 3 and 5 small person icons on top of the cards, blank cards without any numbers, comparison layout, bright blue #2563EB accent; placed on the right half` + 共通の文 (`{ACCENT}` = `#2563EB`)。

**組版の注意**: 「比較」を 2 行目に大きく残す (記事で得られることが「比較」だとわかる語)。
補助語の「ChatGPT」は商品名の表記だけで、ロゴ風の書体にしない。

---

## 3. article 24 — AIエージェント｜意味・種類・選び方の基礎知識

| 項目 | 内容 |
| --- | --- |
| article_id | 24 |
| title | AIエージェント｜意味・種類・選び方の基礎知識 |
| URL | https://bizfluxlab.com/ai-agents/ |
| **見出し (第一候補)** | **AIエージェントとは** |
| 予備 | 「AIエージェント入門」/「種類と選び方」 |
| 補助語 | 意味・種類・選び方 |
| アクセント色 | インディゴ `#4F46E5` |

**モチーフの要約**: 中心の角丸のノードから細い線が伸び、4 つの小さなアイコン
(予定表・文書・データの表・メッセージ) につながる **概念図**。「1 つの仕組みがいくつもの
作業にまたがって動く」ことを表す。入口・定義の記事なので、他より少しだけ抽象的でよい。

**構図**: 中心ノード (直径 約 100px) を (950, 360) に置き、4 つのアイコンを半径 約 140px の
円周上に配置。モチーフ全体は (780, 180) 〜 (1128, 550)。見出しの列は幅を広げて
(72, 230, 690, 385) とし、**2 行「AIエージェント」/「とは」、96px** (1 行目の実測幅 670px
/ 列 690px。palt を有効にするとさらに狭くなる。690px を超えるなら予備の見出しに切り替える)。

**避けるもの**: ロボット・人型・顔のある AI、SF 風の光るネットワーク、脳のイラスト、
特定製品のロゴ。

**画像生成の文**: `a central rounded node connected by thin lines to four small flat icons arranged around it (calendar, document, data table, chat bubble), concept map style, indigo #4F46E5 accent; placed on the right third of the canvas` + 共通の文 (`{ACCENT}` = `#4F46E5`)。

**組版の注意**: 2 行目「とは」は 1 行目と同じサイズ (小さくしない)。補助語は 2 行目の下。

---

## 4. article 20 — ChatGPT Enterprise｜要点と実務上の注意点

| 項目 | 内容 |
| --- | --- |
| article_id | 20 |
| title | ChatGPT Enterprise｜要点と実務上の注意点 |
| URL | https://bizfluxlab.com/chatgpt-enterprise/ |
| **見出し (第一候補)** | **Enterpriseの要点** |
| 予備 | 「組織導入の要点」/「Enterprise検討の要点」 |
| 補助語 | ChatGPT |
| アクセント色 | スレートブルー `#334E68` (主) + ブルー `#2563EB` (差し色) |

**モチーフの要約**: **1 枚の組織の管理パネル** (切り替えスイッチ 3 つ・ユーザー一覧の行) を、
手前の **盾** と、データの積み重ねに付いた **鍵** が守る構図。横に短いチェックリスト
(3 行) を添える。記事の中心である「データ・管理・組織での導入」を、**管理・セキュリティ感**
として強く出す。

**構図**: 管理パネルは (700, 140) 〜 (1080, 540) に縦長にまとめ、盾をパネルの右下手前
(1010, 470) 付近に重ねる。チェックリストはパネルの左下 (680〜760, 430〜560)。
**法人プラン (article 23) と見分けがつくように、1 つにまとまった・守られた形** にする
(横並びのカードにしない)。見出しは **2 行「Enterprise」/「の要点」、100px**
(1 行目の実測幅 517px / 列 568px。1 行「Enterpriseの要点」は 96px でも 785px で入らない)。

**避けるもの**: 横に並ぶプランカード (法人プランの印と混ざる)、金額、OpenAI のロゴや
ChatGPT の実際の画面を模した UI、南京錠だらけ・警告色 (赤) の多用、ハッカー風の暗い演出。

**画像生成の文**: `a single flat organization admin panel with three toggle switches and a short user list, a shield in front of it at the lower right and a small padlock on a stack of data cylinders, a short three-line checklist beside the panel, compact vertical composition conveying management and security, deep slate blue #334E68 with a small #2563EB highlight; placed on the right half` + 共通の文 (`{ACCENT}` = `#334E68`)。

**組版の注意**: 「Enterprise」は欧文なので、和文の見出しより字面が小さく見える。1 行目と
2 行目の見た目の大きさがそろうよう、必要なら 1 行目を 104px にしてよい (実測 537px、幅 568px 以内)。
見出しの印・下端の帯は主のスレートブルー。

---

## 5. できあがった 4 枚の確認 (人に見せる前)

1. **縮小して読めるか**: 320×180 (PC 一覧)・**126×71 (スマホ一覧)**・160×90 (関連記事)・
   120×68 (前後ナビ) に縮小して、見出しが読めること。
2. **ラベルとの重なり**: 縮小した画像の左上に、カテゴリラベルの大きさの箱
   (PC: 62×22 / スマホ: 58×17) を重ねて、文字・顔・主モチーフの中心が隠れないこと。
3. **OGP のトリミング**: 1200×630 と 1200×600 に上下を切って、見出しとモチーフが欠けないこと。
4. **並べたときの印象**: 4 枚を 25 → 23 → 24 → 20 の一覧の順に並べ、統一感があること、
   **23 と 20 が形だけで見分けられること** (グレースケールにしても区別できるか)。
5. **禁止事項**: ロゴ・製品画面・金額・日付・擬似文字・人物写真が無いこと。
6. **文字の役割**: 見出しが一覧のタイトル (画像の横に出る) の繰り返しになっていないこと。
