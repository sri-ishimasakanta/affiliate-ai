# プロジェクトの状態の報告 (T7: T7A / T7B / T7C)

1 つのコマンドで、今のプロジェクトの状態 (フェーズ・git・品質・DB・WordPress・featured
image・カテゴリ・収益化・Threads・承認・C8・スケジューラ・警告・決定・次の行動) をまとめる。
**読むだけ**: WordPress・Threads・スケジューラ・DB には書かない。`/go/` と Threads の API には
問い合わせない。T7A で土台を作り、T7B で出どころの強さ (source precedence)・食い違いの検出
(drift)・不変条件・時間で決まる状態・ドキュメントの健康・報告の比較・`--strict` の契約を足した。
T7C で閉じた: **T7 (Autonomous Project State) は完了**。この報告が、次のフェーズ (N0) からの
引き継ぎの正式な手段になる (下の「T7 の完了の条件」と「報告を作る時機」)。

```bash
uv run python scripts/generate_project_state.py                # 手元 + 安全な読み取り
uv run python scripts/generate_project_state.py --offline      # 外部 (WordPress) を読まない
uv run python scripts/generate_project_state.py --with-tests   # pytest もその場で実行して記録
uv run python scripts/generate_project_state.py --strict       # 契約の失敗があれば終了コード 1
uv run python scripts/generate_project_state.py --compare reports/older.json   # 前の報告との違い
```

`--json-only` / `--markdown-only` / `--no-decision-log` もある。

## 出力

| ファイル | 中身 | git |
| --- | --- | --- |
| `reports/project_state_latest.json` | 機械が読む報告 (下の形) | 管理外 (毎回作り直す) |
| `reports/project_state_latest.md` | 人が読む報告 (同じ dict から作る) | 管理外 |
| `reports/quality_latest.json` | `--with-tests` のときの pytest の結果 | 管理外 |
| `docs/decision-log/YYYY-Www.md` | 長く効く決定 (週ごと、ISO 週、JST) | **追う** |

## 報告の形 (`project-state/2`)

上の階層: `generated_at`・`generator_version`・`mode` (`live` / `offline`)・`project`・`git`・
`quality`・`database`・`wordpress`・`featured_images`・`taxonomy`・`monetization`・`threads`・
`approvals`・`analytics`・`scheduler`・`facts`・`drift`・`invariants`・`timing`・
`documentation_health`・`warnings`・`decisions`・`known_issues`・`next_actions`・
`source_freshness` (一覧は `app/project_state/strict.py` の `TOP_KEYS`)。

主要なセクションは `provenance` を持つ (`source_freshness` に一覧):

| 項目 | 値 |
| --- | --- |
| `source` | `live_rest` (WordPress の GET) / `local_db` (アプリの DB。SQLite は `mode=ro`) / `repository` (追跡しているファイル) / `runtime_report` / `local_system` (Get-ScheduledTask) / `local_command` (git・ruff など) / `derived` |
| `kind` | `observed` (観測) / `declared` (リポジトリの宣言) / `derived` (導いた) / `historical` |
| `freshness` | `fresh` (今観測した) / `recorded` (記録から読んだ) / `stale` / `unverified` (読んでいない) |
| `status` | `ok` / `degraded` / `unavailable` (読めなかった。理由付き) / `skipped` (offline) / `error` |

読めない外部は `unavailable` にして続ける (落ちない)。観測していない事実を観測として書かない
(offline の WordPress はリポジトリの記録を「宣言」として載せるだけ)。

## どこから読むか

