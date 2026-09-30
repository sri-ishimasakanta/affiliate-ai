"""商品の人の記録 (``products/<id>/records.json``。追記だけ。N4 / N5)。

- ``quality_review`` (N4): 人が品質の確認表を見た。検査に誤りが無い内容の hash に結びつく。
- ``content_approval`` (N4): 人がこの内容をリリースしてよいと承認した。同じ hash の品質の
  確認が要る。
  **販売・公開の承認ではない** (配布は別の流れで、人の確認点)。
- ``release_approval`` (N5): 人がリリース候補 (manifest の hash) を承認した。同じ内容の承認と、
  同じ manifest のリリース候補が要る。これも配布ではない。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.products.spec import Product, ProductError

SCHEMA = "product-records/1"
TYPES = ("quality_review", "content_approval", "release_approval")
CHECKLIST = ("target_user_is_clear", "useful_without_this_site", "claims_match_sources",
             "no_internal_or_personal_data", "no_unobserved_results")  # fmt: skip


def records_path(product: Product) -> Path:
    return product.path / "records.json"


def load_records(product: Product) -> list[dict]:
    path = records_path(product)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ProductError(f"{path} is not {SCHEMA}")
    return list(data.get("records") or [])


def _append(product: Product, record: dict) -> dict:
    records = [*load_records(product), record]
    records_path(product).write_text(
        json.dumps({"schema": SCHEMA, "records": records}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")  # fmt: skip
    return record


def latest(records: list[dict], kind: str, **match) -> dict | None:
    found = [r for r in records if r["type"] == kind
             and all(r.get(k) == v for k, v in match.items())]  # fmt: skip
    return found[-1] if found else None


def _who(by: str) -> str:
    by = (by or "").strip()
    if not by or "@" in by:
        raise ProductError("by is a short name (no email)")
    return by[:64]


def record_quality_review(product: Product, result: dict, *, by: str, now: datetime,
                          checklist: dict[str, bool], notes: str = "") -> dict:  # fmt: skip
    if result["errors"]:
        raise ProductError(f"the product has errors: {result['errors'][:3]}")
    missing = [k for k in CHECKLIST if checklist.get(k) is not True]
    if missing:
        raise ProductError(f"the reviewer must confirm every checklist item; missing {missing}")
    return _append(product, {"type": "quality_review", "by": _who(by),
                             "at": now.isoformat(timespec="seconds"),
                             "content_hash": product.content_hash, "version": product.version,
                             "checklist": {k: True for k in CHECKLIST},
                             "notes": notes.strip()[:1000]})  # fmt: skip


def record_content_approval(product: Product, result: dict, *, content_hash: str, by: str,
                            now: datetime) -> dict:  # fmt: skip
    if result["errors"]:
        raise ProductError(f"the product has errors: {result['errors'][:3]}")
    if content_hash != product.content_hash:
        raise ProductError("the approval is for a different content hash (the product changed)")
    if latest(load_records(product), "quality_review", content_hash=content_hash) is None:
        raise ProductError("no quality review for this content hash")
    return _append(product, {"type": "content_approval", "by": _who(by),
                             "at": now.isoformat(timespec="seconds"),
                             "content_hash": content_hash, "version": product.version,
                             "meaning": "ready for release; not a sale or a publication"})


def record_release_approval(product: Product, candidate: dict, *, manifest_hash: str, by: str,
                            now: datetime) -> dict:  # fmt: skip
    manifest = candidate["manifest"]
    if manifest_hash != candidate["manifest_hash"]:
        raise ProductError("the approval is for a different manifest hash")
    if candidate.get("validation_errors"):
        raise ProductError(f"the release candidate failed validation: "
                           f"{candidate['validation_errors'][:3]}")  # fmt: skip
    if manifest["content_hash"] != product.content_hash:
        raise ProductError("the release candidate was built from different content; rebuild")
    records = load_records(product)
    if latest(records, "content_approval", content_hash=manifest["content_hash"]) is None:
        raise ProductError("no content approval for this content hash (N4 comes first)")
    if latest(records, "release_approval", version=manifest["version"]) is not None:
        raise ProductError(f"version {manifest['version']} is already released; bump the version")
    return _append(product, {"type": "release_approval", "by": _who(by),
                             "at": now.isoformat(timespec="seconds"),
                             "version": manifest["version"], "manifest_hash": manifest_hash,
                             "package_sha256": candidate["package_sha256"],
                             "content_hash": manifest["content_hash"], "manifest": manifest,
                             "meaning": "release candidate approved; distribution is a "
                                        "separate human decision"})  # fmt: skip


__all__ = ["CHECKLIST", "SCHEMA", "TYPES", "latest", "load_records", "record_content_approval",
           "record_quality_review", "record_release_approval", "records_path"]
