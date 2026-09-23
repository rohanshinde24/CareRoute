"""Provider-owned data access, including booking.

Booking lives here because it must stay one local transaction. It locks the
slot, re-checks the owning provider's specialty, marks the slot busy and writes
the appointment against a single database, so the effectively-once guarantee is
enforced by PostgreSQL rather than asserted across a network.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .events import EventType, build_event
from .metrics import record_booking_attempt
from .pagination import clamp_limit, decode_cursor, encode_cursor
from .provider_contracts import (
    AppointmentListResult,
    AppointmentRecord,
    BookingRequest,
    BookingResult,
    ProviderListResult,
    ProviderSpecialtyListResult,
    ProviderToolResult,
    ProviderPage,
    SlotDetail,
    SlotListResult,
    SlotPage,
    SlotWithProvider,
    SlotToolResult,
)
from .provider_models import (
    ACTIVE_APPOINTMENT_PER_SLOT,
    Appointment,
    AppointmentSlot,
    AppointmentStatus,
    BookingAttempt,
    Provider,
    ProviderOutbox,
    ProviderSchedule,
    SlotStatus,
)


def find_providers(db: Session, specialty: str, include_evaluation: bool = False) -> ProviderListResult:
    records = db.scalars(
        select(Provider)
        .where(
            Provider.specialty.ilike(specialty),
            Provider.accepting_new_patients.is_(True),
            Provider.is_synthetic.is_(True),
            Provider.is_evaluation.is_(include_evaluation),
        )
        .order_by(Provider.name)
    ).all()
    return ProviderListResult(items=[ProviderToolResult.model_validate(record) for record in records])


def list_specialties(db: Session) -> ProviderSpecialtyListResult:
    items = list(
        db.scalars(
            select(Provider.specialty)
            .where(
                Provider.accepting_new_patients.is_(True),
                Provider.is_synthetic.is_(True),
                Provider.is_evaluation.is_(False),
            )
            .distinct()
            .order_by(Provider.specialty)
        )
    )
    return ProviderSpecialtyListResult(items=items)


def available_slots(db: Session, provider_id: uuid.UUID) -> SlotListResult:
    records = db.scalars(
        select(AppointmentSlot)
        .join(ProviderSchedule)
        .where(ProviderSchedule.provider_id == provider_id, AppointmentSlot.status == SlotStatus.FREE)
        .options(joinedload(AppointmentSlot.schedule))
        .order_by(AppointmentSlot.start_at, AppointmentSlot.id)
    ).all()
    return SlotListResult(
        items=[
            SlotToolResult(id=slot.id, provider_id=slot.schedule.provider_id, start_at=slot.start_at, end_at=slot.end_at)
            for slot in records
        ]
    )


def list_providers(db: Session, specialty: str | None, accepting_new_patients: bool, limit: int | None, cursor: str | None) -> ProviderPage:
    query = select(Provider).where(
        Provider.accepting_new_patients == accepting_new_patients,
        Provider.is_evaluation.is_(False),
    )
    if specialty:
        query = query.where(Provider.specialty.ilike(f"%{specialty}%"))
    size = clamp_limit(limit)
    if cursor:
        created_at, provider_id = decode_cursor(cursor)
        query = query.where(tuple_(Provider.created_at, Provider.id) > (created_at, provider_id))
    rows = db.scalars(query.order_by(Provider.created_at, Provider.id).limit(size + 1)).all()
    items = list(rows[:size])
    next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > size else None
    return ProviderPage(items=[ProviderToolResult.model_validate(item) for item in items], next_cursor=next_cursor)


def list_free_slots(db: Session, provider_id: uuid.UUID | None, specialty: str | None, limit: int | None, cursor: str | None) -> SlotPage:
    query = (
        select(AppointmentSlot)
        .join(ProviderSchedule)
        .join(Provider)
        .where(AppointmentSlot.status == SlotStatus.FREE, Provider.is_evaluation.is_(False))
        .options(joinedload(AppointmentSlot.schedule).joinedload(ProviderSchedule.provider))
    )
    if provider_id:
        query = query.where(Provider.id == provider_id)
    if specialty:
        query = query.where(Provider.specialty.ilike(f"%{specialty}%"))
    size = clamp_limit(limit)
    if cursor:
        start_at, slot_id = decode_cursor(cursor)
        query = query.where(tuple_(AppointmentSlot.start_at, AppointmentSlot.id) > (start_at, slot_id))
    rows = db.scalars(query.order_by(AppointmentSlot.start_at, AppointmentSlot.id).limit(size + 1)).unique().all()
    slots = list(rows[:size])
    next_cursor = encode_cursor(slots[-1].start_at, slots[-1].id) if len(rows) > size else None
    return SlotPage(
        items=[
            SlotWithProvider(
                id=slot.id,
                schedule_id=slot.schedule_id,
                start_at=slot.start_at,
                end_at=slot.end_at,
                status=slot.status.value,
                provider=ProviderToolResult.model_validate(slot.schedule.provider),
            )
            for slot in slots
        ],
        next_cursor=next_cursor,
    )


def appointments_for_referral(db: Session, referral_id: uuid.UUID) -> AppointmentListResult:
    rows = db.scalars(select(Appointment).where(Appointment.referral_id == referral_id).order_by(Appointment.created_at)).all()
    return AppointmentListResult(
        items=[
            AppointmentRecord(id=row.id, referral_id=row.referral_id, slot_id=row.slot_id, status=row.status.value, created_at=row.created_at)
            for row in rows
        ]
    )


def describe_slot(db: Session, slot_id: uuid.UUID) -> SlotDetail | None:
    """Facts about one slot, so the referral domain can validate a selection
    without reading provider tables."""
    slot = db.scalar(
        select(AppointmentSlot)
        .where(AppointmentSlot.id == slot_id)
        .options(joinedload(AppointmentSlot.schedule).joinedload(ProviderSchedule.provider))
    )
    if slot is None:
        return None
    return SlotDetail(
        id=slot.id,
        provider_id=slot.schedule.provider_id,
        provider_specialty=slot.schedule.provider.specialty,
        start_at=slot.start_at,
        end_at=slot.end_at,
        status=slot.status.value,
        is_free=slot.status == SlotStatus.FREE,
    )


def _replay(attempt: BookingAttempt) -> BookingResult:
    return BookingResult(
        outcome=attempt.outcome,
        appointment_id=attempt.appointment_id,
        slot_id=attempt.slot_id,
        detail=attempt.detail,
        replayed=True,
    )


def _announce(db: Session, request: BookingRequest, outcome: str, appointment_id: uuid.UUID | None) -> None:
    """Write the domain event in the booking transaction itself.

    Not after the commit: a publish outside this transaction could announce a
    booking that rolled back, or miss one that did not.
    """
    from .telemetry import current_traceparent

    event_type = EventType.APPOINTMENT_BOOKED if outcome == "booked" else EventType.BOOKING_REFUSED
    payload = {"referral_id": request.referral_id, "slot_id": request.slot_id, "outcome": outcome, "idempotency_key": request.idempotency_key}
    if appointment_id is not None:
        payload["appointment_id"] = appointment_id
    db.add(ProviderOutbox(**build_event(event_type, "careroute-provider-service", payload, current_traceparent())))


def _fingerprint(request: BookingRequest) -> str:
    """What this idempotency key stands for.

    Everything the provider domain acts on. A caller repeating a request with
    the same key must be repeating the same request; if the arguments differ,
    one of the two is a mistake and returning the stored answer would hide it.
    """
    material = f"{request.referral_id}|{request.slot_id}|{request.requested_specialty.casefold()}"
    return hashlib.sha256(material.encode()).hexdigest()


def _remember(db: Session, request: BookingRequest, outcome: str, appointment_id: uuid.UUID | None, detail: str | None) -> BookingResult:
    _announce(db, request, outcome, appointment_id)
    db.add(
        BookingAttempt(
            idempotency_key=request.idempotency_key,
            request_fingerprint=_fingerprint(request),
            referral_id=request.referral_id,
            slot_id=request.slot_id,
            outcome=outcome,
            appointment_id=appointment_id,
            detail=detail,
        )
    )
    return BookingResult(outcome=outcome, appointment_id=appointment_id, slot_id=request.slot_id, detail=detail)


def _lock_slot(db: Session, slot_id: uuid.UUID) -> AppointmentSlot | None:
    """Load the slot and hold its row lock until the transaction ends.

    This is the primary concurrency control: contenders queue on the row, and
    each one after the first sees the slot already BUSY and gets a clean
    refusal. It is a named function so the stress harness can replace it to
    show what the database backstop does on its own; production never does.
    """
    return db.scalar(
        select(AppointmentSlot)
        .where(AppointmentSlot.id == slot_id)
        .with_for_update(of=AppointmentSlot)
        .options(joinedload(AppointmentSlot.schedule).joinedload(ProviderSchedule.provider))
    )


def book_slot(db: Session, request: BookingRequest) -> BookingResult:
    """Book one slot, exactly once, inside one transaction.

    Rejections are recorded as well as successes. A caller that loses the
    response repeats the request with the same key and must learn the original
    outcome; without storing refusals, a retry after a lost conflict would look
    like a fresh attempt on a slot that is now busy for a different reason.

    Three mechanisms, three jobs. The idempotency key makes a repeated request
    return its first answer. The row lock serialises different requests for one
    slot so the loser is refused cleanly. The partial unique index on active
    appointments holds the invariant even when something bypasses the lock.
    """
    existing = db.scalar(select(BookingAttempt).where(BookingAttempt.idempotency_key == request.idempotency_key))
    if existing is not None:
        # A key carries one request. Reused with different arguments it is a
        # caller bug, and replaying the stored decision would answer a question
        # nobody asked - reporting a booking for a slot this request never named.
        # Rows written before fingerprints existed carry none and still replay.
        if existing.request_fingerprint is not None and existing.request_fingerprint != _fingerprint(request):
            record_booking_attempt("idempotency_key_conflict")
            return BookingResult(
                outcome="idempotency_key_conflict",
                slot_id=request.slot_id,
                detail="This idempotency key was already used for a different booking request",
            )
        record_booking_attempt("replayed")
        return _replay(existing)

    try:
        slot = _lock_slot(db, request.slot_id)
        if slot is None:
            result = _remember(db, request, "slot_not_found", None, "Slot does not exist")
        elif slot.status != SlotStatus.FREE:
            result = _remember(db, request, "slot_unavailable", None, "Selected slot is no longer available")
        elif slot.schedule.provider.specialty.casefold() != request.requested_specialty.casefold():
            result = _remember(db, request, "specialty_mismatch", None, "Selected slot does not match the requested specialty")
        else:
            appointment = Appointment(
                referral_id=request.referral_id,
                slot_id=slot.id,
                idempotency_key=request.idempotency_key,
                status=AppointmentStatus.BOOKED,
            )
            slot.status = SlotStatus.BUSY
            db.add(appointment)
            # Inside the try on purpose: a unique violation surfaces here, at
            # the INSERT, not at commit. PostgreSQL makes a second inserter
            # wait on the first's index entry and raises once that commits.
            db.flush()
            result = _remember(db, request, "booked", appointment.id, None)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        return _resolve_conflict(db, request, exc)

    record_booking_attempt(result.outcome)
    return result


def _resolve_conflict(db: Session, request: BookingRequest, exc: IntegrityError) -> BookingResult:
    """Turn a constraint violation into the answer the caller should get.

    Before the slot index existed, the only possible violation was a racing
    request with the same idempotency key, and this handler assumed so. With the
    index, a violation can also mean a different request took the slot. Treating
    that as an idempotency race would look up a winner under this caller's own
    key, find none, and surface a 500 - the backstop would hold the invariant
    and fail the caller while doing it.
    """
    winner = db.scalar(select(BookingAttempt).where(BookingAttempt.idempotency_key == request.idempotency_key))
    if winner is not None:
        # The same logical request was already decided; that answer wins.
        record_booking_attempt("replayed")
        return _replay(winner)
    if not _violates(exc, ACTIVE_APPOINTMENT_PER_SLOT):
        raise exc

    # Reaching here means the slot was taken by a writer that did not queue on
    # the lock. The caller gets the same refusal as the locked path, recorded so
    # a retry replays it. The metric is separate so the event is visible: with
    # every production path taking the lock, it should stay at zero.
    result = _remember(db, request, "slot_unavailable", None, "Selected slot was taken by a concurrent booking")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.scalar(select(BookingAttempt).where(BookingAttempt.idempotency_key == request.idempotency_key))
        if winner is None:
            raise
        record_booking_attempt("replayed")
        return _replay(winner)
    record_booking_attempt("slot_conflict_backstop")
    return result


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """Which constraint failed, by name where the driver reports it."""
    name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if name is not None:
        return name == constraint
    # SQLite reports the columns rather than the index name.
    return "appointments.slot_id" in str(exc.orig)
