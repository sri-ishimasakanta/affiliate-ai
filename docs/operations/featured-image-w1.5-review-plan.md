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

### 1.1 状態の記録

目で見た承認 (§2 の 4) と、WordPress への適用 (§2 の 6〜11) は別に記録する。承認しても
WordPress には何も書いていない。manifest の各記事の `status` は、適用して確かめるまで
`planned` のまま (適用したら `applied` にする。§2 の 11)。

| バッチ | 記事 | 画像の承認 (人) | WordPress への適用 |
| --- | --- | --- | --- |
| 1 | 7 / 2 / 6 / 8 / 9 | **承認済み** (W1.5B のあと、人の報告) | まだ |
| 2 | 3 / 12 / 13 / 15 | **承認済み** (W1.5C のあと、人の報告)。rose `#DB2777` もプロジェクト・タスク管理の色として承認 | まだ |
| 3 | 1 / 4 / 5 / 14 | **承認済み** (W1.5D のあと、人の報告)。orange `#EA580C` (CRM・SFA) も目で見て承認 | まだ |
| 4 | 10 / 11 / 16 / 17 / 18 | **承認済み** (W1.5E のあと、人の報告)。green `#16A34A` (RPA・自動化) も承認 | まだ |
| 5 | 19 / 21 / 22 | **承認済み** (W1.5F のあと、人の報告)。indigo `#4F46E5`・slate `#475569` も承認 | まだ |

承認した画像 (`artifacts/featured-images/w1.5/batch-<N>/` の WebP。`compose-report.json` の値と
一致を確認済み)。適用の manifest にはこの SHA-256 を入れ、違うファイルは使わない:

| article | ファイル | SHA-256 |
| --- | --- | --- |
| 7 | featured-7-ai-meeting-notes.webp | `93bf89650aa5134eddeee80a9e51ce678b88d26d5a6f9ef928a078a5b3dda6d7` |
| 2 | featured-2-ai-meeting-notes-tools.webp | `3eb5e0a93cd7fe9d59a6f471725cf994c92278f19d2e95987f7f8708abb03a5b` |
| 6 | featured-6-ai-meeting-notes-comparison.webp | `cdd41729ca3b47739c289fcd307a37989b5cd16e4d11a09cc6c49847dcf92a3b` |
| 8 | featured-8-ai-meeting-notes-free.webp | `717e492d1d1fd0d9297dc20f3caf433d909cc3be4d3ab2e8c5906fdb5156287b` |
| 9 | featured-9-ai-meeting-notes-pricing.webp | `ffa3f3c125ecbeb3e58dc05e859beece637a84a90ef9285d8f871df55c405f1a` |
| 3 | featured-3-ai-transcription-tools.webp | `6155c04f0fbd1ce674f66cb4783889a662e9f2bd01f8257cc86602f26dd1d60e` |
| 12 | featured-12-ai-transcription-free.webp | `91bf4c676c978f511e85dd22dd04d0dce1dc3cd01f28fe8ccda602f7cc03b828` |
| 13 | featured-13-project-management-tools.webp | `d9ae0d0ba90b787f9f511193d587e92b90bce8b2015e5680cd48f32ac90630a8` |
| 15 | featured-15-notion-task-management.webp | `92703cbeecf6e6bbb8a9bea86a1eed9a7a83f7539607d7acd84b98da4eb86654` |
| 1 | featured-1-business-efficiency-tools-roundup.webp | `ceec18880792a9b1340a5aa800f0ed89e98bd50bd64af36bdb7384143adfefcd` |
| 4 | featured-4-crm-tools.webp | `256366d3e274864f86e65e61400778dd1e97e296cb8b2439034f7140cf29d31a` |
| 5 | featured-5-hubspot-pricing.webp | `d3fbaa0dfa81de0ba3debaacff08e30848250419188448eec31f733a10a7de3e` |
| 14 | featured-14-crm-sfa-difference.webp | `3b63aecf2fb1bd20ed7b818fe41db11196ae1f913c52dc3d38fe94468da9ba76` |
| 10 | featured-10-make-how-to.webp | `96d635f8172916b5d3df0087c7d33c62eb09af30fb0c1b6e38fff898fa76d869` |
| 11 | featured-11-make-pricing.webp | `9822f2f87400ed4dcc92d009f9a3d7759c53feb26f9625a61ae6f1ef557bec58` |
| 16 | featured-16-rpa-tools.webp | `4ecc6d8ff7e76bf07cfca6b26b4020d8121977222e102cfafac6cea1c42374fb` |
| 17 | featured-17-rpa-comparison.webp | `51b82f2b52d558c76e417738d12d87d5d400e887b02937ba6b44f7060a218987` |
| 18 | featured-18-rpa-implementation.webp | `3d9d580272cefe841d7c3418d4019151a0a8753112964da0196270b6c70ffc38` |
| 19 | featured-19-generative-ai-tools.webp | `0b8901734cc1dcc75a86197668be5fc5991eaae95fec893c67a56d90bc4d6cb5` |
| 21 | featured-21-generative-ai-guidelines.webp | `2639de0202ef4d35cbc25ea0c4a2a4bc9c31e7a7124289d5a170c277e969e01a` |
| 22 | featured-22-ai-governance.webp | `f7f577dfee5ec11563446a9b0b24914c365bf9f7ba830db2797e65a5277b6cfc` |

