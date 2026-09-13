"""Repairs referrals whose booking succeeded but whose state never caught up.

The consumer handles this for every event that arrives. The reconciler exists
for the ones that do not: an event lost inside Redis's one-second AOF window, a
relay row that was dispatched but whose consumer died before committing, or a
booking made before any of this machinery existed.

It asks the provider domain rather than reading its tables, and it only ever
drives state forward from an appointment that already exists. It never creates
or cancels an appointment to make the two sides agree - a reconciler that can
invent facts is a second source of truth, which is the problem it is meant to
solve.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .metrics import record_reconciliation
from .models import Referral, ReferralState
from .provider_gateway import ProviderGateway, ProviderGatewayError
from .workflow import InvalidTransition, audit, transition

logger = logging.getLogger(__name__)

# States a referral can legitimately sit in while a booking is in flight.
# CONFIRMED is settled; CANCELLED must never be revived by a late event.
REPAIRABLE = (ReferralState.BOOKING, ReferralState.WAITING_FOR_SLOT_SELECTION)


def candidates(db: Session, older_than_seconds: float, limit: int = 100) -> list[Referral]:
    """Referrals that may have been booked without their state catching up.

    The age filter avoids racing the normal path: a referral booked two seconds
    ago is probably mid-flight, not stranded.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    return list(
        db.scalars(
            select(Referral)
            .where(
                Referral.state.in_(REPAIRABLE),
                Referral.selected_slot_id.is_not(None),
                Referral.updated_at < cutoff,
            )
            .order_by(Referral.updated_at)
            .limit(limit)
        )
    )


async def repair(db: Session, referral: Referral, gateway: ProviderGateway) -> str:
    """Drive one referral forward if the provider domain says it is booked."""
    try:
        result = await gateway.appointments_for_referral(referral.id, referral.id)
    except ProviderGatewayError as exc:
        logger.warning("reconciler could not reach the provider domain for %s: %s", referral.id, exc)
        return "provider_unavailable"

    booked = [item for item in result.items if item.status == "BOOKED"]
    if not booked:
        # No appointment means nothing to repair. The referral is simply waiting,
        # and inventing a booking here would be the worst possible outcome.
        return "no_appointment"

    locked = db.scalar(select(Referral).where(Referral.id == referral.id).with_for_update())
    if locked.state == ReferralState.CONFIRMED:
        return "already_confirmed"

    try:
        if locked.state == ReferralState.WAITING_FOR_SLOT_SELECTION:
            transition(locked, ReferralState.BOOKING)
        locked.selected_slot_id = booked[0].slot_id
        transition(locked, ReferralState.CONFIRMED)
    except InvalidTransition:
        return "transition_refused"

    audit(db, locked.id, "booking_reconciled", {"slot_id": str(booked[0].slot_id), "source": "reconciler"})
    db.commit()
    logger.info("reconciled referral %s from appointment %s", locked.id, booked[0].id)
    return "reconciled"


async def run_once(db_factory, gateway: ProviderGateway, older_than_seconds: float = 60.0) -> dict[str, int]:
    outcomes: dict[str, int] = {}
    with db_factory() as db:
        stranded = candidates(db, older_than_seconds)
    for referral in stranded:
        with db_factory() as db:
            outcome = await repair(db, referral, gateway)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        record_reconciliation(outcome)
    return outcomes


def main() -> None:  # pragma: no cover - process entry point
    import asyncio
    import time

    from .agent import _default_gateway
    from .config import settings
    from .database import SessionLocal

    logging.basicConfig(level=logging.INFO)
    logger.info("reconciler started; interval=%ss", settings.reconciler_interval_seconds)
    while True:
        try:
            outcomes = asyncio.run(run_once(SessionLocal, _default_gateway()))
            if any(v for k, v in outcomes.items() if k == "reconciled"):
                logger.info("reconciled %s", outcomes)
        except Exception:
            logger.exception("reconciler pass failed; retrying")
        time.sleep(settings.reconciler_interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    main()
