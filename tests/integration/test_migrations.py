import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from app.config.database import build_engine
from app.config.settings import get_settings
from app.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


@contextmanager
def _database_url(url: str) -> Iterator[None]:
    """一時的に DATABASE_URL を差し替え、Settings のキャッシュを破棄する。"""

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()

    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


def _upgrade_head(url: str) -> None:
    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), "head")


def test_migrations_apply_on_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "migration_apply.db"
    _upgrade_head(f"sqlite:///{db_path}")

    engine = build_engine(f"sqlite:///{db_path}")
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    expected = set(Base.metadata.tables) | {"alembic_version"}
    assert table_names == expected


def test_head_migration_is_in_sync_with_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "migration_sync.db"
    _upgrade_head(f"sqlite:///{db_path}")

    def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
        if type_ == "table":
            return name != "alembic_version"
        return True

    engine = build_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "render_as_batch": True,
                    "include_name": include_name,
                    "target_metadata": Base.metadata,
                },
            )
            diffs = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diffs == [], f"Alembic metadata と head マイグレーションに差分があります: {diffs}"


def _affiliate_program_columns(url: str) -> set[str]:
    engine = build_engine(url)
    try:
        return {col["name"] for col in inspect(engine).get_columns("affiliate_programs")}
    finally:
        engine.dispose()


def test_affiliate_program_new_columns_present_at_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'aff_cols.db'}"
    _upgrade_head(url)
    assert {"match_terms", "currency"} <= _affiliate_program_columns(url)


def test_affiliate_program_columns_migration_roundtrip(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'aff_roundtrip.db'}"
    _upgrade_head(url)

    # affiliate カラムを追加した migration (abfa2f774ff4) の 1 つ前へ戻す。
    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), "1689a1b083a8")
    cols_after_down = _affiliate_program_columns(url)
    assert "match_terms" not in cols_after_down
    assert "currency" not in cols_after_down

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
    assert {"match_terms", "currency"} <= _affiliate_program_columns(url)


def _table_names(url: str) -> set[str]:
    engine = build_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _existing_table_columns(url: str, table: str) -> set[str]:
    engine = build_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_article_facts_migration_is_add_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'article_facts.db'}"

    # article_facts migration (ba2e8486248e) の 1 つ前 = abfa2f774ff4
    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), "abfa2f774ff4")
    before_tables = _table_names(url)
    before_articles = _existing_table_columns(url, "articles")
    before_sources = _existing_table_columns(url, "sources")
    assert "article_facts" not in before_tables

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), "ba2e8486248e")
    after_tables = _table_names(url)

    # article_facts が 1 つだけ増え、既存 table の列は不変
    assert after_tables - before_tables == {"article_facts"}
    assert _existing_table_columns(url, "articles") == before_articles
    assert _existing_table_columns(url, "sources") == before_sources

    # roundtrip: downgrade で article_facts が消える
    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), "abfa2f774ff4")
    assert "article_facts" not in _table_names(url)


_SNAPSHOT_MIGRATION = "539cdcb66b22"
_BEFORE_SNAPSHOT_MIGRATION = "ba2e8486248e"
_UNCHANGED_TABLES = (
    "articles",
    "article_facts",
    "sources",
    "article_affiliate_programs",
    "affiliate_programs",
)


def test_draft_input_snapshots_migration_is_add_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'draft_input_snapshots.db'}"

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _BEFORE_SNAPSHOT_MIGRATION)
    before_tables = _table_names(url)
    before_columns = {t: _existing_table_columns(url, t) for t in _UNCHANGED_TABLES}
    assert "draft_input_snapshots" not in before_tables

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _SNAPSHOT_MIGRATION)
    after_tables = _table_names(url)

    # snapshot table が 1 つだけ増え、既存 table の列は一切変わらない
    assert after_tables - before_tables == {"draft_input_snapshots"}
    for table, cols in before_columns.items():
        assert _existing_table_columns(url, table) == cols, table

    # downgrade は snapshot table だけを落とす
    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), _BEFORE_SNAPSHOT_MIGRATION)
    assert _table_names(url) == before_tables
    for table, cols in before_columns.items():
        assert _existing_table_columns(url, table) == cols, table


def test_draft_input_snapshots_table_shape_at_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'snapshot_shape.db'}"
    _upgrade_head(url)
    engine = build_engine(url)
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("draft_input_snapshots")}
        uniques = {
            tuple(u["column_names"])
            for u in insp.get_unique_constraints("draft_input_snapshots")
        }
    finally:
        engine.dispose()
    assert {
        "id", "article_id", "snapshot_version", "builder_version",
        "plan_snapshot_origin", "primary_affiliate_program_id",
        "comparison_program_ids", "drafting_allowed_at_freeze", "payload",
        "content_hash", "frozen_at", "created_at",
    } == cols
    assert "updated_at" not in cols
    assert ("article_id", "content_hash") in uniques


_RUNS_MIGRATION = "3e86dc460cd9"
_BEFORE_RUNS_MIGRATION = "539cdcb66b22"
_UNCHANGED_TABLES_FOR_RUNS = (*_UNCHANGED_TABLES, "draft_input_snapshots")


def test_draft_generation_runs_migration_is_add_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'draft_generation_runs.db'}"

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _BEFORE_RUNS_MIGRATION)
    before_tables = _table_names(url)
    before_columns = {
        t: _existing_table_columns(url, t) for t in _UNCHANGED_TABLES_FOR_RUNS
    }
    assert "draft_generation_runs" not in before_tables

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _RUNS_MIGRATION)

    assert _table_names(url) - before_tables == {"draft_generation_runs"}
    for table, cols in before_columns.items():
        assert _existing_table_columns(url, table) == cols, table

    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), _BEFORE_RUNS_MIGRATION)
    assert _table_names(url) == before_tables
    for table, cols in before_columns.items():
        assert _existing_table_columns(url, table) == cols, table


def test_draft_generation_runs_table_shape_at_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'runs_shape.db'}"
    _upgrade_head(url)
    engine = build_engine(url)
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("draft_generation_runs")}
        uniques = {
            tuple(u["column_names"])
            for u in insp.get_unique_constraints("draft_generation_runs")
        }
        fks = {
            (
                fk["referred_table"],
                tuple(fk["constrained_columns"]),
                fk.get("options", {}).get("ondelete"),
            )
            for fk in insp.get_foreign_keys("draft_generation_runs")
        }
    finally:
        engine.dispose()
    assert "updated_at" in cols  # lifecycle record
    assert {"prompt_package", "rendered_prompt", "prompt_input_hash", "status"} <= cols
    assert ("idempotency_key",) in uniques
    assert ("articles", ("article_id",), "CASCADE") in fks
    assert ("draft_input_snapshots", ("snapshot_id",), "RESTRICT") in fks