W1.5D での manifest の変更 (設計は変えていない): article 5 の禁止語 `'月額' as text` を
`monthly-fee wording` にした (画像生成の文に日本語を入れないため。W1.5A の検査が negative の
語を見ていなかった)。manifest の版の印は改行を LF にそろえて測る (`manifest_sha256`)。
バッチ 1 / 2 のパッケージは新しい版の印で作り直した (中身と画像は変わっていない)。

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
5. **適用の manifest を作る (読むだけ)**: `uv run python scripts/plan_featured_image_rollout.py` が
   21 枚ぶんを 1 つにまとめて `artifacts/featured-images/w1.5/wordpress-apply-manifest.json`
   (`schema: featured-image-wordpress-apply/1`) を作る。WordPress は読むだけ。詳細は §6。
6. **本番の確認点 (別の承認が要る)**: ここから先は WordPress に書く。W1.4 と同じく、この
   段階を始める指示を人から受けてから進める。
7. **書く前の状態を保存**: `uv run python scripts/apply_featured_image.py --dir artifacts/featured-images/w1.5 snapshot --out <before>.json`。
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

- `contact-sheet` は 3 枚 (縮小の一覧 + グレースケール、横線つきの並び `-compare`、126×71 を 3 倍の
  `-mobile`) を作る。`--compare-batch <N>` で承認済みの別のバッチの完成画像も並べる
  (例: バッチ 2 は `--compare-batch 1 --with-pilots`)。
- `package`: manifest のバッチを写した `batch-manifest.json`・README・確認項目・画像生成の文
  (`prompts/`) を作る。`validate`: 今の manifest の写しのままかを確かめる。
- `compose`: `backgrounds/<file>-bg.png` (文字なし) に、`typesetting_plan` の座標で組む。
  見出しの palt は font の GPOS から読む (この環境の Pillow には libraqm が無いため)。
  16:9 から 3% より外れた背景は使わない。左上のラベル予約域と見出しの列に何か描かれて
  いれば報告する。既にある完成のファイルは `--force` なしでは上書きしない。
- 組版は決定的 (同じ背景・同じ font なら同じバイト列)。font は
  `C:\Windows\Fonts\NotoSansJP-VF.ttf` を wght 700 / 500 に固定したものを
  `artifacts/featured-images/w1.5/.font-cache/` に作って使う。

### 2.2 試作 4 枚に合わせた組版 (W1.5B.1 で解決)

