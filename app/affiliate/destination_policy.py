"""Affiliate destination の independent な exact-host allowlist policy (pure)。

- :attr:`AffiliateProgram.tracking_url` は **自分の host を自動承認しない**。
  承認は Human が review した provider→host の対応表だけが与える。
- production の :data:`DEFAULT_DESTINATION_HOST_POLICY` は明示リストのみで
  fail closed (D-F1)。Human evidence で個別に承認された provider/host の
  exact pair だけが列挙される -- 現在の承認済み pair は Make -> www.make.com
  の 1 件のみ。ここに無い provider/host は常に ``False``。
- provider が未知 / 承認集合が空 の場合は必ず ``False``。
- 判定は正規化済みホスト名の **完全一致**。``endswith`` / 部分一致 / prefix は使わない。
- テストは deterministic な policy mapping を注入できる。
"""

from __future__ import annotations

from collections.abc import Mapping

# provider -> Human 承認済みの正規化 exact host 集合。
# D-B1 時点では実 host を入れない (fail closed)。
#
# D-F1: Make (www.make.com) を最初の承認済み provider として追加する。
# 根拠: Human が実際に発行した Make アフィリエイトリンクの (無改変で凍結する)
# 実 URL を独立に検証した結果、scheme=https / host=www.make.com /
# query parameter に "pc" (affiliate code) を含む / fragment 無し であることを
# 確認済み。Make 公式アフィリエイトドキュメントも www.make.com のアフィリエイト
# URL (pc パラメータ) を使用し、短縮リンク経由の共有も明示的にサポートすると
# 案内している。実 affiliate code / full URL はこのファイルにも Git 全体にも
# 一切含めない (ここに書くのは承認済み host 文字列のみ)。
#
# provider key の大文字小文字: is_host_approved() は provider を正規化しない
# (呼び出し側が既に正規化済みの値を渡す契約、destination_policy の docstring
# 参照)。既存の AffiliateProgram.provider の実例 ("a8" / "moshimo") に倣い、
# 小文字の短い ASP/provider 識別子として "make" を採用する -- 将来
# AffiliateProgram.provider を作成する Human は、このキーと完全一致する
# "make" (小文字) を入力すること。
#
# host は exact match のみ (wildcard/suffix なし) -- make.com (www 無し) /
# サブドメイン / lookalike host は全て意図的に拒否対象のまま。
DEFAULT_DESTINATION_HOST_POLICY: Mapping[str, frozenset[str]] = {
    "make": frozenset({"www.make.com"}),
}


def is_host_approved(
    *,
    provider: str | None,
    destination_host: str,
    policy: Mapping[str, frozenset[str]] | None = None,
) -> bool:
    """正規化済み ``destination_host`` が ``provider`` の承認集合に完全一致するか。

    ``provider`` が falsy / policy に無い / 承認集合が空 のときは ``False`` (fail closed)。
    """

    if not provider or not destination_host:
        return False
    table = DEFAULT_DESTINATION_HOST_POLICY if policy is None else policy
    approved = table.get(provider)
    if not approved:
        return False
    return destination_host in approved
