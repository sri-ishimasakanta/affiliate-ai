# N7 実パイロット開始前のレビュー (2026-10-01)

**この資料は人の判断のためのもの。何も送っていない・誰も登録していない・基準も確定していない。**
状態: N7 = ACTIVE / 受け入れの仕組み = READY / 本物のパイロット = 0 / 基準 = proposed /
N8 = insufficient_evidence。

人が決めること:

1. 今の基準 (proposed) をこのまま **confirmed** にするか。
2. 直したい基準があるか (直すなら、確認の前に `pilot_policy.json` を直す。版を上げる)。
3. パイロットの募集を始めてよいか。

## A. 今の基準 (pilot_policy.json、全文)

```json
{
  "policy_version": "pilot-policy-1",
  "status": "proposed",
  "min_pilots": 3,
  "criteria": {
    "activation_rate_min": 0.67,
    "time_to_first_value_hours_median_max": 72,
    "workflow_completion_rate_min": 0.67,
    "repeat_usage_active_days_min": 8,
    "repeat_usage_rate_min": 0.5,
    "onboarding_difficulty_median_max": 3,
    "support_minutes_median_max": 120,
    "value_rating_median_min": 4,
    "would_continue_rate_min": 0.5,
    "cost_within_willingness_to_pay": true
  },
  "model_signals": {
    "self_service_onboarding_difficulty_max": 2,
    "self_service_support_minutes_max": 60,
    "managed_value_rating_min": 4,
    "managed_onboarding_difficulty_min": 4,
    "managed_support_minutes_min": 180
  }
}
```

(`note` の欄は説明なので省いた。) **基準の hash: `aa5e72d4df345dc5c7c58da92c65d69a514541baf8e56880c9fe838b9d3023fb`**
(`policy_version`・`min_pilots`・`criteria`・`model_signals` から。`status` と `note` は入らない)。

## B・C. 基準の一覧と判定のしかた

どの基準も、**数を記録した本物のパイロットが `min_pilots` (3) 人に届かなければ `insufficient`**
(証拠が足りない)。数はパイロットごとの最新の値 (直した行は除く)。

| 基準 | 指標 (単位) | 判定 | 今の値 | 無いとき | 0 のとき |
|---|---|---|---|---|---|
| activation | `activated` (0 / 1) | 1 の人の割合 ≥ 値 | 0.67 | その人は数えない (足りなければ insufficient) | 「始められなかった」として数える |
| time_to_first_value | `time_to_first_value_hours` (時間) | 中央値 ≤ 値 | 72 | 同上 | 0 時間として数える |
| workflow_completion | `workflows_completed` (回) | 1 以上の人の割合 ≥ 値 | 0.67 | 同上 | 「終えていない」として数える |
| repeat_usage | `active_days` (日) | 8 日以上の人の割合 ≥ 値 | 8 日 / 0.5 | 同上 | 「繰り返し使っていない」として数える |
| onboarding_difficulty | `onboarding_difficulty` (1〜5、5 = とても難しい) | 中央値 ≤ 値 | 3 | 同上 | 範囲の外 (1〜5) なので記録できない |
| support_burden | `support_minutes` (分) | 中央値 ≤ 値 | 120 | 同上 | 0 分として数える |
| value | `value_rating` (1〜5) | 中央値 ≥ 値 | 4 | 同上 | 記録できない (1〜5) |
| would_continue | `would_continue` (0 / 1) | 1 の人の割合 ≥ 値 | 0.5 | 同上 | 「続けない」として数える |
| cost_vs_willingness_to_pay | `operating_cost_jpy` + `api_cost_jpy` と `willingness_to_pay_jpy` (円) | 費用の中央値 ≤ 払う意思の中央値 | (比べるだけ) | **費用は 2 つとも記録した人だけ** (片方が無いのを 0 にしない。2026-10-01 に直した) | 0 円として数える |

全体の読み: insufficient が 1 つでもある・人数が足りない → `insufficient_evidence`。すべて
満たす → `evidence_supports_go`。半分より多く満たさない → `evidence_against_go`。それ以外 →
`mixed`。**どれも判断ではない。**

