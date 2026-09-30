"""商品の生成・検査・古さの検出 (N5 Digital Product Automation。手元だけ、配布しない)。

- 同じ入力からは同じバイト (再現できる生成): zip の項目は名前の順、時刻は 1980-01-01 に固定、
  権限は固定、本文は LF にそろえる。manifest に時刻を入れない。
- manifest (``product-release/1``): 版・内容の hash・資産ごとの hash・出どころごとの hash・
  生成の入力の hash。manifest の hash は人のリリースの承認に結びつく。
- 検査: 宣言したものだけが入っている・大きさの上限・伏せ字・変更の記録 (CHANGELOG) に版の節が
  ある・版は最後のリリースより新しい。
- 古さ: 最後にリリースした manifest と今の出どころ・資産を比べ、変わったものと上げ方の案を出す
  (版を上げるのは人)。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

from app.products import records as rec
from app.products.spec import Product, parse_version, redaction_findings

MANIFEST_SCHEMA = "product-release/1"
BUILDER = "product-build/1"
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _readme(product: Product) -> str:
    spec = product.spec
    lines = [f"# {spec['title']}", "", f"版: {spec['version']}", "",
             "## 誰のためのものか", "", spec["target_user"], "",
             "## 解決したいこと", "", spec["problem"], "",
             "## できるようになること", "", spec["intended_outcome"], "",
             "## 使える範囲", "", spec["reusable_scope"], "", "## 中身", ""]  # fmt: skip
    for asset in spec["assets"]:
        lines.append(f"- {asset['path']} ({asset['kind']}): {asset.get('title') or ''}".rstrip())
    return "\n".join(lines) + "\n"


def package_files(product: Product) -> dict[str, bytes]:
    """zip に入れるもの (名前 → LF の UTF-8)。サイト固有の資産は入れない。"""

    root = f"{product.id}-{product.version}"
    files = {f"{root}/README.md": _readme(product), f"{root}/CHANGELOG.md": product.changelog}
    for asset in product.spec["assets"]:
        if not asset.get("site_specific") and asset["path"] in product.assets:
            files[f"{root}/{asset['path']}"] = product.assets[asset["path"]]
    return {name: text.replace("\r\n", "\n").encode("utf-8") for name, text in files.items()}


def build_manifest(product: Product, source_hashes: dict[str, str],
                   files: dict[str, bytes]) -> dict:  # fmt: skip
    listed = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
              for name, data in sorted(files.items())]  # fmt: skip
    inputs = json.dumps({"content_hash": product.content_hash, "sources": source_hashes,
                         "files": listed, "builder": BUILDER}, sort_keys=True)  # fmt: skip
    return {"schema": MANIFEST_SCHEMA, "product_id": product.id, "version": product.version,
            "content_hash": product.content_hash, "builder": BUILDER, "files": listed,
            "sources": dict(sorted(source_hashes.items())),
            "build_input_hash": hashlib.sha256(inputs.encode("utf-8")).hexdigest()}


def manifest_hash(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()  # fmt: skip


def zip_bytes(files: dict[str, bytes], manifest: dict) -> bytes:
    buffer = io.BytesIO()
    entries = {**files, f"{product_root(manifest)}/MANIFEST.json":
               (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
               .encode("utf-8")}  # fmt: skip
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            zf.writestr(info, entries[name])
    return buffer.getvalue()


def product_root(manifest: dict) -> str:
    return f"{manifest['product_id']}-{manifest['version']}"


def released_versions(product: Product) -> list[dict]:
    return [r for r in rec.load_records(product) if r["type"] == "release_approval"]


def validate(product: Product, manifest: dict, package: bytes, *, policy: dict,
             terms: list[str]) -> list[str]:  # fmt: skip
    errors = []
    if len(package) > int(policy.get("max_package_bytes", 5_000_000)):
        errors.append(f"package is {len(package)} bytes (limit {policy['max_package_bytes']})")
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        names = sorted(zf.namelist())
        expected = sorted([f["path"] for f in manifest["files"]]
                          + [f"{product_root(manifest)}/MANIFEST.json"])  # fmt: skip
        if names != expected:
            diff = sorted(set(names) ^ set(expected))
            errors.append(f"package entries differ from the manifest: {diff}")
        suffixes = tuple(policy.get("text_suffixes") or (".md",))
        for name in names:
            if not name.endswith(suffixes):
                errors.append(f"{name}: only text files are packaged")
                continue
            text = zf.read(name).decode("utf-8")
            if name.endswith("MANIFEST.json"):
                continue
            errors += [f"{name}: {f}" for f in redaction_findings(text, terms)]
    version = manifest["version"]
    if not re.search(rf"^## {re.escape(version)}(\s|$)", product.changelog, flags=re.M):
        errors.append(f"CHANGELOG.md has no '## {version}' section")
    last = released_versions(product)
    if last:
        newest = max(parse_version(r["version"]) for r in last)
        if parse_version(version) <= newest:
            errors.append(f"version {version} is not newer than the last release "
                          f"{'.'.join(map(str, newest))}")  # fmt: skip
    return errors


def build_candidate(product: Product, source_hashes: dict[str, str], *, policy: dict,
                    terms: list[str]) -> dict:  # fmt: skip
    files = package_files(product)
    manifest = build_manifest(product, source_hashes, files)
    package = zip_bytes(files, manifest)
    return {"manifest": manifest, "manifest_hash": manifest_hash(manifest),
            "package": package, "package_sha256": hashlib.sha256(package).hexdigest(),
            "validation_errors": validate(product, manifest, package, policy=policy,
                                          terms=terms)}  # fmt: skip


def stale_plan(product: Product, source_hashes: dict[str, str]) -> dict:
    """最後のリリースと比べて、何が変わったか・どう上げるかの案 (上げるのは人)。"""

    released = released_versions(product)
    if not released:
        return {"last_release": None, "stale": False, "changed_sources": [],
                "changed_files": [], "content_changed": None,
                "suggested_bump": None, "note": "never released"}  # fmt: skip
    last = max(released, key=lambda r: parse_version(r["version"]))
    manifest = last["manifest"]
    changed_sources = sorted(k for k in set(manifest["sources"]) | set(source_hashes)
                             if manifest["sources"].get(k) != source_hashes.get(k))  # fmt: skip
    current = {f["path"].split("/", 1)[1]: f["sha256"] for f in
               build_manifest(product, source_hashes, package_files(product))["files"]}
    before = {f["path"].split("/", 1)[1]: f["sha256"] for f in manifest["files"]}
    changed_files = sorted(k for k in set(current) | set(before)
                           if current.get(k) != before.get(k))  # fmt: skip
    content_changed = product.content_hash != manifest["content_hash"]
    if set(before) - set(current) or set(current) - set(before):
        bump = "minor"
    elif content_changed:
        bump = "patch"
    else:
        bump = None
    return {"last_release": last["version"], "stale": bool(changed_sources),
            "changed_sources": changed_sources, "changed_files": changed_files,
            "content_changed": content_changed,
            "suggested_bump": bump if content_changed else ("review" if changed_sources else None),
            "note": ("a source changed since the last release: review the affected assets"
                     if changed_sources else "sources unchanged")}  # fmt: skip


__all__ = ["BUILDER", "MANIFEST_SCHEMA", "build_candidate", "build_manifest", "manifest_hash",
           "package_files", "released_versions", "stale_plan", "validate", "zip_bytes"]
