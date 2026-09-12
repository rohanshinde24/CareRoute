import enum
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from .model_providers import ModelResponseError, ReferralModel, ReferralModelInput
from .models import AgentEvent, Referral, ReferralState, WorkflowRun
from .provider_database import provider_session
from .provider_gateway import ProviderGateway, ProviderGatewayProtocolError, configured_provider_gateway


def _default_gateway() -> ProviderGateway:
    from .config import settings

    if settings.provider_service_url:
        return configured_provider_gateway(None)
    return configured_provider_gateway(provider_session())
from .tools import CoverageListResult, DocumentListResult, ProviderListResult, ReferralToolResult, SlotListResult, invoke_tool
from .workflow import transition
from .faults import FaultInjector
from .investigation import DocumentInvestigator, InvestigationPolicyError, ProviderInvestigator, SpecialtyInvestigator
from .telemetry import current_trace_id, operation, set_current_attributes, set_span_attributes, traced
from .metrics import record_model_call, record_model_confidence, record_workflow_run

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
    def __init__(self, db: Session, model: ReferralModel, fault_injector: FaultInjector | None = None, provider_gateway: ProviderGateway | None = None):
        self.db = db
        self.model = model
        self.fault_injector = fault_injector or FaultInjector()
        # The coordinator never reads provider tables through its own session.
        # A caller may inject a gateway; otherwise the configured one is used,
        # which opens a provider session only when there is no service to call.
        self.provider_gateway = provider_gateway or _default_gateway()

    @traced("careroute.workflow.process", {"careroute.component": "referral-coordinator"})
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
        set_current_attributes(
            {
                "careroute.workflow.run_id": str(run.id),
                "careroute.workflow.state": ReferralState.PARSING.value,
                "careroute.model.provider": self.model.name,
                "careroute.data.synthetic": referral.is_synthetic,
                "careroute.data.evaluation": referral.is_evaluation,
            }
        )
        trace_id = current_trace_id()
        self._event(run, "workflow_started", {"model": self.model.name, **({"trace_id": trace_id} if trace_id else {})})
        referral_data = self._tool(run, "getReferral", {"id": referral.id}, ReferralToolResult)
        self._tool(run, "getPatient", {"id": referral_data.patient_id})

        try:
            self.fault_injector.hit("model_timeout")
            with operation("careroute.model.interpret", {"careroute.model.provider": self.model.name}) as model_span:
                started = time.perf_counter()
                interpretation = await self.model.interpret(ReferralModelInput(requested_specialty=referral_data.requested_specialty, reason=referral_data.reason))
                record_model_call(time.perf_counter() - started, self.model.name)
                record_model_confidence(interpretation.confidence, self.model.name)
                set_span_attributes(
                    model_span,
                    {
                        "careroute.model.ambiguous": interpretation.is_ambiguous,
                        "careroute.model.confidence": interpretation.confidence,
                    },
                )
        except (ModelResponseError, ValidationError) as exc:
            summary = str(exc) if isinstance(exc, ModelResponseError) else "Referral specialty failed semantic validation"
            return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, summary)
        self._event(run, "model_interpretation", {"model": self.model.name, "specialty": interpretation.specialty, "confidence": interpretation.confidence, "is_ambiguous": interpretation.is_ambiguous})

        effective_specialty = interpretation.specialty
        if interpretation.is_ambiguous or not interpretation.specialty:
            try:
                effective_specialty = await SpecialtyInvestigator(self.db, self.model, run, self.provider_gateway, self.fault_injector).investigate(referral)
            except (ModelResponseError, ValidationError, InvestigationPolicyError, ProviderGatewayProtocolError) as exc:
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
        try:
            providers = await self._provider_tool(run, "findProviders", specialty=effective_specialty, include_evaluation=referral.is_evaluation)
        except ProviderGatewayProtocolError as exc:
            return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, f"Provider service response failed validation: {exc}")
        if not providers.items:
            return self._finish(run, referral, ReferralState.PROVIDER_UNAVAILABLE, NextAction.PROVIDER_UNAVAILABLE, "No eligible synthetic provider is available")
        ordered_providers = list(providers.items)
        ranking_note = ""
        if referral.location_preference and len(ordered_providers) >= 2:
            try:
                proposed_provider_id = await ProviderInvestigator(self.db, self.model, run, self.provider_gateway, self.fault_injector).investigate(referral, ordered_providers)
            except (ModelResponseError, ValidationError, InvestigationPolicyError, ProviderGatewayProtocolError) as exc:
                return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, f"Provider investigation violated ranking policy: {exc}")
            if proposed_provider_id is not None:
                ordered_providers.sort(key=lambda item: item.id != proposed_provider_id)
                ranking_note = " The location preference was applied using observed availability."
            else:
                ranking_note = " The location preference could not be applied; all eligible alternatives remain available."
        slots = []
        for provider in ordered_providers:
            try:
                result = await self._provider_tool(run, "getAvailableSlots", provider_id=provider.id)
            except ProviderGatewayProtocolError as exc:
                return self._finish(run, referral, ReferralState.NEEDS_HUMAN_REVIEW, NextAction.ESCALATE_HUMAN, f"Provider service response failed validation: {exc}")
            slots.extend(result.items)
        if not slots:
            return self._finish(run, referral, ReferralState.PROVIDER_UNAVAILABLE, NextAction.PROVIDER_UNAVAILABLE, "Eligible providers have no available slots", provider_ids=[item.id for item in ordered_providers])
        return self._finish(run, referral, ReferralState.WAITING_FOR_SLOT_SELECTION, NextAction.PRESENT_SLOTS, "Available slots are ready for human selection." + ranking_note, provider_ids=[item.id for item in ordered_providers], slot_ids=[item.id for item in slots])

    def _tool(self, run: WorkflowRun, name: str, arguments: dict[str, Any], expected_type: type[BaseModel] | None = None):
        self.fault_injector.hit(f"tool:{name}")
        result = invoke_tool(name, self.db, arguments)
        if expected_type is not None and not isinstance(result, expected_type):
            raise TypeError(f"Unexpected {name} result")
        self._event(run, "tool_completed", {"tool": name, "result_count": len(getattr(result, "items", [])) if hasattr(result, "items") else 1})
        return result

    async def _provider_tool(self, run: WorkflowRun, name: str, **arguments):
        self.fault_injector.hit(f"tool:{name}")
        if name == "findProviders":
            result = await self.provider_gateway.find_providers(arguments["specialty"], arguments["include_evaluation"], run.id)
        elif name == "getAvailableSlots":
            result = await self.provider_gateway.get_available_slots(arguments["provider_id"], run.id)
        else:
            raise ValueError(f"Unknown provider operation: {name}")
        self._event(run, "tool_completed", {"tool": name, "boundary": "provider-service", "result_count": len(result.items)})
        return result

    def _event(self, run: WorkflowRun, event_type: str, payload: dict[str, Any]) -> None:
        self.db.add(AgentEvent(workflow_run_id=run.id, event_type=event_type, payload=payload))
        self.db.commit()

    def _finish(self, run: WorkflowRun, referral: Referral, state: ReferralState, action: NextAction, summary: str, **details: Any) -> ProcessingResult:
        with operation(
            "careroute.workflow.complete",
            {
                "careroute.workflow.run_id": str(run.id),
                "careroute.workflow.state": state.value,
                "careroute.workflow.next_action": action.value,
            },
        ):
            record_workflow_run(state.value)
            transition(referral, state)
            run.state = state
            self.db.commit()
            self._event(run, "workflow_completed", {"state": state.value, "next_action": action.value})
            return ProcessingResult(workflow_run_id=run.id, referral_id=referral.id, state=state, next_action=action, summary=summary, **details)
