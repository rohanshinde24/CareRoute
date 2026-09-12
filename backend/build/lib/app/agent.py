import enum
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from .model_providers import ModelResponseError, ReferralModel, ReferralModelInput
from .models import AgentEvent, Referral, ReferralState, WorkflowRun
from .tools import CoverageListResult, DocumentListResult, ProviderListResult, ReferralToolResult, SlotListResult, invoke_tool
from .workflow import transition
from .faults import FaultInjector
from .investigation import DocumentInvestigator, InvestigationPolicyError, SpecialtyInvestigator

class NextAction(str, enum.Enum):
    PRESENT_SLOTS = "PRESENT_SLOTS"
    REQUEST_DOCUMENT = "REQUEST_DOCUMENT"
    VERIFY_COVERAGE = "VERIFY_COVERAGE"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

class ProcessingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_run_id: uuid.UUID
    referral_id: uuid.UUID
    state: ReferralState
    next_action: NextAction
    summary: str
    missing_documents: list[str] = Field(default_factory=list)
    provider_ids: list[uuid.UUID] = Field(default_factory=list)
    slot_ids: list[uuid.UUID] = Field(default_factory=list)

REQUIRED_DOCUMENTS: dict[str, set[str]] = {"orthopedics": {"clinical-note", "imaging-report"}}
DEFAULT_REQUIRED_DOCUMENTS = {"clinical-note"}
DOCUMENT_INVESTIGATION_MARKERS = (" completed ", " performed ", " completed.", " performed.", "completed last", "performed last")

def needs_document_investigation(reason: str) -> bool:
    normalized = f" {reason.casefold()} "
    return any(marker in normalized for marker in DOCUMENT_INVESTIGATION_MARKERS)

