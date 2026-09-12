import uuid
from datetime import timedelta

import inngest
import inngest.fast_api
from sqlalchemy import select

from .agent import ReferralCoordinator
from .booking import book_selected_slot
from .config import settings
from .database import SessionLocal
from .model_providers import configured_model
from .models import ProcessedEvent, Referral, ReferralState, WorkflowRun


inngest_client = inngest.Inngest(
    app_id="careroute",
    is_production=settings.inngest_is_production,
    event_key=settings.inngest_event_key,
    signing_key=settings.inngest_signing_key,
)


def _snapshot(referral_id: uuid.UUID) -> dict:
    with SessionLocal() as db:
        referral = db.get(Referral, referral_id)
        if referral is None:
            raise LookupError("Referral not found")
        confirmed = db.scalar(
            select(ProcessedEvent.id).where(
                ProcessedEvent.referral_id == referral_id,
                ProcessedEvent.event_type == "booking.confirmed",
            )
        ) is not None
        return {
            "state": referral.state.value,
            "selected_slot_id": str(referral.selected_slot_id) if referral.selected_slot_id else None,
            "confirmed": confirmed,
        }


async def _coordinate(referral_id: uuid.UUID, workflow_run_id: uuid.UUID) -> dict:
    with SessionLocal() as db:
        result = await ReferralCoordinator(db, configured_model()).process(referral_id, workflow_run_id)
        return result.model_dump(mode="json")


def _book(referral_id: uuid.UUID) -> dict:
    with SessionLocal() as db:
        appointment = book_selected_slot(db, referral_id)
        return {"appointment_id": str(appointment.id), "slot_id": str(appointment.slot_id), "status": appointment.status.value}


@inngest_client.create_function(
    fn_id="durable-referral-coordination",
    name="Durable referral coordination",
    trigger=inngest.TriggerEvent(event="careroute/referral.received"),
    cancel=[inngest.Cancel(event="careroute/referral.cancelled", if_exp="event.data.referral_id == async.data.referral_id")],
    retries=4,
)
async def durable_referral(ctx: inngest.Context) -> dict:
    referral_id = uuid.UUID(str(ctx.event.data["referral_id"]))
    workflow_run_id = uuid.UUID(str(ctx.event.data["workflow_run_id"]))

    snapshot = await ctx.step.run("initial-snapshot", lambda: _snapshot(referral_id))
    if snapshot["state"] == ReferralState.CANCELLED.value:
        return snapshot
    if snapshot["state"] == ReferralState.CONFIRMED.value:
        return snapshot
    if snapshot["state"] == ReferralState.WAITING_FOR_SLOT_SELECTION.value:
        result = snapshot
    else:
        result = await ctx.step.run("coordinate-0", lambda: _coordinate(referral_id, workflow_run_id))

    for attempt in range(10):
        if result["state"] != ReferralState.WAITING_FOR_DOCUMENTS.value:
            break
        snapshot = await ctx.step.run(f"document-snapshot-{attempt}", lambda: _snapshot(referral_id))
        if snapshot["state"] == ReferralState.WAITING_FOR_DOCUMENTS.value:
            event = await ctx.step.wait_for_event(
                f"wait-for-document-{attempt}",
                event="careroute/document.received",
                if_exp="event.data.referral_id == async.data.referral_id",
                timeout=timedelta(days=30),
            )
            if event is None:
                return {"state": ReferralState.WAITING_FOR_DOCUMENTS.value, "timed_out": True}
        result = await ctx.step.run(f"coordinate-document-{attempt + 1}", lambda: _coordinate(referral_id, workflow_run_id))

    if result["state"] != ReferralState.WAITING_FOR_SLOT_SELECTION.value:
        return result

    snapshot = await ctx.step.run("selection-snapshot", lambda: _snapshot(referral_id))
    if snapshot["selected_slot_id"] is None:
        selected = await ctx.step.wait_for_event(
            "wait-for-slot-selection",
            event="careroute/slot.selected",
            if_exp="event.data.referral_id == async.data.referral_id",
            timeout=timedelta(days=30),
        )
        if selected is None:
            return {"state": ReferralState.WAITING_FOR_SLOT_SELECTION.value, "timed_out": True}

    snapshot = await ctx.step.run("confirmation-snapshot", lambda: _snapshot(referral_id))
    if not snapshot["confirmed"]:
        confirmed = await ctx.step.wait_for_event(
            "wait-for-human-confirmation",
            event="careroute/booking.confirmed",
            if_exp="event.data.referral_id == async.data.referral_id",
            timeout=timedelta(days=7),
        )
        if confirmed is None:
            return {"state": ReferralState.WAITING_FOR_SLOT_SELECTION.value, "timed_out": True}
    return await ctx.step.run("book-selected-slot", lambda: _book(referral_id))


async def send_event(name: str, event_id: str, data: dict) -> list[str]:
    return await inngest_client.send(inngest.Event(name=name, id=event_id, data=data))


def mount_inngest(app) -> None:
    inngest.fast_api.serve(app, inngest_client, [durable_referral])
