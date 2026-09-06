"""Affiliate destination の independent な exact-host allowlist policy (pure)。

- :attr:`AffiliateProgram.tracking_url` は **自分の host を自動承認しない**。
  承認は Human が review した provider→host の対応表だけが与える。
- production の :data:`DEFAULT_DESTINATION_HOST_POLICY` は **空** (fail closed)。
  実 ASP host は Human evidence があるまで足さない。
- provider が未知 / 承認集合が空 の場合は必ず ``False``。
- 判定は正規化済みホスト名の **完全一致**。``endswith`` / 部分一致 / prefix は使わない。
- テストは deterministic な policy mapping を注入できる。
"""

from __future__ import annotations

from collections.abc import Mapping

# provider -> Human 承認済みの正規化 exact host 集合。
# D-B1 時点では実 host を入れない (fail closed)。
DEFAULT_DESTINATION_HOST_POLICY: Mapping[str, frozenset[str]] = {}


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