基準に入っていないが記録して見るもの (判定には使わない):

| 項目 | どこに | 読み方 |
|---|---|---|
| 実際の支払い | `payment_received_jpy` (円、台帳) | 下の「支払いの扱い」 |
| 手助けの回数 | `support_contacts` (回、台帳) | 記録するだけ |
| つまずき | `friction_setup` / `friction_approvals` / `friction_cost` / `friction_trust` (0 / 1、台帳) と `blocker` の出来事 | 要約に並べるだけ |
| 途中でやめた | `withdraw` の出来事 (理由: pilot_choice / no_fit / no_time / other) | 人数を数えるだけ |
| 声 | `feedback` の出来事 | 記録の有無を出すだけ |

## E. SaaS / Managed / Hybrid の既存の規則 (`model_signals`) と N8 の関門

- self-service の兆し: 難しさの中央値 ≤ 2 **かつ** 手助けの分数の中央値 ≤ 60。
- managed の兆し: 価値の中央値 ≥ 4 **かつ** (難しさの中央値 ≥ 4 **または** 手助けの中央値 ≥ 180)。
- 読み: 両方 → 「Hybrid を考える」、self-service だけ → SaaS 寄り、managed だけ → Managed 寄り、
  どちらも無い → はっきりしない。**どれも判断ではない。**
- N8 の関門 (`manage_pilots.py summary` の `n8_gate`) の条件 (今の規則のまま):
  1. `real_pilots_at_least_min`: 本物のパイロット ≥ 3
  2. `no_insufficient_criteria`: どの基準も insufficient でない (必要な数がそろっている)
  3. `policy_confirmed`: 基準の確認の記録の hash が今の基準と同じ
  - 1 か 2 が満たされない → `insufficient_evidence` (今はこれ)。1 と 2 は満たすが 3 が無い →
    `needs_human_policy`。3 つとも → `ready_for_human_decision` (それでも決めるのは人)。
  - **基準を確かめただけでは N8 に進めない** (1 と 2 が要る)。

## 基準の固定 (確認したとき)

- `manage_pilots.py policy` で版と hash を見る → 人が確かめたら
  `manage_pilots.py confirm-policy --policy-hash aa5e72d4df345dc5c7c58da92c65d69a514541baf8e56880c9fe838b9d3023fb --by human`
  (PLAN) → `--execute`。
- 記録: `app/config/pilot_policy_confirmations.json` (追記だけ。版・hash・確認した人と時刻・
  基準の写し)。git で管理する。
- 確認の後に基準を変えると、状態は `changed_after_confirmation` になり、N8 の関門は通らない
  (黙って書き換えられない)。変えるなら版を上げて、もう一度確認する。前の確認は残る。
- **本物の結果を見る前に確認する** (結果を見てから基準を動かさない)。決定の記録 (decision log) にも
  残すとよい (Claude が記録する場合は人の指示で)。

## D. 無い (missing) と 0 の扱い

- 無い数は `missing` と出す。**0 にしない**。0 を入れるのは、本当に 0 を観測したときだけ。
- 支払いの証拠が無ければ `missing` (false でも 0 でもない)。
- 推定 (`inference`)・仮説 (`hypothesis`) は記録できるが数えない。登録・状態の変化・終わり・
  やめたは、事実 (`observed_fact` / `human_reported`) でしか記録できない。
- 登録の無い `pilot-xx` の数、テストの fixture (`test_fixture`) は数えない (要約に名前を出して外す)。

## 支払いの扱い (`payment_received_jpy`)

| 場面 | 記録 | 要約での見え方 |
|---|---|---|
| 支払いを確かめていない | **何も記録しない** | `missing` |
| 払う意思だけ聞いた | `willingness_to_pay_jpy` (月にいくらなら払うか)。支払いは記録しない | 払う意思あり・支払い `missing` |
| 無料のパイロットで、支払いが無いことを確かめた | `payment_received_jpy` = 0 (期間を付ける) | `observed` 0 円 |
| 実際に受け取った | `payment_received_jpy` = 金額 (期間・観測した時刻) | `observed` の金額 |

