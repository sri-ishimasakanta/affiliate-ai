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

    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), "-1")
    cols_after_down = _affiliate_program_columns(url)
    assert "match_terms" not in cols_after_down
    assert "currency" not in cols_after_down

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
    assert {"match_terms", "currency"} <= _affiliate_program_columns(url)
