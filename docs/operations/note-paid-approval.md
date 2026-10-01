# 有料の note 記事の承認 (`note-approval/4`、2026-10-01)

**無料の記事は今までどおり `note-approval/3`** ([note-channel.md](note-channel.md))。`access_mode = paid`
の記事だけ `note-approval/4`。公開済みの記事は変えない (変換しない)。コードは
`app/social/note/paid.py` と `app/social/note/review.py`、CLI は `scripts/manage_note_piece.py`。

(このページを note-channel.md と分けたのは、note-channel.md が product
`approval-gated-automation-kit` の出どころ (ファイル全体の hash) なので、直すと 0.2.0 の release
candidate を作り直すことになるため。)

## 承認に結びつくもの

- **本文の hash (`content_hash`)**: 今までと同じ意味 (題名 + 本文。有料の記事では無料と有料の部分の
  両方)。価格や境界は入らない。価格だけを変えても本文の hash は変わらない。
- **販売の条件の hash (`commercial_hash`)**: 次の正規の JSON (`note-paid-terms/1`、キーの順に並べる)
  の sha256。
  - **有料の境界**: 節の位置 `paid_from_section` (0 始まり。その節から有料) と、無料の節・有料の節の
    見出しの並び。文字の位置には頼らない。同じ本文でも境界を動かせば別の条件。無料の節も有料の節も
    1 つ以上要る (無料の部分を空にしない)。
  - **価格**: 金額 (正の整数) と通貨 (`JPY` だけ)。0・負・小数・ほかの通貨は拒む。価格は product の
    版とは別の運用の値で、後で変えてよい (変えたら承認し直す)。
  - **売る物**: product の id・版・content hash・manifest hash・package sha256。手元の release
    candidate (`reports/products/<id>/<版>/`) から完全な hash を読み、zip のバイトを確かめる。
    短い hash・安全でない id は拒む。
- 人は承認のときに `--content-hash` と `--commercial-hash` の両方を渡す (packet に出る)。

## 承認できる条件と順番

順番: product の release candidate → **H4** (品質の確認・内容の承認) → **H5** (リリースの承認) →
有料記事の承認 → note での公開。

有料記事を承認できるのは、次のすべてを満たすときだけ:

1. 下書きに誤りが無い (販売の条件が足りないのも誤り。`submit` もできない)。
2. 有料の部分が、売る物の package の資産 (手引き・チェックリスト・設定の例・利用の条件) の行を
   すべて含む (zip から読んで確かめる)。
3. 売る物が **H5 で承認された版** (`products/<id>/records.json` の `release_approval` が同じ版・
   manifest・package・内容)。H4 / H5 の記録は仕組みでは作らない (人が行う)。
4. 画像は `note-approval/3` と同じ (承認のときに中身の hash の写しを作り、写しが正本)。

packet の `can_approve` は、有料の記事では H5 が無いと `false`。

## 変わったら承認し直す

本文・境界・価格・通貨・売る物の版や hash・画像のどれかが変われば、前の承認は使えない
(公開の記録で止まる)。承認の後は販売の条件を直せない (`paid-terms` は拒む)。

- 取り消し (`reopen`) → 直す → `submit` → 承認し直す。前の承認は `approval_history` に残る。
- 事後の承認 (`--note`・`--after-publication-at`) も同じ意味で使える。
- 有料の記事の本文を直すのは `import-paid --replace` (無料の部分と有料の部分を分けたまま取り込み
  直す)。境界が曖昧にならないよう、`edit` は有料の記事では拒む。

## 公開の記録

- 販売の条件が承認のときと同じこと、売る物が今も H5 の版であることを確かめる。
- `--observed-price N --observed-currency JPY`: note の画面の価格。承認の価格と違えば記録しない。
  渡さなければ `not_observed`。
- `--free-section-check "..."`: 公開ページの無料の部分を照らし合わせた結果 (渡さなければ
  `not_checked`)。
- **有料の部分は買わないと公開ページで読めない。** 人が note の編集画面や購入者の表示で確かめた
  ときだけ `--paid-verified-by <名前> --paid-verification "<どう確かめたか>"` (後からなら
  `record-paid-verification`) で `human_verified`。それ以外は `human_verification_required` と
  記録する。確かめていないのに一致とは書かない。
- タイトル・タグ・リンク・画像は無料の記事と同じ確かめ方 (公開ページを読む・画像の一致は人)。

## コマンド

```bash
uv run python scripts/manage_note_piece.py import-paid --title "<題名>" --free free.md --paid paid.md --product <product_id> --price 500 --currency JPY
```

- `paid-terms <draft> --price N --currency JPY` / `--product <id>` / `--paid-from <節>`: 承認の前だけ。
- `packet <draft>`: `.packet.md`・`.free.txt`・`.paid.txt`・`.paste.html` (有料ラインの位置つき)。
- migration は要らない (承認と販売の条件は今の JSON の記録に入る。`note_pieces.content_type` に
  制約は無い)。

## 今の有料記事 (2026-10-01)

- `draft-1caf34dd4c` (`review_ready`): approval-gated-automation-kit 0.2.0、500 JPY。H4 / H5・人の
  承認・画像・note での価格の設定・公開はまだ。詳しくは [first-paid-product.md](first-paid-product.md)。
