"""商品の仕様と検査 (N4 Productized Knowledge。pure、ネットワークを使わない)。

商品は ``products/<id>/`` に置く (git で管理する):

- ``product.json`` (``product-spec/1``): 誰の・どんな問題を・どうできるようにするか、版 (semver)、
  資産 (template / checklist / guide / config_example) と、それぞれの **出どころ** (``doc:`` /
  ``decision:`` / ``phase:``)、再利用できる範囲とサイト固有として外したもの。
- ``assets/``: 読み手に見せる本文 (出どころの書き方は本文に出さない。仕様に書く)。
- ``CHANGELOG.md``: 版ごとの変更 (N5)。
- ``records.json``: 人の品質の確認・内容の承認・リリースの承認 (追記だけ)。

検査:

- 出どころ: 資産はすべて 1 つ以上の出どころを持ち、それが今もある (ドキュメントは言い回しも)。
  各出どころの hash は N5 の古さの検出に使う。
- 伏せ字: 秘密・内部の値 (note の検査と同じ) と、このサイトに固有の言葉 (方針 + 手元の設定の
  WordPress の host)。見つかれば誤り。
- 品質: 必須の項目・資産の長さ・見出し・書きかけの印 (TODO など)・記録の無い収益の主張・
  因果の言い切り・煽り・内部の言葉。
- 内容の hash: 仕様と資産の中身 (版・記録を除く) から。人の内容の承認はこの hash に結びつく。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.social.note import safety
from app.social.note.sources import SourceBundle

SCHEMA = "product-spec/1"
ASSET_KINDS = ("template", "checklist", "guide", "config_example")
PRODUCTS_DIR = Path("products")
POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "product_policy.json"
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_UNFINISHED = re.compile(r"\b(TODO|TBD|FIXME|XXX)\b|（仮）|要確認")


class ProductError(ValueError):
    pass


def load_policy(path: Path | None = None) -> dict:
    return json.loads((path or POLICY_PATH).read_text(encoding="utf-8"))


def parse_version(text: str) -> tuple[int, int, int]:
    match = _SEMVER.match(text or "")
    if not match:
        raise ProductError(f"version {text!r} is not semver (MAJOR.MINOR.PATCH)")
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


@dataclass
class Product:
    root: Path
    path: Path
    spec: dict
    assets: dict[str, str] = field(default_factory=dict)  # 資産の相対パス → 本文 (LF)

    @property
    def id(self) -> str:
        return self.spec["id"]

    @property
    def version(self) -> str:
        return self.spec["version"]

    @property
    def content_hash(self) -> str:
        """仕様 (版を除く) と資産の中身。承認はこの hash に結びつく。"""

        spec = {k: v for k, v in self.spec.items() if k != "version"}
        payload = json.dumps({"spec": spec, "assets": {p: sha256_text(t) for p, t in
                                                       sorted(self.assets.items())}},
                             ensure_ascii=False, sort_keys=True)  # fmt: skip
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def changelog(self) -> str:
        path = self.path / "CHANGELOG.md"
        return path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else ""


def load_product(root: Path, product_id: str) -> Product:
    path = root / PRODUCTS_DIR / product_id
    spec_path = path / "product.json"
    if not spec_path.exists():
        raise ProductError(f"no product {product_id} ({spec_path} is missing)")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assets = {}
    for asset in spec.get("assets") or []:
        rel = asset.get("path", "")
        file = (path / rel).resolve()
        if path.resolve() not in file.parents:
            raise ProductError(f"asset path {rel!r} leaves the product directory")
        if file.exists():
            assets[rel] = file.read_text(encoding="utf-8").replace("\r\n", "\n")
    return Product(root=root, path=path, spec=spec, assets=assets)


def list_products(root: Path) -> list[str]:
    base = root / PRODUCTS_DIR
    return sorted(p.parent.name for p in base.glob("*/product.json")) if base.is_dir() else []


# -- 出どころ ---------------------------------------------------------------------------------
def resolve_source(ref: dict, root: Path, sources: SourceBundle) -> tuple[str | None, str | None]:
    """出どころを確かめる。返り値: (hash, 問題)。hash は N5 の古さの検出に使う。"""

    text = ref.get("ref", "")
    kind, _, value = text.partition(":")
    if kind == "doc":
        path = root / value
        if not path.exists():
            return None, f"{text}: file is missing"
        content = path.read_text(encoding="utf-8")
        phrase = ref.get("phrase")
        if phrase and phrase not in content:
            return None, f"{text}: phrase {phrase!r} is no longer there"
        return sha256_text(content), None
    if kind == "decision":
        entry = sources.decisions.get(value)
        if not entry:
            return None, f"{text}: not in the decision log"
        return sha256_text(json.dumps(entry, ensure_ascii=False, sort_keys=True)), None
    if kind == "phase":
        phase = sources.phase(value)
        if not phase or phase.get("status") != "complete":
            return None, f"{text}: not a complete phase"
        return sha256_text(json.dumps(phase, ensure_ascii=False, sort_keys=True)), None
    return None, f"{text}: unknown source kind (doc: / decision: / phase:)"


def trace(product: Product, sources: SourceBundle) -> dict:
    hashes, problems = {}, []
    for asset in product.spec.get("assets") or []:
        refs = asset.get("sources") or []
        if not refs:
            problems.append(f"{asset.get('path')}: no source (every asset is evidence-backed)")
        for ref in refs:
            digest, problem = resolve_source(ref, product.root, sources)
            if problem:
                problems.append(f"{asset.get('path')}: {problem}")
            else:
                hashes[ref["ref"] + (f"#{ref['phrase']}" if ref.get("phrase") else "")] = digest
    return {"sources": dict(sorted(hashes.items())), "problems": problems}


# -- 伏せ字と品質 -----------------------------------------------------------------------------
def site_terms(policy: dict, *, settings=None) -> list[str]:
    terms = list(policy.get("site_specific_terms") or [])
    if settings is not None and getattr(settings, "wordpress_base_url", None):
        from urllib.parse import urlparse

        host = urlparse(settings.wordpress_base_url).hostname
        if host:
            terms.append(host)
    return sorted({t for t in terms if t})


def redaction_findings(text: str, terms: list[str]) -> list[str]:
    found = []
    _cleaned, kinds = safety.sanitize(text)
    found += [f"secret or internal value ({k})" for k in sorted(set(kinds))]
    lowered = text.lower()
    found += [f"site-specific term {t!r}" for t in terms if t.lower() in lowered]
    return found


def check(product: Product, sources: SourceBundle, *, policy: dict, settings=None) -> dict:
    """出どころ・伏せ字・品質をまとめて確かめる。``errors`` が空ならリリースの候補にできる。"""

    spec, errors, warnings = product.spec, [], []
    if spec.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if not _ID.match(spec.get("id", "")) or spec.get("id") != product.path.name:
        errors.append("id must be lowercase-hyphen and equal the directory name")
    try:
        parse_version(spec.get("version", ""))
    except ProductError as exc:
        errors.append(str(exc))
    for key in ("title", "target_user", "problem", "intended_outcome", "reusable_scope"):
        if not str(spec.get(key) or "").strip():
            errors.append(f"{key} is required")
    if not spec.get("site_specific_excluded"):
        warnings.append("site_specific_excluded is empty (say what was left out)")
    if spec.get("distribution"):
        errors.append("distribution is decided outside the product (distribution workstream)")
    terms = site_terms(policy, settings=settings)
    declared = {a.get("path") for a in spec.get("assets") or []}
    if not declared:
        errors.append("a product needs at least one asset")
    for asset in spec.get("assets") or []:
        rel = asset.get("path")
        if asset.get("kind") not in ASSET_KINDS:
            errors.append(f"{rel}: kind must be one of {ASSET_KINDS}")
        if asset.get("site_specific"):
            errors.append(f"{rel}: site-specific assets are not packaged (generalize or drop)")
        text = product.assets.get(rel)
        if text is None:
            errors.append(f"{rel}: file is missing")
            continue
        if len(text.strip()) < int(policy.get("min_asset_chars", 300)):
            errors.append(f"{rel}: too short to be useful on its own")
        if rel.endswith(".json"):
            try:
                json.loads(text)
            except ValueError as exc:
                errors.append(f"{rel}: not valid JSON ({exc})")
        elif not re.search(r"^#{1,3} ", text, flags=re.M):
            warnings.append(f"{rel}: no heading")
        if _UNFINISHED.search(text):
            errors.append(f"{rel}: unfinished markers (TODO / TBD / 要確認)")
        errors += [f"{rel}: {f}" for f in redaction_findings(text, terms)]
        body_errors, body_warnings = safety.check_body(text, commissions_known=False)
        errors += [f"{rel}: {e}" for e in body_errors]
        warnings += [f"{rel}: {w}" for w in body_warnings]
        _clean, internal = safety.reader_facing(text)
        if internal:
            warnings.append(f"{rel}: internal wording {internal}")
    on_disk = {p.relative_to(product.path).as_posix()
               for p in (product.path / "assets").rglob("*") if p.is_file()} \
        if (product.path / "assets").is_dir() else set()  # fmt: skip
    for extra in sorted(on_disk - declared):
        errors.append(f"{extra}: file is not declared in product.json")
    traced = trace(product, sources)
    errors += traced["problems"]
    return {"product_id": product.id, "version": spec.get("version"),
            "content_hash": product.content_hash, "errors": errors,
            "warnings": sorted(set(warnings)), "sources": traced["sources"],
            "ok": not errors}  # fmt: skip


__all__ = ["ASSET_KINDS", "PRODUCTS_DIR", "Product", "ProductError", "SCHEMA", "check",
           "list_products", "load_policy", "load_product", "parse_version", "redaction_findings",
           "resolve_source", "sha256_text", "site_terms", "trace"]
