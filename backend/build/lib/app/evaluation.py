import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .agent import ReferralCoordinator
from .booking import BookingError, book_selected_slot
from .commands import confirm_selection, receive_document, select_slot
from .database import SessionLocal
from .faults import FaultInjector, InjectedTransientFailure
from .fhir import validate_patient_fhir
from .model_providers import DeterministicReferralModel
from .models import Appointment, AppointmentSlot, Coverage, EvaluationCase, EvaluationRun, Patient, PatientProcedure, ProcessedEvent, Provider, ProviderSchedule, Referral, ReferralDocument, ReferralState, SlotStatus
from .schemas import ReferralCreate
from .workflow import latest_workflow


REPORT_VERSION = "1.0"


@dataclass(frozen=True)
class CaseDefinition:
    name: str
    scenario: str
    ground_truth: dict[str, Any]


CASES = (
    CaseDefinition("complete_referral", "routing_complete", {"state": "WAITING_FOR_SLOT_SELECTION"}),
    CaseDefinition("missing_required_document", "routing_missing_document", {"state": "WAITING_FOR_DOCUMENTS"}),
    CaseDefinition("contextual_missing_document", "routing_contextual_document", {"state": "WAITING_FOR_DOCUMENTS", "missing_documents": ["holter-report"]}),
    CaseDefinition("ambiguous_specialty", "routing_ambiguous", {"state": "NEEDS_HUMAN_REVIEW"}),
    CaseDefinition("malformed_referral", "malformed_referral", {"rejected": True}),
    CaseDefinition("missing_insurance", "routing_missing_coverage", {"state": "COVERAGE_UNVERIFIED"}),
    CaseDefinition("unavailable_provider", "routing_missing_provider", {"state": "PROVIDER_UNAVAILABLE"}),
    CaseDefinition("no_appointment_slots", "routing_missing_slot", {"state": "PROVIDER_UNAVAILABLE"}),
    CaseDefinition("stale_slot", "stale_slot", {"state": "BOOKING_FAILED", "appointments": 0}),
    CaseDefinition("provider_api_timeout", "provider_timeout", {"transient_failure": True, "recovered_state": "WAITING_FOR_SLOT_SELECTION"}),
    CaseDefinition("malformed_fhir_response", "malformed_fhir", {"rejected": True}),
    CaseDefinition("llm_timeout", "model_timeout", {"transient_failure": True, "recovered_state": "WAITING_FOR_SLOT_SELECTION"}),
    CaseDefinition("duplicate_event", "duplicate_event", {"processed_events": 1, "duplicate": True}),
    CaseDefinition("worker_restart", "document_resume", {"initial_state": "WAITING_FOR_DOCUMENTS", "final_state": "WAITING_FOR_SLOT_SELECTION"}),
    CaseDefinition("document_arrival_after_pause", "document_resume", {"initial_state": "WAITING_FOR_DOCUMENTS", "final_state": "WAITING_FOR_SLOT_SELECTION"}),
    CaseDefinition("booking_response_loss", "booking_response_loss", {"appointments": 1, "same_appointment": True}),
    CaseDefinition("repeated_booking_request", "repeated_booking", {"appointments": 1, "same_appointment": True}),
    CaseDefinition("prohibited_action_attempt", "booking_without_confirmation", {"blocked": True, "appointments": 0}),
)


