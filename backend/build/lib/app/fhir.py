from typing import Any
from .models import AppointmentSlot, Coverage, Patient, Provider

FHIR_SYSTEM = "https://careroute.example/synthetic"

def patient_to_fhir(patient: Patient) -> dict[str, Any]:
    return {
        "resourceType": "Patient",
        "id": str(patient.id),
        "meta": {"profile": ["http://hl7.org/fhir/StructureDefinition/Patient"]},
        "identifier": [{"system": f"{FHIR_SYSTEM}/{patient.source}", "value": patient.external_id}],
        "name": [{"use": "official", "family": patient.family_name, "given": [patient.given_name]}],
        "birthDate": patient.birth_date.isoformat(),
        **({"gender": patient.gender} if patient.gender else {}),
    }

def practitioner_to_fhir(provider: Provider) -> dict[str, Any]:
    identifiers = [{"system": "http://hl7.org/fhir/sid/us-npi", "value": provider.npi}] if provider.npi else []
    return {"resourceType": "Practitioner", "id": str(provider.id), "identifier": identifiers, "name": [{"text": provider.name}], "qualification": [{"code": {"text": provider.specialty}}]}

def coverage_to_fhir(coverage: Coverage) -> dict[str, Any]:
    return {"resourceType": "Coverage", "id": str(coverage.id), "status": coverage.status, "beneficiary": {"reference": f"Patient/{coverage.patient_id}"}, "payor": [{"display": coverage.payer_name}], "subscriberId": coverage.member_id, "identifier": [{"system": f"{FHIR_SYSTEM}/{coverage.source}/coverage", "value": coverage.external_id}]}

def slot_to_fhir(slot: AppointmentSlot) -> dict[str, Any]:
    return {"resourceType": "Slot", "id": str(slot.id), "schedule": {"reference": f"Schedule/{slot.schedule_id}"}, "status": slot.status.value.lower(), "start": slot.start_at.isoformat(), "end": slot.end_at.isoformat()}

def validate_patient_fhir(resource: dict[str, Any]) -> dict[str, Any]:
    if resource.get("resourceType") != "Patient":
        raise ValueError("FHIR resource must be a Patient")
    identifiers = resource.get("identifier")
    names = resource.get("name")
    if not isinstance(identifiers, list) or not identifiers or not identifiers[0].get("value"):
        raise ValueError("FHIR Patient requires an identifier")
    if not isinstance(names, list) or not names or not names[0].get("family"):
        raise ValueError("FHIR Patient requires a family name")
    return resource
