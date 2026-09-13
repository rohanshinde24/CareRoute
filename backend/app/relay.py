"""Publishes outbox rows to Redis Streams and marks them dispatched.

The relay is deliberately dumb and deliberately at-least-once. It claims rows
with SELECT ... FOR UPDATE SKIP LOCKED so several relays can run without
publishing the same event twice, publishes, then marks them dispatched.

The ordering is publish-then-mark, not mark-then-publish. If the process dies
between the two, the event is delivered again on the next pass - a duplicate,
which consumers are required to absorb. The opposite order would lose the event
entirely, which nothing downstream can repair. Given the choice between a
duplicate and a silence, this system always takes the duplicate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .events import envelope_from_row
from .metrics import record_relay_dispatch, record_outbox_backlog

logger = logging.getLogger(__name__)

STREAM = "careroute.events"


def redis_client(url: str | None = None) -> Redis:
    return Redis.from_url(url or settings.redis_url, decode_responses=True)


def pending(db: Session, model, limit: int) -> list:
    """Oldest undispatched rows, locked so concurrent relays do not overlap."""
    return list(
        db.scalars(
            select(model)
            .where(model.dispatched_at.is_(None))
            .order_by(model.occurred_at, model.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )


def backlog(db: Session, model) -> tuple[int, float | None]:
    """Depth and age of the oldest undispatched event.

    Age matters more than depth: a large backlog draining steadily is healthy,
    while a single row stuck for an hour means the relay is not running.
    """
    rows = list(db.scalars(select(model).where(model.dispatched_at.is_(None)).order_by(model.occurred_at)))
    if not rows:
        return 0, None
    oldest = rows[0].occurred_at
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return len(rows), (datetime.now(timezone.utc) - oldest).total_seconds()


def drain(db: Session, model, client: Redis, batch: int = 100) -> int:
    """Publish one batch. Returns the number of events delivered.

    A broker failure leaves the rows undispatched and the transaction rolled
    back, so the batch is retried whole on the next pass. It never fails the
    caller: the relay runs outside the request path precisely so that a broker
    outage delays delivery instead of breaking a user-facing operation.
    """
    rows = pending(db, model, batch)
    if not rows:
        return 0

    delivered = 0
    try:
        for row in rows:
            client.xadd(STREAM, envelope_from_row(row).redis_fields())
            delivered += 1
    except RedisError as exc:
        db.rollback()
        record_relay_dispatch("broker_unavailable", delivered)
        logger.warning("relay could not reach the broker; %s rows stay undispatched: %s", len(rows), exc)
        return 0

    now = datetime.now(timezone.utc)
    for row in rows:
        row.dispatched_at = now
        row.dispatch_attempts = (row.dispatch_attempts or 0) + 1
    db.commit()
    record_relay_dispatch("dispatched", delivered)
    return delivered


def run_once(referral_session_factory, provider_session_factory, client: Redis | None = None) -> dict[str, int]:
    """One pass over both outboxes. Each domain relays its own."""
    from .models import ReferralOutbox
    from .provider_models import ProviderOutbox

    client = client or redis_client()
    counts: dict[str, int] = {}
    for name, factory, model in (
        ("referral", referral_session_factory, ReferralOutbox),
        ("provider", provider_session_factory, ProviderOutbox),
    ):
        with factory() as db:
            counts[name] = drain(db, model, client)
            depth, age = backlog(db, model)
            record_outbox_backlog(name, depth, age)
    return counts


def main() -> None:  # pragma: no cover - process entry point
    import time

    from .database import SessionLocal
    from .provider_database import ProviderSessionLocal

    logging.basicConfig(level=logging.INFO)
    client = redis_client()
    logger.info("relay started; stream=%s interval=%ss", STREAM, settings.relay_interval_seconds)
    while True:
        try:
            counts = run_once(SessionLocal, ProviderSessionLocal, client)
            if any(counts.values()):
                logger.info("relayed %s", counts)
        except Exception:  # keep relaying after a transient failure
            logger.exception("relay pass failed; retrying")
        time.sleep(settings.relay_interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    main()
