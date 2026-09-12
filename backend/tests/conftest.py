import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app, provider_gateway_dependency
from app.provider_database import ProviderBase
from app.provider_gateway import LocalProviderGateway


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine)


@pytest.fixture
def provider_db(tmp_path):
    """The provider domain's own store, separate from the referral one.

    Two engines rather than two schemas, so a test that accidentally joins across
    the boundary fails the way production would.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'provider.db'}")
    ProviderBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    ProviderBase.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _redirect_provider_session(monkeypatch, provider_db):
    """Point every provider_session() call at the test provider database.

    Without this, modules that own provider fixtures (seeding, evaluation) would
    reach the configured provider database instead of the test one.
    """
    monkeypatch.setattr("app.provider_database.ProviderSessionLocal", lambda: _NonClosing(provider_db))


class _NonClosing:
    """Hand out the test session without letting callers close it."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.fixture
def client(db, provider_db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[provider_gateway_dependency] = lambda: LocalProviderGateway(provider_db)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
