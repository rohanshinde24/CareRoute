"""Outbox and relay guarantees.

The properties worth testing are the ones that distinguish an outbox from just
publishing: the event commits with the state change or not at all, a broker
outage delays delivery instead of breaking anything, and a crash between
publishing and marking produces a duplicate rather than a silence.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from app import relay
from app.events import (
    ALLOWED_PAYLOAD_KEYS,
    EventType,
    PayloadNotAllowed,
    build_event,
    envelope_from_row,
    validate_payload,
)
from app.models import ReferralOutbox
from app.provider_contracts import BookingRequest
from app.provider_models import Appointment, ProviderOutbox


class FakeRedis:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.published: list[dict] = []

    def xadd(self, stream, fields):
        if self.fail:
            raise RedisConnectionError("broker unavailable")
        self.published.append({"stream": stream, **fields})
        return f"{len(self.published)}-0"


def _row(db, **overrides):
    values = build_event(EventType.REFERRAL_CONFIRMED, "careroute-api", {"referral_id": uuid.uuid4()})
    values.update(overrides)
    row = ReferralOutbox(**values)
    db.add(row)
    db.commit()
    return row


def test_payload_allowlist_keeps_referral_content_out_of_events():
    """Events are persisted and cross a service boundary, so they carry the same
    constraint as spans and metric labels."""
    for leaky in ("referral_reason", "patient_name", "member_id", "document_type", "prompt"):
        with pytest.raises(PayloadNotAllowed):
            validate_payload({leaky: "something"})
    assert "referral_reason" not in ALLOWED_PAYLOAD_KEYS
    assert validate_payload({"referral_id": uuid.uuid4()})["referral_id"]


def test_an_envelope_carries_schema_version_and_trace_context(db):
    row = _row(db, traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    envelope = envelope_from_row(row)

    assert envelope.schema_version == 1
    assert envelope.event_type is EventType.REFERRAL_CONFIRMED
    assert envelope.traceparent.startswith("00-")
    fields = envelope.redis_fields()
    assert set(fields) == {"event_id", "event_type", "schema_version", "producer", "occurred_at", "traceparent", "payload"}
    assert all(isinstance(value, str) for value in fields.values())


def test_the_relay_publishes_then_marks_dispatched(db):
    row = _row(db)
    client = FakeRedis()

    delivered = relay.drain(db, ReferralOutbox, client)

    assert delivered == 1
    assert len(client.published) == 1
    assert db.get(ReferralOutbox, row.id).dispatched_at is not None
    assert db.get(ReferralOutbox, row.id).dispatch_attempts == 1


def test_a_dispatched_event_is_never_published_twice(db):
    _row(db)
    client = FakeRedis()

    assert relay.drain(db, ReferralOutbox, client) == 1
    assert relay.drain(db, ReferralOutbox, client) == 0
    assert len(client.published) == 1


def test_a_broker_outage_leaves_the_row_undispatched_and_raises_nothing(db):
    """The relay runs outside the request path so an outage delays delivery.

    Nothing user-facing fails, and the row stays claimable for the next pass.
    """
    row = _row(db)
    client = FakeRedis(fail=True)

    delivered = relay.drain(db, ReferralOutbox, client)

    assert delivered == 0
    assert db.get(ReferralOutbox, row.id).dispatched_at is None

    # ...and the same rows go out once the broker returns.
    recovered = FakeRedis()
    assert relay.drain(db, ReferralOutbox, recovered) == 1
    assert len(recovered.published) == 1


def test_backlog_reports_depth_and_the_age_of_the_oldest_row(db):
    stale = datetime.now(timezone.utc) - timedelta(minutes=30)
    _row(db, occurred_at=stale)
    _row(db)

    depth, age = relay.backlog(db, ReferralOutbox)

    assert depth == 2
    # Age matters more than depth: one row stuck for half an hour is the signal.
    assert age > 1500


def test_an_empty_outbox_reports_no_age(db):
    assert relay.backlog(db, ReferralOutbox) == (0, None)


def test_booking_writes_its_event_in_the_same_transaction(provider_db):
    """The event and the appointment commit together, or neither does."""
    from tests.test_booking_concurrency import _request  # reuse the request builder

    from app.provider_models import AppointmentSlot, Provider, ProviderSchedule, SlotStatus
    from app.provider_queries import book_slot

    provider = Provider(name="Outbox Clinic", specialty="Cardiology", location="Test, CA", is_synthetic=True)
    provider_db.add(provider)
    provider_db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name="S", timezone="UTC")
    provider_db.add(schedule)
    provider_db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=3)
    slot = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30), status=SlotStatus.FREE)
    provider_db.add(slot)
    provider_db.commit()

    result = book_slot(provider_db, _request(slot.id))

    assert result.outcome == "booked"
    events = list(provider_db.scalars(select(ProviderOutbox)))
    assert len(events) == 1
    assert events[0].event_type == EventType.APPOINTMENT_BOOKED.value
    assert events[0].dispatched_at is None, "a freshly written event must not be pre-marked as delivered"
    assert events[0].payload["appointment_id"] == str(result.appointment_id)
    assert set(events[0].payload) <= ALLOWED_PAYLOAD_KEYS
    assert len(list(provider_db.scalars(select(Appointment)))) == 1


def test_a_refused_booking_also_produces_an_event(provider_db):
    """Refusals are announced too; a consumer that only heard about successes
    could not tell a refusal from a lost message."""
    from tests.test_booking_concurrency import _request

    result = __import__("app.provider_queries", fromlist=["book_slot"]).book_slot(provider_db, _request(uuid.uuid4()))

    assert result.outcome == "slot_not_found"
    events = list(provider_db.scalars(select(ProviderOutbox)))
    assert len(events) == 1
    assert events[0].event_type == EventType.BOOKING_REFUSED.value


def test_an_event_written_without_an_active_span_simply_has_no_trace_context():
    """Absence of a span is not an error.

    Events are produced from CLI entry points and background work as well as
    from requests, and an envelope without a traceparent is still a valid event.
    """
    from app.telemetry import current_traceparent

    assert current_traceparent() is None
    row = build_event(EventType.REFERRAL_CONFIRMED, "careroute-api", {"referral_id": uuid.uuid4()}, current_traceparent())
    assert row["traceparent"] is None
    assert envelope_from_row(type("R", (), row)).traceparent is None