class ReferralCoordinator:
    def __init__(self, db: Session, model: ReferralModel, fault_injector: FaultInjector | None = None):
        self.db = db
        self.model = model
        self.fault_injector = fault_injector or FaultInjector()

    async def process(self, referral_id: uuid.UUID, workflow_run_id: uuid.UUID | None = None) -> ProcessingResult:
        referral = self.db.get(Referral, referral_id)
        if referral is None:
            raise LookupError("Referral not found")
        transition(referral, ReferralState.PARSING)
        run = self.db.get(WorkflowRun, workflow_run_id) if workflow_run_id else None
        if run is None:
            run = WorkflowRun(referral_id=referral.id, state=ReferralState.PARSING)
            self.db.add(run)
        elif run.referral_id != referral.id:
            raise ValueError("Workflow run does not belong to referral")
        run.state = ReferralState.PARSING
        self.db.commit()
        self._event(run, "workflow_started", {"model": self.model.name})
        referral_data = self._tool(run, "getReferral", {"id": referral.id}, ReferralToolResult)
        self._tool(run, "getPatient", {"id": referral_data.patient_id})

        try:
            self.fault_injector.hit("model_timeout")
            interpretation = await self.model.interpret(ReferralModelInput(requested_specialty=referral_data.requested_specialty, reason=referral_data.reason))
        except (ModelResponseError, ValidationError) as exc:
            summary = str(exc) if isinstance(exc, ModelResponseError) else "Referral specialty failed semantic validation"
            return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, summary)
        self._event(run, "model_interpretation", {"model": self.model.name, "specialty": interpretation.specialty, "confidence": interpretation.confidence, "is_ambiguous": interpretation.is_ambiguous})

        effective_specialty = interpretation.specialty
        if interpretation.is_ambiguous or not interpretation.specialty:
            try:
                effective_specialty = await SpecialtyInvestigator(self.db, self.model, run).investigate(referral)
            except (ModelResponseError, ValidationError, InvestigationPolicyError) as exc:
                return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, f"Investigation could not resolve specialty: {exc}")
            if effective_specialty is None:
                return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, "Agent investigation found insufficient evidence; human clarification is required")
        elif interpretation.specialty.casefold() != referral_data.requested_specialty.strip().casefold():
            return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, "Model specialty conflicts with submitted referral")

        transition(referral, ReferralState.VALIDATING)
        run.state = ReferralState.VALIDATING
        self.db.commit()
        documents = self._tool(run, "getReferralDocuments", {"id": referral.id}, DocumentListResult)
        required = REQUIRED_DOCUMENTS.get(effective_specialty.casefold(), DEFAULT_REQUIRED_DOCUMENTS)
        received = {document.document_type.casefold() for document in documents.items}
        missing = sorted(required - received)
        if not missing and needs_document_investigation(referral.reason):
            try:
                proposed_document = await DocumentInvestigator(self.db, self.model, run).investigate(referral, effective_specialty, received)
            except (ModelResponseError, ValidationError, InvestigationPolicyError) as exc:
                return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, f"Document investigation could not resolve the requirement: {exc}")
            if proposed_document is None:
                return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, "Document investigation found insufficient evidence; human clarification is required")
            missing = [proposed_document]
        if missing:
            for document_type in missing:
                self._tool(run, "requestMissingDocument", {"referral_id": referral.id, "document_type": document_type})
            return self._finish(run, referral, ReferralState.WAITING_FOR_DOCUMENTS, NextAction.REQUEST_DOCUMENT, "Required referral documentation is missing", missing_documents=missing)

        transition(referral, ReferralState.CHECKING_COVERAGE)
        run.state = ReferralState.CHECKING_COVERAGE
        self.db.commit()
        coverages = self._tool(run, "getCoverage", {"id": referral.patient_id}, CoverageListResult)
        if not any(item.status.casefold() == "active" for item in coverages.items):
            return self._finish(run, referral, ReferralState.COVERAGE_UNVERIFIED, NextAction.VERIFY_COVERAGE, "No active synthetic coverage record is available")

        transition(referral, ReferralState.MATCHING_PROVIDER)
        run.state = ReferralState.MATCHING_PROVIDER
        self.db.commit()
        providers = self._tool(run, "findProviders", {"specialty": effective_specialty}, ProviderListResult)
        if not providers.items:
            return self._finish(run, referral, ReferralState.PROVIDER_UNAVAILABLE, NextAction.PROVIDER_UNAVAILABLE, "No eligible synthetic provider is available")
        slots = []
        for provider in providers.items:
            result = self._tool(run, "getAvailableSlots", {"provider_id": provider.id}, SlotListResult)
            slots.extend(result.items)
        if not slots:
            return self._finish(run, referral, ReferralState.PROVIDER_UNAVAILABLE, NextAction.PROVIDER_UNAVAILABLE, "Eligible providers have no available slots", provider_ids=[item.id for item in providers.items])
        return self._finish(run, referral, ReferralState.WAITING_FOR_SLOT_SELECTION, NextAction.PRESENT_SLOTS, "Available slots are ready for human selection", provider_ids=[item.id for item in providers.items], slot_ids=[item.id for item in slots])

    def _tool(self, run: WorkflowRun, name: str, arguments: dict[str, Any], expected_type: type[BaseModel] | None = None):
        self.fault_injector.hit(f"tool:{name}")
        result = invoke_tool(name, self.db, arguments)
        if expected_type is not None and not isinstance(result, expected_type):
            raise TypeError(f"Unexpected {name} result")
        self._event(run, "tool_completed", {"tool": name, "result_count": len(getattr(result, "items", [])) if hasattr(result, "items") else 1})
        return result

    def _event(self, run: WorkflowRun, event_type: str, payload: dict[str, Any]) -> None:
        self.db.add(AgentEvent(workflow_run_id=run.id, event_type=event_type, payload=payload))
        self.db.commit()

    def _finish(self, run: WorkflowRun, referral: Referral, state: ReferralState, action: NextAction, summary: str, **details: Any) -> ProcessingResult:
        transition(referral, state)
        run.state = state
        self.db.commit()
        self._event(run, "workflow_completed", {"state": state.value, "next_action": action.value})
        return ProcessingResult(workflow_run_id=run.id, referral_id=referral.id, state=state, next_action=action, summary=summary, **details)