W1.5B では、試作 4 枚 (適用済み) が W1.5A の仕様の座標とは違う配置で作られていたことが
わかった (仕様だと見出しが約 85px 高く、印と帯が細い)。W1.5B.1 で試作の画像を測り、W1.5 の
本番の組版を試作に合わせて較正した。値は `featured-image-pipeline.md` §2.4.1、測った値と
理由は manifest の `typesetting_calibration`。座標の系は 1 つだけ (manifest の `typesetting`
= `PRODUCTION_TYPESETTING`。違えば道具が止まる)。

較正のあとに確かめること: `contact-sheet --with-pilots` の 3 枚
(`…-proofs.png` 縮小の一覧、`…-compare.png` 横線つきの並び、`…-mobile.png` 126×71 を 3 倍)。

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
  W1.5D (バッチ 3 の制作パッケージ) でも slug は manifest の値のまま (変えていない。
  WordPress の ID も解決していない)。**本番に書く前の `plan` で、WordPress が返す
  パーセントエンコードの slug に解決してから適用の manifest に入れる**ことは変わらない。
- **W1.5G で解決 (読むだけ)**: article 1 は post 25。WordPress が返す slug は
  `%e6%a5%ad%e5%8b%99%e5%8a%b9%e7%8e%87%e5%8c%96-%e3%83%84%e3%83%bc%e3%83%ab-%e3%81%8a%e3%81%99%e3%81%99%e3%82%81-roundup`
  (小文字のパーセントエンコード)。decode すると manifest の slug と完全に同じ。slug での検索は
  日本語・小文字・大文字のどの形でも post 25 の 1 件だけ。**適用の manifest の `slug` は
  WordPress が返す形** (道具は返ってきた slug と完全一致で比べるため)。W1.5 の design manifest
  の slug はそのまま (`application_slug` として記録)。post の slug は変えていない。
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

## 6. 本番適用の計画 (W1.5G。**まだ実行していない**)

5 バッチ・21 枚とも人が目で見て承認した (§1.1)。W1.5G では WordPress を **読むだけ** で
対象を決め、適用の manifest を作った。**upload・`featured_media` の設定・media の変更・
削除は一度もしていない。** 本番に書くのは、別の指示 (本番の確認点) を受けてから。

### 6.1 道具

```bash
# 計画 (読むだけ): ローカルの 21 枚・承認の記録・WordPress の post と media を突き合わせる
uv run python scripts/plan_featured_image_rollout.py
# 21 件の PLAN (読むだけ): 何を書くかを 1 件ずつ表示する
uv run python scripts/apply_featured_image.py --dir artifacts/featured-images/w1.5 plan
```

- 出力: `artifacts/featured-images/w1.5/wordpress-apply-manifest.json` (apply の道具が読む) と
  `rollout-plan-report.{json,md}` (git 管理外)。1 つでも確かめられなければ `approved: false`、
  終了コード 1。
- manifest の各項目: `file` は upload のファイル名 (パス区切りなし)、`source` は
  `batch-<N>/<file>`。`media_action` (`upload` / `reuse` / `human_review`) と
  `planned_media_id`。元の `featured_media` (巻き戻し用)・`modified_gmt`・カテゴリも記録。
- 本番は `scripts/rollout_featured_image.py` で 1 記事ずつ行う (既定は読むだけ):

  ```bash
  uv run python scripts/rollout_featured_image.py next             # 次の 1 記事の事前確認と PLAN
  uv run python scripts/rollout_featured_image.py next --execute   # 次の 1 記事だけを適用して確かめる
  uv run python scripts/rollout_featured_image.py status           # 進み具合
  uv run python scripts/rollout_featured_image.py check-public --link <URL> --stem <stem> --alt <alt>
  ```

  書くのは既存の `apply_featured_image.py` (`apply_one`) だけ。この道具はその前 (post・
  タイトル・slug・`featured_media`・`modified_gmt`・ローカルの SHA-256・media の名前と byte
  の衝突・試作 4 件・media 95 / 99) と後 (REST の読み戻し・公開ページ・カテゴリ一覧・
  og:image・twitter:image) を確かめ、`rollout/article-<id>.json` に残す。承認した順の
  次の 1 記事だけを扱い、失敗した記事が残っていれば次へ進まない。
