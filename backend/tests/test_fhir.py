from datetime import date
from app.fhir import patient_to_fhir
from app.models import Patient

def test_patient_fhir_has_required_identity_fields():
    patient = Patient(external_id="SYN-42", source="synthea", given_name="Sam", family_name="Taylor", birth_date=date(2000, 2, 3), gender="unknown", is_synthetic=True)
    resource = patient_to_fhir(patient)
    assert resource["resourceType"] == "Patient"
    assert resource["identifier"][0]["value"] == "SYN-42"
    assert resource["name"][0]["family"] == "Taylor"

