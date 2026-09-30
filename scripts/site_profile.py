"""サイトのプロファイル (N6)。**手元だけ。外の連携はプロファイルで無効なら値ごと空にする。**

    uv run python scripts/site_profile.py validate sites/example-local/site.json
    uv run python scripts/site_profile.py bootstrap sites/example-local/site.json  # 自分の DB
    uv run python scripts/site_profile.py dry-run sites/example-local/site.json    # 点検

プロファイルを使わないときの動き (本番) は変わらない。bootstrap はプロファイルの DB だけを
作る (本番の DB と同じ場所は拒む)。dry-run はネットワークへの接続をすべて拒む。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None, *, root: Path = ROOT, production_database="auto", runner=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "bootstrap", "dry-run"))
    parser.add_argument("profile")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from app.sites.dry_run import bootstrap, dry_run
    from app.sites.profile import ProfileError, load_profile, production_database_path

    if production_database == "auto":
        production_database = production_database_path(root)
    try:
        profile = load_profile(Path(args.profile) if Path(args.profile).is_absolute()
                               else root / args.profile, root=root,
                               production_database=production_database)  # fmt: skip
    except (ProfileError, ValueError) as exc:
        print(f"refused: {exc}")
        return 2
    if args.command == "validate":
        result = {"site": profile.id, "database": str(profile.database_path), "valid": True}
    elif args.command == "bootstrap":
        result = bootstrap(profile, runner=runner)
    else:
        result = dry_run(profile)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    ok = result.get("ok", result.get("returncode", 0) == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
