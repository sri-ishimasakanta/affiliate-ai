# Featured image 残り 21 記事の確認と適用の段取り (W1.5)

W1.5A (この文書を書いた段階) は **設計だけ**。画像は作っていない。WordPress には何も
書いていない (upload・`featured_media` の設定・post の変更・media 99 の削除・Cocoon の設定
変更のどれもしていない)。

- 設計の入力: [`featured-image-w1.5-manifest.json`](featured-image-w1.5-manifest.json)
  (`schema: featured-image-w1.5/1`、21 記事とも `status: planned`)
- 制作ブリーフ: [`featured-image-w1.5-prompts.md`](featured-image-w1.5-prompts.md)
- 仕様と W1.4 の記録: [`featured-image-pipeline.md`](featured-image-pipeline.md)
- 検査: `tests/unit/test_featured_image_w15_manifest.py` (manifest の形・寸法・見出しの幅・
  禁止事項。WordPress にも DB にも触らない)

対象は article 1〜19・21・22。20 / 23 / 24 / 25 は W1.4 で適用済みなので除く。

## 1. バッチ

トピックの系統ごとに 4〜5 枚 (最後だけ 3 枚)。1 バッチずつ作り、確かめ、承認し、適用する。

| バッチ | 系統 | 記事 (一覧の順) | 枚数 | 並べて見る試作 |
| --- | --- | --- | --- | --- |
| 1 | 議事録 | 7 → 2 → 6 → 8 → 9 | 5 | 24 (「とは」の型) |
| 2 | 文字起こし + プロジェクト・タスク管理 | 3 → 12 → 13 → 15 | 4 | バッチ 1 の 5 枚 |
| 3 | CRM・SFA + ツール総論 | 1 → 4 → 5 → 14 | 4 | 25 (ティール)、23 (段差のカード) |
| 4 | RPA・自動化 (Make + RPA) | 10 → 11 → 16 → 17 → 18 | 5 | 1 (タイルの形) |
| 5 | 生成AI・ガイドライン・ガバナンス | 19 → 21 → 22 | 3 | 24 (indigo)、20 (slate + 盾) |

順番の理由:

- **バッチ 1 が最初**: 同じ 2 ツール (Krisp / Fireflies.ai) を扱う記事が 5 本あり、見分けが
  いちばん難しい。ここで「形で見分ける」設計が効くかを先に確かめる。効かなければ、残りを
  作る前に設計を直す。
- **バッチ 2**: sky の系統 (文字起こしは少し深い `#0369A1`) を仕上げる。W1.5 で新しく足す
  アクセント rose `#DB2777` (プロジェクト・タスク管理) を 2 記事だけで試し、人が色を
  確かめる。合わなければこの 2 枚だけ色を替える。
- **バッチ 3〜5**: 系統の中で閉じているので、どの順でもよい。

## 2. 1 バッチの流れ

各段階の終わりで止まり、次に進むのは人が決める。

1. **作る (ローカルだけ)**: ブリーフのとおりにモチーフを作り、組版し、
   `artifacts/featured-images/w1.5/batch-<N>/` に PNG (master) と WebP を置く (git 管理外)。
2. **自分で確かめる**: ブリーフ §7 の 7 項目 (縮小・ラベル・OGP の切り取り・並べた印象・
   禁止事項・文字の役割・ファイル)。
3. **一覧の見本を作る**: 320×180 と 126×71 の縮小を、カテゴリラベルの箱を重ねて一覧の順に
   並べた 1 枚 (contact sheet)。試作の 4 枚と、前のバッチで適用済みの画像も並べる。
   グレースケール版も付ける (§1 の組が形だけで見分けられるか)。
4. **人が承認する**: バッチ単位。直すものは直して 2〜3 に戻る。承認されたら、ファイルの
   SHA-256 を記録する。
5. **適用の manifest を作る**: `artifacts/featured-images/w1.5/batch-<N>/wordpress-apply-manifest.json`
   (`schema: featured-image-wordpress-apply/1`、`approved: true`)。各項目は W1.5 manifest の
   `article_id` / `slug` / `title` / `planned_file` / `alt_text` と、承認した画像の
   `sha256` / 1200 / 675 / `image/webp`。
