import uuid

from sqlalchemy.orm import Session

from .model_providers import DocumentInvestigationAction, DocumentInvestigationDecision, DocumentInvestigationInput, InvestigationAction, InvestigationDecision, InvestigationInput, ProviderCandidate, ProviderInvestigationAction, ProviderInvestigationDecision, ProviderInvestigationInput, ReferralModel
from .models import AgentEvent, Referral, WorkflowRun
from .faults import FaultInjector
from .provider_gateway import ProviderGateway
from .tools import DocumentListResult, ProcedureListResult, ProviderToolResult, ReferralHistoryResult, SlotListResult, invoke_tool
from .telemetry import operation, set_current_attributes, set_span_attributes, traced


class InvestigationPolicyError(ValueError):
    pass


class SpecialtyInvestigator:
    max_turns = 4
    max_tool_calls = 2

    def __init__(self, db: Session, model: ReferralModel, run: WorkflowRun, provider_gateway: ProviderGateway, fault_injector: FaultInjector):
        self.db = db
        self.model = model
        self.run = run
        self.provider_gateway = provider_gateway
        self.fault_injector = fault_injector

    @traced("careroute.agent.investigate.specialty", {"careroute.agent.kind": "specialty"})
    async def investigate(self, referral: Referral) -> str | None:
        set_current_attributes({"careroute.workflow.run_id": str(self.run.id)})
        self.fault_injector.hit("tool:listProviderSpecialties")
        candidates = (await self.provider_gateway.list_specialties(self.run.id)).items
        self._event("investigation_observation", {"tool": "listProviderSpecialties", "boundary": "provider-service", "result_count": len(candidates)})
        observations: dict = {}
        tool_calls = 0
        for turn in range(self.max_turns):
            available = self._available(observations, tool_calls)
            with operation("careroute.agent.turn", {"careroute.agent.kind": "specialty", "careroute.agent.turn": turn + 1}) as turn_span:
                decision = await self.model.investigate(InvestigationInput(referral_reason=referral.reason, submitted_specialty=referral.requested_specialty, candidate_specialties=candidates, available_actions=available, observations=observations))
                set_span_attributes(turn_span, {"careroute.agent.action": decision.action.value})
                with operation("careroute.agent.policy.validate", {"careroute.agent.kind": "specialty", "careroute.agent.action": decision.action.value}) as policy_span:
                    self._validate(decision, available, candidates, referral, observations)
                    set_span_attributes(policy_span, {"careroute.policy.outcome": "accepted"})
            self._event("investigation_decision", {"turn": turn + 1, "action": decision.action.value, "evidence_sources": sorted(observations)})
            if decision.action == InvestigationAction.GET_DOCUMENTS:
                result = invoke_tool("getReferralDocuments", self.db, {"id": referral.id})
                assert isinstance(result, DocumentListResult)
                observations["document_types"] = [item.document_type for item in result.items]
                tool_calls += 1
                self._event("investigation_observation", {"tool": "getReferralDocuments", "result_count": len(result.items)})
            elif decision.action == InvestigationAction.GET_REFERRAL_HISTORY:
                result = invoke_tool("getReferralHistory", self.db, {"id": referral.id})
                assert isinstance(result, ReferralHistoryResult)
                observations["prior_specialties"] = [item.requested_specialty for item in result.items]
                tool_calls += 1
                self._event("investigation_observation", {"tool": "getReferralHistory", "result_count": len(result.items)})
            elif decision.action == InvestigationAction.PROPOSE_SPECIALTY:
                self._event("investigation_completed", {"outcome": "specialty_proposed", "specialty": decision.proposed_specialty, "turns": turn + 1, "tool_calls": tool_calls})
                return decision.proposed_specialty
            else:
                self._event("investigation_completed", {"outcome": decision.action.value, "turns": turn + 1, "tool_calls": tool_calls})
                return None
        self._event("investigation_completed", {"outcome": "turn_limit", "turns": self.max_turns, "tool_calls": tool_calls})
        return None

    def _available(self, observations: dict, tool_calls: int) -> list[InvestigationAction]:
        actions = [InvestigationAction.PROPOSE_SPECIALTY] if tool_calls > 0 else []
        if tool_calls > 0:
            actions.extend([InvestigationAction.REQUEST_CLARIFICATION, InvestigationAction.ESCALATE])
        if tool_calls < self.max_tool_calls:
            if "document_types" not in observations:
                actions.insert(0, InvestigationAction.GET_DOCUMENTS)
            if "prior_specialties" not in observations:
                actions.insert(0, InvestigationAction.GET_REFERRAL_HISTORY)
        return actions

    def _validate(self, decision: InvestigationDecision, available: list[InvestigationAction], candidates: list[str], referral: Referral, observations: dict) -> None:
        if decision.action not in available:
            raise InvestigationPolicyError("Model selected an action not allowed by policy")
        if decision.action != InvestigationAction.PROPOSE_SPECIALTY:
            if decision.proposed_specialty is not None:
                raise InvestigationPolicyError("Only PROPOSE_SPECIALTY may include a specialty")
            return
        if decision.proposed_specialty not in candidates:
            raise InvestigationPolicyError("Proposed specialty is not in the deterministic candidate set")
        evidence_space = " ".join([referral.reason, *observations.get("document_types", []), *observations.get("prior_specialties", [])]).casefold()
        if decision.proposed_specialty.casefold() not in evidence_space:
            raise InvestigationPolicyError("Proposed specialty is not present in observed evidence")

    def _event(self, event_type: str, payload: dict) -> None:
        self.db.add(AgentEvent(workflow_run_id=self.run.id, event_type=event_type, payload=payload))
        self.db.commit()


