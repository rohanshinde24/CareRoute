from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.booking import BookingError, book_selected_slot
from app.commands import CommandConflict, cancel, confirm_selection, receive_document, select_slot
from app.models import Appointment, AppointmentSlot, Coverage, Patient, ProcessedEvent, Provider, ProviderSchedule, Referral, ReferralState, SlotStatus
from app.workflow import InvalidTransition, transition


def booking_case(db):
    patient = Patient(external_id="p2-patient", source="synthea", given_name="P2", family_name="Case", birth_date=date(1985, 1, 1), is_synthetic=True)
    db.add(patient)
    db.flush()
    db.add(Coverage(patient_id=patient.id, external_id="p2-coverage", source="synthetic-payer", payer_name="Plan", member_id="p2-member", status="active", is_synthetic=True))
    referral = Referral(patient_id=patient.id, requested_specialty="Cardiology", reason="Synthetic P2 booking", state=ReferralState.WAITING_FOR_SLOT_SELECTION, is_synthetic=True)
    provider = Provider(name="P2 Cardiologist", specialty="Cardiology", location="Test, CA", is_synthetic=True)
    db.add_all([referral, provider])
    db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name="P2 schedule", timezone="UTC")
    db.add(schedule)
    db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    slot = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30))
    db.add(slot)
    db.commit()
    return referral, slot


def test_state_machine_rejects_skipping_confirmation(db):
    referral, _ = booking_case(db)
    with pytest.raises(InvalidTransition, match="WAITING_FOR_SLOT_SELECTION to CONFIRMED"):
        transition(referral, ReferralState.CONFIRMED)


def test_duplicate_selection_and_confirmation_are_idempotent(db):
    referral, slot = booking_case(db)
    assert select_slot(db, referral, "selection-1", slot.id) is False
    assert select_slot(db, referral, "selection-1", slot.id) is True
    assert confirm_selection(db, referral, "confirmation-1") is False
    assert confirm_selection(db, referral, "confirmation-1") is True
    assert db.query(ProcessedEvent).count() == 2


def test_event_id_cannot_be_reused_for_different_command(db):
    referral, slot = booking_case(db)
    select_slot(db, referral, "same-event", slot.id)
    with pytest.raises(CommandConflict, match="different command"):
        confirm_selection(db, referral, "same-event")


def test_booking_is_effectively_once_after_response_loss(db):
    referral, slot = booking_case(db)
    select_slot(db, referral, "selection", slot.id)
    confirm_selection(db, referral, "confirmation")
    first = book_selected_slot(db, referral.id)

    # Simulate the caller losing the committed response and retrying the step.
    second = book_selected_slot(db, referral.id)
    assert first.id == second.id
    assert db.query(Appointment).count() == 1
    assert db.get(AppointmentSlot, slot.id).status == SlotStatus.BUSY
    assert db.get(Referral, referral.id).state == ReferralState.CONFIRMED


def test_booking_cannot_bypass_explicit_confirmation(db):
    referral, slot = booking_case(db)
    select_slot(db, referral, "selection", slot.id)
    with pytest.raises(BookingError, match="Explicit confirmation"):
        book_selected_slot(db, referral.id)


def test_stale_slot_fails_without_creating_appointment(db):
    referral, slot = booking_case(db)
    select_slot(db, referral, "selection", slot.id)
    confirm_selection(db, referral, "confirmation")
    slot.status = SlotStatus.BUSY
    db.commit()
    with pytest.raises(BookingError, match="no longer available"):
        book_selected_slot(db, referral.id)
    assert db.get(Referral, referral.id).state == ReferralState.BOOKING_FAILED
    assert db.query(Appointment).count() == 0


def test_cancellation_prevents_booking_and_is_idempotent(db):
    referral, slot = booking_case(db)
    select_slot(db, referral, "selection", slot.id)
    assert cancel(db, referral, "cancel-1", "Patient declined") is False
    assert cancel(db, referral, "cancel-1", "Patient declined") is True
    with pytest.raises(BookingError, match="CANCELLED"):
        book_selected_slot(db, referral.id)


def test_document_arrival_deduplicates_metadata(db):
    referral, _ = booking_case(db)
    referral.state = ReferralState.WAITING_FOR_DOCUMENTS
    db.commit()
    first, duplicate = receive_document(db, referral, "document-1", "clinical-note", "synthetic://p2/note")
    second, repeated = receive_document(db, referral, "document-1", "clinical-note", "synthetic://p2/note")
    assert duplicate is False
    assert repeated is True
    assert first.id == second.id
