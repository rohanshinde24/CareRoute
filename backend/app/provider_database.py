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
        # Socket-level deadlines. Without them a thread can block forever on a
        # connection the server has already terminated - and a permanently
        # blocked thread is worse than a failed request, because it never comes
        # back to serve anything else.
        "connect_args": {
            "options": f"-c idle_in_transaction_session_timeout={int(settings.db_idle_transaction_timeout_seconds * 1000)}",
            "connect_timeout": 5,
            "keepalives": 1,
            "keepalives_idle": 10,
            "keepalives_interval": 5,
            "keepalives_count": 3,
            "tcp_user_timeout": 15000,
        },
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
    """Same deterministic cleanup as the referral session. See app/database.py."""
    session = ProviderSessionLocal()
    try:
        yield session
    finally:
        try:
            session.rollback()
        finally:
            session.close()