class DocumentInvestigator:
    max_turns = 4
    max_tool_calls = 2
    candidate_document_types = ["holter-report", "imaging-report", "lab-results", "pathology-report"]

    def __init__(self, db: Session, model: ReferralModel, run: WorkflowRun):
        self.db = db
        self.model = model
        self.run = run

    @traced("careroute.agent.investigate.document", {"careroute.agent.kind": "document"})
    async def investigate(self, referral: Referral, specialty: str, current_document_types: set[str]) -> str | None:
        set_current_attributes({"careroute.workflow.run_id": str(self.run.id)})
        observations: dict = {}
        tool_calls = 0
        for turn in range(self.max_turns):
            available = self._available(observations, tool_calls)
            with operation("careroute.agent.turn", {"careroute.agent.kind": "document", "careroute.agent.turn": turn + 1}) as turn_span:
                decision = await self.model.investigate_documents(DocumentInvestigationInput(referral_reason=referral.reason, specialty=specialty, current_document_types=sorted(current_document_types), candidate_document_types=self.candidate_document_types, available_actions=available, observations=observations))
                set_span_attributes(turn_span, {"careroute.agent.action": decision.action.value})
                with operation("careroute.agent.policy.validate", {"careroute.agent.kind": "document", "careroute.agent.action": decision.action.value}) as policy_span:
                    self._validate(decision, available, referral, current_document_types, observations)
                    set_span_attributes(policy_span, {"careroute.policy.outcome": "accepted"})
            self._event("document_investigation_decision", {"turn": turn + 1, "action": decision.action.value, "evidence_sources": sorted(observations)})
            if decision.action == DocumentInvestigationAction.GET_RECENT_PROCEDURES:
                result = invoke_tool("getRecentProcedures", self.db, {"id": referral.patient_id})
                assert isinstance(result, ProcedureListResult)
                observations["recent_procedures"] = [item.model_dump(mode="json") for item in result.items]
                tool_calls += 1
                self._event("document_investigation_observation", {"tool": "getRecentProcedures", "result_count": len(result.items)})
            elif decision.action == DocumentInvestigationAction.GET_REFERRAL_HISTORY:
                result = invoke_tool("getReferralHistory", self.db, {"id": referral.id})
                assert isinstance(result, ReferralHistoryResult)
                observations["prior_specialties"] = [item.requested_specialty for item in result.items]
                tool_calls += 1
                self._event("document_investigation_observation", {"tool": "getReferralHistory", "result_count": len(result.items)})
            elif decision.action == DocumentInvestigationAction.PROPOSE_DOCUMENT:
                self._event("document_investigation_completed", {"outcome": "document_proposed", "document_type": decision.proposed_document_type, "turns": turn + 1, "tool_calls": tool_calls})
                return decision.proposed_document_type
            else:
                self._event("document_investigation_completed", {"outcome": decision.action.value, "turns": turn + 1, "tool_calls": tool_calls})
                return None
        self._event("document_investigation_completed", {"outcome": "turn_limit", "turns": self.max_turns, "tool_calls": tool_calls})
        return None

    def _available(self, observations: dict, tool_calls: int) -> list[DocumentInvestigationAction]:
        actions = [DocumentInvestigationAction.PROPOSE_DOCUMENT, DocumentInvestigationAction.REQUEST_CLARIFICATION, DocumentInvestigationAction.ESCALATE] if tool_calls > 0 else []
        if tool_calls < self.max_tool_calls:
            if "recent_procedures" not in observations:
                actions.insert(0, DocumentInvestigationAction.GET_RECENT_PROCEDURES)
            if "prior_specialties" not in observations:
                actions.append(DocumentInvestigationAction.GET_REFERRAL_HISTORY)
        return actions

    def _validate(self, decision: DocumentInvestigationDecision, available: list[DocumentInvestigationAction], referral: Referral, current_document_types: set[str], observations: dict) -> None:
        if decision.action not in available:
            raise InvestigationPolicyError("Model selected a document action not allowed by policy")
        if decision.action != DocumentInvestigationAction.PROPOSE_DOCUMENT:
            return
        proposed = decision.proposed_document_type
        if proposed not in self.candidate_document_types:
            raise InvestigationPolicyError("Proposed document is not in the deterministic candidate set")
        if proposed.casefold() in current_document_types:
            raise InvestigationPolicyError("Proposed document is already present")
        matching = [item for item in observations.get("recent_procedures", []) if item["report_document_type"].casefold() == proposed.casefold()]
        if not matching or not any(item["procedure_type"].casefold() in referral.reason.casefold() for item in matching):
            raise InvestigationPolicyError("Proposed document is not grounded in an observed referenced procedure")

    def _event(self, event_type: str, payload: dict) -> None:
        self.db.add(AgentEvent(workflow_run_id=self.run.id, event_type=event_type, payload=payload))
        self.db.commit()


