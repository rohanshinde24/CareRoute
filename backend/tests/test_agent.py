import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

from app.provider_gateway import LocalProviderGateway
from app.agent import NextAction, ReferralCoordinator
from app.model_providers import DeterministicReferralModel, DocumentInvestigationAction, DocumentInvestigationDecision, InvestigationAction, InvestigationDecision, ModelResponseError, ProviderInvestigationAction, ProviderInvestigationDecision, ReferralInterpretation
from app.models import AgentEvent, Coverage, Patient, PatientProcedure, Referral, ReferralDocument, ReferralState
from app.provider_models import AppointmentSlot, Provider, ProviderSchedule
from app.provider_gateway import LocalProviderGateway

def build_case(db, provider_db, *, document=True, coverage=True, provider=True, slot=True):
    patient = Patient(external_id="agent-patient", source="synthea", given_name="Case", family_name="Agent", birth_date=date(1980, 1, 1), is_synthetic=True)
    db.add(patient)
    db.flush()
    referral = Referral(patient_id=patient.id, requested_specialty="Cardiology", reason="Administrative cardiology referral", is_synthetic=True)
    db.add(referral)
    db.flush()
    if document:
        db.add(ReferralDocument(referral_id=referral.id, document_type="clinical-note", storage_locator="synthetic://note"))
    if coverage:
        db.add(Coverage(patient_id=patient.id, external_id="coverage-1", source="synthetic-payer", payer_name="Test Plan", member_id="member-1", status="active", is_synthetic=True))
    db.commit()
    if provider:
        clinician = Provider(name="Synthetic Cardiologist", specialty="Cardiology", location="Test, CA", is_synthetic=True)
        provider_db.add(clinician)
        provider_db.flush()
        schedule = ProviderSchedule(provider_id=clinician.id, name="Clinic", timezone="UTC")
        provider_db.add(schedule)
        provider_db.flush()
        if slot:
            start = datetime.now(timezone.utc) + timedelta(days=1)
            provider_db.add(AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30)))
        provider_db.commit()
    return referral

def run(coordinator, referral):
    return asyncio.run(coordinator.process(referral.id))

def test_processes_complete_referral_to_slot_selection(db, provider_db):
    referral = build_case(db, provider_db)
    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    assert result.state == ReferralState.WAITING_FOR_SLOT_SELECTION
    assert result.next_action == NextAction.PRESENT_SLOTS
    assert len(result.slot_ids) == 1
    events = db.query(AgentEvent).filter_by(workflow_run_id=result.workflow_run_id).all()
    assert [event.event_type for event in events][0] == "workflow_started"
    assert events[-1].payload["next_action"] == "PRESENT_SLOTS"

def test_coordinator_uses_provider_gateway_with_workflow_correlation(db, provider_db):
    referral = build_case(db, provider_db)

    class RecordingGateway(LocalProviderGateway):
        def __init__(self, session):
            super().__init__(session)
            self.calls = []

        async def find_providers(self, specialty, include_evaluation, correlation_id):
            self.calls.append(("findProviders", correlation_id))
            return await super().find_providers(specialty, include_evaluation, correlation_id)

        async def get_available_slots(self, provider_id, correlation_id):
            self.calls.append(("getAvailableSlots", correlation_id))
            return await super().get_available_slots(provider_id, correlation_id)

    gateway = RecordingGateway(provider_db)
    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=gateway), referral)
    events = db.query(AgentEvent).filter_by(workflow_run_id=result.workflow_run_id).all()

    assert [name for name, _ in gateway.calls] == ["findProviders", "getAvailableSlots"]
    assert all(correlation_id == result.workflow_run_id for _, correlation_id in gateway.calls)
    provider_events = [event for event in events if event.payload.get("boundary") == "provider-service"]
    assert [event.payload["tool"] for event in provider_events] == ["findProviders", "getAvailableSlots"]

def test_requests_missing_required_document(db, provider_db):
    referral = build_case(db, provider_db, document=False)
    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    assert result.state == ReferralState.WAITING_FOR_DOCUMENTS
    assert result.missing_documents == ["clinical-note"]

def test_escalates_when_coverage_is_missing(db, provider_db):
    referral = build_case(db, provider_db, coverage=False)
    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    assert result.state == ReferralState.COVERAGE_UNVERIFIED
    assert result.next_action == NextAction.VERIFY_COVERAGE

class ConflictingModel:
    name = "conflicting-test-model"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Neurology", confidence=1, is_ambiguous=False, evidence=["test"])

class InvalidModel:
    name = "invalid-test-model"

    async def interpret(self, _):
        raise ModelResponseError("Invalid structured model response")

