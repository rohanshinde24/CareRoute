import uuid
from datetime import date, datetime, timedelta, timezone
from app.models import AppointmentSlot, Coverage, Patient, Provider, ProviderSchedule, Referral, ReferralDocument, ReferralState

def patient(db):
    record = Patient(external_id="test-1", source="synthea", given_name="Avery", family_name="Ng", birth_date=date(1991, 1, 2), gender="unknown", is_synthetic=True)
    db.add(record)
    db.commit()
    return record

def test_create_and_get_referral(client, db):
    person = patient(db)
    response = client.post("/api/referrals", json={"patient_id": str(person.id), "requested_specialty": "Cardiology", "reason": "Synthetic specialist referral", "location_preference": "San Francisco", "is_synthetic": True})
    assert response.status_code == 201
    detail = client.get(f"/api/referrals/{response.json()['id']}")
    assert detail.status_code == 200
    assert detail.json()["patient"]["external_id"] == "test-1"
    assert detail.json()["state"] == "RECEIVED"
    assert detail.json()["location_preference"] == "San Francisco"

def test_rejects_non_synthetic_referral(client, db):
    person = patient(db)
    response = client.post("/api/referrals", json={"patient_id": str(person.id), "requested_specialty": "Cardiology", "reason": "Not synthetic", "is_synthetic": False})
    assert response.status_code == 422

def test_rejects_uuid_as_specialty(client, db):
    person = patient(db)
    response = client.post("/api/referrals", json={"patient_id": str(person.id), "requested_specialty": "fcc61c2e-6df2-4404-9808-996c6fd18c95", "reason": "Synthetic specialist referral", "is_synthetic": True})
    assert response.status_code == 422
    assert "not an identifier" in response.text

def test_lists_free_slots_with_provider(client, db):
    provider = Provider(name="Synthetic Provider", specialty="Cardiology", location="Test, CA", is_synthetic=True)
    db.add(provider)
    db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name="Clinic", timezone="UTC")
    db.add(schedule)
    db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    db.add(AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30)))
    db.commit()
    response = client.get("/api/slots?specialty=cardio")
    assert response.status_code == 200
    assert response.json()["items"][0]["provider"]["name"] == "Synthetic Provider"

def test_product_lists_hide_evaluation_fixtures(client, db):
    person = patient(db)
    referral = Referral(patient_id=person.id, requested_specialty="Evaluation-only", reason="Synthetic benchmark case", is_synthetic=True, is_evaluation=True)
    provider = Provider(name="Evaluation Provider", specialty="Evaluation-only", location="Evaluation, CA", is_synthetic=True, is_evaluation=True)
    db.add_all([referral, provider])
    db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name="Evaluation", timezone="UTC")
    db.add(schedule)
    db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    db.add(AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30)))
    db.commit()

    assert all(item["id"] != str(referral.id) for item in client.get("/api/referrals").json()["items"])
    assert client.get("/api/providers?specialty=Evaluation-only").json() == []
    assert client.get("/api/slots?specialty=Evaluation-only").json()["items"] == []

def test_process_endpoint_persists_trace(client, db):
    person = patient(db)
    db.add(Coverage(patient_id=person.id, external_id="api-cov", source="synthetic-payer", payer_name="Test Plan", member_id="member-api", status="active", is_synthetic=True))
    create = client.post("/api/referrals", json={"patient_id": str(person.id), "requested_specialty": "Cardiology", "reason": "Synthetic specialist referral", "is_synthetic": True})
    referral_id = create.json()["id"]
    db.add(ReferralDocument(referral_id=uuid.UUID(referral_id), document_type="clinical-note", storage_locator="synthetic://api-note"))
    provider = Provider(name="API Cardiologist", specialty="Cardiology", location="Test, CA", is_synthetic=True)
    db.add(provider)
    db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name="Clinic", timezone="UTC")
    db.add(schedule)
    db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    db.add(AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30)))
    db.commit()
    processed = client.post(f"/api/referrals/{referral_id}/process")
    assert processed.status_code == 200
    assert processed.json()["next_action"] == "PRESENT_SLOTS"
    events = client.get(f"/api/workflows/{processed.json()['workflow_run_id']}/events")
    assert events.status_code == 200
    assert events.json()[-1]["event_type"] == "workflow_completed"

def test_p2_selection_confirmation_api_is_duplicate_safe(client, db, monkeypatch):
    async def accepted(*_args, **_kwargs):
        return ["inngest-event"]

    monkeypatch.setattr("app.main.send_event", accepted)
    person = patient(db)
    referral = client.post("/api/referrals", json={"patient_id": str(person.id), "requested_specialty": "Cardiology", "reason": "Synthetic P2 referral", "is_synthetic": True}).json()
    record = db.get(Referral, uuid.UUID(referral["id"]))
    record.state = ReferralState.WAITING_FOR_SLOT_SELECTION
    provider = Provider(name="API P2 Cardiologist", specialty="Cardiology", location="Test, CA", is_synthetic=True)
    db.add(provider)
    db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name="Clinic", timezone="UTC")
    db.add(schedule)
    db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    slot = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30))
    db.add(slot)
    db.commit()

    selection = {"event_id": "api-selection", "slot_id": str(slot.id)}
    first = client.post(f"/api/referrals/{record.id}/slot-selection", json=selection)
    repeated = client.post(f"/api/referrals/{record.id}/slot-selection", json=selection)
    confirmation = client.post(f"/api/referrals/{record.id}/confirmation", json={"event_id": "api-confirmation"})
    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert repeated.status_code == 200 and repeated.json()["duplicate"] is True
    assert confirmation.status_code == 202


def test_reprocessing_an_advanced_referral_is_a_conflict_not_a_server_error(client, db):
    person = patient(db)
    referral = Referral(patient_id=person.id, requested_specialty="Cardiology", reason="Already advanced", state=ReferralState.CONFIRMED, is_synthetic=True)
    db.add(referral)
    db.commit()

    response = client.post(f"/api/referrals/{referral.id}/process")

    # A 500 here would count a client mistake against the service error rate and
    # trip the P4C alerting for something no operator needs to act on.
    assert response.status_code == 409
    assert "CONFIRMED" in response.json()["detail"]