class ProviderInvestigator:
    max_turns = 4
    max_tool_calls = 3

    def __init__(self, db: Session, model: ReferralModel, run: WorkflowRun, provider_gateway: ProviderGateway, fault_injector: FaultInjector):
        self.db = db
        self.model = model
        self.run = run
        self.provider_gateway = provider_gateway
        self.fault_injector = fault_injector

    @traced("careroute.agent.investigate.provider", {"careroute.agent.kind": "provider"})
    async def investigate(self, referral: Referral, providers: list[ProviderToolResult]) -> uuid.UUID | None:
        set_current_attributes({"careroute.workflow.run_id": str(self.run.id), "careroute.agent.candidate_count": len(providers)})
        if not referral.location_preference:
            return None
        candidates = [ProviderCandidate(id=provider.id, location=provider.location) for provider in providers]
        matching = [candidate for candidate in candidates if referral.location_preference.casefold() in candidate.location.casefold()]
        if not matching:
            self._event("provider_investigation_completed", {"outcome": "no_location_match", "turns": 0, "tool_calls": 0})
            return None
        if len(matching) > self.max_tool_calls:
            self._event("provider_investigation_completed", {"outcome": "candidate_limit", "candidate_count": len(matching), "turns": 0, "tool_calls": 0})
            return None
        observations: dict = {}
        tool_calls = 0
        for turn in range(self.max_turns):
            available = self._available(observations, tool_calls, matching)
            with operation("careroute.agent.turn", {"careroute.agent.kind": "provider", "careroute.agent.turn": turn + 1}) as turn_span:
                decision = await self.model.investigate_providers(ProviderInvestigationInput(referral_id=referral.id, location_preference=referral.location_preference, candidate_providers=candidates, available_actions=available, observations=observations))
                set_span_attributes(turn_span, {"careroute.agent.action": decision.action.value})
                with operation("careroute.agent.policy.validate", {"careroute.agent.kind": "provider", "careroute.agent.action": decision.action.value}) as policy_span:
                    self._validate(decision, available, candidates, referral.location_preference, observations)
                    set_span_attributes(policy_span, {"careroute.policy.outcome": "accepted"})
            self._event("provider_investigation_decision", {"turn": turn + 1, "action": decision.action.value, "evidence_sources": sorted(observations)})
            if decision.action == ProviderInvestigationAction.GET_AVAILABLE_SLOTS:
                self.fault_injector.hit("tool:getAvailableSlots")
                result = await self.provider_gateway.get_available_slots(decision.target_provider_id, self.run.id)
                slots = observations.setdefault("available_slots", {})
                slots[str(decision.target_provider_id)] = [{"id": str(item.id), "start_at": item.start_at.isoformat()} for item in result.items]
                tool_calls += 1
                self._event("provider_investigation_observation", {"tool": "getAvailableSlots", "provider_id": str(decision.target_provider_id), "result_count": len(result.items)})
            elif decision.action == ProviderInvestigationAction.PROPOSE_PROVIDER:
                self._event("provider_investigation_completed", {"outcome": "provider_proposed", "provider_id": str(decision.proposed_provider_id), "turns": turn + 1, "tool_calls": tool_calls})
                return decision.proposed_provider_id
            else:
                self._event("provider_investigation_completed", {"outcome": decision.action.value, "turns": turn + 1, "tool_calls": tool_calls})
                return None
        self._event("provider_investigation_completed", {"outcome": "turn_limit", "turns": self.max_turns, "tool_calls": tool_calls})
        return None

    def _available(self, observations: dict, tool_calls: int, matching: list[ProviderCandidate]) -> list[ProviderInvestigationAction]:
        observed = observations.get("available_slots", {})
        all_observed = all(str(candidate.id) in observed for candidate in matching)
        actions = [ProviderInvestigationAction.REQUEST_CLARIFICATION, ProviderInvestigationAction.ESCALATE] if tool_calls > 0 else []
        if all_observed and any(observed[str(candidate.id)] for candidate in matching):
            actions.insert(0, ProviderInvestigationAction.PROPOSE_PROVIDER)
        if tool_calls < self.max_tool_calls and any(str(candidate.id) not in observed for candidate in matching):
            actions.insert(0, ProviderInvestigationAction.GET_AVAILABLE_SLOTS)
        return actions

    def _validate(self, decision: ProviderInvestigationDecision, available: list[ProviderInvestigationAction], candidates: list[ProviderCandidate], location_preference: str, observations: dict) -> None:
        if decision.action not in available:
            raise InvestigationPolicyError("Model selected a provider action not allowed by policy")
        candidate_ids = {candidate.id for candidate in candidates}
        if decision.action == ProviderInvestigationAction.GET_AVAILABLE_SLOTS:
            if decision.target_provider_id not in candidate_ids or decision.proposed_provider_id is not None:
                raise InvestigationPolicyError("Provider availability target is outside the deterministic candidate set")
            target = next(item for item in candidates if item.id == decision.target_provider_id)
            if location_preference.casefold() not in target.location.casefold():
                raise InvestigationPolicyError("Provider availability target does not match the explicit preference")
            if str(decision.target_provider_id) in observations.get("available_slots", {}):
                raise InvestigationPolicyError("Provider availability was already observed")
            return
        if decision.action != ProviderInvestigationAction.PROPOSE_PROVIDER:
            if decision.target_provider_id is not None or decision.proposed_provider_id is not None:
                raise InvestigationPolicyError("Provider IDs are not allowed for this action")
            return
        if decision.target_provider_id is not None or decision.proposed_provider_id not in candidate_ids:
            raise InvestigationPolicyError("Proposed provider is not in the deterministic candidate set")
        candidate = next(item for item in candidates if item.id == decision.proposed_provider_id)
        if location_preference.casefold() not in candidate.location.casefold():
            raise InvestigationPolicyError("Proposed provider location does not match the explicit preference")
        matching = [item for item in candidates if location_preference.casefold() in item.location.casefold()]
        available_slots = observations.get("available_slots", {})
        if any(str(item.id) not in available_slots for item in matching):
            raise InvestigationPolicyError("Provider proposal was made before all matching candidates were observed")
        observed_slots = observations.get("available_slots", {}).get(str(candidate.id), [])
        if not observed_slots:
            raise InvestigationPolicyError("Proposed provider has no observed free-slot availability")
        ranked = [
            (min(slot["start_at"] for slot in available_slots[str(item.id)]), item.id)
            for item in matching
            if available_slots[str(item.id)]
        ]
        expected = min(ranked, key=lambda item: (item[0], str(item[1])))[1]
        if decision.proposed_provider_id != expected:
            raise InvestigationPolicyError("Proposed provider does not have the earliest observed availability")

    def _event(self, event_type: str, payload: dict) -> None:
        self.db.add(AgentEvent(workflow_run_id=self.run.id, event_type=event_type, payload=payload))
        self.db.commit()