6. **本番の確認点 (別の承認が要る)**: ここから先は WordPress に書く。W1.4 と同じく、この
   段階を始める指示を人から受けてから進める。
7. **書く前の状態を保存**: `uv run python scripts/apply_featured_image.py --dir <batch> snapshot --out <before>.json`。
8. **1 記事目 (canary)**: `plan` で slug・タイトルの完全一致・`featured_media = 0` を確かめて
   から `apply --slug <slug> --execute`。記事ページ・カテゴリ一覧・`og:image` を確かめる。
9. **残りを 1 記事ずつ**: 同じく `apply --slug ... --execute`。1 記事ごとに read-back を確かめる。
10. **書いた後の比較**: `snapshot` をもう一度とり、`compare --before --after`。変わってよいのは
    対象 post の `featured_media` と `modified_gmt` だけ。
11. **記録**: `featured-image-pipeline.md` にバッチの適用結果 (post ID・media ID・時刻・確認)
    を足し、W1.5 manifest の該当記事の `status` を `applied` にする。

### 2.1 道具 (W1.5B で用意)

リポジトリには W1.3/W1.4 の組版の道具が無かった (試作 4 枚は完成品として受け取った) ので、
W1.5B で足した。どれも WordPress にも DB にも触らない。

```bash
uv run python scripts/prepare_featured_image_batch.py package --batch 1
uv run python scripts/prepare_featured_image_batch.py validate --batch 1
uv run --no-project --with pillow --with fonttools python scripts/compose_featured_image.py --dir artifacts/featured-images/w1.5/batch-1 proof
uv run --no-project --with pillow --with fonttools python scripts/compose_featured_image.py --dir artifacts/featured-images/w1.5/batch-1 compose
uv run --no-project --with pillow --with fonttools python scripts/compose_featured_image.py --dir artifacts/featured-images/w1.5/batch-1 contact-sheet --with-pilots
```

- `package`: manifest のバッチを写した `batch-manifest.json`・README・確認項目・画像生成の文
  (`prompts/`) を作る。`validate`: 今の manifest の写しのままかを確かめる。
- `compose`: `backgrounds/<file>-bg.png` (文字なし) に、`typesetting_plan` の座標で組む。
  見出しの palt は font の GPOS から読む (この環境の Pillow には libraqm が無いため)。
  16:9 から 3% より外れた背景は使わない。左上のラベル予約域と見出しの列に何か描かれて
  いれば報告する。既にある完成のファイルは `--force` なしでは上書きしない。
- 組版は決定的 (同じ背景・同じ font なら同じバイト列)。font は
  `C:\Windows\Fonts\NotoSansJP-VF.ttf` を wght 700 / 500 に固定したものを
  `artifacts/featured-images/w1.5/.font-cache/` に作って使う。

### 2.2 確かめたい点: 試作 4 枚の実際の配置

試作 4 枚 (適用済み) は、仕様の座標とは違う配置で作られていた (画像から測った値):

| 項目 | 仕様 (W1.5 の組版) | 試作 4 枚 |
| --- | --- | --- |
| 見出しの印 | 64 × 8、(72, 250) | 約 128 × 17〜20、x ≈ 63、y ≈ 297〜352 |
| 見出しの位置 | 上端 y = 282 から下へ | 下に寄せてある (最終行の下端 y ≈ 575〜618) |
| 下端の帯 | 高さ 12 | 高さ 17〜22 |

W1.5B は仕様 (manifest) のとおりに組む。試作と並べたとき見出しが高く、印と帯が細く見える。
合わせるかどうかは、バッチ 1 の一覧の見本 (`contact-sheet --with-pilots`) を見て人が決める。
合わせる場合は manifest の `typesetting` を直し、パッケージを作り直す (道具は座標を
manifest と `featured_image_batch.py` の定数から取る)。

止める条件 (W1.4 と同じ):

- slug で公開済みの post が 1 件に決まらない、タイトルが manifest と完全一致しない、
  `featured_media` が 0 でない → 書かない。