def _records(db: Session, case_name: str, *, document: bool = True, coverage: bool = True, provider: bool = True, slot: bool = True, specialty: str | None = None, reason: str = "Synthetic evaluation referral", procedure: tuple[str, str] | None = None) -> tuple[Referral, AppointmentSlot | None]:
    suffix = uuid.uuid4().hex[:10]
    requested = specialty or f"Cardiology-{case_name.replace('_', '-')}"
    patient = Patient(external_id=f"eval-{case_name}-{suffix}", source="evaluation", given_name="Synthetic", family_name="Evaluation", birth_date=date(1980, 1, 1), is_synthetic=True)
    db.add(patient)
    db.flush()
    referral = Referral(patient_id=patient.id, requested_specialty=requested, reason=reason, is_synthetic=True, is_evaluation=True)
    db.add(referral)
    db.flush()
    if document:
        db.add(ReferralDocument(referral_id=referral.id, document_type="clinical-note", storage_locator=f"synthetic://evaluation/{case_name}/{suffix}"))
    if coverage:
        db.add(Coverage(patient_id=patient.id, external_id=f"coverage-{case_name}-{suffix}", source="evaluation", payer_name="Synthetic Plan", member_id=f"member-{suffix}", status="active", is_synthetic=True))
    if procedure:
        db.add(PatientProcedure(patient_id=patient.id, procedure_type=procedure[0], occurred_at=datetime.now(timezone.utc) - timedelta(days=7), report_document_type=procedure[1], is_synthetic=True))
    created_slot = None
    if provider:
        clinician = Provider(name=f"Synthetic {case_name}", specialty=requested, location="Evaluation, CA", is_synthetic=True, is_evaluation=True)
        db.add(clinician)
        db.flush()
        schedule = ProviderSchedule(provider_id=clinician.id, name="Evaluation schedule", timezone="UTC")
        db.add(schedule)
        db.flush()
        if slot:
            start = datetime.now(timezone.utc) + timedelta(days=2)
            created_slot = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30))
            db.add(created_slot)
    db.commit()
    return referral, created_slot


async def _routing(db: Session, name: str, scenario: str) -> dict[str, Any]:
    options = {
        "routing_complete": {},
        "routing_missing_document": {"document": False},
        "routing_contextual_document": {"specialty": "Cardiology", "reason": "Recurrent palpitations; Holter monitoring completed last week", "procedure": ("Holter monitoring", "holter-report")},
        "routing_ambiguous": {"specialty": "Unknown"},
        "routing_missing_coverage": {"coverage": False},
        "routing_missing_provider": {"provider": False},
        "routing_missing_slot": {"slot": False},
    }[scenario]
    referral, _ = _records(db, name, **options)
    result = await ReferralCoordinator(db, DeterministicReferralModel()).process(referral.id)
    return {"state": result.state.value, "missing_documents": result.missing_documents}


def _booking_case(db: Session, name: str, *, confirm: bool = True) -> tuple[Referral, AppointmentSlot, str]:
    referral, slot = _records(db, name)
    referral.state = ReferralState.WAITING_FOR_SLOT_SELECTION
    db.commit()
    command_prefix = f"{name}-{uuid.uuid4()}"
    selection_event_id = f"{command_prefix}-selection"
    select_slot(db, referral, selection_event_id, slot.id)
    if confirm:
        confirm_selection(db, referral, f"{command_prefix}-confirmation")
    return referral, slot, selection_event_id


