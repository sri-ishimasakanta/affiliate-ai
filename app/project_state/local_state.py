"""git・品質 (テスト・lint・migration)・DB の状態 (手元だけ。読むだけ)。

- git: ``git`` の読み取りのコマンドだけ (status / rev-parse / rev-list / log)。push しない。
- 品質: ruff・``alembic check``・``git diff --check`` は速いので毎回その場で実行する。pytest は
  時間がかかるので ``--with-tests`` のときだけ実行する。実行しなかったときは、この道具が前回
  記録した結果 (``reports/quality_latest.json``) を「記録」として載せ、無ければ「不明」。
  **テストの件数を作らない。**
- DB: Alembic の head とデータベースの今の revision を読む (migration しない。接続先の URL は
  種類とファイル名だけを出す)。
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from app.project_state.provenance import provenance, unavailable

QUALITY_RECORD = Path("reports") / "quality_latest.json"
Runner = Callable[[Sequence[str]], tuple[int, str]]


def default_runner(root: Path) -> Runner:
    def run(args: Sequence[str]) -> tuple[int, str]:
        done = subprocess.run(
            list(args),
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    return run


# == git =========================================================================
def parse_ahead_behind(text: str) -> tuple[int, int] | None:
    """``git rev-list --left-right --count origin/main...HEAD`` → (behind, ahead)。"""

    parts = text.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def parse_porcelain(text: str) -> dict[str, list[str]]:
    staged, unstaged, untracked = [], [], []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if code == "??":
            untracked.append(path)
            continue
        if code[0] not in (" ", "?"):
            staged.append(path)
        if code[1] not in (" ", "?"):
            unstaged.append(path)
    return {"staged": staged, "unstaged": unstaged, "untracked": untracked}


def collect_git(run: Runner, *, now: datetime, remote: str = "origin/main") -> dict:
    code, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if code != 0:
        return {"provenance": unavailable("local_command", "git is not available")}
    _, sha = run(["git", "rev-parse", "HEAD"])
    _, subject = run(["git", "log", "-1", "--format=%s"])
    _, porcelain = run(["git", "status", "--porcelain"])
    code_ab, ab = run(["git", "rev-list", "--left-right", "--count", f"{remote}...HEAD"])
    _, log = run(["git", "log", "--oneline", "-15"])
    counts = parse_ahead_behind(ab) if code_ab == 0 else None
    files = parse_porcelain(porcelain)
    tracked_dirty = bool(files["staged"] or files["unstaged"])
    return {
        "provenance": provenance(
            "local_command",
            kind="observed",
            status="ok",
            observed_at=now,
            freshness="fresh",
            detail="remote-tracking ref as last fetched (no fetch is run)",
        ),
        "branch": branch.strip(),
        "head": sha.strip(),
        "head_subject": subject.strip(),
        "clean": not tracked_dirty and not files["untracked"],
        "tracked_changes": tracked_dirty,
        **files,
        "remote": remote,
        "behind": counts[0] if counts else None,
        "ahead": counts[1] if counts else None,
        "push_pending": bool(counts and counts[1] > 0),
        "recent_commits": [line for line in log.splitlines() if line.strip()],
    }


# == quality =====================================================================
_PYTEST_SUMMARY = re.compile(r"(\d+) passed(?:, (\d+) failed)?(?:.*?(\d+) error)?")


def parse_pytest(text: str) -> dict:
    tail = [line for line in text.strip().splitlines() if line.strip()]
    last = tail[-1] if tail else ""
    passed = re.search(r"(\d+) passed", last)
    failed = re.search(r"(\d+) failed", last)
    errors = re.search(r"(\d+) error", last)
    return {
        "summary": last,
        "passed": int(passed.group(1)) if passed else None,
        "failed": int(failed.group(1)) if failed else 0,
        "errors": int(errors.group(1)) if errors else 0,
    }


def collect_quality(run: Runner, root: Path, *, now: datetime, with_tests: bool) -> dict:
    checks = {}
    code, out = run(["uv", "run", "ruff", "check", "."])
    checks["ruff"] = {
        "ok": code == 0,
        "summary": out.strip().splitlines()[-1] if out.strip() else "",
    }
    code, out = run(["uv", "run", "alembic", "check"])
    meaningful = [ln for ln in out.strip().splitlines() if not ln.startswith("INFO ")]
    checks["alembic_check"] = {
        "ok": code == 0,
        "summary": meaningful[-1] if meaningful else (out.strip().splitlines() or [""])[-1],
    }
    code, out = run(["git", "diff", "--check"])
    checks["git_diff_check"] = {"ok": code == 0, "summary": "clean" if code == 0 else out[-300:]}
    stamp = now.isoformat(timespec="seconds")
    for check in checks.values():
        check.update(freshness="fresh", observed_at=stamp)
    record_path = root / QUALITY_RECORD
    if with_tests:
        code, out = run(["uv", "run", "pytest", "-q"])
        result = parse_pytest(out)
        checks["pytest"] = {"ok": code == 0, **result, "freshness": "fresh", "observed_at": stamp}
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps({"pytest": checks["pytest"]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif record_path.exists():
        recorded = json.loads(record_path.read_text(encoding="utf-8")).get("pytest") or {}
        checks["pytest"] = {**recorded, "freshness": "recorded"}
    else:
        checks["pytest"] = {
            "ok": None,
            "passed": None,
            "freshness": "unverified",
            "summary": "not run and no earlier record (run with --with-tests)",
        }
    status = "ok" if all(c.get("ok") is not False for c in checks.values()) else "degraded"
    return {
        "provenance": provenance(
            "local_command",
            kind="observed",
            status=status,
            observed_at=now,
            freshness="fresh",
            detail="pytest is fresh only with --with-tests",
        ),
        **checks,
    }


# == database ====================================================================
def describe_url(url: str) -> str:
    """接続先を秘密なしで表す (種類とファイル名 / ホスト名だけ)。"""

    scheme = url.split(":", 1)[0]
    if scheme.startswith("sqlite"):
        return f"{scheme} ({Path(url.split('///', 1)[-1]).name})"
    host = re.sub(r"^.*@", "", url.split("://", 1)[-1]).split("/", 1)[0].split("?", 1)[0]
    return f"{scheme} ({host.split(':', 1)[0]})"


def collect_database(root: Path, *, database_url: str, now: datetime, engine_factory=None) -> dict:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    # alembic.ini の script_location (``%(here)s/migrations``) を使う (決め打ちしない)。
    config = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = list(script.get_heads())
    head = script.get_revision(heads[0]) if len(heads) == 1 else None
    out = {
        "target": describe_url(database_url),
        "code_heads": heads,
        "latest_migration": {
            "revision": head.revision if head else None,
            "message": (head.doc or "").strip() if head else None,
        },
    }
    try:
        if engine_factory is None:
            from sqlalchemy import create_engine

            engine = create_engine(database_url)
        else:
            engine = engine_factory()
        with engine.connect() as connection:
            current = list(MigrationContext.configure(connection).get_current_heads())
    except Exception as exc:  # 読めないときは「読めなかった」とだけ言う
        return {**out, "provenance": unavailable("local_db", type(exc).__name__)}
    pending = [] if set(current) == set(heads) else sorted(set(heads) - set(current))
    return {
        **out,
        "provenance": provenance(
            "local_db",
            kind="observed",
            status="ok" if not pending else "degraded",
            observed_at=now,
            freshness="fresh",
        ),
        "db_revisions": current,
        "db_at_code_head": set(current) == set(heads),
        "pending_migrations": pending,
    }
