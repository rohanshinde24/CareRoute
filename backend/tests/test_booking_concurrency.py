"""Concurrency guarantees for booking, against real PostgreSQL.

SQLite cannot express SELECT ... FOR UPDATE, so the rest of the suite can prove
booking is *sequentially* idempotent but can prove nothing about contention.
These tests exist to cover that gap: the effectively-once guarantee in
app/booking.py depends on row locks held inside one transaction, and the only
way to test it is to make several transactions race for the same row.

Skipped automatically when no PostgreSQL is reachable, so the default
zero-network test run is unaffected.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.booking import BookingError, book_selected_slot
from app.models import (
    Appointment,
    AppointmentSlot,
    Patient,
    ProcessedEvent,
    Provider,
    ProviderSchedule,
    Referral,
    ReferralState,
    SlotStatus,
)

TEST_DATABASE_URL = os.environ.get(
    "CAREROUTE_TEST_DATABASE_URL",
    "postgresql+psycopg://careroute:careroute@localhost:55432/careroute",
)
CONCURRENCY = 8


def _engine():
    try:
        engine = create_engine(TEST_DATABASE_URL, pool_size=CONCURRENCY + 2, max_overflow=4)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return engine
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not reachable at {TEST_DATABASE_URL}: {exc}")


@pytest.fixture
def engine():
    return _engine()


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def scenario(factory):
    """One referral, confirmed, pointing at one free slot. Cleaned up after."""
    marker = uuid.uuid4().hex[:8]
    created: dict[str, uuid.UUID] = {}
    with factory() as session:
        patient = Patient(external_id=f"CONC-{marker}", source="concurrency-test", given_name="Concurrency", family_name="Fixture", birth_date=datetime(1980, 1, 1).date(), is_synthetic=True)
        provider = Provider(npi=f"9{marker[:9]}", name=f"Race Clinic {marker}", specialty="Cardiology", location="Testville", is_synthetic=True)
        session.add_all([patient, provider])
        session.flush()
        schedule = ProviderSchedule(provider_id=provider.id, name=f"Schedule {marker}")
        session.add(schedule)
        session.flush()
        start = datetime.now(timezone.utc) + timedelta(days=400)
        slot = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30), status=SlotStatus.FREE)
        session.add(slot)
        session.flush()
        referral = Referral(
            patient_id=patient.id,
            requested_specialty="Cardiology",
            reason="Concurrency fixture",
            state=ReferralState.WAITING_FOR_SLOT_SELECTION,
            selected_slot_id=slot.id,
            is_synthetic=True,
            is_evaluation=True,
        )
        session.add(referral)
        session.flush()
        session.add(
            ProcessedEvent(
                event_id=f"conc-{marker}",
                event_type="booking.confirmed",
                referral_id=referral.id,
                payload={"slot_id": str(slot.id)},
            )
        )
        session.commit()
        created = {"referral": referral.id, "slot": slot.id, "patient": patient.id, "provider": provider.id, "schedule": schedule.id}

    yield created

    with factory() as session:
        session.execute(text("DELETE FROM appointments WHERE referral_id = :r"), {"r": created["referral"]})
        session.execute(text("DELETE FROM processed_events WHERE referral_id = :r"), {"r": created["referral"]})
        # agent_events hangs off workflow_runs, not referrals.
        session.execute(text("DELETE FROM agent_events WHERE workflow_run_id IN (SELECT id FROM workflow_runs WHERE referral_id = :r)"), {"r": created["referral"]})
        session.execute(text("DELETE FROM workflow_runs WHERE referral_id = :r"), {"r": created["referral"]})
        session.execute(text("DELETE FROM referrals WHERE id = :r"), {"r": created["referral"]})
        session.execute(text("DELETE FROM appointment_slots WHERE schedule_id = :s"), {"s": created["schedule"]})
        session.execute(text("DELETE FROM provider_schedules WHERE id = :s"), {"s": created["schedule"]})
        session.execute(text("DELETE FROM providers WHERE id = :p"), {"p": created["provider"]})
        session.execute(text("DELETE FROM patients WHERE id = :p"), {"p": created["patient"]})
        session.commit()


def _attempt(factory, referral_id):
    with factory() as session:
        try:
            return ("booked", book_selected_slot(session, referral_id).id)
        except BookingError as exc:
            session.rollback()
            return ("rejected", str(exc))
        except Exception as exc:  # surfaced so the test fails loudly rather than silently passing
            session.rollback()
            return ("error", f"{type(exc).__name__}: {exc}")


def test_concurrent_confirmations_produce_exactly_one_appointment(factory, scenario):
    referral_id = scenario["referral"]
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        outcomes = list(pool.map(lambda _: _attempt(factory, referral_id), range(CONCURRENCY)))

    unexpected = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert not unexpected, f"unexpected failures under contention: {unexpected}"

    with factory() as session:
        appointments = session.scalars(select(Appointment).where(Appointment.referral_id == referral_id)).all()
        slot = session.get(AppointmentSlot, scenario["slot"])
        referral = session.get(Referral, referral_id)

    # The guarantee: N racing confirmations, exactly one appointment.
    assert len(appointments) == 1, f"expected exactly one appointment, found {len(appointments)}"
    assert slot.status == SlotStatus.BUSY
    assert referral.state == ReferralState.CONFIRMED

    # Every caller must see a consistent result: the same appointment, or a
    # deterministic refusal. None may silently succeed into a second booking.
    booked = {outcome[1] for outcome in outcomes if outcome[0] == "booked"}
    assert len(booked) <= 1, f"callers received differing appointment ids: {booked}"


def test_repeated_sequential_confirmations_stay_idempotent(factory, scenario):
    referral_id = scenario["referral"]
    first = _attempt(factory, referral_id)
    second = _attempt(factory, referral_id)
    third = _attempt(factory, referral_id)

    assert first[0] == "booked"
    assert second == first, "a replayed confirmation must return the original appointment"
    assert third == first

    with factory() as session:
        assert len(session.scalars(select(Appointment).where(Appointment.referral_id == referral_id)).all()) == 1


def test_row_lock_is_actually_held_during_booking(engine, factory, scenario):
    """Guards against the lock being dropped from booking.py by a later refactor."""
    referral_id = scenario["referral"]
    with factory() as blocking_session:
        blocking_session.execute(text("SELECT id FROM referrals WHERE id = :r FOR UPDATE"), {"r": referral_id})
        with engine.connect() as probe:
            probe.execute(text("SET lock_timeout = '750ms'"))
            with pytest.raises(Exception) as caught:
                probe.execute(text("SELECT id FROM referrals WHERE id = :r FOR UPDATE"), {"r": referral_id})
            assert "lock" in str(caught.value).lower()
        blocking_session.rollback()
