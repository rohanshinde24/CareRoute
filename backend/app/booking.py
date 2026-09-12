import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .metrics import record_booking_attempt
from .models import Appointment, AppointmentSlot, AppointmentStatus, ProcessedEvent, ProviderSchedule, Referral, ReferralState, SlotStatus
from .workflow import audit, transition
from .faults import FaultInjector


class BookingError(ValueError):
    pass


def booking_key(referral_id: uuid.UUID, slot_id: uuid.UUID) -> str:
    return f"booking:{referral_id}:{slot_id}"


def book_selected_slot(db: Session, referral_id: uuid.UUID, fault_injector: FaultInjector | None = None) -> Appointment:
    fault_injector = fault_injector or FaultInjector()
    referral = db.scalar(select(Referral).where(Referral.id == referral_id).with_for_update())
    if referral is None:
        raise LookupError("Referral not found")
    if referral.selected_slot_id is None:
        raise BookingError("A slot must be selected before confirmation")

    key = booking_key(referral.id, referral.selected_slot_id)
    existing = db.scalar(select(Appointment).where(Appointment.idempotency_key == key))
    if existing is not None:
        record_booking_attempt("duplicate_suppressed")
        return existing
    if referral.state != ReferralState.WAITING_FOR_SLOT_SELECTION:
        raise BookingError(f"Referral cannot be booked from {referral.state.value}")
    confirmation = db.scalar(
        select(ProcessedEvent.id).where(
            ProcessedEvent.referral_id == referral.id,
            ProcessedEvent.event_type == "booking.confirmed",
            ProcessedEvent.payload["slot_id"].as_string() == str(referral.selected_slot_id),
        )
    )
    if confirmation is None:
        raise BookingError("Explicit confirmation is required before booking")

    slot = db.scalar(
        select(AppointmentSlot)
        .where(AppointmentSlot.id == referral.selected_slot_id)
        .with_for_update(of=AppointmentSlot)
        .options(joinedload(AppointmentSlot.schedule).joinedload(ProviderSchedule.provider))
    )
    transition(referral, ReferralState.BOOKING)
    if slot is None or slot.status != SlotStatus.FREE:
        transition(referral, ReferralState.BOOKING_FAILED)
        audit(db, referral.id, "booking_failed", {"reason": "selected_slot_unavailable", "slot_id": str(referral.selected_slot_id)})
        db.commit()
        record_booking_attempt("slot_unavailable")
        raise BookingError("Selected slot is no longer available")
    if slot.schedule.provider.specialty.casefold() != referral.requested_specialty.casefold():
        transition(referral, ReferralState.BOOKING_FAILED)
        audit(db, referral.id, "booking_failed", {"reason": "specialty_mismatch", "slot_id": str(slot.id)})
        db.commit()
        raise BookingError("Selected slot does not match the requested specialty")

    appointment = Appointment(
        referral_id=referral.id,
        slot_id=slot.id,
        idempotency_key=key,
        status=AppointmentStatus.BOOKED,
    )
    slot.status = SlotStatus.BUSY
    transition(referral, ReferralState.CONFIRMED)
    db.add(appointment)
    audit(db, referral.id, "booking_completed", {"slot_id": str(slot.id), "idempotency_key": key})
    db.commit()
    db.refresh(appointment)
    fault_injector.hit("booking_response_loss")
    record_booking_attempt("booked")
    return appointment
