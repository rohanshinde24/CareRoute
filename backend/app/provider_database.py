"""Session and metadata for the provider-owned database.

Separate from app/database.py on purpose. The provider domain owns its own
storage, its own migration lineage, and its own failure behaviour, so it must
not share a declarative base with referral-owned tables: a shared base makes it
trivial to write a join across the boundary and impossible for a migration to
describe one database without the other.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class ProviderBase(DeclarativeBase):
    pass


def _engine_options() -> dict:
    if settings.provider_database_url.startswith("sqlite"):
        return {}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }


provider_engine = create_engine(settings.provider_database_url, pool_pre_ping=True, **_engine_options())
ProviderSessionLocal = sessionmaker(bind=provider_engine, expire_on_commit=False)


def provider_session() -> Session:
    """Open a provider-database session.

    Callers use this rather than binding ProviderSessionLocal at import time, so
    tests can redirect the provider domain to their own database without
    reaching into every module that touches it.
    """
    return ProviderSessionLocal()


def get_provider_db() -> Generator[Session, None, None]:
    with ProviderSessionLocal() as session:
        yield session