- 記録するのは **仮名・金額・期間・観測した時刻・出どころ・入れた人** だけ。カードの番号・口座・
  請求書の番号・決済サービスの ID・領収書の画像・名前は入れない (カードの番号らしい数字の並びは
  拒む)。払う意思と実際の支払いは別の証拠。

## F. 募集の文 (送っていない。人が使う。【】は人が決めて埋める)

### 短い募集文

> AIで記事・SNS投稿・運用作業を自動化するときに、「どこで人が確認するか」を決めて運用する仕組みを、
> 実際に使ってみてくれる方を少人数で探しています。
>
> - 試すもの: 自動化の作業を段階に分け、人の承認を中身に結びつけて運用する仕組みと、その手順
> - お願いしたいこと: 【期間: 人が決める】ほど自分の作業で使い、ときどき使った感想を短く教えていただくこと
> - 記録すること: 使った回数・かかった時間・つまずいた点・感想など (お名前や連絡先は記録に残しません)
> - 費用: 【無料 / 有料: 人が決める。無料にするなら「パイロット中は無料です」】
> - 売上や成果、作業時間の削減などは約束できません。途中でやめても大丈夫です。

### 少し詳しい参加の説明

- **目的**: この仕組みが、自分以外の運営者にも役に立つか、どこでつまずくかを知るため。結果は今後
  どんな形で提供するか (自分で使う形・運用を任せる形など) を決める材料にします。
- **試す流れ**: 書き込み先を一覧にする → 5 つの段階に分ける → 人の承認を中身に結びつける → 実際の
  作業で使う → 振り返る。
- **お願いする協力**: 最初の設定 (一緒に行います)・期間中の利用・週に 1 回ほどの短い感想・最後の
  振り返り (10〜20 分ほど)。
- **感想の伝え方**: 話してもらった内容を、こちらで短くまとめて記録します。使いやすさ (1〜5)・続けて
  使いたいか・支払うならいくらまでか、を最後に伺うことがあります。答えたくない質問は答えなくて
  構いません。
- **データの扱い**: 記録するのは、仮の名前 (pilot-01 など)・数・短いまとめだけです。お名前・
  メール・電話・住所・アカウントの情報・パスワード・支払いの情報は、記録に入れません (連絡先は
  こちらで別に管理し、記録とは結びつけません)。
- **お金について**: 【無料 / 有料: 人が決める】。「払うならいくらか」を伺うことがありますが、
  それは意見としてだけ扱い、実際の支払いとは別のものです。
- **やめるとき**: いつでもやめられます。理由は言わなくても構いません。

### 人が候補者ごとに確かめること (記録の前に)

- [ ] 参加の合意をもらった (合意そのものは、この仕組みの外で人が持つ) → `--agreement-confirmed`
- [ ] 試す用途を短く確かめた (個人や会社が特定できない書き方で) → `--use-case`
- [ ] どこで知ったか (own_network / referral / community / inbound / other) → `--acquisition`
- [ ] 名前・メール・電話・住所・URL・アカウント情報を記録に入れない (連絡先は人が別に持つ)
- [ ] 実際に始めた日時 → `--started-at`
- [ ] 仮名を決めた (`pilot-01`、`pilot-02` … 実名や会社名から作らない)

## G. 受け入れの手順 (pilot-01〜03 の例。**まだ誰も登録していない**)

コマンドは既定が PLAN (書かない)。確かめてから同じ行に `--execute`。共通: `--source "<どこで>"
--by human --observed-at <人が見た・聞いた時刻+09:00> --evidence-kind <種類>`。

