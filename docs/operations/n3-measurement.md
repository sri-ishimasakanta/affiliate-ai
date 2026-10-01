# N3: note の実測 (G4) の手順

**状態 (2026-10-01): 始める準備ができた。実測の値はまだ 0 件 (人の入力待ち)。** 架空の値・推定・
「見えないから 0」は入れない。

- 記録: `manual_metric_entries` (migration `a4a74a5bcb8b`、本番に適用済み)。CLI は
  `scripts/record_manual_metric.py`、判定は `app/n_track/metrics.py`、記録と報告は
  `app/services/manual_metrics_service.py`。
- 測る記事: `app/config/note_measurement_targets.json`。bootstrap と仕組みの流れの記事を分ける。
- 完了の条件 (N3 の DoD): 公開済みの 3 本以上について 4 週間以上の数が記録され、要約が標本の
  大きさを出して因果を言わず、頻度の判断が決定の記録に残る (`n-track-plan.md`)。

## 測る記事

| 参照 (`--entry` の REF) | 役割 | URL | note の公開時刻 |
|---|---|---|---|
| `external-nd9c4fb635aad` | bootstrap (仕組みの外で公開。台帳に無い・承認なし) | https://note.com/ai_growth_jp/n/nd9c4fb635aad | 2026-09-30 11:49 |
| `draft-c9d0eae558` | controlled #1 | https://note.com/ai_growth_jp/n/n5b1256b5861b | 2026-09-30 21:09 |
| `draft-e068f0146e` | controlled #2 | https://note.com/ai_growth_jp/n/nd9f002a5e34e | 2026-10-01 08:42 |
| `draft-bab70591d9` | controlled #3 | https://note.com/ai_growth_jp/n/n53dfa4d2e2c3 | 2026-10-01 11:47 |

- 公開時刻は note の公開ページ (公開の API の `publish_at`) を 2026-10-01 に読んだ値 (JST)。
- bootstrap は台帳 (`note_pieces`) に入れない (承認の hash の無い公開は台帳の CHECK で持てない。
  承認を後から作らない)。数だけを、登録した外部の参照 `external-n…` で記録する。登録の無い
  `external-` の参照は拒む。
- 新しく公開した記事は、台帳に同期してから、この登録に足す (人が URL と note の公開時刻を確かめる)。

## 人が入力する数

| 指標 (`METRIC`) | 単位 | 単位の対象 | 読み方 | 画面 |
|---|---|---|---|---|
| `views` | 回 (count) | 記事ごと | **累計** (その時点までの全期間の値) | note のダッシュボード (アクセス状況) の記事ごとのビュー。期間は「全期間」 |
| `likes` | 回 (count) | 記事ごと | **累計** | 同じ表のスキ |
| `comments` | 件 (count) | 記事ごと | **累計** | 同じ表のコメント |
| `followers` | 人 (count) | アカウント (`note`) | **その時点の数** | note のアカウントのフォロワー数 |

- 画面の項目の名前・期間の選び方は、人が自分の画面で確かめる (Claude は note の管理画面を
  見ていない)。画面に無い数は **記録しない** (0 にしない)。
- 期間を付けずに記録した `views` / `likes` / `comments` / `followers` は「その時点の累計」と
  読み、報告で前の観測との差を出す (`CUMULATIVE_WITHOUT_PERIOD`)。週の値を写すなら `--period`
  を付ける (その場合は差を取らない)。1 つの指標で読み方を混ぜない。
- `--observed-at` は **人が画面を見た時刻** (JST、`+09:00` が要る)。note の公開時刻とは別。
  保存は UTC にそろえる。同じ時刻を別のオフセットで書いても 1 行。
- `--source` はどの画面から写したか (例 `note dashboard (all time)`)。`--by` は短い名前。
- 今は入力しない: 販売の数 (有料はまだ無い)、`referral_sessions` (GA4 の流入の新しい問い
  合わせの形は人の承認が要る。G4 の条件)。

## 入力のしかた (1 回の観測をまとめて)

まず PLAN (検査だけ。何も書かない):

```bash
uv run python scripts/record_manual_metric.py record-snapshot --observed-at 2026-10-02T21:00:00+09:00 --source "note dashboard (all time)" --by human --entry external-nd9c4fb635aad:views=<値> --entry external-nd9c4fb635aad:likes=<値> --entry external-nd9c4fb635aad:comments=<値> --entry draft-c9d0eae558:views=<値> --entry draft-c9d0eae558:likes=<値> --entry draft-c9d0eae558:comments=<値> --entry draft-e068f0146e:views=<値> --entry draft-e068f0146e:likes=<値> --entry draft-e068f0146e:comments=<値> --entry draft-bab70591d9:views=<値> --entry draft-bab70591d9:likes=<値> --entry draft-bab70591d9:comments=<値> --entry note:followers=<値>
```

問題が無ければ、同じ行の最後に `--execute` を付けて記録する。全部を検査してから書き、1 つでも
拒まれれば何も書かない。`<値>` は画面の数をそのまま (整数)。見えない数の `--entry` は外す。

- 間違えた値を直す: 新しい観測として `record ... --supersedes <古い行の id>` (行は消さない)。
- 同じ観測をもう一度送っても 1 行のまま (値が違えば止まる)。

## 約 4 週間の観測の予定

- **baseline**: 最初の観測 (できれば 2026-10-02 の夜など、早いうちに 1 回)。
- そこから **7 日ごと** に 4 回 (week 1〜4)。前後 3 日の窓に入っていれば、その回は観測済み。
  同じ時刻でなくてよい (`observed_at` は毎回残る)。
- 例: baseline 2026-10-02 → 10-09 → 10-16 → 10-23 → 10-30。
- 観測は毎回、4 本と `followers` をまとめて 1 回の `record-snapshot` にする。

## 報告

```bash
uv run python scripts/record_manual_metric.py report
```

- 記事ごと (役割つき): 観測の推移・公開からの日数・前の観測との差と日数・累計が減ったときの注意。
- `followers` の推移。
- 観測の予定: baseline と week 1〜4 の目標日と状態 (`observed` / `due` / `missed` / `upcoming`)。
- 指標ごとの標本の大きさ (`small_sample`: 対象 3 未満か 28 日未満なら比べない)。
- 読み方: 観測した値だけ。推定・補間・順位・因果は出さない。記事の数も期間も小さいので、
  「このテーマだから伸びた」とは言わない。公開からの日数が違う記事を、同じ日の累計で比べない
  (比べるなら公開からの日数をそろえた点どうし)。

## まだしないこと

- note の管理画面を自動で読むこと (ログインの自動化・非公式の API はしない)。
- GA4 の新しい問い合わせの形 (人の承認が要る)。
- 4 週間に満たない数での結論、販売の文言への利用。
