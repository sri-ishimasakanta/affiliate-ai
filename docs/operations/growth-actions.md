# Growth Action の受け箱・履歴・人のレビュー (C9 Batch 2)

[成長の候補](growth-opportunities.md) (C9 Batch 1) の `GrowthActionCandidate` を、毎回の分析の結果から
**同じ機会を見分け → 重複を抑え → 状態を追い → 人が見る受け箱・まとめ → 人のレビュー** へつなぐ。
外への実行はしない。**承認は「次の段階へ進めてよい」という許可だけ** で、WordPress の更新・
Threads の投稿・記事の公開・アフィリエイトの設定は何も起きない。

```bash
uv run python scripts/manage_growth_actions.py list                 # 既定: いま動けるものだけ (種類ごとに 3 件)
uv run python scripts/manage_growth_actions.py list --all --action-type review_internal_links
uv run python scripts/manage_growth_actions.py explain <id|opportunity_key>
uv run python scripts/manage_growth_actions.py refresh               # PLAN (書かない)
uv run python scripts/manage_growth_actions.py refresh --execute     # C9 の履歴の表だけに書く
uv run python scripts/manage_growth_actions.py review <id> --execute
uv run python scripts/manage_growth_actions.py approve <review_id> --fingerprint <sha> --execute
uv run python scripts/manage_growth_actions.py reject <review_id> --fingerprint <sha> --reason "..." --execute
uv run python scripts/plan_growth_action_digest.py                   # まとめの PLAN (送らない)
```

## 既存の承認の仕組みとの関係 (調べた結果)

- **ChangeRequest は使わない。** 記事の本文の差分 (`article_id`・`proposed_body`・本文の hash が必須)
  を WordPress に適用する流れのための表で、`source_engine` は seo/revenue/manual の CHECK があり、
  `approved` の次は `applied`。Growth の候補を入れると、偽の値と「承認 = 適用できる」の意味の
  衝突が起きる。候補が変更の依頼を **生む** ときは、その依頼の流れ (`propose_change.py`) に渡す。
- **mobile approval の中継はまだ使わない。** 対象の種類は DB の CHECK と WordPress 側の中継の
  許可の一覧で決まっていて、新しい種類を足すには WordPress の再配置 (外への書き込み) が要る。
  このバッチのレビューは管理用 CLI。使い回せる部品 (能力の token・中継の client・通知の記録・
  まとめの選び方) は、次の段階で使える。
- 使い回した考え方: 固定した hash と一致したときだけ承認する・決定は追記だけ・状態の遷移を
  狭く持つ・「PLAN が既定、書くのは --execute だけ」。

## 表 (migration `74bfaf6c9c9f`、**本番にはまだ適用していない**)

| 表 | 中身 |
|---|---|
| `growth_action_candidates` | 候補の **版**。`opportunity_key`・`revision`・`candidate_fingerprint` (一意)・`evidence_fingerprint`・行動・対象・状態・availability・最初の候補の写し (上書きしない)・`first_seen_at`/`last_seen_at`/`seen_count`・`superseded_by_id` |
| `growth_action_reviews` | 1 つの版に 1 つのレビュー。見せた内容の写しと hash・版と証拠の指紋・状態 (pending/approved/rejected/stale)・決めた人・理由 |
| `growth_action_events` | 出来事 (追記だけ): observed / availability_changed / superseded / not_observed / review_requested / approved / rejected / review_stale / dismissed |

downgrade は 3 つの表を落とすだけ (ほかの表には触れない)。本番の写しで upgrade → check →
downgrade (ほかの表の行数は同じ) → upgrade を確かめた。履歴の表が無い DB でも `list` /
`explain` / `refresh` (PLAN) / まとめの PLAN は動く (書く操作は理由つきで断る)。

## 識別

- `opportunity_key` = `action_type:subject_type:subject_id[:variant]`。同じ記事の内部リンクの
  見直しは同じ機会。Threads の別の切り口は `angle=<切り口>` を変種に持つ (切り口ごとに別の機会)。
- `evidence_fingerprint` = 判断に意味のある値 (`material`: 元のエンジンの候補の種類と理由・
  信頼できるクリックの数・切り口・順位が中央値の上か下か・スコアの ID など) と、**その行動に関係する
  出所だけ** の状態 (鮮度は状態として。`ACTION_EVIDENCE_SOURCES`: 内部リンクは SEO・GA4・索引、
  Threads の候補は Threads、など) の正規の JSON の sha256。関係の無い出所の変化 (例: Threads の
  投稿が 24h を過ぎた) で、内部リンクの候補が新しい版にならない。全体の証拠の段階は表示だけ。**時間が経つだけで変わる値 (経過日数・投稿の
  経過時間・views・data-through の日付) は入れない** (表示の写しには残る)。入れると毎日新しい版に
  なり、却下したものが毎日出てくるため。