- apply の道具は、manifest の計画と `--media-id` が食い違うと何も書かない
  (`upload` なのに `--media-id` がある、`reuse` の media と違う、`human_review`)。
  まとめて書く命令は作っていない。**1 回の `apply` で 1 記事だけ。**

### 6.2 読んで確かめた結果 (2026-09-26)

- ローカル: 21/21 の WebP があり、本物の WebP・1200×675・0 バイトでない。SHA-256 は
  compose の報告と、§1.1 の承認の表の両方と一致。ファイル名は design manifest の
  `planned_file` と一致。
- WordPress: post 25 件を読んだ。21/21 が公開済みで、slug (manifest の値と WordPress の値の
  両方) でちょうど 1 件に決まり、タイトルが完全一致。21/21 とも `featured_media = 0`。
- 試作 4 件の読み戻し: 78 → 100、74 → 98、76 → 97、72 → 96 (変わっていない)。
- media library: 6 件 (95〜100)。95 は人が 2026-09-23 に upload した PNG
  (1254×1254、W1 とは無関係)。96〜98・100 は試作が使用中。99 は W1.4 の重複 (下の §6.6)。
  W1.5 の 21 枚と byte が同じ media も、同じ名前 (`-1` などを含む) の media も無い
  → **21/21 とも新しく upload**。
- 読んで分かったこと: media の一覧を `context=edit` で読むと、この API の利用者が upload した
  media (100) しか返らない (95〜99 は別の利用者の upload)。一覧は `context=view` で読む。
  また別の利用者の media は `get_media` (edit) で 403 になるので、W1.4 の「既存の media の
  再利用」は別の利用者の media には使えない (安全側に止まる)。今回は再利用の対象が無い。

### 6.3 適用の順番と対象 (承認したバッチの順)

1 記事目 (canary) は article 7 (post 50)。article 1 (post 25、日本語の slug) はバッチ 3 の
先頭で、PLAN でも WordPress の形の slug で 1 件に決まることを確かめ済み。各記事の
`modified_gmt` は **適用すると新しい時刻に変わり、Cocoon が更新日を表示する** (§3.1)。

| # | バッチ | article | WP post | 今の featured_media | 今の modified_gmt (変わる) | カテゴリ |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 7 | 50 | 0 | 2026-09-22T10:11:34 | [4] |
| 2 | 1 | 2 | 36 | 0 | 2026-09-22T07:01:44 | [4] |
| 3 | 1 | 6 | 40 | 0 | 2026-09-22T07:31:56 | [4] |
| 4 | 1 | 8 | 52 | 0 | 2026-09-22T10:11:53 | [4] |
| 5 | 1 | 9 | 54 | 0 | 2026-09-22T10:12:12 | [4] |
| 6 | 2 | 3 | 37 | 0 | 2026-09-22T07:31:49 | [4] |
| 7 | 2 | 12 | 60 | 0 | 2026-09-22T10:12:28 | [4] |
| 8 | 2 | 13 | 58 | 0 | 2026-09-22T10:11:42 | [4] |
| 9 | 2 | 15 | 64 | 0 | 2026-09-22T10:13:02 | [4] |
| 10 | 3 | 1 | 25 | 0 | 2026-09-16T15:54:53 | [4] |
| 11 | 3 | 4 | 38 | 0 | 2026-09-22T07:31:51 | [4] |
| 12 | 3 | 5 | 39 | 0 | 2026-09-22T07:31:53 | [4] |
| 13 | 3 | 14 | 62 | 0 | 2026-09-22T10:12:25 | [4] |
| 14 | 4 | 10 | 82 | 0 | 2026-09-22T12:20:00 | [4] |
| 15 | 4 | 11 | 56 | 0 | 2026-09-22T12:20:02 | [4] |
| 16 | 4 | 16 | 66 | 0 | 2026-09-22T13:03:52 | [4] |
| 17 | 4 | 17 | 80 | 0 | 2026-09-22T13:04:09 | [4] |
| 18 | 4 | 18 | 68 | 0 | 2026-09-22T17:43:40 | [4] |
| 19 | 5 | 19 | 70 | 0 | 2026-09-22T13:04:01 | [4] |
| 20 | 5 | 21 | 84 | 0 | 2026-09-22T13:04:17 | [4] |
| 21 | 5 | 22 | 86 | 0 | 2026-09-22T13:04:34 | [4] |

