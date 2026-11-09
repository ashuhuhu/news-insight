from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.db import Base, get_session
from app.main import app, get_analyzer, get_fetcher, get_news_client
from tests.helpers import FakeAnalyzer, FakeFetcher, FakeNews


@pytest.fixture
def session_factory():
    # StaticPool so every session in a test sees the same in-memory database.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def override_session(session_factory):
    def session_override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def api(session_factory, override_session, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    news, analyzer, fetcher = FakeNews(), FakeAnalyzer(), FakeFetcher()
    app.dependency_overrides[get_news_client] = lambda: news
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    app.dependency_overrides[get_fetcher] = lambda: fetcher
    # TestClient is used without "with" so the startup hook never touches the real database.
    return SimpleNamespace(
        client=TestClient(app), news=news, analyzer=analyzer, fetcher=fetcher, session_factory=session_factory
    )