def test_conflicting_model_specialty_requires_human_review(db, provider_db):
    referral = build_case(db, provider_db)
    result = run(ReferralCoordinator(db, ConflictingModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert result.next_action == NextAction.ESCALATE_HUMAN

def test_invalid_model_response_requires_human_review(db, provider_db):
    referral = build_case(db, provider_db)
    result = run(ReferralCoordinator(db, InvalidModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "Invalid structured" in result.summary

def test_ambiguous_referral_invokes_bounded_evidence_investigation(db, provider_db):
    patient = Patient(external_id="investigation-patient", source="synthea", given_name="Case", family_name="Investigation", birth_date=date(1980, 1, 1), is_synthetic=True)
    db.add(patient)
    db.flush()
    db.add(Referral(patient_id=patient.id, requested_specialty="Cardiology", reason="Prior synthetic referral", state=ReferralState.CONFIRMED, is_synthetic=True))
    referral = Referral(patient_id=patient.id, requested_specialty="Unknown", reason="Follow-up requested; specialty not stated", is_synthetic=True)
    db.add(referral)
    db.flush()
    db.add_all([
        ReferralDocument(referral_id=referral.id, document_type="clinical-note", storage_locator="synthetic://investigation-note"),
        Coverage(patient_id=patient.id, external_id="investigation-coverage", source="synthetic-payer", payer_name="Test Plan", member_id="investigation-member", status="active", is_synthetic=True),
    ])
    db.commit()
    provider = Provider(name="Investigation Cardiologist", specialty="Cardiology", location="Test, CA", is_synthetic=True)
    provider_db.add(provider)
    provider_db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name="Clinic", timezone="UTC")
    provider_db.add(schedule)
    provider_db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    provider_db.add(AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30)))
    provider_db.commit()

    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    events = db.query(AgentEvent).filter_by(workflow_run_id=result.workflow_run_id).all()
    decisions = [event.payload["action"] for event in events if event.event_type == "investigation_decision"]
    assert result.state == ReferralState.WAITING_FOR_SLOT_SELECTION
    assert decisions == ["GET_REFERRAL_HISTORY", "PROPOSE_SPECIALTY"]

class PrematureProposalModel:
    name = "premature-proposal"

    async def interpret(self, _):
        return ReferralInterpretation(specialty=None, confidence=0, is_ambiguous=True, evidence=[])

    async def investigate(self, _):
        return InvestigationDecision(action=InvestigationAction.PROPOSE_SPECIALTY, proposed_specialty="Cardiology")

def test_investigation_policy_rejects_premature_specialty_proposal(db, provider_db):
    referral = build_case(db, provider_db)
    referral.requested_specialty = "Unknown"
    db.commit()
    result = run(ReferralCoordinator(db, PrematureProposalModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "not allowed by policy" in result.summary

def test_referenced_procedure_triggers_bounded_document_investigation(db, provider_db):
    referral = build_case(db, provider_db)
    referral.reason = "Recurrent palpitations; Holter monitoring completed last week"
    db.add(PatientProcedure(patient_id=referral.patient_id, procedure_type="Holter monitoring", occurred_at=datetime.now(timezone.utc) - timedelta(days=7), report_document_type="holter-report", is_synthetic=True))
    db.commit()

    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    events = db.query(AgentEvent).filter_by(workflow_run_id=result.workflow_run_id).all()
    decisions = [event.payload["action"] for event in events if event.event_type == "document_investigation_decision"]
    assert result.state == ReferralState.WAITING_FOR_DOCUMENTS
    assert result.missing_documents == ["holter-report"]
    assert decisions == ["GET_RECENT_PROCEDURES", "PROPOSE_DOCUMENT"]

class UngroundedDocumentModel:
    name = "ungrounded-document"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_documents(self, _):
        return DocumentInvestigationDecision(action=DocumentInvestigationAction.PROPOSE_DOCUMENT, proposed_document_type="pathology-report")

def test_document_policy_rejects_premature_ungrounded_proposal(db, provider_db):
    referral = build_case(db, provider_db)
    referral.reason = "Holter monitoring completed last week"
    db.commit()
    result = run(ReferralCoordinator(db, UngroundedDocumentModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "not allowed by policy" in result.summary

def add_rankable_provider(db, provider_db, referral, *, name="Z Preferred Cardiologist", location="San Francisco, CA", days_until_slot=2, with_slot=True):
    provider = Provider(name=name, specialty="Cardiology", location=location, is_synthetic=True)
    provider_db.add(provider)
    provider_db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name="Preferred clinic", timezone="UTC")
    provider_db.add(schedule)
    provider_db.flush()
    if with_slot:
        start = datetime.now(timezone.utc) + timedelta(days=days_until_slot)
        provider_db.add(AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30)))
    provider_db.commit()
    referral.location_preference = "San Francisco"
    db.commit()
    return provider

def test_contextual_provider_ranking_collects_availability_then_reorders(db, provider_db):
    referral = build_case(db, provider_db)
    preferred = add_rankable_provider(db, provider_db, referral)

    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    events = db.query(AgentEvent).filter_by(workflow_run_id=result.workflow_run_id).all()
    decisions = [event.payload["action"] for event in events if event.event_type == "provider_investigation_decision"]

    assert result.state == ReferralState.WAITING_FOR_SLOT_SELECTION
    assert result.provider_ids[0] == preferred.id
    assert decisions == ["GET_AVAILABLE_SLOTS", "PROPOSE_PROVIDER"]
    assert referral.selected_slot_id is None

def test_contextual_provider_ranking_abstains_without_location_match(db, provider_db):
    referral = build_case(db, provider_db)
    first_provider_id = provider_db.query(Provider).one().id
    add_rankable_provider(db, provider_db, referral, location="Berkeley, CA")

    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.WAITING_FOR_SLOT_SELECTION
    assert result.provider_ids[0] == first_provider_id
    assert "preference could not be applied" in result.summary

def test_contextual_provider_ranking_compares_matching_candidates(db, provider_db):
    referral = build_case(db, provider_db)
    later = add_rankable_provider(db, provider_db, referral, name="A Later Cardiologist", days_until_slot=3)
    earlier = add_rankable_provider(db, provider_db, referral, name="Z Earlier Cardiologist", days_until_slot=2)

    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)
    events = db.query(AgentEvent).filter_by(workflow_run_id=result.workflow_run_id).all()
    decisions = [event.payload["action"] for event in events if event.event_type == "provider_investigation_decision"]

    assert result.provider_ids[0] == earlier.id
    assert result.provider_ids[0] != later.id
    assert decisions == ["GET_AVAILABLE_SLOTS", "GET_AVAILABLE_SLOTS", "PROPOSE_PROVIDER"]
    assert "preference was applied" in result.summary

class PrematureProviderProposalModel:
    name = "premature-provider-proposal"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=step.candidate_providers[0].id)