| セクション | 出どころ |
| --- | --- |
| project | `docs/project-roadmap.json` (フェーズの宣言)。根拠のファイルがフェーズの ID を含むこと・commit があることを確かめる。根拠の無いもの (N0 など) は `declared_only` と出す。ドキュメントどうし・ドキュメントと実際の値の食い違いも出す (直さない) |
| git | `git` の読み取りのコマンドだけ (fetch・push はしない。ahead / behind は最後に fetch した時点) |
| quality | ruff・`alembic check`・`git diff --check` はその場で実行。pytest は `--with-tests` のときだけ。実行しなければ前回の記録、無ければ「不明」(**件数を作らない**) |
| database | Alembic の head (`alembic.ini`) と DB の revision (読むだけ。接続先は種類とファイル名 / ホスト名だけ) |
| wordpress / featured_images / taxonomy | WordPress の REST の GET (post・カテゴリ・タグ・media) と、W1.4 / W1.5 の manifest、W2 の計画のコード (`app/wordpress/taxonomy_plan.py`) |
| monetization | アフィリエイトの表 (tracking URL は「ある / ない」だけ。token・遷移先は選ばない) |
| threads | Threads の表・worker のロックの heartbeat・`app/config/threads_operations_policy.json`・スケジュールの launcher の flag (`--maintain-proposal-stock` の有無)・`data/threads-generation/status.json` (記録)・成績の診断の報告 |
| approvals | 携帯の承認の表・中継の README・在庫のドキュメントの「実物の携帯表示」の行 (中継には問い合わせない) |
| analytics | C8 の `operations_runs` / 手順 / 通知 / 取り込みの記録 |
| scheduler | `Get-ScheduledTask` / `Get-ScheduledTaskInfo` だけ (タスクを変えない・動かさない) |

## 警告・決定・次の行動

- 警告は `id`・重さ・領域・内容・根拠・必要な対応・止めるか、を持つ。同じ `id` は 1 つに
  まとめる。意図した設計 (在庫の保守を無効にしている・API の利用者が author) は `info` にする。
- 決定は `app/project_state/decisions.py`。根拠のファイルに決まった言い回しが今もあるかを
  確かめ、無ければ警告にする。
- 次の行動は優先度 → id の順。本番に書く行動・人の確認が要る行動はそう印を付ける。C10 は前提
  (T7・N0) が済むまで出さない。

## 秘密

最終防壁として `app/project_state/redaction.py` をかける (既存の
`app/operations/notifications.sanitize_payload` と `app/social/threads/errors.redact` を重ね、
URL / DSN の `user:password@`・Bearer / Basic・Meta / Threads の token らしい値・`/go/` の URL を
伏せる)。SHA-256 などの digest は根拠として残す。

## 決定の記録 (`docs/decision-log/`)

- 1 週 1 ファイル (`2026-W39.md` のように ISO 週、JST)。
- 各項目は `<!-- decision:<id> -->` の印を持つ。**すべての週のファイル** にその印があれば
  書かない (同じ決定を 2 度書かない)。報告を作るたびに、根拠のある決定のうちまだ無いものだけを
  今の週のファイルに足す。
- コマンドの記録ではない。長く効く決定と出来事だけ。

## フェーズの宣言 (`docs/project-roadmap.json`)

フェーズが変わったらこのファイルを更新する (歴史を書き換えない)。`status` は `complete` /
`active` / `planned` / `deferred`。根拠 (`evidence.files` / `evidence.commits`) を付ける。

- `current_phase`: 進めているフェーズ (`active`)。進めているものが無ければ `null`。
- `next_phase`: 次に始めるフェーズ (`planned`。**始めたとは言わない**)。前提がすべて `complete`。
- `last_completed_phase`: 最後に完了したフェーズ。

T7C の後: `current_phase = null`、`last_completed_phase = T7`、`next_phase = N0`。N0・N1・N2・
N3・C10 はリポジトリに作業が無いので `declared_only` のまま。

## 出どころの強さ (T7B: source precedence)

同じ事実について出どころが違うことを言うとき、上ほど強い (`app/project_state/precedence.py`):

