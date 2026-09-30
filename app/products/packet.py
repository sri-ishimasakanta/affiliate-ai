"""商品の確認用のまとめ (N4 / N5)。人が H4 (品質の確認・内容の承認) と H5 (リリースの承認) を
判断するための 1 枚。**承認はしない。** 書くのは手元 (``reports/products/``) だけ。
"""

from __future__ import annotations

from app.products import records as rec
from app.products.spec import Product

CHECKLIST_GUIDE = {
    "target_user_is_clear": "誰の・どんな場面の問題かが、最初の段落で分かるか",
    "useful_without_this_site": "このサイトを知らない人が、自分の仕組みに当てはめられるか",
    "claims_match_sources": "各資産の主張が、上の出どころの対応の範囲に収まっているか",
    "no_internal_or_personal_data": "内部の名前・パス・アカウント・個人の情報が無いか "
                                    "(伏せ字の結果も見る)",
    "no_unobserved_results": "収益・効果・事故が減った等、観測していない結果を書いていないか",
}


def render(product: Product, result: dict, candidate: dict, *, reproducible: bool | None) -> str:
    spec, records = product.spec, rec.load_records(product)
    manifest = candidate["manifest"]
    reviewed = rec.latest(records, "quality_review", content_hash=product.content_hash)
    approved = rec.latest(records, "content_approval", content_hash=product.content_hash)
    released = [r["version"] for r in records if r["type"] == "release_approval"]
    by_asset = {a["path"]: a for a in spec["assets"]}
    lines = [
        f"# Review packet: {spec['title']}",
        "",
        "<!-- 人の判断のための資料。承認はしていない。配布・販売・公開はしない。 -->",
        "",
        f"- product id: `{product.id}` · current version: **{spec['version']}**",
        f"- content hash (H4): `{product.content_hash}`",
        f"- manifest hash (H5): `{candidate['manifest_hash']}`",
        f"- package sha256: `{candidate['package_sha256']}`",
        f"- human records: quality review {'done' if reviewed else 'NOT done'} · content "
        f"approval {'done' if approved else 'NOT done'} · released versions "
        f"{released or 'none'}",
        "",
        "## Target user", "", spec["target_user"], "",
        "## Problem solved", "", spec["problem"], "",
        "## Intended outcome", "", spec["intended_outcome"], "",
        "## Product contents / included files", "",
        "| File | Kind | Title | Bytes | sha256 |", "|---|---|---|---|---|",
    ]  # fmt: skip
    for f in manifest["files"]:
        rel = f["path"].split("/", 1)[1]
        asset = by_asset.get(rel, {})
        lines.append(f"| `{rel}` | {asset.get('kind', 'generated')} | "
                     f"{asset.get('title', '(README / CHANGELOG)')} | {f['bytes']} | "
                     f"`{f['sha256'][:12]}` |")  # fmt: skip
    lines += ["", "## Evidence / source mapping", "",
              "| Asset | Source | Pinned phrase | Resolved |", "|---|---|---|---|"]
    for asset in spec["assets"]:
        for ref in asset.get("sources") or []:
            key = ref["ref"] + (f"#{ref['phrase']}" if ref.get("phrase") else "")
            lines.append(f"| `{asset['path']}` | `{ref['ref']}` | {ref.get('phrase') or '—'} | "
                         f"{'yes' if key in result['sources'] else '**NO**'} |")  # fmt: skip
    lines += ["", "## Reusable knowledge", "", spec["reusable_scope"], "",
              "## Excluded site-specific material", ""]  # fmt: skip
    lines += [f"- {x}" for x in spec.get("site_specific_excluded") or ["(none listed)"]]
    redaction = [e for e in result["errors"] if "secret" in e or "site-specific" in e]
    lines += ["", "## Redaction / security result", "",
              "- clean: no secret, internal value or site-specific term found in any asset"
              if not redaction else "", *[f"- **{e}**" for e in redaction],
              "- the built package was scanned again (every text file)"
              + (" — clean" if not candidate["validation_errors"] else
                 f" — {candidate['validation_errors']}"),
              "", "## Quality validation result", "",
              f"- check errors: {len(result['errors'])}" + (
                  "" if not result["errors"] else " — " + "; ".join(result["errors"])),
              f"- warnings: {len(result['warnings'])}" + (
                  "" if not result["warnings"] else " — " + "; ".join(result["warnings"])),
              f"- package validation errors: {len(candidate['validation_errors'])}",
              f"- reproducible build (rebuilt twice, same bytes): "
              f"{'yes' if reproducible else 'no' if reproducible is False else 'not checked'}",
              "", "## Known limitations", ""]  # fmt: skip
    lines += [f"- {x}" for x in spec.get("known_limitations") or ["(none listed)"]]
    lines += ["", "## Expected distribution format", "",
              f"- format: `{spec.get('format')}` — a zip of Markdown / JSON files "
              f"(README, CHANGELOG, assets, MANIFEST)",
              f"- license: {spec.get('license')}",
              "- channel, price and listing: **not decided** (distribution workstream; human)",
              "", "## H4 quality approval — what to check", ""]  # fmt: skip
    lines += [f"- [ ] `{k}`: {v}" for k, v in CHECKLIST_GUIDE.items()]
    lines += [f"- command after checking: `manage_products.py review {product.id} --by <name> "
              "--confirm-all --notes \"...\"`",
              "", "## H4 content approval — what to check", "",
              f"- [ ] the content above (hash `{product.content_hash[:12]}`) is the version you "
              "want to release; any later edit changes the hash and needs a new approval",
              "- [ ] title, target user, problem and outcome describe the product honestly",
              "- [ ] the known limitations are acceptable to state to a buyer",
              "- meaning: ready for release — **not** a sale, a listing or a publication",
              f"- command: `manage_products.py approve-content {product.id} --content-hash "
              f"{product.content_hash} --by <name>`",
              "", "## H5 release approval — what to check", "",
              f"- [ ] version `{spec['version']}` and its CHANGELOG entry are right",
              "- [ ] the file list above is exactly what should be delivered",
              f"- [ ] the release candidate is reproducible (`manage_products.py verify "
              f"{product.id}`) and matches manifest hash `{candidate['manifest_hash'][:12]}`",
              "- [ ] the H4 content approval exists for the same content hash",
              "- meaning: this exact package may be released — distribution (where / price / "
              "when) is still a separate human decision",
              f"- command: `manage_products.py approve-release {product.id} --manifest-hash "
              f"{candidate['manifest_hash']} --by <name>`", ""]  # fmt: skip
    return "\n".join(x for x in lines if x is not None) + "\n"


__all__ = ["CHECKLIST_GUIDE", "render"]
