import uuid

from sqlalchemy.orm import Session

from .models import AgentEvent, Referral, ReferralState, WorkflowRun


class InvalidTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[ReferralState, set[ReferralState]] = {
    ReferralState.RECEIVED: {ReferralState.PARSING, ReferralState.CANCELLED},
    ReferralState.PARSING: {ReferralState.VALIDATING, ReferralState.NEEDS_HUMAN_REVIEW, ReferralState.CANCELLED},
    ReferralState.VALIDATING: {ReferralState.PARSING, ReferralState.WAITING_FOR_DOCUMENTS, ReferralState.CHECKING_COVERAGE, ReferralState.NEEDS_HUMAN_REVIEW, ReferralState.CANCELLED},
    ReferralState.WAITING_FOR_DOCUMENTS: {ReferralState.PARSING, ReferralState.CANCELLED},
    ReferralState.CHECKING_COVERAGE: {ReferralState.PARSING, ReferralState.MATCHING_PROVIDER, ReferralState.COVERAGE_UNVERIFIED, ReferralState.CANCELLED},
    ReferralState.MATCHING_PROVIDER: {ReferralState.PARSING, ReferralState.WAITING_FOR_SLOT_SELECTION, ReferralState.NEEDS_HUMAN_REVIEW, ReferralState.PROVIDER_UNAVAILABLE, ReferralState.CANCELLED},
    ReferralState.WAITING_FOR_SLOT_SELECTION: {ReferralState.BOOKING, ReferralState.CANCELLED},
    ReferralState.BOOKING: {ReferralState.CONFIRMED, ReferralState.BOOKING_FAILED, ReferralState.CANCELLED},
    ReferralState.BOOKING_FAILED: {ReferralState.WAITING_FOR_SLOT_SELECTION, ReferralState.CANCELLED},
    ReferralState.COVERAGE_UNVERIFIED: {ReferralState.PARSING, ReferralState.CANCELLED},
    ReferralState.PROVIDER_UNAVAILABLE: {ReferralState.PARSING, ReferralState.CANCELLED},
    ReferralState.NEEDS_HUMAN_REVIEW: {ReferralState.PARSING, ReferralState.CANCELLED},
    ReferralState.CONFIRMED: set(),
    ReferralState.CANCELLED: set(),
}


def transition(referral: Referral, target: ReferralState) -> None:
    if referral.state == target:
        return
    if target not in ALLOWED_TRANSITIONS[referral.state]:
        raise InvalidTransition(f"Cannot transition referral from {referral.state.value} to {target.value}")
    referral.state = target


def latest_workflow(db: Session, referral_id: uuid.UUID) -> WorkflowRun | None:
    return (
        db.query(WorkflowRun)
        .filter(WorkflowRun.referral_id == referral_id)
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .first()
    )


def audit(db: Session, referral_id: uuid.UUID, event_type: str, payload: dict) -> None:
    run = latest_workflow(db, referral_id)
    if run is not None:
        run.state = db.get(Referral, referral_id).state
        db.add(AgentEvent(workflow_run_id=run.id, event_type=event_type, payload=payload))