- upload の結果が不明 (timeout など) → 記録して止まる。media library を人が確かめる。
- **同じ記事のために画像を何度も upload し直さない** (成功させるための再 upload はしない)。
- 同じ時間帯に別の主体 (人や別のセッション) が featured image を設定しない。W1.4 では
  別の主体が同時に 3 記事へ設定していた。1 バッチは 1 つの主体だけが扱う。

## 3. 適用の前に知っておくこと

### 3.1 更新日の表示 (隠さない)

`featured_media` を設定すると WordPress は post の更新として記録し、`modified_gmt` が
変わる。Cocoon はそれを見て、記事に **更新日** を表示する (公開日は変わらない)。W1.4 の
4 記事でも起き、人が確認して許容した。

- 21 記事をまとめて適用すると、21 記事すべてに同じ日の更新日が出る。本文は変わっていない
  のに「更新した」ように見える。
- サイトマップの `lastmod` も変わる (検索エンジンが見直すきっかけにはなるが、内容の変化は
  無い)。
- **Cocoon の設定は変えない** (更新日を隠す設定にはしない)。これはこの計画の範囲外。
- 選べること (人が決める): (a) W1.4 と同じく許容して、バッチを続けて適用する。
  (b) バッチを日を分けて適用し、同じ日に更新日が並ぶ記事を 4〜5 本に抑える。
  どちらでも手順は同じ。各バッチの本番の確認点 (§2 の 6) で決める。

### 3.2 slug と post ID

- WordPress の post ID は DB の `articles.wordpress_post_id` を manifest に
  `wordpress_post_id_hint` として写しただけ。適用のときは使わず、**slug で探し、タイトルの
  完全一致を確かめてから書く** (道具の既定の動き)。
- **article 1 の slug は日本語** (`業務効率化-ツール-おすすめ-roundup`)。WordPress は slug を
  パーセントエンコードした小文字の形 (`%e6%a5%ad...-roundup`) で保存して返す。道具は返って
  きた `slug` と manifest の `slug` を完全一致で比べるので、**適用の manifest には REST API
  が返す形の slug を入れる**。バッチ 3 の `plan` (読むだけ) で先に確かめる。一致しなければ
  書かずに止め、道具の側で直す (W1.5A では道具を変えていない)。
- タイトルが WordPress 側で変わっていたら、W1.5 manifest を直してから進める。

### 3.3 そのほか

- **ページキャッシュ**: W1.4 では、設定直後にクエリなしの一覧 URL が古い HTML (NO IMAGE) を
  数十分返した。キャッシュの設定は変えずに待つ。確かめるときはクエリ付きの URL も見る。
- **`og:image`**: 各記事のリンクの見た目 (SNS のカード) が新しい画像に替わる。すでに共有
  されたカードは各サービスのキャッシュのまま。Threads の投稿・承認・公開には何もしない。
- **media library に既にある画像**: 承認した画像と SHA-256 が一致し、どこにも添付されて
  いない media だけを `apply --media-id <id>` で使ってよい (W1.4 の規則)。それ以外は新しく
  upload する。
- **新しいアクセント** rose `#DB2777` はバッチ 2 で人が確かめる。

## 4. 任意の片付け: media 99

W1.4 で、media library に同じ画像が 2 つできた。事前 upload の **media 99**
(`featured-25-ai-business-efficiency.webp`、どの post にも使われていない) と、canary で
upload した **media 100** (post 78 が使用中)。

- media 99 は不要。ただし **この計画では削除しない**。削除は取り消せない操作なので、
  やるなら人が wp-admin のメディアから行う。
- 削除する前に確かめること: どの post の `featured_media` でもない (media 100 と
  取り違えない)、どの post にも添付されていない、本文から参照されていない。
- 削除しなくても表示には影響しない (容量が 1 ファイル分多いだけ)。

## 5. W1.5A でしていないこと

- 画像の生成・組版 (まだ 1 枚も作っていない)
- WordPress への upload、`featured_media` の設定、post の変更、media 99 の削除
- Cocoon の設定の変更
- Threads の投稿・提案の承認や却下、worker・スケジューラの変更
- DB の変更、`/go/` への問い合わせ、git の push