def test_provider_policy_rejects_premature_proposal(db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral)

    result = run(ReferralCoordinator(db, PrematureProviderProposalModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "not allowed by policy" in result.summary

class OutOfCandidateProviderModel:
    name = "out-of-candidate-provider"

    def __init__(self):
        self.observed = False

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        if not self.observed:
            self.observed = True
            matching = next(candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location)
            return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=matching.id)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=uuid.uuid4())

def test_provider_policy_rejects_out_of_candidate_proposal(db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral)

    result = run(ReferralCoordinator(db, OutOfCandidateProviderModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "not in the deterministic candidate set" in result.summary

class UnobservedProviderModel:
    name = "unobserved-provider"

    def __init__(self):
        self.observed = False

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        matching = [candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location]
        if not self.observed:
            self.observed = True
            return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=matching[0].id)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=matching[1].id)

def test_provider_policy_rejects_unobserved_matching_candidate(db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral, name="A Observed Cardiologist")
    add_rankable_provider(db, provider_db, referral, name="B Unobserved Cardiologist")

    result = run(ReferralCoordinator(db, UnobservedProviderModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "not allowed by policy" in result.summary

class EmptyAvailabilityProviderModel:
    name = "empty-availability-provider"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        matching = [candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location]
        observed = step.observations.get("available_slots", {})
        for candidate in matching:
            if str(candidate.id) not in observed:
                return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=candidate.id)
        empty = next(candidate for candidate in matching if "No availability" in candidate.location)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=empty.id)

def test_provider_policy_rejects_candidate_without_free_slots(db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral, name="A Empty Cardiologist", location="San Francisco, CA - No availability", with_slot=False)
    add_rankable_provider(db, provider_db, referral, name="B Available Cardiologist", location="San Francisco, CA - Available")

    result = run(ReferralCoordinator(db, EmptyAvailabilityProviderModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "no observed free-slot availability" in result.summary

class LaterAvailabilityProviderModel:
    name = "later-availability-provider"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        matching = [candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location]
        observed = step.observations.get("available_slots", {})
        for candidate in matching:
            if str(candidate.id) not in observed:
                return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=candidate.id)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=matching[0].id)

def test_provider_policy_rejects_later_availability(db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral, name="A Later Cardiologist", days_until_slot=3)
    add_rankable_provider(db, provider_db, referral, name="Z Earlier Cardiologist", days_until_slot=2)

    result = run(ReferralCoordinator(db, LaterAvailabilityProviderModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "does not have the earliest observed availability" in result.summary
