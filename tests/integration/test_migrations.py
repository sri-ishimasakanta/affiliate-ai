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


_PROMOTIONS_MIGRATION = "71fe1f7da534"
_BEFORE_PROMOTIONS_MIGRATION = "3e86dc460cd9"
_UNCHANGED_TABLES_FOR_PROMOTIONS = (
    *_UNCHANGED_TABLES,
    "draft_input_snapshots",
    "draft_generation_runs",
)


def test_article_draft_promotions_migration_is_add_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'article_draft_promotions.db'}"

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _BEFORE_PROMOTIONS_MIGRATION)
    before_tables = _table_names(url)
    before_columns = {
        t: _existing_table_columns(url, t) for t in _UNCHANGED_TABLES_FOR_PROMOTIONS
    }
    assert "article_draft_promotions" not in before_tables

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _PROMOTIONS_MIGRATION)

    assert _table_names(url) - before_tables == {"article_draft_promotions"}
    for table, cols in before_columns.items():
        assert _existing_table_columns(url, table) == cols, table

    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), _BEFORE_PROMOTIONS_MIGRATION)
    assert _table_names(url) == before_tables
    for table, cols in before_columns.items():
        assert _existing_table_columns(url, table) == cols, table


def test_article_draft_promotions_table_shape_at_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'promotions_shape.db'}"
    _upgrade_head(url)
    engine = build_engine(url)
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("article_draft_promotions")}
        uniques = {
            tuple(u["column_names"])
            for u in insp.get_unique_constraints("article_draft_promotions")
        }
        fks = {
            (
                fk["referred_table"],
                tuple(fk["constrained_columns"]),
                fk.get("options", {}).get("ondelete"),
            )
            for fk in insp.get_foreign_keys("article_draft_promotions")
        }
    finally:
        engine.dispose()
    assert "updated_at" not in cols  # immutable append-only
    assert {
        "body_markdown",
        "meta_description",
        "body_hash",
        "meta_hash",
        "candidate_content_hash",
        "source_prompt_input_hash",
        "source_rendered_prompt_hash",
        "validation_report",
        "promoted_at",
    } <= cols
    assert ("idempotency_key",) in uniques
    assert ("articles", ("article_id",), "CASCADE") in fks
    assert ("draft_generation_runs", ("source_run_id",), "RESTRICT") in fks


_WP_RUNS_MIGRATION = "2b8c28dfb5e9"
_BEFORE_WP_RUNS_MIGRATION = "71fe1f7da534"
_UNCHANGED_TABLES_FOR_WP_RUNS = (
    *_UNCHANGED_TABLES,
    "draft_input_snapshots",
    "draft_generation_runs",
    "article_draft_promotions",
)


def test_wordpress_draft_runs_migration_is_add_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'wordpress_draft_runs.db'}"

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _BEFORE_WP_RUNS_MIGRATION)
    before_tables = _table_names(url)
    before_columns = {
        t: _existing_table_columns(url, t) for t in _UNCHANGED_TABLES_FOR_WP_RUNS
    }
    assert "wordpress_draft_runs" not in before_tables

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _WP_RUNS_MIGRATION)

    assert _table_names(url) - before_tables == {"wordpress_draft_runs"}
    for table, cols in before_columns.items():
        assert _existing_table_columns(url, table) == cols, table

    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), _BEFORE_WP_RUNS_MIGRATION)
    assert _table_names(url) == before_tables
    for table, cols in before_columns.items():
        assert _existing_table_columns(url, table) == cols, table


def test_wordpress_draft_runs_table_shape_at_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'wp_runs_shape.db'}"
    _upgrade_head(url)
    engine = build_engine(url)
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("wordpress_draft_runs")}
        uniques = {
            tuple(u["column_names"])
            for u in insp.get_unique_constraints("wordpress_draft_runs")
        }
        fks = {
            (
                fk["referred_table"],
                tuple(fk["constrained_columns"]),
                fk.get("options", {}).get("ondelete"),
            )
            for fk in insp.get_foreign_keys("wordpress_draft_runs")
        }
    finally:
        engine.dispose()
    assert "updated_at" not in cols  # append-only
    assert {
        "payload_json", "payload_hash", "request_identity_hash",
        "target_request_identity_hash", "target_base_url", "status",
        "renderer_version", "rendered_content_hash",
    } <= cols
    assert ("idempotency_key",) in uniques
    assert ("articles", ("article_id",), "RESTRICT") in fks
    assert ("article_draft_promotions", ("source_promotion_id",), "RESTRICT") in fks


