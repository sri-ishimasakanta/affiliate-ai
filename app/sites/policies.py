"""サイトごとの方針のファイル (N6 hardening)。**プロファイル無しの本番の動きは変えない。**

アフィリエイト・キーワード・Threads の方針のファイルは、今は ``app/config/`` の決まった場所から
読む (本番はそのまま)。各読み込みの関数はもともと場所を受け取れるので、プロファイルの
``policies`` にサイト自身のファイルを書けば、そのサイトではそちらを使う。書かなければ本番の
ファイルを継ぐ (``inherited``)。ここは「どのファイルを使うか」を決めて、実際の読み込みの関数で
読めることを確かめるだけ (外に問い合わせない)。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _loaders() -> dict[str, tuple[Path, Callable[[Path], object]]]:
    from app.affiliate.catalog_hygiene import load_hygiene_config
    from app.article.keyword_expansion import parse_expansion_rules
    from app.keyword.affiliate_fit import load_fit_config
    from app.keyword.affiliate_tiers import load_tier_config
    from app.keyword.idea_seeds import load_idea_seed_config
    from app.social.threads.growth_strategy import load_facts
    from app.social.threads.policy import load_policy

    def rules(path: Path):
        return parse_expansion_rules(json.loads(path.read_text(encoding="utf-8")))

    return {
        "affiliate_match_fit": (CONFIG / "affiliate_match_fit.json", load_fit_config),
        "affiliate_match_tiers": (CONFIG / "affiliate_match_tiers.json", load_tier_config),
        "affiliate_catalog_hygiene": (CONFIG / "affiliate_catalog_hygiene.json",
                                      load_hygiene_config),
        "keyword_idea_seeds": (CONFIG / "keyword_idea_seeds_v2.json", load_idea_seed_config),
        "keyword_expansion_rules": (CONFIG / "keyword_expansion_rules.json", rules),
        "threads_style_policy": (CONFIG / "threads_style_policy.json", load_policy),
        "threads_growth_facts": (CONFIG / "threads_growth_facts.json", load_facts),
    }  # fmt: skip


def policy_names() -> tuple[str, ...]:
    return tuple(_loaders())


def resolve(overrides: dict[str, Path] | None) -> dict[str, dict]:
    """方針の名前 → 使うファイルと出どころ (``profile`` / ``inherited``)。"""

    overrides = overrides or {}
    unknown = sorted(set(overrides) - set(_loaders()))
    if unknown:
        raise ValueError(f"unknown site policies {unknown}; known: {sorted(_loaders())}")
    return {name: {"path": overrides.get(name, default),
                   "source": "profile" if name in overrides else "inherited"}
            for name, (default, _loader) in _loaders().items()}  # fmt: skip


def load_all(overrides: dict[str, Path] | None) -> dict[str, dict]:
    """全部を実際の読み込みの関数で読む。読めなければその名前に ``error`` を付ける。"""

    loaders = _loaders()
    out = {}
    for name, item in resolve(overrides).items():
        try:
            loaders[name][1](Path(item["path"]))
            out[name] = {**item, "path": str(item["path"]), "ok": True}
        except Exception as exc:  # noqa: BLE001 - どのファイルが壊れているかを返す
            out[name] = {**item, "path": str(item["path"]), "ok": False,
                         "error": f"{type(exc).__name__}: {exc}"[:200]}  # fmt: skip
    return out


__all__ = ["load_all", "policy_names", "resolve"]
