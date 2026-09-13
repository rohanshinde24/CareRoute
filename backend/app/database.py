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
        # Backstop, not the fix. If a transaction is ever abandoned anyway, the
        # database reclaims it instead of holding a pool slot indefinitely, so a
        # leak degrades into slowness rather than a permanent wedge.
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


engine = create_engine(settings.database_url, pool_pre_ping=True, **_engine_options())
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def get_db() -> Generator[Session, None, None]:
    """Yield a session and return its connection to the pool, always.

    The previous form relied on the `with` block being reached when FastAPI
    resumed this generator after the response. Under load that resumption is not
    guaranteed - a cancelled request can leave the generator suspended at the
    yield - and a session that is never closed keeps its connection checked out
    with an open transaction. Fifteen of those exhausts the pool and the service
    stops serving without recovering.

    rollback() before close() is deliberate: close() alone returns the
    connection, but an explicit rollback ends the transaction first so the
    connection never goes back to the pool mid-transaction.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        try:
            session.rollback()
        finally:
            session.close()

