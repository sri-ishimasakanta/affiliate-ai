"""有料の note 記事の販売の条件と、その承認 (``note-approval/4``、2026-10-01)。

pure と手元の読み取りだけ (外に書かない)。

無料の記事は今までどおり ``note-approval/3``。有料 (``access_mode == "paid"``) の記事だけ、本文
(``content_hash``: 題名 + 無料 + 有料の本文。意味は今までと同じ) に加えて、**販売の条件** を
人の承認に結びつける:

- 有料の境界: 下書きの節 (``sections``) の順で、``paid_from_section`` 番目 (0 始まり) から有料。
  承認には位置と、境界の前後の見出しの並びが入る。同じ本文でも境界を動かせば別の条件。
- 価格: 金額 (正の整数) と通貨 (``JPY`` だけ)。0・負・小数・ほかの通貨は拒む。価格は product の
  版とは別の運用の値。価格だけを変えても本文の hash は変わらないが、承認は効かなくなる。
- 売る物: product の id・版・content hash・manifest hash・package sha256 (どれも完全な hash を
  手元の release candidate から読む。短い hash は受け付けない)。

``commercial_hash`` は、この販売の条件 (境界・価格・売る物・形) の正規の JSON の sha256。承認は
``content_hash`` と ``commercial_hash`` の両方に結びつき、どちらかが変われば承認は効かない
(取り消し → 出し直し → 承認し直し)。

最後の有料の承認は、売る物が **H5 で承認された版** (``products/<id>/records.json`` の
``release_approval`` が同じ版・manifest・package) であることを要る。H4 / H5 の記録は作らない。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from app.social.note.models import NoteDraft, NoteStatusError

APPROVAL_SCHEMA_PAID = "note-approval/4"
TERMS_SCHEMA = "note-paid-terms/1"
SUPPORTED_CURRENCIES = ("JPY",)
#: 無料の部分は空にしない (無料の記事を薄くしない方針。first-paid-product.md)。有料の部分も要る。
MIN_FREE_SECTIONS = 1
MIN_PAID_SECTIONS = 1
_PRODUCT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PRODUCT_KEYS = ("product_id", "version", "content_hash", "manifest_hash", "package_sha256")


def is_paid(draft: NoteDraft) -> bool:
    return draft.access_mode == "paid"


def check_price(amount, currency) -> dict:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise NoteStatusError("the price is a whole number (e.g. 500)")
    if amount <= 0:
        raise NoteStatusError("a paid article needs a price above 0 (0 is not a paid article)")
    if currency not in SUPPORTED_CURRENCIES:
        raise NoteStatusError(f"currency must be one of {SUPPORTED_CURRENCIES}")
    return {"amount": amount, "currency": currency}


def check_product(binding: dict) -> dict:
    if not isinstance(binding, dict) or set(binding) != set(PRODUCT_KEYS):
        raise NoteStatusError(f"the product binding needs exactly {PRODUCT_KEYS}")
    if not _PRODUCT_ID.match(str(binding["product_id"])):
        raise NoteStatusError(f"unsafe product id {binding['product_id']!r}")
    if not _SEMVER.match(str(binding["version"])):
        raise NoteStatusError(f"product version {binding['version']!r} is not semver")
    for key in ("content_hash", "manifest_hash", "package_sha256"):
        if not _SHA256.match(str(binding[key])):
            raise NoteStatusError(f"product {key} must be a full sha256 (64 hex)")
    return dict(binding)


def boundary(draft: NoteDraft) -> dict:
    """有料の境界 (節の位置と前後の見出し)。不正なら拒む。"""

    index = draft.paid_from_section
    count = len(draft.sections)
    if not isinstance(index, int) or isinstance(index, bool):
        raise NoteStatusError("a paid article needs a paid boundary (paid_from_section)")
    if index < MIN_FREE_SECTIONS:
        raise NoteStatusError("the free part is empty (at least one free section)")
    if count - index < MIN_PAID_SECTIONS:
        raise NoteStatusError("the paid part is empty (at least one paid section)")
    headings = [s["heading"] for s in draft.sections]
    return {"paid_from_section": index, "free_headings": headings[:index],
            "paid_headings": headings[index:]}  # fmt: skip


def terms(draft: NoteDraft) -> dict:
    """承認に入る販売の条件 (正規の形)。足りなければ拒む。"""

    if not is_paid(draft):
        raise NoteStatusError("only a paid article has commercial terms")
    if not draft.price:
        raise NoteStatusError("a paid article needs an approved price (price)")
    price = check_price(draft.price.get("amount"), draft.price.get("currency"))
    if not draft.product:
        raise NoteStatusError("a paid article needs the product it sells (product)")
    return {"schema": TERMS_SCHEMA, "access_mode": "paid", "boundary": boundary(draft),
            "price": price, "product": check_product(draft.product)}  # fmt: skip


def commercial_hash(draft: NoteDraft) -> str:
    payload = json.dumps(terms(draft), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def problems(draft: NoteDraft) -> list[str]:
    """有料の記事として足りないもの (無料の記事には何も言わない)。"""

    if not is_paid(draft):
        return []
    try:
        terms(draft)
    except NoteStatusError as exc:
        return [str(exc)]
    return []


def section_text(draft: NoteDraft, part: str) -> str:
    """``free`` / ``paid`` の部分の本文 (``## 見出し`` と段落)。"""

    index = draft.paid_from_section or 0
    sections = draft.sections[:index] if part == "free" else draft.sections[index:]
    out = []
    for section in sections:
        out += [f"## {section['heading']}", *section["paragraphs"]]
    return "\n\n".join(out)


# -- 売る物を手元の release candidate から読む ----------------------------------------------------
def _candidate_dir(root: Path, product_id: str, version: str) -> Path:
    return Path(root) / "reports" / "products" / product_id / version


def bind_product(root: Path | str, product_id: str) -> dict:
    """今の product の版の release candidate から、売る物の完全な hash を読む。

    product の content hash と、candidate の manifest の content hash・版が同じこと、
    zip のバイトが package sha256 と同じことを確かめる。
    """

    from app.products.spec import load_product

    if not _PRODUCT_ID.match(product_id or ""):
        raise NoteStatusError(f"unsafe product id {product_id!r}")
    product = load_product(Path(root), product_id)
    base = _candidate_dir(Path(root), product_id, product.version)
    candidate_path = base / "candidate.json"
    if not candidate_path.is_file():
        raise NoteStatusError(f"no release candidate for {product_id} {product.version} (build it)")
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    manifest = candidate.get("manifest") or {}
    if candidate.get("validation_errors"):
        raise NoteStatusError("the release candidate has validation errors")
    if (manifest.get("version"), manifest.get("content_hash")) != (product.version,
                                                                   product.content_hash):
        raise NoteStatusError("the release candidate is not the current product content "
                              "(rebuild it)")  # fmt: skip
    zip_path = base / f"{product_id}-{product.version}.zip"
    data = zip_path.read_bytes() if zip_path.is_file() else b""
    if hashlib.sha256(data).hexdigest() != candidate.get("package_sha256"):
        raise NoteStatusError("the package bytes do not match the release candidate")
    return check_product({"product_id": product_id, "version": product.version,
                          "content_hash": product.content_hash,
                          "manifest_hash": candidate["manifest_hash"],
                          "package_sha256": candidate["package_sha256"]})  # fmt: skip


def package_asset_lines(root: Path | str, binding: dict) -> dict[str, list[str]]:
    """売る物の package (zip) の資産の、見出しを除いた行。zip のバイトを先に確かめる。"""

    base = _candidate_dir(Path(root), binding["product_id"], binding["version"])
    data = (base / f"{binding['product_id']}-{binding['version']}.zip").read_bytes()
    if hashlib.sha256(data).hexdigest() != binding["package_sha256"]:
        raise NoteStatusError("the package bytes do not match the bound package sha256")
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in sorted(archive.namelist()):
            if "/assets/" in name:
                text = archive.read(name).decode("utf-8")
                out[name.split("/", 1)[1]] = [line.strip() for line in text.splitlines()
                                              if line.strip() and not line.startswith("#")]
    return out


def missing_asset_lines(draft: NoteDraft, root: Path | str) -> dict[str, int]:
    """有料の部分に入っていない、売る物の資産の行の数 (資産ごと)。0 ならすべて入っている。"""

    paid = section_text(draft, "paid")
    lines = package_asset_lines(root, check_product(draft.product))
    return {asset: sum(1 for line in rows if line not in paid) for asset, rows in lines.items()}


def h5_release(root: Path | str, binding: dict) -> dict | None:
    """売る物の版の H5 (release_approval) の記録。manifest と package が同じものだけ。"""

    path = Path(root) / "products" / binding["product_id"] / "records.json"
    if not path.is_file():
        return None
    records = json.loads(path.read_text(encoding="utf-8")).get("records") or []
    found = [r for r in records if r.get("type") == "release_approval"
             and r.get("version") == binding["version"]
             and r.get("manifest_hash") == binding["manifest_hash"]
             and r.get("package_sha256") == binding["package_sha256"]
             and r.get("content_hash") == binding["content_hash"]]  # fmt: skip
    return found[-1] if found else None


__all__ = ["APPROVAL_SCHEMA_PAID", "MIN_FREE_SECTIONS", "MIN_PAID_SECTIONS", "PRODUCT_KEYS",
           "SUPPORTED_CURRENCIES", "TERMS_SCHEMA", "bind_product", "boundary", "check_price",
           "check_product", "commercial_hash", "h5_release", "is_paid", "missing_asset_lines",
           "package_asset_lines", "problems", "section_text", "terms"]