| # | 強さ | 例 |
| --- | --- | --- |
| 1 | `live_observed` | WordPress の REST の GET・DB の revision・Get-ScheduledTask |
| 2 | `runtime_config` | 本番のコードが実際に読む設定 (`threads_operations_policy.json`・launcher の flag) |
| 3 | `runtime_record` | システムが書いた記録 (DB の行・worker の起動のログ・配備の記録) |
| 4 | `committed_manifest` | コミットした機械可読の manifest・計画・`docs/project-roadmap.json` |
| 5 | `operations_doc` | 運用のドキュメント |
| 6 | `historical_note` | 過去の記録の文章 |
| 7 | `prompt_expectation` | 指示や記憶にある想定 (事実としては使わない) |

観測した本番の状態と実行の設定は、古い文章より強い。領域によっては、もっとふさわしい出どころを
使う (`AUTHORITY_BY_FACT`): 自動公開の有効 / 無効は policy の値 (`runtime_config`)、worker が
実際に公開できるかはロックを持つ pid の起動の記録 (`runtime_record`)、中継の配備は README の
配備の記録 (hash と読み取りの確認付き)。

ドキュメントの食い違いを理由に本番を変えない。食い違いは直すべきもの (古いドキュメント) か、
意図した違いか、人が確かめるものかに分けて出すだけで、policy・worker・スケジューラ・DB・
WordPress には触れない。

## 事実 (`facts`)

重要な値は `value`・`authority` (上の強さ)・`authority_rank`・`source`・`observed_at`・
`confidence` (`high` / `medium` / `low`)・`conflicts` (同じ項目について弱い出どころが違うことを
言っている drift の要約) を持つ。current_phase・git・DB の revision・featured image・カテゴリ・
Make の tracking・自動公開・worker が公開できるか・worker の profile・在庫の保守・中継の配備・
スケジュールのタスク・毎日の運用の状態。

Make の tracking は article 1・10・11 (DB の有効な target と mapping が根拠。article 1 は
Make が主のプログラムで、HubSpot などは副)。違えば `live_drift` として出す (tracking の値は
作らない・写さない)。

## 食い違い (`drift`)

各 finding は `id`・`area`・`field`・`authoritative_value`・`conflicting_value`・
`authoritative_source`・`conflicting_source`・`classification`・`severity`・`blocking`・
`recommended_resolution` を持つ (`app/project_state/drift.py`・`docs_health.py`)。

| 分類 | 意味 | 対応 |
| --- | --- | --- |
| `stale_doc` | ドキュメント (または設定の中の説明の文) が今の状態を述べているが、強い出どころと違う | ドキュメントを直す (本番は変えない) |
| `stale_runtime_record` | 記録が古い | 新しい記録で置き換わるのを待つ |
| `live_drift` | 本番の状態が、あるべき状態 (計画・決定) と違う | 人が調べる |
| `configuration_drift` | 実行の設定が、決定や契約と違う | 人が決める |
| `unresolved` | どちらが正しいか決められない | 人が確かめる |
| `expected_difference` | 違って見えるが意図どおり | 何もしない (説明付きの `info`) |

重さ: `critical` (本番の安全の約束が破れた・書ける設定が想定外に変わった) / `high` (守るべき
運用の規則と違う) / `medium` (劣化・気にかけるべき古さ) / `low` (観測の欠け・急がない片付け) /
`info` (意図した状態)。`blocking` は重さと別に決める (例: 公開してはいけないのに worker が
公開できる = `critical` + blocking。WordPress が manifest / 計画と違う = `high` + blocking)。

T7B で見つけて直したもの (`<!-- state-corrected: ... -->` の印を残した):

| 食い違い | 分類 | 対応 |
| --- | --- | --- |
| `threads-autopublish.md` が「本番では無効」 / policy `enabled=true` (`7aa8990`)・worker の起動の記録 `can_publish=True` | `stale_doc` | ドキュメントだけ直した。policy・worker・公開は変えない |
| 中継の README の見出しが NOT DEPLOYED / 同じファイルに T6.1 の配備の記録 | `stale_doc` | 見出しだけ直した。配備の記録 (hash・確認) はそのまま。再配備も中継への問い合わせもしない |
| `threads-proposal-stock.md` が「本番 DB は `33d93394f342` のまま」 / DB は `afc2f36bb3ca` (head) | `stale_doc` | ドキュメントだけ直した。migration はしない |
| worker のロックの表示 `mode=plan` / `publish` プロファイルで自動公開 | `expected_difference` (正しいが紛らわしい表示) | `threads-worker.md` に意味を書いた。worker の再起動・ロックの書き換えはしない |

