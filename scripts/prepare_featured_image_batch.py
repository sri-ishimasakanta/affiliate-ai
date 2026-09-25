"""管理用 CLI: W1.5 の 1 バッチ分の featured image 制作パッケージを作る / 確かめる。

    uv run python scripts/prepare_featured_image_batch.py package --batch 1
    uv run python scripts/prepare_featured_image_batch.py validate --batch 1

入力は ``docs/operations/featured-image-w1.5-manifest.json`` だけ。出力は
``artifacts/featured-images/w1.5/batch-<N>/`` (git 管理外):

- ``batch-manifest.json`` (機械可読。設計の写し + 画像生成の文 + 組版の座標)
- ``README.md`` (制作する人への説明) / ``validation-checklist.md``
- ``prompts/<file>.prompt.txt`` / ``.negative.txt`` / ``.single.txt``
- 空の ``backgrounds/`` と ``proofs/``

WordPress にも DB にも触らない。画像は作らない (組版は ``compose_featured_image.py``)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.wordpress.featured_image_batch import (  # noqa: E402
    build_batch_package,
    manifest_sha256,
    render_checklist_markdown,
    render_handoff_markdown,
    validate_batch_package,
)

MANIFEST = ROOT / "docs" / "operations" / "featured-image-w1.5-manifest.json"
MANIFEST_REL = "docs/operations/featured-image-w1.5-manifest.json"
DEFAULT_ROOT = ROOT / "artifacts" / "featured-images" / "w1.5"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("package", "validate"))
    parser.add_argument("--batch", type=int, required=True)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    digest = manifest_sha256(args.manifest)
    out_dir = args.out_root / f"batch-{args.batch}"
    package_path = out_dir / "batch-manifest.json"

    if args.command == "package":
        package = build_batch_package(
            manifest, args.batch, manifest_path=MANIFEST_REL, manifest_sha256=digest
        )
        _write(package_path, json.dumps(package, ensure_ascii=False, indent=2) + "\n")
        _write(out_dir / "README.md", render_handoff_markdown(package))
        _write(out_dir / "validation-checklist.md", render_checklist_markdown(package))
        for item in package["items"]:
            stem = item["files"]["master_png"].removesuffix(".png")
            gen = item["generation"]
            _write(out_dir / "prompts" / f"{stem}.prompt.txt", gen["prompt"] + "\n")
            _write(out_dir / "prompts" / f"{stem}.negative.txt", gen["negative_prompt"] + "\n")
            _write(out_dir / "prompts" / f"{stem}.single.txt", gen["single_prompt"] + "\n")
        (out_dir / "backgrounds").mkdir(parents=True, exist_ok=True)
        (out_dir / "proofs").mkdir(parents=True, exist_ok=True)
        print(f"wrote {package_path}")
        print("articles: " + " -> ".join(str(i["article_id"]) for i in package["items"]))

    package = json.loads(package_path.read_text(encoding="utf-8"))
    problems = validate_batch_package(package, manifest, manifest_sha256=digest)
    for problem in problems:
        print(f"problem: {problem}")
    print(f"validation: {'ok' if not problems else f'{len(problems)} problem(s)'}")
    print("no WordPress or database access")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