| # | 時点 | 記録 | どこに | 種類の目安 |
|---|---|---|---|---|
| 1 | 合意 | (記録しない。人が外で持つ) | — | — |
| 2 | 登録 (PLAN) | `register pilot-01 --started-at … --use-case … --acquisition … --agreement-confirmed` | 出来事 | human_reported |
| 3 | 登録 (実行) | 同じ行に `--execute` | 出来事 | — |
| 4 | 設定 | `onboarding pilot-01 --state in_progress` → 終われば `--state completed` (止まったら `blocked`) | 出来事 | observed_fact |
| 5 | 最初の利用 | `usage pilot-01 --note "<何をしたか>"`、数: `activated`=1・`time_to_first_value_hours` | 出来事 + 台帳 | observed_fact / measured_metric |
| 6 | 繰り返しの利用 | 数: `active_days`・`workflows_completed` (期間を付けて)。毎回の利用を出来事にしなくてよい | 台帳 | measured_metric |
| 7 | 結果 | `outcome pilot-01 --note "<観測した結果>"` | 出来事 | observed_fact |
| 8 | 声 | `feedback pilot-01 --note "<要約>"`、数: `value_rating`・`onboarding_difficulty` | 出来事 + 台帳 | human_reported |
| 9 | つまずき | `blocker pilot-01 --category setup|approvals|cost|trust|other --note …`、数: `friction_*` (0 / 1) | 出来事 + 台帳 | observed_fact / human_reported |
| 10 | 続けたいか | 数: `would_continue` (0 / 1) | 台帳 | human_reported |
| 11 | 払う意思 | 数: `willingness_to_pay_jpy` | 台帳 | human_reported |
| 12 | 実際の支払い | 数: `payment_received_jpy` (確かめたときだけ。上の表) | 台帳 | observed_fact |
| 13 | 終わり | `close pilot-01 --note …` か `withdraw pilot-01 --reason …` | 出来事 | human_reported |

- 費用: `operating_cost_jpy` と `api_cost_jpy` は期間ごとに **2 つとも** 記録する (片方だけだと
  費用の比較に入らない)。手助けは `support_minutes`・`support_contacts`。
- 台帳の数: `uv run python scripts/record_manual_metric.py record --kind pilot --ref pilot-01 --metric <m> --value <v> --observed-at <時刻> --period <開始:終わり> --source "<どこで>" --by human` (PLAN → `--execute`)。
- 間違えたら: 出来事は `correct pilot-01 --supersedes <id> --reason …`、数は
  `record ... --supersedes <行の id>`。消さない。

## 観測のタイミング (新しい閾値は作らない)

| 時点 | 何を | 回数 |
|---|---|---|
| 登録 | register | 1 回 |
| 設定 | onboarding の状態が変わったとき | 変わるたび |
| 最初の利用 | usage 1 回 + activated・time_to_first_value_hours | 1 回 |
| 繰り返しの利用 | active_days・workflows_completed (期間つき) | 週に 1 回ほど (同じ期間を二重に入れない) |
| 結果 | outcome | 観測したとき |
| 声 | feedback + value_rating・onboarding_difficulty | 中間に 1 回・終わりに 1 回ほど |
| 払う意思・支払い | willingness_to_pay_jpy・payment_received_jpy | 終わりに 1 回 (支払いは確かめたときだけ) |
| 終わり | close / withdraw | 1 回 |

同じ事実を出来事と数の両方に書かない (数は台帳、言葉は出来事)。累計を写すなら期間なし、期間の値
なら `--period` (1 つの指標で混ぜない)。

## H. 個人の情報を入れない

- 拒むもの (出来事の文・台帳のメモ): メール・電話・URL・住所 (郵便番号・番地)・カードの番号らしい
  数字の並び・パスワードや token・API の鍵らしい書き方・秘密らしい値。
- **名前は仕組みでは見分けられない**: 仮名 (`pilot-xx`) だけを使い、文に名前・会社名を書かない
  (人の責任)。
- 連絡先や合意の文書は、人がこの仕組みの外で管理し、仮名との対応表もリポジトリに入れない。
- 記録は手元だけ (`data/n7/`・手元の DB)。外に送らない。

## I・J. 今の状態 (2026-10-01、読むだけで確かめた)

- 本物のパイロット: **0** (出来事のファイルはまだ無い)。
- 基準: `pilot-policy-1`、hash `aa5e72d4…`、**proposed** (確認の記録 0)。
- N8 の関門: **insufficient_evidence**。条件: real_pilots_at_least_min = false、
  no_insufficient_criteria = false、policy_confirmed = false。
