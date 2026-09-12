from collections.abc import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from .config import settings

class Base(DeclarativeBase):
    pass

def _engine_options() -> dict:
    # SQLite uses a pool class that rejects these arguments, and the unit suite
    # runs on SQLite, so only size a pool where one exists.
    if settings.database_url.startswith("sqlite"):
        return {}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }


engine = create_engine(settings.database_url, pool_pre_ping=True, **_engine_options())
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session

