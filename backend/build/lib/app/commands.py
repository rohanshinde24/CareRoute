import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .models import AppointmentSlot, ProcessedEvent, ProviderSchedule, Referral, ReferralDocument, ReferralState, SlotStatus
from .workflow import InvalidTransition, audit, transition


class CommandConflict(ValueError):
    pass


def _existing(db: Session, event_id: str, event_type: str, referral_id: uuid.UUID, payload: dict) -> ProcessedEvent | None:
    event = db.scalar(select(ProcessedEvent).where(ProcessedEvent.event_id == event_id))
    if event and (event.event_type != event_type or event.referral_id != referral_id or event.payload != payload):
        raise CommandConflict("Event ID was already used for a different command")
    return event


def _record(db: Session, event_id: str, event_type: str, referral_id: uuid.UUID, payload: dict) -> ProcessedEvent:
    event = ProcessedEvent(event_id=event_id, event_type=event_type, referral_id=referral_id, payload=payload)
    db.add(event)
    return event


def receive_document(db: Session, referral: Referral, event_id: str, document_type: str, storage_locator: str) -> tuple[ReferralDocument, bool]:
    referral = db.scalar(select(Referral).where(Referral.id == referral.id).with_for_update())
    payload = {"document_type": document_type, "storage_locator": storage_locator}
    if _existing(db, event_id, "document.received", referral.id, payload):
        document = db.scalar(select(ReferralDocument).where(ReferralDocument.referral_id == referral.id, ReferralDocument.document_type == document_type, ReferralDocument.storage_locator == storage_locator))
        return document, True
    document = ReferralDocument(referral_id=referral.id, document_type=document_type, storage_locator=storage_locator)
    db.add(document)
    _record(db, event_id, "document.received", referral.id, payload)
    audit(db, referral.id, "document_received", {"document_type": document_type, "event_id": event_id})
    db.commit()
    db.refresh(document)
    return document, False


def select_slot(db: Session, referral: Referral, event_id: str, slot_id: uuid.UUID) -> bool:
    referral = db.scalar(select(Referral).where(Referral.id == referral.id).with_for_update())
    payload = {"slot_id": str(slot_id)}
    if _existing(db, event_id, "slot.selected", referral.id, payload):
        return True
    if referral.state not in {ReferralState.WAITING_FOR_SLOT_SELECTION, ReferralState.BOOKING_FAILED}:
        raise InvalidTransition(f"Cannot select a slot while referral is {referral.state.value}")
    slot = db.scalar(select(AppointmentSlot).where(AppointmentSlot.id == slot_id).options(joinedload(AppointmentSlot.schedule).joinedload(ProviderSchedule.provider)))
    if slot is None or slot.status != SlotStatus.FREE:
        raise CommandConflict("Selected slot is not available")
    if slot.schedule.provider.specialty.casefold() != referral.requested_specialty.casefold():
        raise CommandConflict("Selected slot does not match the requested specialty")
    if referral.state == ReferralState.BOOKING_FAILED:
        transition(referral, ReferralState.WAITING_FOR_SLOT_SELECTION)
    referral.selected_slot_id = slot.id
    _record(db, event_id, "slot.selected", referral.id, payload)
    audit(db, referral.id, "slot_selected", {"slot_id": str(slot.id), "event_id": event_id})
    db.commit()
    return False


def confirm_selection(db: Session, referral: Referral, event_id: str) -> bool:
    referral = db.scalar(select(Referral).where(Referral.id == referral.id).with_for_update())
    payload = {"slot_id": str(referral.selected_slot_id) if referral.selected_slot_id else None}
    if _existing(db, event_id, "booking.confirmed", referral.id, payload):
        return True
    if referral.state != ReferralState.WAITING_FOR_SLOT_SELECTION or referral.selected_slot_id is None:
        raise InvalidTransition("A currently available slot must be selected before confirmation")
    _record(db, event_id, "booking.confirmed", referral.id, payload)
    audit(db, referral.id, "human_confirmation_received", {"slot_id": payload["slot_id"], "event_id": event_id})
    db.commit()
    return False


def cancel(db: Session, referral: Referral, event_id: str, reason: str | None) -> bool:
    referral = db.scalar(select(Referral).where(Referral.id == referral.id).with_for_update())
    payload = {"reason": reason}
    if _existing(db, event_id, "referral.cancelled", referral.id, payload):
        return True
    transition(referral, ReferralState.CANCELLED)
    _record(db, event_id, "referral.cancelled", referral.id, payload)
    audit(db, referral.id, "referral_cancelled", {"reason": reason, "event_id": event_id})
    db.commit()
    return False