- `candidate_fingerprint` = 機会 + 証拠の指紋 + 行動の意味 (承認・外への書き込み・戻せるか・
  前提・止める理由)。

## 状態と重複の抑え

版の状態: `observed` (いまは人に出さない) / `active` (レビューに出せる) / `pending_review` /
`approved` / `rejected` / `superseded` / `converted` (まだ使わない) / `dismissed`。

| 場合 | 扱い |
|---|---|
| 同じ `candidate_fingerprint` | 行を増やさない (`last_seen_at`・`seen_count` だけ)。知らせない |
| 却下した版と同じ | 出さない (`suppressed_rejected`)。証拠が変われば新しい版で出る |
| 新しい証拠 | 新しい版 (`revision + 1`)。古い版は `superseded`。開いていたレビューは `stale` |
| 同じ機会にレビューが開いている | 2 つ目のレビューを作らない (受け箱では「既存の仕事が担う」) |
| 開いている Threads の提案・変更の依頼 | 「既存の仕事が担う」(理由を表示) |
| 承認済みで同じ証拠 | 出さない (`suppressed_completed`) |
| 待つ・データの質の確認 | `informational` (承認を求めない) |
| 評価に出なくなった | `not_observed` (承認できない) |

## Threads の重なり

別の切り口・もう一度の紹介の候補は、次のときに「既存の仕事が担う」: 記事に開いている通常の
提案 (proposed / awaiting_approval / 公開前の approved) がある、通常の投稿が 72 時間以内にあった
(先の投稿を見届ける)、在庫の保守が次にその記事を計画している。切り口の変種は、サイト全体の直近
1〜2 本の通常の投稿の切り口を避けて選ぶ (既存の弱い好み)。Growth の提案は記事の候補を覆わず、
通常の提案は Growth の候補を覆わない。

## 受け箱とまとめ

- 受け箱の既定: いま動けて人の判断を待つものだけ。行動の種類ごとにまとめ、種類ごとに 3 件
  (残りは件数)。止める理由・既存の仕事の理由・証拠の段階・5 つの成分を出す。
- まとめ (PLAN): 新しく出てきた (`new` / `new_revision`) 動ける候補から、既定 8 件・種類ごとに
  2 件。種類を順番に回して選ぶので、1 つの種類で埋まらない。1 つの点数は作らない。送らない。

## 人のレビュー

`review` で版の内容 (指紋・証拠・根拠・止める理由・成分・鮮度・変換の計画) を固定し、`approve` /
`reject` はレビューに見せた指紋 (`--fingerprint`) が合うときだけ。版が古い・新しい版がある・
評価に出なくなった・写しの hash が違う → レビューを `stale` にして **承認しない**。同じ決定の
繰り返しは何もしない (冪等)。承認のあとで動くのは、次の段階の入口 (下の変換) を人が使うとき。

## 変換 (承認のあと、PLAN だけ)

| 行動 | 変換 |
|---|---|
| `review_internal_links` | supported: C6 の候補を保存 → `propose_change.py --candidate-id` (変更の依頼。独自の承認と適用) |
| `create_new_article` | supported: `export_article_plan.py --keyword-id` (読むだけの記事の計画) |
| `create_growth_post` | 既存の Growth の枠が担う (変換なし) |
| `review_affiliate_placement` | 人が見る (`manage_article_link_mapping.py` は PLAN が既定) |
| `create_regular_threads_post` / `create_threads_alternative_angle` | **unsupported**: 在庫の保守は記事・切り口を指定する入口を持たない |
| `update_existing_article` / `improve_search_snippet` | **unsupported**: 変更の依頼は v1 で内部リンクしか作らない |
| `wait_for_more_data` / `investigate_data_quality` | 対象外 (情報) |

## worker

subsystem `growth_opportunity_evaluation` (`threads_operations_policy.json` の
`subsystems.growth_opportunity_evaluation`)。**既定は無効**。

| 設定 | 既定 |
|---|---|
| `enabled` | `false` |
| `check_interval_minutes` | 60 (軽い点検: 取り込みの成功の時刻と新しい行の印だけ) |
| `interval_minutes` | 1440 (重い評価) |
| `min_interval_minutes` | 360 (印が変わったときの前倒しの最短) |
| `write_history` | `true` (書くのは C9 の表だけ) |

履歴の表が無ければ何もしない (理由を返す)。本番で使うには、migration の適用と方針の有効化
(worker の再起動) が要る。
