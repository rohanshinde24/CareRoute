"""Provider-owned data access, including booking.

Booking lives here because it must stay one local transaction. It locks the
slot, re-checks the owning provider's specialty, marks the slot busy and writes
the appointment against a single database, so the effectively-once guarantee is
enforced by PostgreSQL rather than asserted across a network.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

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
    Appointment,
    AppointmentSlot,
    AppointmentStatus,
    BookingAttempt,
    Provider,
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


def _remember(db: Session, request: BookingRequest, outcome: str, appointment_id: uuid.UUID | None, detail: str | None) -> BookingResult:
    db.add(
        BookingAttempt(
            idempotency_key=request.idempotency_key,
            referral_id=request.referral_id,
            slot_id=request.slot_id,
            outcome=outcome,
            appointment_id=appointment_id,
            detail=detail,
        )
    )
    return BookingResult(outcome=outcome, appointment_id=appointment_id, slot_id=request.slot_id, detail=detail)


def book_slot(db: Session, request: BookingRequest) -> BookingResult:
    """Book one slot, exactly once, inside one transaction.

    Rejections are recorded as well as successes. A caller that loses the
    response repeats the request with the same key and must learn the original
    outcome; without storing refusals, a retry after a lost conflict would look
    like a fresh attempt on a slot that is now busy for a different reason.
    """
    existing = db.scalar(select(BookingAttempt).where(BookingAttempt.idempotency_key == request.idempotency_key))
    if existing is not None:
        record_booking_attempt("replayed")
        return _replay(existing)

    slot = db.scalar(
        select(AppointmentSlot)
        .where(AppointmentSlot.id == request.slot_id)
        .with_for_update(of=AppointmentSlot)
        .options(joinedload(AppointmentSlot.schedule).joinedload(ProviderSchedule.provider))
    )
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
        db.flush()
        result = _remember(db, request, "booked", appointment.id, None)

    try:
        db.commit()
    except IntegrityError:
        # Another transaction won the same idempotency key between the lookup and
        # the commit. Its record is authoritative; report what it decided.
        db.rollback()
        winner = db.scalar(select(BookingAttempt).where(BookingAttempt.idempotency_key == request.idempotency_key))
        if winner is None:
            raise
        record_booking_attempt("replayed")
        return _replay(winner)

    record_booking_attempt(result.outcome)
    return result
