from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.database import build_engine, get_session
from app.main import app
from app.models import Base


@pytest.fixture
def engine() -> Generator[Engine, None, None]:
    """テスト用のインメモリ SQLite エンジン。"""

    test_engine = build_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(test_engine)

    try:
        yield test_engine
    finally:
        Base.metadata.drop_all(test_engine)
        test_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()

    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def api_client(session: Session) -> Generator[TestClient, None, None]:
    """テスト用 SQLite セッションを注入した TestClient。

    ``get_session`` を dependency_overrides で差し替えるため、
    実際の ``affiliate_ai.db`` には一切触れない。
    """

    def _override_get_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = _override_get_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
