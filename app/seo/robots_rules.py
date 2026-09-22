"""robots.txt の最小パーサ (C5.1)。

目的はただ 1 つ: 「この URL path は ``User-agent: *`` 系のグループで crawl を
禁止されていないか」を判定すること。robots.txt を書き換える手段は持たない。

サポートする範囲 (Google の実装に合わせた必要最小限):

- ``User-agent`` グループ (``*`` と ``Googlebot`` を対象にする)
- ``Disallow`` / ``Allow``
- ``Sitemap`` (グループ非依存の宣言として収集する)
- ``*`` ワイルドカードと ``$`` 終端アンカー
- 最長一致が優先され、同長なら ``Allow`` が勝つ (Google の規則)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DEFAULT_AGENTS = ("*", "googlebot")


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str

    @property
    def length(self) -> int:
        return len(self.pattern)


@dataclass
class RobotsTxt:
    rules: list[RobotsRule] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    raw: str = ""

    def is_allowed(self, path: str) -> bool:
        """``path`` (先頭 ``/``) が crawl 許可されているか。規則が無ければ許可。"""

        best: RobotsRule | None = None
        for rule in self.rules:
            if not _matches(rule.pattern, path):
                continue
            if (
                best is None
                or rule.length > best.length
                or (rule.length == best.length and rule.allow and not best.allow)
            ):
                best = rule
        return True if best is None else best.allow

    def matching_rules(self, path: str) -> list[RobotsRule]:
        return [r for r in self.rules if _matches(r.pattern, path)]


def _matches(pattern: str, path: str) -> bool:
    if pattern == "":
        return False
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.match(regex + ("$" if anchored_end else ""), path) is not None


def parse_robots_txt(text: str, *, agents: tuple[str, ...] = _DEFAULT_AGENTS) -> RobotsTxt:
    """``agents`` のいずれかに適用されるグループの規則だけを取り出す。"""

    result = RobotsTxt(raw=text or "")
    current_agents: list[str] = []
    group_open = False
    for raw_line in (text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()
        if field_name == "sitemap":
            if value:
                result.sitemaps.append(value)
            continue
        if field_name == "user-agent":
            if group_open:
                current_agents = []
                group_open = False
            current_agents.append(value.lower())
            continue
        if field_name in ("disallow", "allow") and current_agents:
            group_open = True
            if not any(a in current_agents for a in agents):
                continue
            # 空の Disallow は「全て許可」を意味する (規則としては無視する)。
            if field_name == "disallow" and value == "":
                continue
            result.rules.append(RobotsRule(allow=field_name == "allow", pattern=value))
    return result
