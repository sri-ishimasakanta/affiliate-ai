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

## 承認した行動の変換と効果の観測 (C9 Batch 3)

```bash
uv run python scripts/convert_growth_action.py <growth_action_id>            # PLAN (書かない)
uv run python scripts/convert_growth_action.py <growth_action_id> --execute  # 手元の依頼だけ
uv run python scripts/analyze_growth_action_outcomes.py list                  # 読むだけ
uv run python scripts/analyze_growth_action_outcomes.py show <id> --checkpoint 7d
```

### 対応の表 (`app/growth/conversion.py` の `CONVERSION_MATRIX`)

| 行動 | 実行の形 | 先の流れ / いま足りないもの |
|---|---|---|
| `review_internal_links` | **local_handoff** | 保存済みの C6 の候補 (無ければ C6 の評価を保存。C6 がいまも提案していることを確かめてから) → `ChangeRequestService.propose_from_seo_candidate` → `change_requests` (awaiting_approval、`source_engine=seo` のまま)。**承認・適用はしない** |
| `create_new_article` | **local_handoff** (C9-B) | 記事の計画の依頼 (`article_planning`)。記事は作らない |
| `create_growth_post` | plan_only | Growth の枠が生成を持つ (1 日の呼び出しの上限・目的の検査)。渡すものが無い |
| `create_regular_threads_post` / `create_threads_alternative_angle` | **local_handoff** (C9-B) | 記事 (と切り口) を指定した生成の依頼 (`threads_generation`) |
| `review_affiliate_placement` / `update_existing_article` / `improve_search_snippet` | **local_handoff** (C9-B) | 変更の準備の依頼 (`change_preparation`: `affiliate_placement` / `body_update` / `meta_description`) |
| `wait_for_more_data` / `investigate_data_quality` | not_applicable | 情報 |

### 変換の約束 (`growth-action-conversion/1`)

計画: 版・指紋・レビュー・行動・先の流れ・対象 (レビューに固定した内部リンクの先)・実行の形・
前提・止める理由・予定の手元の書き込み・外の書き込み (常に無し)・idempotency key・plan hash。

`--execute` の直前に全部を確かめ、どれかが違えば断る (`conversion_refused` の出来事に理由):
承認済みのレビュー / 固定した指紋と写しの hash が合う / 最新の版 / superseded でない / いまの評価が
同じ指紋の候補を出している / いま動ける / 止める理由がレビューのときと同じ / その記事に開いている
変更の依頼が無い。途中の失敗は `conversion_failed` (何も変換済みにしない)。

冪等: `growth_action_conversions.idempotency_key` (一意) と `change_requests.idempotency_key`。
同じ変換の 2 回目は前の結果を返す (書かない)。

### 記録 (migration `74dbecaa4bb2`、**本番にはまだ適用していない**)

`growth_action_conversions` (成功した変換だけ: 版・レビュー・先の種類と ID・計画と hash・書いた
手元の表・実行した時刻)。出来事に `conversion_executed` / `conversion_refused` /
`conversion_failed` を足す。downgrade は変換の記録が 1 行でもあれば止まる。表が無い DB では PLAN
だけが動き、`--execute` は理由つきで断る。

### その先と効果

- 先の状態は読むだけ: 変更の依頼の状態・最新の決定・適用。**変換 ≠ 適用**、**承認 ≠ 公開**。
- `effective_at` は実際の適用の成功 (`change_applications.finished_at`) だけ。承認・変換の時刻は
  使わない。まだなら `None` で、観測は `waiting`。
- 窓 24h / 72h / 7d / 14d / 28d は `ChangeEffectService` (同じ長さの前後の窓、GSC の取り込みの
  遅れ、信頼できるクリックだけ、`causal_claim=none`) を使う。状態: waiting / insufficient /
  observable / stale_data / completed_window。観測の文は「変更後の窓では X を観測」「変更前の窓との
  差は Y」だけ。1 つの点数・勝ち負けは作らない。
- 閉じた輪: 変換した版は同じ証拠なら出てこない (`suppressed_completed`)。新しい証拠は新しい版
  (変換済みの版は converted のまま、`superseded_by_id` でつながる)。開いている変更の依頼は、同じ
  記事の WordPress の行動を「既存の仕事が担う」にする (同じ記事の同時の編集を避ける)。
- worker は変換しない (評価と手元の履歴の更新だけ、Batch 2 のまま)。

## C9-A: 機会の識別の固定・優先・まとめ (weekly digest)

### 別の切り口の機会は記事ごとに 1 つ

原因: C9 Batch 2 では「サイトの直近 1〜2 本と違う、まだ試していない最初の切り口」を機会の鍵
(`...:angle=<切り口>`) と material に入れていた。直近の投稿が入れ替わるだけで鍵が変わり、行が
増えた (既存の仕事が担うので知らせてはいなかった)。