# ---------------------------------------------------------------------------
# Phase 3C-5F-D-C3-B1: outbound click import foundation (2 chained migrations)
# ---------------------------------------------------------------------------
_CLICK_RUNS_MIGRATION = "4cb3c9ea6507"  # add affiliate_click_import_runs
_CLICKS_MIGRATION = "a0674bcc7cb2"  # add affiliate_outbound_clicks
_BEFORE_CLICK_RUNS = "821856f0c58d"  # add affiliate_link_targets


def _indexes(url: str, table: str) -> set[str]:
    engine = build_engine(url)
    try:
        return {ix["name"] for ix in inspect(engine).get_indexes(table)}
    finally:
        engine.dispose()


def test_affiliate_click_import_runs_migration_is_add_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'click_import_runs.db'}"

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _BEFORE_CLICK_RUNS)
    before_tables = _table_names(url)
    link_cols_before = _existing_table_columns(url, "affiliate_link_targets")
    assert "affiliate_click_import_runs" not in before_tables

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _CLICK_RUNS_MIGRATION)

    assert _table_names(url) - before_tables == {"affiliate_click_import_runs"}
    assert _existing_table_columns(url, "affiliate_link_targets") == link_cols_before

    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), _BEFORE_CLICK_RUNS)
    assert _table_names(url) == before_tables


def test_affiliate_outbound_clicks_migration_is_add_only(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'outbound_clicks.db'}"

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _CLICK_RUNS_MIGRATION)
    before_tables = _table_names(url)
    runs_cols_before = _existing_table_columns(url, "affiliate_click_import_runs")
    assert "affiliate_outbound_clicks" not in before_tables

    with _database_url(url):
        command.upgrade(Config(str(ALEMBIC_INI)), _CLICKS_MIGRATION)

    assert _table_names(url) - before_tables == {"affiliate_outbound_clicks"}
    assert (
        _existing_table_columns(url, "affiliate_click_import_runs") == runs_cols_before
    )

    with _database_url(url):
        command.downgrade(Config(str(ALEMBIC_INI)), _CLICK_RUNS_MIGRATION)
    assert _table_names(url) == before_tables


def test_affiliate_click_import_runs_shape_at_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'click_runs_shape.db'}"
    _upgrade_head(url)
    cols = _existing_table_columns(url, "affiliate_click_import_runs")
    assert "updated_at" not in cols  # append-only run record
    assert {
        "status", "requested_since_id", "requested_limit", "http_status",
        "server_code", "response_count", "response_next_since_id",
        "inserted_count", "duplicate_count", "unresolved_token_count",
        "first_source_click_id", "last_source_click_id", "has_more",
        "error_message", "response_snapshot", "created_at", "started_at",
        "finished_at",
    } <= cols
    # secret / signature / raw body / headers を保持しない
    assert cols.isdisjoint(
        {"shared_secret", "signature", "response_body", "raw_body", "headers"}
    )
    assert _indexes(url, "affiliate_click_import_runs") >= {
        "ix_affiliate_click_import_runs_status",
        "ix_affiliate_click_import_runs_created_id",
    }


def test_affiliate_outbound_clicks_shape_at_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'outbound_clicks_shape.db'}"
    _upgrade_head(url)
    engine = build_engine(url)
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("affiliate_outbound_clicks")}
        uniques = {
            tuple(u["column_names"])
            for u in insp.get_unique_constraints("affiliate_outbound_clicks")
        }
        fks = {
            (
                fk["referred_table"],
                tuple(fk["constrained_columns"]),
                fk.get("options", {}).get("ondelete"),
            )
            for fk in insp.get_foreign_keys("affiliate_outbound_clicks")
        }
    finally:
        engine.dispose()

    assert cols == {
        "id", "source_click_id", "token", "clicked_at",
        "source_import_run_id", "created_at",
    }
    assert "updated_at" not in cols  # append-only replica
    # PII / destination / attribution FK を持たない
    assert cols.isdisjoint(
        {
            "ip", "hashed_ip", "user_agent", "referer", "cookie", "session_id",
            "user_id", "email", "device", "destination_url", "article_id",
            "affiliate_program_id", "source_event_hash",
        }
    )
    assert ("source_click_id",) in uniques
    assert ("affiliate_click_import_runs", ("source_import_run_id",), "RESTRICT") in fks
    # affiliate_link_targets への FK は張らない
    assert not any(ref == "affiliate_link_targets" for ref, _, _ in fks)
    assert _indexes(url, "affiliate_outbound_clicks") >= {
        "ix_affiliate_outbound_clicks_source_click_id",
        "ix_affiliate_outbound_clicks_token",
        "ix_affiliate_outbound_clicks_source_import_run_id",
    }
