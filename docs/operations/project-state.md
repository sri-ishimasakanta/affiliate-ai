# プロジェクトの状態の報告 (T7)

1 つのコマンドで、今のプロジェクトの状態 (フェーズ・git・品質・DB・WordPress・featured
image・カテゴリ・収益化・Threads・承認・C8・スケジューラ・警告・決定・次の行動) をまとめる。
**読むだけ**: WordPress・Threads・スケジューラ・DB には書かない。`/go/` と Threads の API には
問い合わせない。

```bash
uv run python scripts/generate_project_state.py                # 手元 + 安全な読み取り
uv run python scripts/generate_project_state.py --offline      # 外部 (WordPress) を読まない
uv run python scripts/generate_project_state.py --with-tests   # pytest もその場で実行して記録
uv run python scripts/generate_project_state.py --strict       # 止めるべき警告があれば終了コード 1
```

`--json-only` / `--markdown-only` / `--no-decision-log` もある。

## 出力

| ファイル | 中身 | git |
| --- | --- | --- |
| `reports/project_state_latest.json` | 機械が読む報告 (下の形) | 管理外 (毎回作り直す) |
| `reports/project_state_latest.md` | 人が読む報告 (同じ dict から作る) | 管理外 |
| `reports/quality_latest.json` | `--with-tests` のときの pytest の結果 | 管理外 |
| `docs/decision-log/YYYY-Www.md` | 長く効く決定 (週ごと、ISO 週、JST) | **追う** |

## 報告の形 (`project-state/1`)

上の階層: `generated_at`・`generator_version`・`mode` (`live` / `offline`)・`project`・`git`・
`quality`・`database`・`wordpress`・`featured_images`・`taxonomy`・`monetization`・`threads`・
`approvals`・`analytics`・`scheduler`・`warnings`・`decisions`・`known_issues`・`next_actions`・
`source_freshness`。

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
