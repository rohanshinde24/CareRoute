"""End-to-end proof that the reconciler recovers from genuinely lost events.

Every other test of the reconciler uses a fake gateway and a fake broker, which
proves the code runs but not that the safety net works. The reconciler exists for
exactly one situation - a booking committed in the provider domain whose event
never reached the consumer - and until this test nothing had ever produced that
situation for real.

The loss here is deliberate and deterministic: the relay publishes the event and
then it is removed from the stream before any consumer reads it. That stands in
for Redis's one-second AOF window, or a broker that dropped the entry, without
having to crash anything.

Requires both PostgreSQL databases and Redis. Skips otherwise, and fails rather
than skips when CAREROUTE_REQUIRE_POSTGRES is set, so CI cannot pass by not
running it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import consumer, relay
from app.models import AgentEvent, Patient, Referral, ReferralOutbox, ReferralState, WorkflowRun
from app.provider_contracts import BookingRequest
from app.provider_gateway import LocalProviderGateway
from app.provider_models import (
    Appointment,
    AppointmentSlot,
    Provider,
    ProviderOutbox,
    ProviderSchedule,
    SlotStatus,
)
from app.provider_queries import book_slot

REFERRAL_URL = os.environ.get(
    "CAREROUTE_TEST_DATABASE_URL",
    "postgresql+psycopg://careroute:careroute@localhost:55432/careroute",
)
PROVIDER_URL = os.environ.get(
    "CAREROUTE_TEST_PROVIDER_DATABASE_URL",
    "postgresql+psycopg://careroute:careroute@localhost:55433/careroute_provider",
)
REDIS_URL = os.environ.get("CAREROUTE_TEST_REDIS_URL", "redis://localhost:6379/0")


def _unavailable(reason: str):
    if os.environ.get("CAREROUTE_REQUIRE_POSTGRES"):
        pytest.fail(reason)
    pytest.skip(reason)


def _factory(url: str, label: str):
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return sessionmaker(bind=engine, expire_on_commit=False)
    except Exception as exc:  # pragma: no cover - environment dependent
        _unavailable(f"{label} not reachable at {url}: {exc}")


@pytest.fixture
def referral_factory():
    return _factory(REFERRAL_URL, "referral PostgreSQL")


@pytest.fixture
def provider_factory():
    return _factory(PROVIDER_URL, "provider PostgreSQL")


@pytest.fixture
def broker():
    from redis.exceptions import RedisError

    client = relay.redis_client(REDIS_URL)
    try:
        client.ping()
    except RedisError as exc:  # pragma: no cover - environment dependent
        _unavailable(f"Redis not reachable at {REDIS_URL}: {exc}")
    return client


@pytest.fixture
def scenario(referral_factory, provider_factory):
    """A referral awaiting booking, and a free slot it can be booked into."""
    marker = uuid.uuid4().hex[:8]
    created: dict = {"marker": marker}

    with provider_factory() as pdb:
        provider = Provider(npi=f"5{marker[:9]}", name=f"Loss Clinic {marker}", specialty="Cardiology", location="Testville", is_synthetic=True, is_evaluation=True)
        pdb.add(provider)
        pdb.flush()
        schedule = ProviderSchedule(provider_id=provider.id, name=f"Loss {marker}", timezone="UTC")
        pdb.add(schedule)
        pdb.flush()
        start = datetime.now(timezone.utc) + timedelta(days=1200)
        slot = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30), status=SlotStatus.FREE)
        pdb.add(slot)
        pdb.commit()
        created.update(provider=provider.id, schedule=schedule.id, slot=slot.id)

    with referral_factory() as rdb:
        patient = Patient(external_id=f"loss-{marker}", source="loss-test", given_name="Loss", family_name="Fixture", birth_date=date(1985, 1, 1), is_synthetic=True)
        rdb.add(patient)
        rdb.flush()
        referral = Referral(
            patient_id=patient.id,
            requested_specialty="Cardiology",
            reason="Synthetic event-loss fixture",
            state=ReferralState.WAITING_FOR_SLOT_SELECTION,
            selected_slot_id=created["slot"],
            is_synthetic=True,
            is_evaluation=True,
        )
        rdb.add(referral)
        rdb.flush()
        rdb.add(WorkflowRun(referral_id=referral.id, state=referral.state))
        # Age it past the reconciler's grace window; a referral booked seconds
        # ago is mid-flight, not stranded.
        referral.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        rdb.commit()
        created.update(referral=referral.id, patient=patient.id)

    yield created

    with referral_factory() as rdb:
        rid = created["referral"]
        rdb.execute(text("DELETE FROM agent_events WHERE workflow_run_id IN (SELECT id FROM workflow_runs WHERE referral_id = :r)"), {"r": rid})
        rdb.execute(text("DELETE FROM workflow_runs WHERE referral_id = :r"), {"r": rid})
        rdb.execute(text("DELETE FROM referral_outbox WHERE payload->>'referral_id' = :r"), {"r": str(rid)})
        rdb.execute(text("DELETE FROM referrals WHERE id = :r"), {"r": rid})
        rdb.execute(text("DELETE FROM patients WHERE id = :p"), {"p": created["patient"]})
        rdb.commit()
    with provider_factory() as pdb:
        sid = created["schedule"]
        pdb.execute(text("DELETE FROM provider_outbox WHERE payload->>'slot_id' = :s"), {"s": str(created["slot"])})
        pdb.execute(text("DELETE FROM booking_attempts WHERE slot_id = :s"), {"s": created["slot"]})
        pdb.execute(text("DELETE FROM appointments WHERE slot_id = :s"), {"s": created["slot"]})
        pdb.execute(text("DELETE FROM appointment_slots WHERE schedule_id = :s"), {"s": sid})
        pdb.execute(text("DELETE FROM provider_schedules WHERE id = :s"), {"s": sid})
        pdb.execute(text("DELETE FROM providers WHERE id = :p"), {"p": created["provider"]})
        pdb.commit()


def _book(provider_factory, scenario) -> uuid.UUID:
    """Book in the provider domain. The event lands in its outbox."""
    with provider_factory() as pdb:
        result = book_slot(
            pdb,
            BookingRequest(
                referral_id=scenario["referral"],
                slot_id=scenario["slot"],
                requested_specialty="Cardiology",
                idempotency_key=f"loss:{scenario['marker']}",
            ),
        )
    assert result.outcome == "booked"
    return result.appointment_id


def _publish_then_lose(provider_factory, broker) -> int:
    """Relay the event to the real stream, then remove it before anyone reads.

    This is the failure the reconciler exists for, made deterministic: the event
    was genuinely published and is genuinely gone.
    """
    before = broker.xlen(relay.STREAM)
    with provider_factory() as pdb:
        delivered = relay.drain(pdb, ProviderOutbox, broker)
    assert delivered >= 1, "the relay should have published the booking event"

    entries = broker.xrevrange(relay.STREAM, count=delivered)
    removed = 0
    for entry_id, _fields in entries:
        removed += broker.xdel(relay.STREAM, entry_id)
    assert removed >= 1, "the event must actually be gone from the stream"
    assert broker.xlen(relay.STREAM) <= before
    return removed


def test_the_reconciler_recovers_a_booking_whose_event_was_lost(referral_factory, provider_factory, broker, scenario):
    appointment_id = _book(provider_factory, scenario)
    _publish_then_lose(provider_factory, broker)

    # Give the real consumer its chance. It must find nothing, because the event
    # is genuinely gone - without this step the deletion would be ceremonial and
    # the test would only be proving that no consumer happened to run.
    consumer.ensure_group(broker)
    consumed = consumer.poll(referral_factory, broker, block_ms=100)
    assert consumed.get("reconciled", 0) == 0, f"the consumer should have found nothing, got {consumed}"

    # The referral is now stranded: a real appointment exists in one database
    # and the other does not know about it. This is the exact inconsistency the
    # domain split made possible.
    with referral_factory() as rdb:
        stranded = rdb.get(Referral, scenario["referral"])
        assert stranded.state is ReferralState.WAITING_FOR_SLOT_SELECTION
    with provider_factory() as pdb:
        assert pdb.get(Appointment, appointment_id) is not None

    with provider_factory() as pdb:
        gateway = LocalProviderGateway(pdb)
        outcomes = asyncio.run(relay_free_reconcile(referral_factory, gateway))

    assert outcomes.get("reconciled", 0) >= 1, f"the reconciler should have repaired it, got {outcomes}"

    with referral_factory() as rdb:
        repaired = rdb.get(Referral, scenario["referral"])
        assert repaired.state is ReferralState.CONFIRMED
        assert repaired.selected_slot_id == scenario["slot"]

        run = rdb.scalar(select(WorkflowRun).where(WorkflowRun.referral_id == scenario["referral"]))
        audit = rdb.scalars(select(AgentEvent).where(AgentEvent.workflow_run_id == run.id)).all()
        sources = [event.payload.get("source") for event in audit if event.event_type == "booking_reconciled"]
        assert "reconciler" in sources, "the repair must be attributable to the reconciler, not to an event"

    # And it never invents a second appointment.
    with provider_factory() as pdb:
        appointments = pdb.scalars(select(Appointment).where(Appointment.slot_id == scenario["slot"])).all()
        assert len(appointments) == 1


def relay_free_reconcile(referral_factory, gateway):
    """Run one reconciler pass with no grace period, for test determinism."""
    from app import reconciler

    return reconciler.run_once(referral_factory, gateway, older_than_seconds=1.0)


def test_a_second_reconciler_pass_changes_nothing(referral_factory, provider_factory, broker, scenario):
    """Repair must be idempotent.

    The reconciler runs on a timer, so it will see the same referral again the
    moment anything delays the sweep. A second pass must be a no-op rather than
    a second transition.
    """
    _book(provider_factory, scenario)
    _publish_then_lose(provider_factory, broker)

    with provider_factory() as pdb:
        gateway = LocalProviderGateway(pdb)
        first = asyncio.run(relay_free_reconcile(referral_factory, gateway))
        second = asyncio.run(relay_free_reconcile(referral_factory, gateway))

    assert first.get("reconciled", 0) >= 1
    assert second.get("reconciled", 0) == 0, f"a repaired referral must not be repaired again, got {second}"

    with referral_factory() as rdb:
        assert rdb.get(Referral, scenario["referral"]).state is ReferralState.CONFIRMED
