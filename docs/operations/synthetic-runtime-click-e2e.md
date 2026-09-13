# Synthetic runtime click E2E — operational record

- **Phase**: Phase 3C-5F-D-C3-C (design: C0/C0.1/C0.2、実装: C1、実行: C2〜C5)
- **Completion date**: 2026-09-13
- **Status**: CLOSED — 恒久的な監査証跡として保持する (削除しない)

## 目的

production WordPress の `/go` raw click 取り込みパス全体を、**実
`AffiliateLinkTarget` を一切作らずに** end-to-end で検証する:

```
synthetic projection (active, v1)
  -> GET /go/{synthetic token}  (302)
  -> WordPress bfl_outbound_clicks に 1 row 追加
  -> signed click-export GET (scripts.import_affiliate_outbound_clicks --execute)
  -> affiliate-ai AffiliateOutboundClick に 1 row 取り込み (unresolved)
  -> synthetic projection を disable (v2) して runtime target を無効化
```

設計 (D-C3-C0 / C0.1 / C0.2) と実装 (D-C3-C1, commit `271c300`) は本リポジトリの
`app/affiliate/synthetic_probe.py` / `scripts/probe_synthetic_runtime_click.py`
を参照。本ファイルは **その live 実行結果の恒久記録** であり、コード変更は伴わない。

## 安全な識別子のみを記録する

**完全な synthetic token はこのファイルにも、他のいかなる Git 管理ファイルにも
記録しない。** 以下の安全な派生値のみを記録する。

| 項目 | 値 |
| --- | --- |
| token_fingerprint (SHA-256) | `dc207140d0a49fb28cd432cfd2784745806918a8e175aacb7ba6aec1c9ae4a45` |
| masked prefix | `Ldvy…` |
| link_identity_hash | `bf172399f19eb5912193cb20b1a9cfac754b5101e0a6c18f7211dce9138e4d94` |
| destination_url | `https://example.com/` |
| destination_host | `example.com` |
| active_projection_entry_hash | `a7ef22b3240bbd93778e4e8564ee1be0efbd673adc21226534c331a2de1e74bf` |
| active_projection_snapshot_hash | `e12244b80fe02e6df5608ce46e93daf01782bb8c0732580f2d5613497e210d63` |
| disabled_projection_entry_hash | `c2fbb73a7576248f65b878f7966c339da8023a0943aed77f13e2461691a88a64` |
| disabled_projection_snapshot_hash | `b6068272ef1b51e89385da86bf8d45d362b2ce3f116c11960476655215d9abad` |
| activated_at | `2026-09-13T14:21:28+00:00` |
| disabled_at | `2026-09-13T14:40:01+00:00` |
| source_click_id (WordPress raw `bfl_outbound_clicks.id`) | `1` |
| import_run_id (`AffiliateClickImportRun.id`) | `2` |

完全な token は WordPress runtime/raw テーブル、affiliate-ai の imported replica、
および Human が別途管理する外部 probe-state file にのみ存在する。そのファイルの
実ファイルパスは本リポジトリのいかなる場所にも記録しない。

## 最終 live state (Human 確認済み)

**WordPress runtime target** (`wp_bfl_affiliate_targets`):

- rows = 1
- status = `disabled`
- projection_version = `2`
- destination_url / destination_host は上表の通り

**WordPress raw click** (`wp_bfl_outbound_clicks`): rows = `1`

**Local probe state**: `state = disabled_confirmed`、`click_contaminated = false`

## Formal import result (`AffiliateClickImportRun #2`)

| フィールド | 値 |
| --- | --- |
| run_id | 2 |
| status | succeeded |
| http_status | 200 |
| requested_since_id | 0 |
| response_count | 1 |
| inserted_count | 1 |
| duplicate_count | 0 |
| unresolved_token_count | 1 |
| next_since_id (`response_next_since_id`) | 1 |
| has_more | False |

affiliate-ai 側の imported replica (`affiliate_outbound_clicks`): この synthetic
probe に帰属する行数 = **1** (`source_click_id = 1`, `source_import_run_id = 2`)。

## 取り込まれた synthetic click は「意図的に unresolved」である

この synthetic click は **local に該当 `AffiliateLinkTarget` が存在しないため
意図的に unresolved であり、同一 token の `AffiliateLinkTarget` が存在しない間、
business attribution から構造的に除外される。**

「unresolved forever (永久に unresolved)」とは表現しない — 将来同一 token の
`AffiliateLinkTarget` が (理論上、無視できるほど低い確率で) 作られない限り、という
条件付きの状態である。

## Business KPI exclusion rule (必須・恒久ルール)

以下の business KPI:

- Article outbound clicks
- AffiliateProgram outbound clicks
- CTR / CVR
- affiliate revenue funnel

は必ず

```
AffiliateOutboundClick INNER JOIN AffiliateLinkTarget ON token = token
```

を経由して計算しなければならない。この synthetic unresolved click は
(同一 token の `AffiliateLinkTarget` が存在しない限り) この inner join に
一切現れないため、上記のいかなる business KPI にも寄与しない。

raw / data-quality 指標 (`affiliate_outbound_clicks` の総行数、
`AffiliateClickImportRun.unresolved_token_count` など) は、この synthetic click を
**引き続きカウントしてよい** — これは business attribution とは別の指標である。

## 永久保持ポリシー (削除しない)

以下は本 E2E の恒久的な監査証跡として **意図的に保持** する。クリーンアップ目的で
削除しないこと:

- WordPress runtime synthetic target row (`disabled` / `projection_version=2`)
- WordPress raw click 1 row
- affiliate-ai `AffiliateOutboundClick` replica 1 row (unresolved)
- affiliate-ai `AffiliateClickImportRun #2`
- 外部 Human probe-state file (ファイルパスは本リポジトリに記録しない)

`AffiliateLinkTarget` は作成していない (件数は 0 のまま)。Article / AffiliateProgram
の business データは一切変更していない。