### 6.4 本番で 1 記事ずつやること (まだしない)

事前 (preflight):

1. **作業者は 1 つだけ。** W1.4 では別の主体が同じ時間に featured image を設定していた。
   実行の前に、別の Claude のセッション・別の人が WordPress の media / featured image を
   触っていないことを人が確かめる。W1.5 の適用は 1 つのセッションだけが行う。
2. 計画を作り直す (`plan_featured_image_rollout.py` が ready、`apply ... plan` が 21/21 ready)。
3. `snapshot --out <before>.json` で全 post の状態を保存する。

1 記事ごと (前の記事がすべて問題なければ次へ):

1. `apply --slug <manifest の slug> --execute` (upload → media の read-back → alt / title →
   `{"featured_media": id}` → post の read-back。道具が 1 回ずつ行い、記録を
   `artifacts/featured-images/w1.5/applied/<slug>.json` に残す)。
2. REST の読み戻し: `featured_media` が新しい media、タイトル・slug・本文・カテゴリ・タグ・
   公開日が変わっていない (道具が確かめる)。
3. 記事ページ: アイキャッチが本文の上に出る。
4. カテゴリ一覧: カードの画像 (320×180 / スマホ 126×71)。
5. `og:image` / `twitter:image` が新しい画像。
6. 止める条件: 道具が STOP を出した、upload の結果が不明 (timeout など。media library を人が
   確かめるまで再送しない)、読み戻しが合わない。**同じ記事のために upload し直さない。**

事後: `snapshot --out <after>.json` と `compare`。変わってよいのは 21 件の `featured_media` と
`modified_gmt` だけ。

### 6.5 キャッシュ (W1.4 で見たこと)

設定の直後、クエリなしの一覧 URL はサーバーのページキャッシュで古い HTML (NO IMAGE) を
しばらく返した (W1.4 では数十分で切り替わった)。確かめ方を分ける:

1. REST の読み戻し (すぐ): これが合っていれば WordPress への書き込みは成功。
2. キャッシュを避けた読み取り (クエリ付きの URL など、読むだけ): 記事ページとカテゴリ一覧。
3. 時間を置いてから、ふつうの URL で確認。

短い間の NO IMAGE を失敗とみなさない (1 と 2 が合っていれば)。キャッシュの消去や設定の変更は
しない。

### 6.6 巻き戻し・更新日・media 99

- **巻き戻し**: 元の `featured_media` は 21 件とも 0 (manifest に記録)。道具の書き込みの契約は
  正の `featured_media` しか送れない (0 に戻す経路は無い。意図してそうしている)。戻すときは人が
  wp-admin の投稿の編集画面で「アイキャッチ画像を削除」する。upload した media は消さずに残す
  (片付けは W1 のあとに別に)。`modified_gmt` は元に戻せない (戻さない)。
- **更新日**: 21 件とも `modified_gmt` が適用の時刻に変わり、Cocoon が更新日を表示する。
  古い時刻を残す・日付を戻す・更新日を隠す・Cocoon を変える、のどれもしない (W1.4 と同じく人が
  許容済みの副作用)。
- **media 99**: まだあり、どこにも添付されず、どの post の featured image でもなく、upload の
  あと一度も変更されていない (`modified_gmt` = `date_gmt` = 2026-09-25T08:15:13)。バイトは
  article 25 の画像と同じ。**W1.5 では削除・添付・変更・再利用をしない**。計画でも
  再利用の候補から外している。片付けは W1 が終わってからの別の作業 (§4)。
