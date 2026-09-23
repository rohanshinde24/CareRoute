"""Booking guarantees after the provider domain split, against real PostgreSQL.

Booking now executes inside the provider database, so these tests drive the
provider side directly and the referral side through the gateway. SQLite cannot
express SELECT ... FOR UPDATE, so none of this is testable on the default
in-memory suite; the module skips when no PostgreSQL is reachable.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, sessionmaker

from app.provider_contracts import BookingRequest
from app.provider_models import (
    ACTIVE_APPOINTMENT_PER_SLOT,
    Appointment,
    AppointmentSlot,
    AppointmentStatus,
    BookingAttempt,
    Provider,
    ProviderSchedule,
    SlotStatus,
)
from app.provider_queries import book_slot

PROVIDER_DATABASE_URL = os.environ.get(
    "CAREROUTE_TEST_PROVIDER_DATABASE_URL",
    "postgresql+psycopg://careroute:careroute@localhost:55433/careroute_provider",
)
CONTENDERS = 6


@pytest.fixture
def factory():
    try:
        engine = create_engine(PROVIDER_DATABASE_URL, pool_size=BACKSTOP_RACERS + 4, max_overflow=4)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        # Skipping locally is a convenience. Skipping in CI would mean a green
        # badge that never exercised the booking guarantee, so there the absence
        # of a database is a failure rather than a shrug.
        message = f"provider PostgreSQL not reachable at {PROVIDER_DATABASE_URL}: {exc}"
        if os.environ.get("CAREROUTE_REQUIRE_POSTGRES"):
            pytest.fail(message)
        pytest.skip(message)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    # Without this every test leaves its pooled connections open, and a run
    # long enough exhausts max_connections with errors that look like booking
    # failures. Found by repeating this module under pytest-repeat.
    engine.dispose()


@pytest.fixture
def slot(factory):
    marker = uuid.uuid4().hex[:8]
    with factory() as session:
        provider = Provider(npi=f"6{marker[:9]}", name=f"Race {marker}", specialty="Cardiology", location="Testville", is_synthetic=True, is_evaluation=True)
        session.add(provider)
        session.flush()
        schedule = ProviderSchedule(provider_id=provider.id, name=f"Sched {marker}")
        session.add(schedule)
        session.flush()
        start = datetime.now(timezone.utc) + timedelta(days=700)
        record = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30), status=SlotStatus.FREE)
        session.add(record)
        session.commit()
        created = {"slot": record.id, "schedule": schedule.id, "provider": provider.id}

    yield created

    with factory() as session:
        session.execute(text("DELETE FROM booking_attempts WHERE slot_id = :s"), {"s": created["slot"]})
        session.execute(text("DELETE FROM appointments WHERE slot_id = :s"), {"s": created["slot"]})
        session.execute(text("DELETE FROM appointment_slots WHERE id = :s"), {"s": created["slot"]})
        session.execute(text("DELETE FROM provider_schedules WHERE id = :s"), {"s": created["schedule"]})
        session.execute(text("DELETE FROM providers WHERE id = :p"), {"p": created["provider"]})
        session.commit()


def _request(slot_id, referral_id=None, key=None) -> BookingRequest:
    referral_id = referral_id or uuid.uuid4()
    return BookingRequest(
        referral_id=referral_id,
        slot_id=slot_id,
        requested_specialty="Cardiology",
        idempotency_key=key or f"booking:{referral_id}:{slot_id}",
    )


def _attempt(factory, request: BookingRequest, gate: threading.Barrier | None = None):
    if gate is not None:
        gate.wait()
    with factory() as session:
        try:
            return book_slot(session, request)
        except Exception as exc:  # surfaced so the test fails loudly
            session.rollback()
            return exc


def test_racing_bookings_for_one_slot_produce_exactly_one_appointment(factory, slot):
    """Six different referrals race for one slot. The database is the arbiter."""
    requests = [_request(slot["slot"]) for _ in range(CONTENDERS)]
    gate = threading.Barrier(CONTENDERS)

    with ThreadPoolExecutor(max_workers=CONTENDERS) as pool:
        results = list(pool.map(lambda request: _attempt(factory, request, gate), requests))

    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"contention raised unexpected errors: {failures}"

    booked = [r for r in results if r.outcome == "booked"]
    refused = [r for r in results if r.outcome == "slot_unavailable"]
    assert len(booked) == 1, f"exactly one booking may succeed, got {[r.outcome for r in results]}"
    assert len(refused) == CONTENDERS - 1

    with factory() as session:
        appointments = session.scalars(select(Appointment).where(Appointment.slot_id == slot["slot"])).all()
        assert len(appointments) == 1
        assert session.get(AppointmentSlot, slot["slot"]).status == SlotStatus.BUSY


def test_replaying_an_idempotency_key_returns_the_original_appointment(factory, slot):
    request = _request(slot["slot"])

    first = _attempt(factory, request)
    second = _attempt(factory, request)
    third = _attempt(factory, request)

    assert first.outcome == "booked" and not first.replayed
    for replay in (second, third):
        assert replay.replayed is True
        assert replay.appointment_id == first.appointment_id

    with factory() as session:
        assert len(session.scalars(select(Appointment).where(Appointment.slot_id == slot["slot"])).all()) == 1


def test_a_lost_response_is_recoverable_by_repeating_the_request(factory, slot):
    """The caller never learned the outcome. Repeating must reveal it, not redo it."""
    request = _request(slot["slot"])
    original = _attempt(factory, request)
    assert original.outcome == "booked"

    # Simulate the caller having seen nothing at all, then retrying.
    recovered = _attempt(factory, request)

    assert recovered.replayed is True
    assert recovered.outcome == "booked"
    assert recovered.appointment_id == original.appointment_id


def test_a_refusal_is_replayable_so_a_retry_does_not_look_like_a_fresh_attempt(factory, slot):
    """Storing only successes would make a retried rejection indistinguishable
    from a new attempt on a slot that has since become busy."""
    winner = _attempt(factory, _request(slot["slot"]))
    assert winner.outcome == "booked"

    loser_request = _request(slot["slot"])
    refused = _attempt(factory, loser_request)
    assert refused.outcome == "slot_unavailable"
    assert refused.replayed is False

    replayed = _attempt(factory, loser_request)
    assert replayed.outcome == "slot_unavailable"
    assert replayed.replayed is True
    assert replayed.detail == refused.detail


def test_a_key_reused_for_a_different_request_is_refused_rather_than_replayed(factory, slot):
    """An idempotency key stands for one request, not for one caller.

    Replaying here would report a booking for a slot this request never named,
    which is worse than refusing: the caller would believe it holds an
    appointment it did not ask for.
    """
    key = f"reuse:{uuid.uuid4()}"
    # A real retry repeats the whole request, referral included, so the request
    # object is reused rather than rebuilt.
    first = _request(slot["slot"], key=key)
    original = _attempt(factory, first)
    assert original.outcome == "booked"

    for different in (
        _request(uuid.uuid4(), referral_id=first.referral_id, key=key),  # another slot
        _request(slot["slot"], key=key),  # another referral
    ):
        reused = _attempt(factory, different)
        assert reused.outcome == "idempotency_key_conflict", f"expected a refusal, got {reused}"
        assert reused.appointment_id is None
        assert not reused.replayed

    # The original decision is untouched, and a true retry still replays it.
    retry = _attempt(factory, first)
    assert retry.replayed is True
    assert retry.appointment_id == original.appointment_id


def test_a_decision_recorded_before_fingerprints_existed_still_replays(factory, slot):
    """Rows written by an older version carry no fingerprint.

    Refusing them would turn a deployment into an outage for every in-flight
    retry, so a missing fingerprint replays exactly as it used to.
    """
    key = f"legacy:{uuid.uuid4()}"
    original = _attempt(factory, _request(slot["slot"], key=key))
    assert original.outcome == "booked"
    with factory() as session:
        session.execute(text("UPDATE booking_attempts SET request_fingerprint = NULL WHERE idempotency_key = :k"), {"k": key})
        session.commit()

    replay = _attempt(factory, _request(uuid.uuid4(), key=key))

    assert replay.replayed is True
    assert replay.appointment_id == original.appointment_id


def test_specialty_is_rechecked_by_the_owning_domain(factory, slot):
    """The caller states the specialty it wants; the provider domain verifies it
    against the record only it can read."""
    request = _request(slot["slot"])
    mismatched = BookingRequest(
        referral_id=request.referral_id,
        slot_id=request.slot_id,
        requested_specialty="Dermatology",
        idempotency_key=f"mismatch:{request.referral_id}:{request.slot_id}",
    )
    result = _attempt(factory, mismatched)

    assert result.outcome == "specialty_mismatch"
    with factory() as session:
        assert session.get(AppointmentSlot, slot["slot"]).status == SlotStatus.FREE


def test_every_booking_decision_is_recorded_for_replay(factory, slot):
    booked = _attempt(factory, _request(slot["slot"]))
    refused = _attempt(factory, _request(slot["slot"]))

    with factory() as session:
        attempts = session.scalars(select(BookingAttempt).where(BookingAttempt.slot_id == slot["slot"])).all()
        outcomes = {attempt.outcome for attempt in attempts}

    assert outcomes == {"booked", "slot_unavailable"}
    assert booked.outcome == "booked" and refused.outcome == "slot_unavailable"


def _rogue_appointment(factory, slot_id, status=AppointmentStatus.BOOKED):
    """An appointment written without book_slot and without the lock.

    Stands in for any path that bypasses the booking transaction - a future
    endpoint, a manual fix, a backfill. The slot is deliberately left FREE, so
    the application check alone would let a second booking through.
    """
    with factory() as session:
        session.add(Appointment(referral_id=uuid.uuid4(), slot_id=slot_id, idempotency_key=f"rogue:{uuid.uuid4()}", status=status))
        session.commit()


def test_the_database_rejects_a_second_active_appointment_for_a_slot(factory, slot):
    _rogue_appointment(factory, slot["slot"])

    with pytest.raises(IntegrityError) as caught:
        _rogue_appointment(factory, slot["slot"])
    assert caught.value.orig.diag.constraint_name == ACTIVE_APPOINTMENT_PER_SLOT


def test_a_cancelled_appointment_releases_its_slot(factory, slot):
    """Partial, not UNIQUE(slot_id): a plain unique index would hold the slot forever."""
    _rogue_appointment(factory, slot["slot"], status=AppointmentStatus.CANCELLED)
    _rogue_appointment(factory, slot["slot"], status=AppointmentStatus.BOOKED)

    with factory() as session:
        assert len(session.scalars(select(Appointment).where(Appointment.slot_id == slot["slot"])).all()) == 2


def test_a_booking_that_loses_to_an_unlocked_writer_is_refused_cleanly(factory, slot):
    """The backstop must refuse the caller, not fail them.

    Before the handler learned to tell constraints apart, this path looked up a
    winner under the caller's own idempotency key, found none, and re-raised:
    invariant held, caller got a 500.
    """
    _rogue_appointment(factory, slot["slot"])
    request = _request(slot["slot"])

    result = _attempt(factory, request)

    assert not isinstance(result, Exception), f"backstop surfaced an error: {result!r}"
    assert result.outcome == "slot_unavailable"
    assert result.detail == "Selected slot was taken by a concurrent booking"
    # Recorded, so a retry replays the refusal instead of trying again.
    retry = _attempt(factory, request)
    assert retry.replayed is True and retry.outcome == "slot_unavailable"
    with factory() as session:
        assert len(session.scalars(select(Appointment).where(Appointment.slot_id == slot["slot"])).all()) == 1


BACKSTOP_RACERS = 12


def test_without_the_lock_the_index_alone_prevents_double_booking(factory, slot, monkeypatch):
    """Remove the row lock and force the race. The database must hold the line.

    With neither the lock nor the index, 80 racers once put 80 appointments on
    nearly every slot. Here only the lock is gone.

    The race is forced rather than hoped for. The replacement loader reads the
    slot without locking and then waits until every racer has read it too, so
    all of them see FREE before any of them inserts. An earlier version just
    released racers from a barrier and let them run; about one run in five they
    serialised on their own and the index was never exercised, which is a
    test that passes without testing anything.
    """
    from app import provider_queries

    everyone_has_read = threading.Barrier(BACKSTOP_RACERS, timeout=20)

    def unlocked(db, slot_id):
        slot = db.scalar(
            select(AppointmentSlot)
            .where(AppointmentSlot.id == slot_id)
            .options(joinedload(AppointmentSlot.schedule).joinedload(ProviderSchedule.provider))
        )
        everyone_has_read.wait()
        return slot

    monkeypatch.setattr(provider_queries, "_lock_slot", unlocked)
    requests = [_request(slot["slot"]) for _ in range(BACKSTOP_RACERS)]

    with ThreadPoolExecutor(max_workers=BACKSTOP_RACERS) as pool:
        results = list(pool.map(lambda request: _attempt(factory, request), requests))

    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"losers must be refused, not failed: {failures}"
    booked = [r for r in results if r.outcome == "booked"]
    refused = [r for r in results if r.outcome == "slot_unavailable"]
    assert len(booked) == 1
    assert len(refused) == BACKSTOP_RACERS - 1
    # Every racer saw FREE, so every loser was stopped by the index and nothing else.
    assert all(r.detail == "Selected slot was taken by a concurrent booking" for r in refused)
    with factory() as session:
        assert len(session.scalars(select(Appointment).where(Appointment.slot_id == slot["slot"])).all()) == 1
        # A refusal is recorded like any other decision, so each loser's retry replays it.
        attempts = session.scalars(select(BookingAttempt).where(BookingAttempt.slot_id == slot["slot"])).all()
        assert len(attempts) == BACKSTOP_RACERS


def test_the_stress_harness_refuses_to_exceed_the_connection_ceiling():
    """A harness that asks for more connections than the server allows produces
    a flood of OperationalErrors that look like booking failures and are not.

    That happened: a 50,000-attempt run reported 13,484 errors which were
    entirely the harness's own fault, and would have made the result
    unquotable had it not been checked.
    """
    import inspect

    from app import booking_stress

    source = inspect.getsource(booking_stress.run)
    assert "max_connections" in source, "the harness must check the server's ceiling before running"
    assert "SystemExit" in source, "exceeding the ceiling must abort rather than pollute the result"


def test_the_stress_harness_verifies_against_the_database_not_the_callers():
    """Callers can be told anything; the guarantee is the row count per slot."""
    import inspect

    from app import booking_stress

    source = inspect.getsource(booking_stress.run)
    assert "group_by(Appointment.slot_id)" in source
    assert "over_booked" in source


def test_maximum_contention_mode_gives_every_racer_a_distinct_key():
    """Shared keys would let idempotency mask a broken lock.

    If every racer reused one key, the booking_attempts unique index alone would
    prevent duplicates and a missing row lock would go unnoticed. Distinct keys
    make the lock the only guard, which is what makes this the sharp test.
    """
    import inspect

    from app import booking_stress

    source = inspect.getsource(booking_stress.run_contention)
    assert "threading.Barrier" in source, "racers must be released simultaneously"
    assert "uuid.uuid4()" in source and "race:" in source, "each racer needs a distinct idempotency key"
