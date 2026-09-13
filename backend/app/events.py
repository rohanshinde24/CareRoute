"""Domain event envelopes and the transactional outbox.

The outbox exists because a database commit followed by a separate publish is
not atomic. Either the state change lands and the publish fails, leaving a
silently missing event, or the publish succeeds and the commit is rolled back,
announcing something that never happened. Writing the event to a table inside
the same transaction makes "the change happened" and "the event exists"
inseparable; delivery becomes a later, retryable problem rather than a
correctness one.

Envelopes carry the producing operation's W3C traceparent so a consumer can link
its work back to the request that caused it. Consumers link rather than nest:
queue dwell time is not operation duration, and a child span would report it as
though it were.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

SCHEMA_VERSION = 1

# Event payloads cross a service boundary and are persisted, so they carry the
# same privacy constraint as spans and metric labels: identifiers needed for
# correlation are allowed, the content of a referral is not.
ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "appointment_id",
        "idempotency_key",
        "outcome",
        "provider_id",
        "referral_id",
        "slot_id",
        "state",
    }
)


class EventType(str, enum.Enum):
    APPOINTMENT_BOOKED = "appointment.booked"
    BOOKING_REFUSED = "booking.refused"
    REFERRAL_CONFIRMED = "referral.confirmed"


class PayloadNotAllowed(ValueError):
    """Raised when an event carries a field outside the allowlist."""


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID
    event_type: EventType
    schema_version: int = SCHEMA_VERSION
    producer: str
    occurred_at: datetime
    traceparent: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def redis_fields(self) -> dict[str, str]:
        """Flatten for XADD. Redis stream fields are strings."""
        import json

        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type.value,
            "schema_version": str(self.schema_version),
            "producer": self.producer,
            "occurred_at": self.occurred_at.isoformat(),
            "traceparent": self.traceparent or "",
            "payload": json.dumps(self.payload, sort_keys=True),
        }


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    extra = set(payload) - ALLOWED_PAYLOAD_KEYS
    if extra:
        raise PayloadNotAllowed(f"event payload carries fields outside the allowlist: {sorted(extra)}")
    return {key: (str(value) if isinstance(value, uuid.UUID) else value) for key, value in payload.items()}


class OutboxMixin:
    """Columns shared by both domains' outbox tables.

    Each database has its own outbox because each must write events in its own
    transaction; a shared outbox would reintroduce exactly the cross-database
    write this design exists to avoid.
    """

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[int] = mapped_column(Integer, default=SCHEMA_VERSION)
    producer: Mapped[str] = mapped_column(String(60))
    traceparent: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Null until the relay has published it. Indexed because the relay's only
    # query is "the oldest undispatched rows".
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=0)


def build_event(event_type: EventType, producer: str, payload: dict[str, Any], traceparent: str | None = None) -> dict[str, Any]:
    """Row values for an outbox insert. Caller supplies the session and the transaction."""
    return {
        "id": uuid.uuid4(),
        "event_type": event_type.value,
        "schema_version": SCHEMA_VERSION,
        "producer": producer,
        "traceparent": traceparent,
        "payload": validate_payload(payload),
        "occurred_at": datetime.now(timezone.utc),
        "dispatched_at": None,
        "dispatch_attempts": 0,
    }


def envelope_from_row(row) -> EventEnvelope:
    return EventEnvelope(
        event_id=row.id,
        event_type=EventType(row.event_type),
        schema_version=row.schema_version,
        producer=row.producer,
        occurred_at=row.occurred_at,
        traceparent=row.traceparent,
        payload=row.payload or {},
    )
