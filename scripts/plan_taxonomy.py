"""管理用 CLI: W2 のカテゴリ整理の計画を作る (**読むだけ**)。

    uv run python scripts/plan_taxonomy.py

- WordPress (読むだけ): カテゴリ・タグ・全 post (カテゴリ・タグ・URL)。
- アプリの DB (読むだけ): 記事 (article_id・タイトル・slug) と WordPress の post の対応。
- 出力: ``artifacts/taxonomy/w2-category-plan.json`` と ``.md`` (git の管理外)。

カテゴリを作らない。記事のカテゴリを変えない。確かめられないことがあれば ``ready`` を False
にして、終了コード 1 (推測で埋めない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.wordpress.taxonomy_plan import build_plan, render_markdown  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "artifacts" / "taxonomy"


def read_articles(session) -> list[dict]:
    from sqlalchemy import select

    from app.models import Article
    from app.services.content_queue_service import read_only_session

    with read_only_session(session):
        rows = session.execute(
            select(Article.id, Article.title, Article.slug, Article.wordpress_post_id)
        ).all()
    return [
        {"id": r.id, "title": r.title, "slug": r.slug, "wordpress_post_id": r.wordpress_post_id}
        for r in rows
    ]


def main(argv=None, *, client=None, articles=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if client is None:
        from app.config.settings import get_settings
        from app.wordpress.client import WordPressClient

        client = WordPressClient(get_settings())
    if articles is None:
        from app.config.database import SessionLocal

        with SessionLocal() as session:
            articles = read_articles(session)

    plan = build_plan(
        categories=client.list_categories(),
        tags=client.list_tags(),
        posts=client.list_post_states(),
        articles=articles,
        base_url=client.target_base_url,
        generated_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "w2-category-plan.json"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "w2-category-plan.md").write_text(render_markdown(plan), encoding="utf-8")
    for name, ok in plan["checks"].items():
        print(f"{'OK' if ok else 'NG'} {name}")
    for problem in plan["problems"]:
        print(f"problem: {problem}")
    print(f"ready: {plan['ready']}")
    print(f"wrote {json_path}")
    print("read-only: no category was created and no post was changed")
    return 0 if plan["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
