from collections.abc import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import settings

_SQLITE_MEMORY_URLS = frozenset({"sqlite://", "sqlite:///:memory:"})


def build_engine(database_url: str | None = None, *, echo: bool | None = None) -> Engine:
    """設定に応じた SQLAlchemy エンジンを生成する。

    SQLite の場合のみ ``check_same_thread`` を無効化する。
    インメモリ SQLite では単一コネクションを共有する ``StaticPool`` を使い、
    別スレッド (テストの ``TestClient`` 等) からも同じ DB を参照できるようにする。
    PostgreSQL など他のバックエンドでは接続プールの死活監視のみ有効にする。
    """

    url = database_url if database_url is not None else settings.database_url
    echo_sql = echo if echo is not None else settings.database_echo

    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"echo": echo_sql, "future": True}

    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if url in _SQLITE_MEMORY_URLS:
            engine_kwargs["poolclass"] = StaticPool
    else:
        engine_kwargs["pool_pre_ping"] = True

    return create_engine(url, connect_args=connect_args, **engine_kwargs)


engine: Engine = build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def get_session() -> Generator[Session, None, None]:
    """FastAPI の依存性注入などで利用するセッションプロバイダ。"""

    session = SessionLocal()

    try:
        yield session
    finally:
        session.close()


def check_database_connection(target_engine: Engine | None = None) -> bool:
    """DB へ接続できるかを確認する。接続できれば ``True`` を返す。"""

    active_engine = target_engine if target_engine is not None else engine

    with active_engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    return True