- 今の鍵: `create_threads_alternative_angle:article:article:<id>` (変種なし)。
- 勧める切り口は `recommendation` (弱い好み)。**鍵・証拠の指紋に入れない** (変わっても同じ版)。
- 新しい版になるのは material の変化だけ: 順位が中央値を跨ぐ・試した切り口・関係する出所の状態・
  止める理由。
- 古い形の行は消さない・書き換えない。今の規則で計算し直した指紋 (`stable_candidate_fingerprint`)
  が同じなら、その行を同じ候補として扱う (新しい行を作らない。却下・変換・レビュー中はそのまま効く)。
  1 つの機会に生きている行は 1 つ (ほかの古い変種は `not_observed`: 置き換えられた)。証拠が前の版に
  戻ったときは、いまの版を生きたまま残す。migration は無い。
- 識別の版 (`growth-action-identity/1`) は変えていない (ほかの行動の指紋は同じ)。

### 優先 (人が扱える少数へ)

対象: `active` + `actionable_now` + まだ扱われていない + 同じ状態 (同じ指紋) で知らせていない。
除外の理由: blocked / covered_by_existing_work / informational / not_observed / レビュー中・承認済み・
却下・変換済み・見送り・置き換え (not_active) / already_notified_same_state / digest_limit。

並べ方は C9 の成分の順 (証拠の強さ → 機会 → 収益との関係 → 手間 (少ない方) → 急ぎ)。1 つの点数は
作らない。候補ごとに「選んだ理由」と「次の候補より前に来た理由」(最初に違う成分) を出す。多様さは
弱い調整: 種類ごとに一番よいものを先に入れ、残りは全体の順で埋める (候補が少なければ同じ種類が
複数でもよい)。

### まとめ (digest)

```bash
uv run python scripts/manage_growth_actions.py digest-plan            # 読むだけ・送らない
uv run python scripts/manage_growth_actions.py digest-send            # PLAN
uv run python scripts/manage_growth_actions.py digest-send --execute  # 方針で有効なときだけ送る
uv run python scripts/manage_growth_actions.py review <id> --fingerprint <sha> --execute
```

| 項目 | 内容 |
|---|---|
| 件数 | 5 件まで (`growth_action_policy.json` `max_items`)。足りなければ少ないまま |
| 間隔 | 週 1 回 (`cadence_days` 7)。前に送ってから 7 日経つまで送らない |
| 窓 | 承認の通知の窓 (`threads_operations_policy.json` の `approval_notification_window`、08:00–21:00 JST) の中だけ。夜に候補が増えても送らない |
| 送信 | `sending_enabled` (既定 **false**) と `--execute` の両方が要る。**最初の本番の送信は人の判断** |
| 履歴 | 既存の `notification_deliveries` の 1 行 (種類 `growth_action_digest`、dedupe key はまとめの ID、`detail_json` に候補の ID・版・候補の指紋・通知の指紋・除外の数)。新しい表は無い |
| 重複 | 同じ状態の候補はもう知らせない。同じまとめは 2 回送らない。証拠が変われば指紋が変わるので知らせてよい |
| レビュー | 1 件ずつ (一括の承認は無い)。まとめの指紋で `review --fingerprint` を依頼し、古ければ断る。承認・却下は既存の指紋の照合のまま |
| 携帯 | **DEFERRED**: mobile approval の中継の対象の種類は DB の CHECK と WordPress の中継で決まり、Growth Action を足すには中継の再配置が要る。CLI のレビューのまま |
| worker | まとめを送らない (評価と手元の履歴だけ、Batch 2 のまま)。定期の実行は送信の承認のあとで決める |

## C9-B: 下流への安全な引き渡し (手元の依頼)

承認した Growth Action を、既存の流れの **手元の依頼** に変える。表は 1 つ
(`growth_handoff_requests`、migration `4fe83827d695`、2026-09-29 に本番へ適用済み。追加だけで
既存の表は変えない。downgrade は依頼の行が 1 行でもあれば止まる)。どの依頼も OpenAI・Threads・
WordPress を呼ばない。**Growth Action の変換 ≠ 先の承認 ≠ 公開 / 適用** (状態は別々に見る)。

```bash
uv run python scripts/convert_growth_action.py <id>                        # PLAN (書かない)
uv run python scripts/convert_growth_action.py <id> --execute              # Threads・記事の計画
uv run python scripts/convert_growth_action.py <id> --execute --expected-source-hash <hash>  # 変更の準備
uv run python scripts/manage_growth_actions.py handoff list|show <request_id>
uv run python scripts/manage_growth_actions.py handoff approve-plan|reject-plan <request_id> --execute
uv run python scripts/manage_growth_actions.py handoff materialize <request_id> --article-id <id> --execute
uv run python scripts/manage_growth_actions.py handoff prepare <request_id>     --downstream-type change_request|editorial_revision|link_mapping --downstream-id <id> --execute
uv run python scripts/manage_growth_actions.py handoff close <request_id> --reason "..." --execute
```