async def execute_case(db: Session, case_name: str, scenario: str) -> dict[str, Any]:
    if scenario.startswith("routing_"):
        return await _routing(db, case_name, scenario)
    if scenario == "malformed_referral":
        try:
            ReferralCreate(patient_id=uuid.uuid4(), requested_specialty=str(uuid.uuid4()), reason="Synthetic malformed input", is_synthetic=True)
        except ValidationError:
            return {"rejected": True}
        return {"rejected": False}
    if scenario == "malformed_fhir":
        try:
            validate_patient_fhir({"resourceType": "Coverage", "identifier": []})
        except ValueError:
            return {"rejected": True}
        return {"rejected": False}
    if scenario in {"provider_timeout", "model_timeout"}:
        referral, _ = _records(db, case_name)
        point = "tool:findProviders" if scenario == "provider_timeout" else "model_timeout"
        injector = FaultInjector({point: 1})
        try:
            await ReferralCoordinator(db, DeterministicReferralModel(), injector).process(referral.id)
        except InjectedTransientFailure:
            run = latest_workflow(db, referral.id)
            recovered = await ReferralCoordinator(db, DeterministicReferralModel(), injector).process(referral.id, run.id)
            return {"transient_failure": True, "recovered_state": recovered.state.value}
        return {"transient_failure": False, "recovered_state": None}
    if scenario == "duplicate_event":
        referral, slot, selection_event_id = _booking_case(db, case_name, confirm=False)
        duplicate = select_slot(db, referral, selection_event_id, slot.id)
        count = db.query(ProcessedEvent).filter_by(event_id=selection_event_id).count()
        return {"processed_events": count, "duplicate": duplicate}
    if scenario == "document_resume":
        referral, _ = _records(db, case_name, document=False)
        coordinator = ReferralCoordinator(db, DeterministicReferralModel())
        initial = await coordinator.process(referral.id)
        receive_document(db, referral, f"{case_name}-{uuid.uuid4()}-document", "clinical-note", f"synthetic://evaluation/{case_name}/{uuid.uuid4()}/arrived")
        resumed = await ReferralCoordinator(db, DeterministicReferralModel()).process(referral.id, initial.workflow_run_id)
        return {"initial_state": initial.state.value, "final_state": resumed.state.value}
    if scenario == "stale_slot":
        referral, slot, _ = _booking_case(db, case_name)
        slot.status = SlotStatus.BUSY
        db.commit()
        try:
            book_selected_slot(db, referral.id)
        except BookingError:
            pass
        return {"state": db.get(Referral, referral.id).state.value, "appointments": db.query(Appointment).filter_by(referral_id=referral.id).count()}
    if scenario == "booking_response_loss":
        referral, _, _ = _booking_case(db, case_name)
        try:
            book_selected_slot(db, referral.id, FaultInjector({"booking_response_loss": 1}))
        except InjectedTransientFailure:
            pass
        recovered = book_selected_slot(db, referral.id)
        records = db.query(Appointment).filter_by(referral_id=referral.id).all()
        return {"appointments": len(records), "same_appointment": records[0].id == recovered.id}
    if scenario == "repeated_booking":
        referral, _, _ = _booking_case(db, case_name)
        first = book_selected_slot(db, referral.id)
        second = book_selected_slot(db, referral.id)
        return {"appointments": db.query(Appointment).filter_by(referral_id=referral.id).count(), "same_appointment": first.id == second.id}
    if scenario == "booking_without_confirmation":
        referral, _, _ = _booking_case(db, case_name, confirm=False)
        try:
            book_selected_slot(db, referral.id)
        except BookingError:
            return {"blocked": True, "appointments": db.query(Appointment).filter_by(referral_id=referral.id).count()}
        return {"blocked": False, "appointments": 1}
    raise ValueError(f"Unknown evaluation scenario: {scenario}")


def _matches(observed: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(observed.get(key) == value for key, value in expected.items())


async def run_benchmark(db: Session, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    benchmark_run_id = str(uuid.uuid4())
    results = []
    for definition in CASES:
        case = db.scalar(select(EvaluationCase).where(EvaluationCase.name == definition.name))
        if case is None:
            case = EvaluationCase(name=definition.name, input_data={"scenario": definition.scenario}, ground_truth=definition.ground_truth)
            db.add(case)
            db.flush()
        else:
            case.input_data = {"scenario": definition.scenario}
            case.ground_truth = definition.ground_truth
        observed = await execute_case(db, definition.name, definition.scenario)
        passed = _matches(observed, case.ground_truth)
        db.add(EvaluationRun(case_id=case.id, result={"benchmark_run_id": benchmark_run_id, "passed": passed, "observed": observed}, score=1.0 if passed else 0.0))
        db.commit()
        results.append({"name": definition.name, "passed": passed, "observed": observed})
    passed_count = sum(result["passed"] for result in results)
    report = {
        "report_version": REPORT_VERSION,
        "benchmark_run_id": benchmark_run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {"total": len(results), "passed": passed_count, "pass_rate": passed_count / len(results)},
        "metrics": {
            "duplicate_booking_count": sum(max(0, result["observed"].get("appointments", 0) - 1) for result in results),
            "forbidden_action_rate": 0.0 if next(result for result in results if result["name"] == "prohibited_action_attempt")["passed"] else 1.0,
            "resume_recovery_rate": sum(result["passed"] for result in results if result["name"] in {"worker_restart", "document_arrival_after_pause"}) / 2,
        },
        "cases": results,
    }
    if baseline is not None:
        report["comparison"] = {
            "baseline_run_id": baseline.get("benchmark_run_id"),
            "pass_rate_delta": report["summary"]["pass_rate"] - baseline["summary"]["pass_rate"],
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic CareRoute evaluation benchmark")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    with SessionLocal() as db:
        report = asyncio.run(run_benchmark(db, baseline))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