T7B で残した `threads_operations_policy.json` の `automatic_publication.note` ("Committed
disabled…") は、T7C で直した。先に、説明の文が動作を決めないことを確かめた: この note を読む
コードは報告の `docs_health.py` だけで、公開の判断は `automatic_publication.enabled` (と
`preflight_read`) を `ThreadsOperationsPolicy` 経由で読む。note の文で分かれる処理は無く、policy
の file の hash を取るコードも無い。直したのは note の文だけで、動作を決める値 (enabled・窓・
間隔・在庫など) は変えていない (テストで、note を除いた policy が同じであることを確かめる)。
実行の設定の中の説明の文は、動作を決める値と食い違ってはいけない。食い違えば
`config-note-threads-autopublish` (`stale_doc`) として出す。

## 不変条件 (`invariants`)

`app/project_state/invariants.py`。結果は `pass` / `fail` / `unknown` (読めなかった)。

| 強さ | 例 | 失敗したとき |
| --- | --- | --- |
| `hard` | DB が head・公開の窓 07:00–23:00・承認メールの窓 08:00–21:00・間隔 120 分・1 回に 1 本 (`MAX_PUBLICATIONS_PER_CYCLE = 1`)・スケジュールの契約 (daily: 月〜土 06:30 `run_operations_task.cmd daily`・weekly: 日 07:30 `weekly`・worker: `publish`・15 分ごと (`PT15M`)・すべて IgnoreNew) | 警告。`critical` なら止める |
| `expected_state` | 自動公開 ON・在庫の保守 OFF・worker が動いている・featured image 25/25・W2 のカテゴリ・中継の配備 | 警告 (理由を確かめる) |
| `advisory` | 1 日 3〜5 本 (目安であって割り当てでも上限でもない)・push 済み・診断が新しい | 報告に出すだけ。警告にも `--strict` の失敗にもしない |

## 時間で決まる状態 (`timing`)

`app/project_state/timing.py`。時刻は報告を作った時刻 (テストでは固定)。**待たない**: 次の
実行の時刻まで眠らず、その時点で一番新しい記録を使う。

- 毎日の運用: `latest_success` / `latest_partial` / `latest_failed` / `running`。最後の実行が
  うまくいかなかったとき、次の予定の実行 (`next_expected_run`) + 45 分までは `not_due`、
  それを過ぎても新しい記録が無ければ `due`、6 時間を過ぎれば `overdue`。新しい成功の実行が
  あれば、前の部分的な実行は `superseded_non_success_run_ids` に入り、警告にならない。
  置き換わるまでは次の行動 (`confirm-next-daily-run`、`due: false` と `due_at`) として残る。
  alert が解決済みなら「未解決」とは言わない。
- 週の報告: `not_scheduled` / `not_yet_due` (タスクがまだ 1 度も予定の時刻になっていない) /
  `ok` / `missing` (予定の時刻に動いたのに記録が無い)。
- Threads の成績の診断: `not_due` / `due` (新しく 6h を過ぎた公開が 3 本以上、または報告が
  7 日より古い) / `overdue` (6 本以上、または古くて新しい公開もある)。`not_due` の間は
  再実行を勧めない。

## 次の行動の規則 (T7B / T7C)

優先度 (P0〜P3) の中は決まった順 (`findings.ACTION_RANK`) で並べる:

1. 止めるべき食い違い (P0) / 進行中の劣化・時刻の来た確認 (P1: 未解決の alert・worker が
   止まっている・時刻の過ぎた daily の確認) / まだ時刻の来ていない daily の確認 (P2、`due: false`)
2. 実物の携帯表示の確認 (まだなら)
3. アフィリエイトの tracking の用意
4. Threads の成績の診断 (`due` / `overdue` のときだけ)
5. 在庫の運用の決定 (実物の携帯表示を確かめた後だけ)
6. N0 (roadmap の `next_phase`)
7. N1 / N2 / N3
8. C10 (前提の後だけ)

時刻の来た本番の健康の確認より N0 を上に出さない。解決済みの alert、意図した状態 (在庫の保守
OFF・author の権限・media 99) を「直す」行動は出さない。

## `--strict` の契約

`app/project_state/strict.py`。0 以外で終わるのは次のときだけ:

1. 止めるべき (`blocking`) 食い違い・警告がある
2. `hard` / `expected_state` の不変条件が `critical` / `high` で破れている
3. 必要な強い出どころが読めない (project・git・database・threads・analytics。live のときは
   wordpress・scheduler も)
4. 報告の形が契約と違う (上の階層のキー・finding / fact の項目・分類・重さ・結果の値)

media 99・在庫の保守 OFF・git の未 push・携帯の表示の確認待ち・直したドキュメント・まだ
時刻の来ていない確認・advisory の目安では失敗しない。

## 報告の比較 (`--compare`)

`--compare <older.json>` は前の報告と比べ、`reports/project_state_diff_latest.json` / `.md` に
書く (`app/project_state/compare.py`)。作った時刻・観測の時刻・heartbeat の経過秒・スケジューラの
前回 / 次回の時刻などの毎回変わる値は比べない。事実の値・食い違い・不変条件の結果・警告・
次の行動・時間で決まる状態の変化と、そのほかの値の変化 (パス付き) を出す。

## ドキュメントの健康 (`documentation_health`)

今の状態を述べる文だけを確かめる (`docs_health.py` の一覧)。過去の出来事の記録は古くない。
`stale_documents` (直すべきもの)・`corrected_documents` (`<!-- state-corrected: ... -->` の印の
一覧)・`unresolved_mismatches` (実行の設定の中の説明の文など、T7 が直さないもの)。

## 報告を作る時機 (T7C: on-demand)

プロジェクトの状態の報告は必要なときに作る (on-demand)。**自動のスケジュールにはまだ載せない**
(意図した決定であって、足りない機能の警告ではない。報告の `project.generation_policy` に
`mode: on_demand`・`scheduled: false` と出るだけで、警告にはしない)。

理由:

- 今の generator は、必要なときに動かせば十分に信頼できる。
- 派生の報告を新しくするためだけに、C8 / スケジューラの構成を変えない。
- 具体的な運用の必要が出たときに、スケジュールを考え直す。

しないこと: Windows のスケジュールされたタスクを作る・C8 の daily / weekly のタスクを変える・
generator を既存のタスクにつなぐ・常駐の worker を再起動する。

フェーズの始めと終わりに `uv run python scripts/generate_project_state.py --strict` を実行し、
前の報告と `--compare` で比べる。

## T7 の完了の条件 (T7C)

T7 を完了とするのは次がすべて成り立つときだけ (テスト: `tests/unit/test_project_state*.py`):

- live の読み取りだけのモードで動く / offline のモードで動く (WordPress を 1 度も呼ばない)
- JSON と Markdown が同じ事実を述べる
- 強い事実が出どころ (provenance) を持つ
- 食い違いの検出が動いている
- `--strict` の契約がテストされている / 秘密の伏せ字がテストされている
- 報告の比較が動く
- 週ごとの決定の記録が動き、重複しない
- roadmap が機械で読める
- 次の行動の順が決まっている
- 報告を信頼できなくするような未解決の食い違いが無い

運用の警告は T7 の完了を止めない: git の未 push・最後の daily の実行が partial・実物の携帯表示の
確認待ち・アフィリエイトのプログラムの欠け・在庫の保守 OFF (意図)・media 99 (任意の片付け)・
診断がまだ due でない・worker のロックの `mode=plan` (意図した違い)。
