"""Consumer and reconciler guarantees.

The properties that matter are the ones at-least-once delivery forces on you:
a redelivered event must be a no-op, a lost event must still get repaired, and
neither path may invent state the provider domain never recorded.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import consumer, reconciler
from app.events import EventType
from app.models import ConsumedEvent, Patient, Referral, ReferralState, WorkflowRun
from app.provider_contracts import AppointmentListResult, AppointmentRecord


def _referral(db, state=ReferralState.WAITING_FOR_SLOT_SELECTION, slot_id=None, age_seconds=0):
    person = Patient(external_id=f"c-{uuid.uuid4().hex[:8]}", source="test", given_name="C", family_name="T", birth_date=date(1990, 1, 1), is_synthetic=True)
    db.add(person)
    db.flush()
    referral = Referral(
        patient_id=person.id,
        requested_specialty="Cardiology",
        reason="Synthetic consumer test",
        state=state,
        selected_slot_id=slot_id or uuid.uuid4(),
        is_synthetic=True,
    )
    db.add(referral)
    db.flush()
    db.add(WorkflowRun(referral_id=referral.id, state=referral.state))
    if age_seconds:
        referral.updated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    db.commit()
    return referral


class FakeGateway:
    """Only the one call the reconciler makes."""

    def __init__(self, appointments):
        self.appointments = appointments
        self.calls = 0

    async def appointments_for_referral(self, referral_id, correlation_id):
        self.calls += 1
        return AppointmentListResult(items=self.appointments)


def _appointment(referral_id, slot_id, status="BOOKED"):
    return AppointmentRecord(id=uuid.uuid4(), referral_id=referral_id, slot_id=slot_id, status=status, created_at=datetime.now(timezone.utc))


# --- span links -------------------------------------------------------------

def test_a_valid_traceparent_becomes_a_link_not_a_parent():
    """Nesting under the producer would report queue dwell time as duration."""
    links = consumer._link_from("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")

    assert len(links) == 1
    assert links[0].context.trace_id == int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
    assert links[0].context.is_remote is True


def test_a_malformed_traceparent_never_blocks_consumption():
    """Correlation is a convenience; delivery is not."""
    for bad in (None, "", "garbage", "99-abc-def-01", "00-notahex-00f067aa0ba902b7-01"):
        assert consumer._link_from(bad) == []


# --- idempotency ------------------------------------------------------------

def test_a_redelivered_event_is_a_no_op(db):
    referral = _referral(db)
    event_id = uuid.uuid4()
    payload = {"referral_id": str(referral.id), "slot_id": str(referral.selected_slot_id)}

    first = consumer.consume_one(db, event_id, EventType.APPOINTMENT_BOOKED.value, payload, None)
    second = consumer.consume_one(db, event_id, EventType.APPOINTMENT_BOOKED.value, payload, None)

    assert first == "reconciled"
    assert second == "duplicate"
    assert len(list(db.scalars(select(ConsumedEvent).where(ConsumedEvent.event_id == event_id)))) == 1
    assert db.get(Referral, referral.id).state == ReferralState.CONFIRMED


def test_the_effect_and_the_dedup_row_commit_together(db):
    """An event marked consumed whose effect did not land would be lost forever."""
    referral = _referral(db)
    event_id = uuid.uuid4()

    consumer.consume_one(db, event_id, EventType.APPOINTMENT_BOOKED.value, {"referral_id": str(referral.id)}, None)

    assert db.get(Referral, referral.id).state == ReferralState.CONFIRMED
    assert db.scalar(select(ConsumedEvent).where(ConsumedEvent.event_id == event_id)) is not None


def test_an_unknown_event_type_is_recorded_rather_than_retried_forever(db):
    outcome = consumer.consume_one(db, uuid.uuid4(), "something.unrecognised", {}, None)
    assert outcome == "no_handler"


# --- forward-only repair ----------------------------------------------------

def test_a_cancelled_referral_is_never_revived_by_a_late_event(db):
    """A booking event arriving after cancellation must not resurrect it."""
    referral = _referral(db, state=ReferralState.CANCELLED)

    outcome = consumer.consume_one(db, uuid.uuid4(), EventType.APPOINTMENT_BOOKED.value, {"referral_id": str(referral.id)}, None)

    assert outcome == "transition_refused"
    assert db.get(Referral, referral.id).state == ReferralState.CANCELLED


def test_an_event_for_an_unknown_referral_is_not_guessed_at(db):
    outcome = consumer.consume_one(db, uuid.uuid4(), EventType.APPOINTMENT_BOOKED.value, {"referral_id": str(uuid.uuid4())}, None)
    assert outcome == "unknown_referral"


# --- reconciler -------------------------------------------------------------

def test_the_reconciler_repairs_a_referral_whose_event_was_lost(db):
    """The gap the domain split created: booking committed, state did not."""
    referral = _referral(db, state=ReferralState.BOOKING, age_seconds=300)
    gateway = FakeGateway([_appointment(referral.id, referral.selected_slot_id)])

    import asyncio

    outcome = asyncio.run(reconciler.repair(db, referral, gateway))

    assert outcome == "reconciled"
    assert db.get(Referral, referral.id).state == ReferralState.CONFIRMED


def test_the_reconciler_never_invents_a_booking(db):
    """No appointment means nothing to repair, not something to create."""
    referral = _referral(db, state=ReferralState.BOOKING, age_seconds=300)
    gateway = FakeGateway([])

    import asyncio

    outcome = asyncio.run(reconciler.repair(db, referral, gateway))

    assert outcome == "no_appointment"
    assert db.get(Referral, referral.id).state == ReferralState.BOOKING


def test_the_reconciler_ignores_a_cancelled_appointment(db):
    referral = _referral(db, state=ReferralState.BOOKING, age_seconds=300)
    gateway = FakeGateway([_appointment(referral.id, referral.selected_slot_id, status="CANCELLED")])

    import asyncio

    assert asyncio.run(reconciler.repair(db, referral, gateway)) == "no_appointment"


def test_the_reconciler_leaves_recent_referrals_alone(db):
    """A referral booked seconds ago is mid-flight, not stranded."""
    _referral(db, state=ReferralState.BOOKING, age_seconds=0)

    assert reconciler.candidates(db, older_than_seconds=60) == []


def test_the_reconciler_only_considers_states_a_booking_could_explain(db):
    _referral(db, state=ReferralState.CANCELLED, age_seconds=300)
    _referral(db, state=ReferralState.CONFIRMED, age_seconds=300)
    repairable = _referral(db, state=ReferralState.BOOKING, age_seconds=300)

    found = reconciler.candidates(db, older_than_seconds=60)

    assert [item.id for item in found] == [repairable.id]


def test_a_provider_outage_defers_repair_rather_than_guessing(db):
    from app.provider_gateway import ProviderGatewayTransientError

    referral = _referral(db, state=ReferralState.BOOKING, age_seconds=300)

    class Unavailable:
        async def appointments_for_referral(self, *_):
            raise ProviderGatewayTransientError("provider down")

    import asyncio

    outcome = asyncio.run(reconciler.repair(db, referral, Unavailable()))

    assert outcome == "provider_unavailable"
    assert db.get(Referral, referral.id).state == ReferralState.BOOKING
