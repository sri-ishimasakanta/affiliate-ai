"""自前の知識の商品 (N4 / N5)。**手元だけ。公開・出品・販売・アップロードはしない。**

    uv run python scripts/manage_products.py candidates          # 根拠を確かめた商品の候補
    uv run python scripts/manage_products.py check <id>          # 出どころ・伏せ字・品質
    uv run python scripts/manage_products.py review <id> --by <name> --confirm-all [--notes ...]
    uv run python scripts/manage_products.py approve-content <id> --content-hash <sha> --by <name>
    uv run python scripts/manage_products.py plan <id>           # 古さ・作り直しの案
    uv run python scripts/manage_products.py build <id>          # リリース候補 (reports/products/)
    uv run python scripts/manage_products.py verify <id>         # 作り直して同じバイトか
    uv run python scripts/manage_products.py approve-release <id> --manifest-hash <sha> --by <name>
    uv run python scripts/manage_products.py status <id>
    uv run python scripts/manage_products.py packet <id>         # H4 / H5 の確認用のまとめ

人の記録は ``products/<id>/records.json`` に足す (commit する)。リリースの候補の zip と manifest は
``reports/products/<id>/<version>/`` (git 管理外)。承認は「リリースしてよい」であって、販売・公開
ではない (配布は別の流れで、人が決める)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.products import build as pb  # noqa: E402
from app.products import records as rec  # noqa: E402
from app.products.candidates import discover  # noqa: E402
from app.products.spec import (  # noqa: E402
    ProductError,
    check,
    load_policy,
    load_product,
    site_terms,
)
from app.social.note.sources import load_sources  # noqa: E402

OUT = Path("reports/products")


def _settings():
    try:
        from app.config.settings import get_settings

        return get_settings()
    except Exception:  # noqa: BLE001 - 設定が読めなくても方針の言葉だけで検査する
        return None


def _candidate_dir(root: Path, product) -> Path:
    return root / OUT / product.id / product.version


def _write_candidate(root: Path, product, candidate: dict) -> Path:
    out = _candidate_dir(root, product)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{product.id}-{product.version}.zip").write_bytes(candidate["package"])
    (out / "candidate.json").write_text(json.dumps(
        {k: v for k, v in candidate.items() if k != "package"}, ensure_ascii=False, indent=2,
        sort_keys=True) + "\n", encoding="utf-8")  # fmt: skip
    return out


def _read_candidate(root: Path, product) -> dict:
    path = _candidate_dir(root, product) / "candidate.json"
    if not path.exists():
        raise ProductError(f"no release candidate for {product.id} {product.version} (build first)")
    candidate = json.loads(path.read_text(encoding="utf-8"))
    package = (path.parent / f"{product.id}-{product.version}.zip").read_bytes()
    candidate["package"] = package
    return candidate


def main(argv=None, *, root: Path = ROOT, now: datetime | None = None, settings="auto") -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("candidates")
    for name in ("check", "plan", "build", "verify", "status", "packet"):
        sub.add_parser(name).add_argument("product_id")
    review = sub.add_parser("review")
    review.add_argument("product_id")
    review.add_argument("--by", required=True)
    review.add_argument("--confirm-all", action="store_true",
                        help="the reviewer confirms every checklist item")
    review.add_argument("--notes", default="")
    content = sub.add_parser("approve-content")
    content.add_argument("product_id")
    content.add_argument("--content-hash", required=True)
    content.add_argument("--by", required=True)
    release = sub.add_parser("approve-release")
    release.add_argument("product_id")
    release.add_argument("--manifest-hash", required=True)
    release.add_argument("--by", required=True)
    args = parser.parse_args(argv)
    now = now or datetime.now(UTC)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    settings = _settings() if settings == "auto" else settings
    policy = load_policy()
    sources = load_sources(root)

    def emit(obj) -> None:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))

    try:
        if args.command == "candidates":
            emit(discover(root, sources))
            return 0
        product = load_product(root, args.product_id)
        result = check(product, sources, policy=policy, settings=settings)
        terms = site_terms(policy, settings=settings)
        if args.command == "check":
            emit(result)
            return 0 if result["ok"] else 1
        if args.command == "review":
            checklist = {k: True for k in rec.CHECKLIST} if args.confirm_all else {}
            emit(rec.record_quality_review(product, result, by=args.by, now=now,
                                           checklist=checklist, notes=args.notes))  # fmt: skip
        elif args.command == "approve-content":
            emit(rec.record_content_approval(product, result, content_hash=args.content_hash,
                                             by=args.by, now=now))  # fmt: skip
        elif args.command == "plan":
            plan = pb.stale_plan(product, result["sources"])
            records = rec.load_records(product)
            plan["next_step"] = (
                "fix the check errors" if result["errors"] else
                "human quality review (review --confirm-all)"
                if not rec.latest(records, "quality_review", content_hash=product.content_hash)
                else "human content approval (approve-content)"
                if not rec.latest(records, "content_approval", content_hash=product.content_hash)
                else "build a release candidate, then the human release gate")  # fmt: skip
            plan["content_hash"] = product.content_hash
            emit(plan)
        elif args.command == "build":
            if result["errors"]:
                emit({"refused": "the product has check errors", "errors": result["errors"]})
                return 2
            candidate = pb.build_candidate(product, result["sources"], policy=policy,
                                           terms=terms)  # fmt: skip
            out = _write_candidate(root, product, candidate)
            emit({"release_candidate": str(out), "version": product.version,
                  "manifest_hash": candidate["manifest_hash"],
                  "package_sha256": candidate["package_sha256"],
                  "validation_errors": candidate["validation_errors"]})  # fmt: skip
            return 0 if not candidate["validation_errors"] else 1
        elif args.command == "verify":
            stored = _read_candidate(root, product)
            again = pb.build_candidate(product, result["sources"], policy=policy, terms=terms)
            same = (again["package_sha256"] == stored["package_sha256"]
                    and again["manifest_hash"] == stored["manifest_hash"])  # fmt: skip
            emit({"reproducible": same, "manifest_hash": again["manifest_hash"],
                  "package_sha256": again["package_sha256"]})  # fmt: skip
            return 0 if same else 1
        elif args.command == "approve-release":
            stored = _read_candidate(root, product)
            again = pb.build_candidate(product, result["sources"], policy=policy, terms=terms)
            if again["package_sha256"] != stored["package_sha256"]:
                raise ProductError("the stored release candidate no longer matches a rebuild "
                                   "(sources or content changed); build again")  # fmt: skip
            emit(rec.record_release_approval(product, stored, manifest_hash=args.manifest_hash,
                                             by=args.by, now=now))  # fmt: skip
        elif args.command == "packet":
            from app.products import packet as pk

            first = pb.build_candidate(product, result["sources"], policy=policy, terms=terms)
            again = pb.build_candidate(product, result["sources"], policy=policy, terms=terms)
            text = pk.render(product, result, first,
                             reproducible=first["package"] == again["package"])  # fmt: skip
            out = _candidate_dir(root, product)
            out.mkdir(parents=True, exist_ok=True)
            (out / "review-packet.md").write_text(text, encoding="utf-8")
            emit({"review_packet": str(out / "review-packet.md"),
                  "content_hash": product.content_hash,
                  "manifest_hash": first["manifest_hash"], "check_ok": result["ok"]})
        elif args.command == "status":
            records = rec.load_records(product)
            emit({"product_id": product.id, "version": product.version,
                  "content_hash": product.content_hash, "check_ok": result["ok"],
                  "errors": len(result["errors"]),
                  "quality_reviewed": bool(rec.latest(records, "quality_review",
                                                      content_hash=product.content_hash)),
                  "content_approved": bool(rec.latest(records, "content_approval",
                                                      content_hash=product.content_hash)),
                  "released_versions": [r["version"] for r in pb.released_versions(product)],
                  "distribution": "not decided (separate workstream; human)"})  # fmt: skip
    except (ProductError, ValueError, OSError) as exc:
        print(f"refused: {exc}")
        return 2
    print("local only: nothing was published, listed, uploaded or sold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
