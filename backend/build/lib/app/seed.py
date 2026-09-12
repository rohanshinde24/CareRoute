from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from .database import SessionLocal
from .models import AppointmentSlot, Coverage, Patient, PatientProcedure, Provider, ProviderSchedule, Referral, ReferralDocument


DEMO_CASES = (
    ("SYN-1001", "Maya", "Rivera", "Cardiology", "Intermittent palpitations; specialist consultation requested", ("clinical-note",)),
    ("SYN-1002", "Eli", "Chen", "Orthopedics", "Persistent knee pain after a sports injury", ("clinical-note", "imaging-report")),
    ("SYN-1003", "Nora", "Williams", "Neurology", "Recurring headaches requiring specialist evaluation", ("clinical-note",)),
    ("SYN-1004", "Owen", "Patel", "Dermatology", "Changing skin lesion requiring specialist evaluation", ("clinical-note",)),
    ("SYN-1005", "Sofia", "Garcia", "Gastroenterology", "Ongoing digestive symptoms requiring specialist evaluation", ("clinical-note",)),
)

DEMO_PROVIDERS = (
    ("9000000001", "Dr. Jordan Lee", "Cardiology", "Oakland, CA"),
    ("9000000002", "Dr. Priya Shah", "Orthopedics", "San Francisco, CA"),
    ("9000000003", "Dr. Camille Brooks", "Neurology", "Berkeley, CA"),
    ("9000000004", "Dr. Mateo Flores", "Dermatology", "Alameda, CA"),
    ("9000000005", "Dr. Avery Kim", "Gastroenterology", "San Francisco, CA"),
)


def seed() -> None:
    with SessionLocal() as db:
        for index, (external_id, given, family, specialty, reason, document_types) in enumerate(DEMO_CASES, start=1):
            patient = db.scalar(select(Patient).where(Patient.source == "synthea", Patient.external_id == external_id))
            if patient is None:
                patient = Patient(external_id=external_id, source="synthea", given_name=given, family_name=family, birth_date=date(1980 + index, index, min(10 + index, 28)), gender=None, is_synthetic=True)
                db.add(patient)
                db.flush()
            if db.scalar(select(Coverage).where(Coverage.source == "synthetic-payer", Coverage.external_id == f"COV-{external_id}")) is None:
                db.add(Coverage(patient_id=patient.id, external_id=f"COV-{external_id}", source="synthetic-payer", payer_name="Synthetic Health Plan", member_id=f"MEM-{1000 + index}", status="active", is_synthetic=True))
            referral = db.scalar(select(Referral).where(Referral.patient_id == patient.id, Referral.requested_specialty == specialty, Referral.is_evaluation.is_(False)))
            if referral is None:
                referral = Referral(patient_id=patient.id, requested_specialty=specialty, reason=reason, is_synthetic=True)
                db.add(referral)
                db.flush()
            existing_documents = set(db.scalars(select(ReferralDocument.document_type).where(ReferralDocument.referral_id == referral.id)))
            for document_type in document_types:
                if document_type not in existing_documents:
                    db.add(ReferralDocument(referral_id=referral.id, document_type=document_type, storage_locator=f"synthetic://referrals/{external_id}/{document_type}"))

        maya = db.scalar(select(Patient).where(Patient.source == "synthea", Patient.external_id == "SYN-1001"))
        if db.scalar(select(PatientProcedure).where(PatientProcedure.patient_id == maya.id, PatientProcedure.procedure_type == "Holter monitoring")) is None:
            db.add(PatientProcedure(patient_id=maya.id, procedure_type="Holter monitoring", occurred_at=datetime.now(timezone.utc) - timedelta(days=7), report_document_type="holter-report", is_synthetic=True))

        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
        for index, (npi, name, specialty, location) in enumerate(DEMO_PROVIDERS):
            provider = db.scalar(select(Provider).where(Provider.npi == npi))
            if provider is None:
                provider = Provider(npi=npi, name=name, specialty=specialty, location=location, is_synthetic=True)
                db.add(provider)
                db.flush()
            schedule = db.scalar(select(ProviderSchedule).where(ProviderSchedule.provider_id == provider.id))
            if schedule is None:
                schedule = ProviderSchedule(provider_id=provider.id, name="Synthetic outpatient schedule", timezone="America/Los_Angeles")
                db.add(schedule)
                db.flush()
            if db.scalar(select(AppointmentSlot.id).where(AppointmentSlot.schedule_id == schedule.id)) is None:
                db.add_all([AppointmentSlot(schedule_id=schedule.id, start_at=start + timedelta(days=index, hours=hour), end_at=start + timedelta(days=index, hours=hour, minutes=30)) for hour in (0, 1)])
        db.commit()


if __name__ == "__main__":
    seed()