`show` / `explain` / `history` の linkage に、依頼の状態とその先 (提案・記事・変更) の状態が出る。

| 流れ | 状態 | 作るとき (変換の直前の検査、どれも fail closed) | その先 |
|---|---|---|---|
| `threads_generation` (`regular` / `alternative_angle`) | pending → claimed → proposal_created (claimed → pending: 答えが来ないまま古くなった) / cancelled | 記事が公開済み (URL あり) / その記事に開いている・承認済みで未公開の提案が無い / その記事に開いている指定の依頼が無い / 別の切り口は、変換の時点の勧めの切り口を固定し、もう試した・公開した切り口なら断る | 在庫の保守が使う (下) → 提案は awaiting_approval → 既存の承認・公開 |
| `article_planning` | pending → approved → materialized / rejected / cancelled | キーワードがある / 同じキーワード (ID か正規化した文字列) の archived でない記事が無い / 開いている計画の依頼が無い / 既存の記事の計画 (`ArticlePlanService.plan_for_keyword`、読むだけ) で重なりの確認が要らない | 人が承認 → 既存の流れ (`export_article_plan.py` → `POST /api/v1/keywords/{id}/article-plan/approve`) で記事を作る → `materialize` で結ぶ (同じキーワード・planned 以降で archived でない・依頼より後に作った・ほかの依頼と結んでいない記事だけ) |
| `change_preparation` (`body_update` / `meta_description` / `affiliate_placement`) | pending → prepared / rejected / cancelled | 記事が公開済み / その記事に開いている変更の依頼が無い / 同じ種類の開いている準備が無い / **計画で見た元の hash** (`--expected-source-hash`) が今と同じ | 人が既存の流れで具体的な変更を作る → `prepare` で結ぶ。結べる先: 本文 = 変更の依頼か編集の版、メタ = 編集の版、配置 = リンクの置き換え (`manage_article_link_mapping.py`) か変更の依頼。固定した元の hash の上に作ったもの・依頼より後に作ったもの・却下 / 古いでないもの・ほかの依頼と結んでいないものだけ |

固定する中身 (`frozen_json`): 版・指紋・レビューの写しの hash・止める理由・証拠の要素、記事の
題と状態 (URL は入れない)、Threads は道と切り口と試した切り口、記事の計画はキーワードと重なりの
確認、変更の準備は本文とメタの hash (配置はさらにプログラム・有効な追跡先・有効な置き換えの ID)。
**生成した中身・推測した URL・追跡の URL は入れない。** 空の変更の依頼を作って変換済みにしない。
待っている変更の準備は、元が変わると `source_drift` に出る (`prepare` は断る。閉じて見直す)。

### 在庫の保守が指定の依頼を使う規則

- `growth_action_policy.json` の `threads_generation_requests.consume_in_stock_maintenance`
  (コードの既定は無効: 鍵が無い・true 以外なら使わない)。本番の方針は 2026-09-29 に人の判断で
  **true** (その先で既存の OpenAI の生成が指定の記事に向く)。false に戻せば今までと同じ
  (依頼は pending のまま待つ)。待っている依頼が無ければ、true でも計画は同じ。
- true のとき: 在庫の規則で **もともと生成するとき** だけ、公平の順の 1 つの代わりに使う
  (下限 3 / 1 回 3 本 / 答え待ちの抑止 / 1 記事 1 本 / トピックの散らし / 切り口の重なりはそのまま)。
  呼び出しの数は増えない。使えない (その記事に在庫がある・休みの期間・固定した切り口が最近使われた・
  記事が対象外) なら使わずに待つ。
- 依頼を出したら `claimed` (生成の依頼の ID)、提案を保存したら `proposal_created` (提案の ID)。
  答えが来ないまま古くなれば `pending` へ戻る。依頼の記録に失敗しても在庫の保守は止めない。
- worker は変換しない (変換は人の `--execute` だけ)。

### わかっている制約 (C9-B で変えていない)

- 変更の適用 (`ChangeApplicationService`) は V1 のまま: 1 本の内部リンクの挿入だけを通す。本文の
  書き換え・配置の変更の変更の依頼は、準備と結べても、適用の段で止まる (fail closed)。
- メタディスクリプションは WordPress への更新の経路が無い (更新は本文だけ、抜粋は最初の下書きの
  ときだけ)。手元だけ変えると後の照合が合わなくなる。適用の経路は後の段階。
- 記事の計画の承認は API だけ (記事の行そのものが承認の記録)。計画の依頼は、その記事を結ぶだけ。
- 変換の先の効果の観測は、変更の依頼の適用だけ (Threads の公開・記事の公開の窓は C9-C)。

