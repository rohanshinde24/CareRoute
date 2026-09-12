"""Referral-side booking: gate, delegate, then record the outcome.

The appointment and the slot live in the provider database, so the exactly-once
guarantee is enforced there, inside one transaction (see provider_queries.book_slot).
What stays here is everything the referral domain owns: the confirmation gate,
the workflow state machine, and the decision to ask at all.

The referral state transition is a second transaction against a different
database, so it can fail after a booking succeeds. The appointment is
authoritative in that case and the repair is always forward - re-drive the
transition from the recorded booking - never by inventing or deleting an
appointment.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .faults import FaultInjector
from .metrics import record_booking_attempt
from .models import ProcessedEvent, Referral, ReferralState
from .provider_contracts import BookingRequest, BookingResult
from .provider_gateway import ProviderGateway, ProviderGatewayError
from .workflow import audit, transition


class BookingError(RuntimeError):
    pass


def booking_key(referral_id: uuid.UUID, slot_id: uuid.UUID) -> str:
    return f"booking:{referral_id}:{slot_id}"


async def book_selected_slot(
    db: Session,
    referral_id: uuid.UUID,
    gateway: ProviderGateway,
    fault_injector: FaultInjector | None = None,
) -> BookingResult:
    fault_injector = fault_injector or FaultInjector()
    referral = db.scalar(select(Referral).where(Referral.id == referral_id).with_for_update())
    if referral is None:
        raise LookupError("Referral not found")
    if referral.selected_slot_id is None:
        raise BookingError("A slot must be selected before confirmation")

    slot_id = referral.selected_slot_id
    key = booking_key(referral.id, slot_id)

    if referral.state == ReferralState.CONFIRMED:
        # Already booked and already recorded. Replay the provider's decision so
        # the caller sees the original appointment rather than a new attempt.
        result = await _ask(gateway, referral, slot_id, key)
        record_booking_attempt("duplicate_suppressed")
        return result

    if referral.state != ReferralState.WAITING_FOR_SLOT_SELECTION:
        raise BookingError(f"Referral cannot be booked from {referral.state.value}")

    confirmation = db.scalar(
        select(ProcessedEvent.id).where(
            ProcessedEvent.referral_id == referral.id,
            ProcessedEvent.event_type == "booking.confirmed",
            ProcessedEvent.payload["slot_id"].as_string() == str(slot_id),
        )
    )
    if confirmation is None:
        raise BookingError("Explicit confirmation is required before booking")

    transition(referral, ReferralState.BOOKING)
    db.commit()

    try:
        result = await _ask(gateway, referral, slot_id, key)
    except ProviderGatewayError as exc:
        # The provider domain may or may not have acted. The referral stays in
        # BOOKING, which is resumable: the same idempotency key returns the true
        # outcome on the next attempt.
        audit(db, referral.id, "booking_unresolved", {"slot_id": str(slot_id), "reason": type(exc).__name__})
        db.commit()
        raise

    referral = db.scalar(select(Referral).where(Referral.id == referral.id).with_for_update())
    if result.succeeded:
        referral.selected_slot_id = slot_id
        transition(referral, ReferralState.CONFIRMED)
        audit(db, referral.id, "booking_completed", {"slot_id": str(slot_id), "idempotency_key": key, "outcome": result.outcome})
    else:
        transition(referral, ReferralState.BOOKING_FAILED)
        audit(db, referral.id, "booking_failed", {"slot_id": str(slot_id), "reason": result.outcome})
    db.commit()

    fault_injector.hit("booking_response_loss")
    if not result.succeeded:
        raise BookingError(result.detail or "Booking was refused by the provider service")
    return result


async def _ask(gateway: ProviderGateway, referral: Referral, slot_id: uuid.UUID, key: str) -> BookingResult:
    return await gateway.book(
        BookingRequest(
            referral_id=referral.id,
            slot_id=slot_id,
            requested_specialty=referral.requested_specialty,
            idempotency_key=key,
        ),
        referral.id,
    )
