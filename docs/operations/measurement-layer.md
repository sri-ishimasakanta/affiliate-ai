# 計測レイヤ (GSC / GA4 / アフィリエイト) — 運用手順

- **Phase**: C5.2–C5.3
- **Status**: OPEN — 日次運用で使う
- **前提**: C5.1 のインデックス可能性監視 (`scripts/report_article_indexability.py`)

公開記事について、次の 7 つを **別々の事実として** answer するための取り込みと
レポートをまとめる。

| # | 問い | 出所 |
|---|------|------|
| 1 | Google はページを見ているか | live probe + sitemap + URL Inspection (C5.1) |
| 2 | どんなクエリで露出しているか | Search Console Search Analytics (C1.1) |
| 3 | どれだけ流入しているか | GA4 Data API (C5.2) |
| 4 | どれだけ読まれているか | GA4 engagement (C5.2) |
| 5 | アフィリエイト CTA は押されたか | first-party outbound click (D-B2 / E1) |
| 6 | 成果・報酬は分かっているか | Make commission (E1) |
| 7 | 何が分からないか | 各レポートが明示する |

**この 4 つは別概念であり、1 つのスコアに畳まない。** 検索実績の行が無いことは
「インデックスされていない」ことを意味せず、URL Inspection の verdict は流入を
意味しない。報酬をクリック数で按分することは決してしない。

## 独立に実行できるコマンド

いずれも単独で実行でき、相互に依存しない。取り込み系は `--execute` を付けたとき
だけ書き込む。

```bash
# 1. Search Console (C1.1)
uv run python scripts/import_search_console.py --days 30 --lag 1 --execute

# 2. GA4 (C5.2)  ※ GA4_PROPERTY_ID 未設定なら何もせず終了する
uv run python scripts/import_ga4.py --days 30 --lag 1 --execute

# 3. アフィリエイトの outbound click (D-B2 / E1)
uv run python scripts/import_affiliate_outbound_clicks.py --execute

# 4. Make の成果・報酬 (E1)
uv run python scripts/import_make_affiliate_commissions.py --execute

# 5. インデックス可能性 (C5.1、read-only)
uv run python scripts/report_article_indexability.py --json indexability.json

# 6. 記事別の計測ベースライン (C5.2/C5.3、read-only)
uv run python scripts/report_article_measurement.py --days 30 --json measurement.json
```

レポート 5 と 6 は読み取り専用で、DB にも WordPress にも Google にも書き込まない。
6 は取り込み済みの行を読むだけで、自分では取り込まない。

## 想定する実行間隔 (まだ自動化しない)

自動化は C8 の範囲。ここでは**意図した間隔だけ**を記録する。

| コマンド | 想定間隔 | 理由 |
|----------|----------|------|
| Search Console 取り込み | 日次 | `dataState=final` のため確定分のみ。lag 1〜3 日 |
| GA4 取り込み | 日次 | 当日データは未確定。lag 1 日 |
| outbound click 取り込み | 日次 | cursor ベースで増分取得 |
| Make commission 取り込み | 日次 | 成果の状態 (承認/却下) が後から変わる |
| インデックス可能性レポート | 週次 | URL Inspection はプロパティ単位で 1 日 2,000 件の上限 |
| 計測レポート | 日次または任意 | 取り込み済みデータを読むだけ |

再取り込みは安全:指標行はいずれも UPSERT (`(property, date, page[, query])` /
`(property, date, page_path, channel_scope)`) で、同じ期間を重ねて取り込んでも
行は重複しない。実行記録 (`*_import_runs`) は append-only で監査できる。

## 日付とタイムゾーンの扱い

| 出所 | 暦日の基準 |
|------|------------|
| Search Console | Pacific Time (`app/search_console/date_window.py`) |
| GA4 | **property のタイムゾーン**。`Ga4ImportRun.property_timezone` に凍結する |
| outbound click | UTC (WordPress runtime が `gmdate` で返す) |
| Make commission | provider のタイムスタンプをそのまま保持 |

GA4 の property タイムゾーンが未取得の間、`scripts/import_ga4.py` の
`--days/--lag` は UTC 暦日で計算し、その旨を出力する。

## 帰属 (attribution) の範囲

- **アフィリエイトクリック**: first-party なので記事に帰属できる。
  `AffiliateOutboundClick.token` → `AffiliateLinkTarget` で記事とプログラムが決まる。
- **成果・報酬**: Make の commission には click / 記事への join key が無い。
  よって記事別収益は作らず、`UNATTRIBUTED_TO_ARTICLE` としてプログラム単位で
  そのまま出す。按分も推定もしない。
- 将来 subid 相当のパラメータで記事別の帰属が可能になる場合でも、**稼働中の
  トラッキング URL を無断で変更しない**。アフィリエイトアカウント側の仕様を
  確認したうえで、別フェーズで扱う。

## 既知の無害な観測

- `report_article_measurement.py` は、`AffiliateLinkTarget` に存在しない token の
  クリックを `unattributed` として報告する。2026-09-13 の 1 件は D-C3-C の
  synthetic runtime click E2E プローブ (`docs/operations/synthetic-runtime-click-e2e.md`)
  によるもので、設計上ローカル target を作らない。欠陥ではない。
- カテゴリページや固定ページの Search Console / GA4 行は記事に紐付かないため
  「not mapped to an article」として報告される。記事ではないので正常。
