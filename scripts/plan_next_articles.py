"""次に作る記事の計画 (C10-2 / C10-D、Next Article Orchestrator)。**読むだけ。記事は作らない。**

    uv run python scripts/plan_next_articles.py
    uv run python scripts/plan_next_articles.py --format json

夜の分析 (``run_nightly_analysis.py --section next``) と同じ計画を出す。既にある Keyword の候補は
既存の Growth Action (create_new_article) と同じ識別で、人のレビュー → 記事の計画の依頼 (C9-B)
→ 記事の計画の承認 → 記事、の流れのまま。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_nightly_analysis import main as _nightly_main  # noqa: E402


def main(argv=None, **kwargs) -> int:
    return _nightly_main(argv, default_section="next", **kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
