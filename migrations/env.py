from logging.config import fileConfig

from alembic import context

from app.config.database import build_engine
from app.config.settings import get_settings
from app.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 接続先は常に Settings (=DATABASE_URL) から取得する。
database_url = get_settings().database_url

# autogenerate / --sql の比較対象は SQLAlchemy の Base.metadata。
target_metadata = Base.metadata

# SQLite は ALTER 制限があるため batch モードで差分を適用する。
render_as_batch = database_url.startswith("sqlite")


def run_migrations_offline() -> None:
    """'offline' モード: URL のみでマイグレーション SQL を生成する。"""

    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=render_as_batch,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """'online' モード: Engine を作成して接続経由でマイグレーションを行う。"""

    connectable = build_engine(database_url, echo=False)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=render_as_batch,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
