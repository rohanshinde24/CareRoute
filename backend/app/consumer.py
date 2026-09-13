"""Consumes domain events from Redis Streams into the referral domain.

Two properties do the real work here.

Idempotency: delivery is at-least-once, so the same event arrives again after a
relay crash or a redelivered stream entry. The handler's effect and the
`consumed_events` row commit in one transaction, so an event counts as consumed
only if its effect actually landed, and a redelivery is a no-op rather than a
second effect.

Span links, not parent-child: an event may sit in the stream for seconds or
hours before anyone reads it. Nesting the consumer's span under the producer's
would report that waiting as operation duration - a trace showing a twenty
minute "booking" that was really twenty minutes of queue. A link records the
causal relationship without lying about time.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.trace import Link, NonRecordingSpan, SpanContext, TraceFlags
from redis import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .events import EventType
from .metrics import record_event_consumed
from .models import ConsumedEvent, Referral, ReferralState
from .relay import STREAM, redis_client
from .telemetry import set_span_attributes, tracer
from .workflow import InvalidTransition, audit, transition

logger = logging.getLogger(__name__)

GROUP = "careroute-referral"
CONSUMER_NAME = "referral-consumer"


def _link_from(traceparent: str | None) -> list[Link]:
    """Parse a W3C traceparent into a span link, ignoring anything malformed.

    A bad traceparent must not stop an event being consumed: correlation is a
    convenience, delivery is not.
    """
    if not traceparent:
        return []
    try:
        version, trace_id, span_id, flags = traceparent.split("-")
        if version != "00":
            return []
        context = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(int(flags, 16)),
        )
        return [Link(context)]
    except (ValueError, AttributeError):
        return []


def ensure_group(client: Redis) -> None:
    """Create the consumer group, tolerating the common case that it exists."""
    try:
        client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _as_uuid(value) -> "uuid.UUID | None":
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _already_consumed(db: Session, event_id) -> bool:
    return db.scalar(select(ConsumedEvent.id).where(ConsumedEvent.event_id == event_id)) is not None


def handle_appointment_booked(db: Session, payload: dict) -> str:
    """Drive a referral forward to CONFIRMED from an authoritative appointment.

    This is the repair for the gap the domain split created: booking commits in
    the provider database and the referral transition is a second transaction in
    another one, so an interruption leaves a real appointment on a referral that
    is not marked confirmed.

    The appointment is authoritative. Repair is forward only - never invent or
    delete an appointment to make the two agree.
    """
    # Envelopes are JSON, so identifiers arrive as strings. Coerce here rather
    # than trusting the column type to do it.
    referral_id = _as_uuid(payload.get("referral_id"))
    if referral_id is None:
        return "ignored_no_referral"

    referral = db.scalar(select(Referral).where(Referral.id == referral_id).with_for_update())
    if referral is None:
        # A booking for a referral this domain does not know about is a real
        # inconsistency, but not one a consumer can fix by guessing.
        return "unknown_referral"
    if referral.state == ReferralState.CONFIRMED:
        return "already_confirmed"

    slot_id = _as_uuid(payload.get("slot_id"))
    try:
        if referral.state == ReferralState.WAITING_FOR_SLOT_SELECTION:
            transition(referral, ReferralState.BOOKING)
        if slot_id:
            referral.selected_slot_id = slot_id
        transition(referral, ReferralState.CONFIRMED)
    except InvalidTransition:
        # The referral moved somewhere the appointment cannot justify, e.g. it
        # was cancelled. Leave it for a human rather than forcing the state.
        return "transition_refused"

    audit(db, referral.id, "booking_reconciled", {"slot_id": str(slot_id), "source": "appointment.booked"})
    return "reconciled"


HANDLERS = {
    EventType.APPOINTMENT_BOOKED.value: handle_appointment_booked,
}


def consume_one(db: Session, event_id, event_type: str, payload: dict, traceparent: str | None) -> str:
    """Handle one event and record it as consumed, in a single transaction."""
    with tracer().start_as_current_span("careroute.event.consume", links=_link_from(traceparent)) as span:
        set_span_attributes(span, {"careroute.event.type": event_type})
        if _already_consumed(db, event_id):
            record_event_consumed(event_type, "duplicate")
            return "duplicate"

        handler = HANDLERS.get(event_type)
        outcome = handler(db, payload) if handler else "no_handler"

        db.add(ConsumedEvent(event_id=event_id, event_type=event_type, consumer=GROUP, outcome=outcome))
        try:
            db.commit()
        except IntegrityError:
            # Another consumer won the same event between the check and the
            # commit. Its work stands; ours is discarded.
            db.rollback()
            record_event_consumed(event_type, "duplicate")
            return "duplicate"

        set_span_attributes(span, {"careroute.event.outcome": outcome})
        record_event_consumed(event_type, outcome)
        return outcome


def poll(db_factory, client: Redis, count: int = 50, block_ms: int = 2000) -> dict[str, int]:
    """One read-handle-ack cycle over the consumer group."""
    try:
        batches = client.xreadgroup(GROUP, CONSUMER_NAME, {STREAM: ">"}, count=count, block=block_ms)
    except RedisError as exc:
        logger.warning("consumer could not read from the broker: %s", exc)
        return {}

    outcomes: dict[str, int] = {}
    for _stream, entries in batches or []:
        for entry_id, fields in entries:
            try:
                event_id = uuid.UUID(fields["event_id"])
                payload = json.loads(fields.get("payload") or "{}")
                with db_factory() as db:
                    outcome = consume_one(db, event_id, fields["event_type"], payload, fields.get("traceparent") or None)
            except Exception:
                # Acking a poison message would lose it silently; leaving it
                # pending keeps it visible in XPENDING for a human.
                logger.exception("event %s could not be handled; leaving it pending", entry_id)
                continue
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            client.xack(STREAM, GROUP, entry_id)
    return outcomes


def main() -> None:  # pragma: no cover - process entry point
    import time

    from .database import SessionLocal

    logging.basicConfig(level=logging.INFO)
    client = redis_client()
    ensure_group(client)
    logger.info("consumer started; stream=%s group=%s", STREAM, GROUP)
    while True:
        try:
            outcomes = poll(SessionLocal, client)
            if outcomes:
                logger.info("consumed %s", outcomes)
        except Exception:
            logger.exception("consumer pass failed; retrying")
            time.sleep(settings.relay_interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    main()
