from sqlalchemy import select
from sqlalchemy.orm import Session

from .model_providers import DocumentInvestigationAction, DocumentInvestigationDecision, DocumentInvestigationInput, InvestigationAction, InvestigationDecision, InvestigationInput, ReferralModel
from .models import AgentEvent, Provider, Referral, WorkflowRun
from .tools import DocumentListResult, ProcedureListResult, ReferralHistoryResult, invoke_tool


class InvestigationPolicyError(ValueError):
    pass


class SpecialtyInvestigator:
    max_turns = 4
    max_tool_calls = 2

    def __init__(self, db: Session, model: ReferralModel, run: WorkflowRun):
        self.db = db
        self.model = model
        self.run = run

    async def investigate(self, referral: Referral) -> str | None:
        candidates = list(self.db.scalars(select(Provider.specialty).where(Provider.accepting_new_patients.is_(True), Provider.is_synthetic.is_(True), Provider.is_evaluation.is_(False)).distinct().order_by(Provider.specialty)))
        observations: dict = {}
        tool_calls = 0
        for turn in range(self.max_turns):
            available = self._available(observations, tool_calls)
            decision = await self.model.investigate(InvestigationInput(referral_reason=referral.reason, submitted_specialty=referral.requested_specialty, candidate_specialties=candidates, available_actions=available, observations=observations))
            self._validate(decision, available, candidates, referral, observations)
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

    async def investigate(self, referral: Referral, specialty: str, current_document_types: set[str]) -> str | None:
        observations: dict = {}
        tool_calls = 0
        for turn in range(self.max_turns):
            available = self._available(observations, tool_calls)
            decision = await self.model.investigate_documents(DocumentInvestigationInput(referral_reason=referral.reason, specialty=specialty, current_document_types=sorted(current_document_types), candidate_document_types=self.candidate_document_types, available_actions=available, observations=observations))
            self._validate(decision, available, referral, current_document_types, observations)
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
