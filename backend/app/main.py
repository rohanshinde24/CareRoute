import uuid
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, joinedload, selectinload
from .agent import ProcessingResult, ReferralCoordinator
from .config import settings
from .database import engine, get_db
from .fhir import patient_to_fhir
from .models import Patient, Referral, ReferralState
from .model_providers import ModelResponseError, configured_model
from .provider_database import ProviderSessionLocal
from .provider_gateway import ProviderGatewayError, ProviderGatewayTransientError, configured_provider_gateway
from .models import AgentEvent, WorkflowRun
from .commands import CommandConflict, cancel, confirm_selection, receive_document, select_slot
from .durable import mount_inngest, send_event
from .schemas import AgentEventRead, AppointmentRead, Cancellation, CommandRead, Confirmation, DocumentCreate, DurableProcessCreate, DurableProcessRead, PatientRead, ProviderRead, ReferralCreate, ReferralDetail, ReferralRead, SlotRead, SlotSelection, WorkflowRead
from .workflow import InvalidTransition
from .telemetry import configure_telemetry
from .metrics import configure_metrics
from .pagination import CursorPage, InvalidCursor, clamp_limit, decode_cursor, encode_cursor
from .sessions import ReleasesSessionRoute

@asynccontextmanager
async def lifespan(_: FastAPI):
    yield

app = FastAPI(title="CareRoute API", version="0.1.0", lifespan=lifespan)
# Set before any route is declared: a sync endpoint must release its
# connection before the response is serialized. See app/sessions.py.
app.router.route_class = ReleasesSessionRoute
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
mount_inngest(app)

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/api/referrals", response_model=CursorPage[ReferralRead])
def list_referrals(
    limit: int | None = Query(None, ge=1, le=200),
    cursor: str | None = Query(None),
    db: Session = Depends(get_db),
):
    size = clamp_limit(limit)
    query = select(Referral).where(Referral.is_evaluation.is_(False))
    if cursor:
        try:
            created_at, referral_id = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Newest first, so the next page is strictly "older than" the cursor row.
        query = query.where(tuple_(Referral.created_at, Referral.id) < (created_at, referral_id))
    # Fetch one extra row to learn whether a further page exists without counting.
    rows = db.scalars(query.order_by(Referral.created_at.desc(), Referral.id.desc()).limit(size + 1)).all()
    items = list(rows[:size])
    next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > size else None
    return CursorPage[ReferralRead](items=items, next_cursor=next_cursor)

@app.post("/api/referrals", response_model=ReferralRead, status_code=status.HTTP_201_CREATED)
def create_referral(payload: ReferralCreate, db: Session = Depends(get_db)):
    patient = db.get(Patient, payload.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    if not patient.is_synthetic:
        raise HTTPException(status_code=422, detail="Patient must be synthetic")
    referral = Referral(**payload.model_dump())
    db.add(referral)
    db.commit()
    db.refresh(referral)
    return referral

@app.get("/api/referrals/{referral_id}", response_model=ReferralDetail)
def get_referral(referral_id: uuid.UUID, db: Session = Depends(get_db)):
    referral = db.scalar(select(Referral).where(Referral.id == referral_id).options(joinedload(Referral.patient), selectinload(Referral.documents)))
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found")
    return referral

@app.get("/api/patients/{patient_id}", response_model=PatientRead)
def get_patient(patient_id: uuid.UUID, db: Session = Depends(get_db)):
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient

@app.get("/api/patients/{patient_id}/fhir")
def get_patient_fhir(patient_id: uuid.UUID, db: Session = Depends(get_db)):
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient_to_fhir(patient)

def provider_gateway_dependency():
    """Yield a gateway, opening a provider-database session only if there is no
    provider service to call.

    A configured deployment sets PROVIDER_SERVICE_URL, and then the referral API
    holds no connection to the provider database at all - which is the point of
    the split. The local path exists for single-process runs and tests.
    """
    if settings.provider_service_url:
        yield configured_provider_gateway(None)
        return
    with ProviderSessionLocal() as provider_db:
        yield configured_provider_gateway(provider_db)


@app.get("/api/providers", response_model=CursorPage[ProviderRead])
async def list_providers(
    specialty: str | None = None,
    accepting_new_patients: bool = True,
    limit: int | None = Query(None, ge=1, le=200),
    cursor: str | None = Query(None),
    gateway = Depends(provider_gateway_dependency),
):
    try:
        page = await gateway.catalog_providers(specialty, accepting_new_patients, limit, cursor, uuid.uuid4())
    except ProviderGatewayError as exc:
        raise HTTPException(status_code=503, detail="Provider service is temporarily unavailable") from exc
    return CursorPage[ProviderRead](items=page.items, next_cursor=page.next_cursor)

@app.get("/api/slots", response_model=CursorPage[SlotRead])
async def list_slots(
    provider_id: uuid.UUID | None = None,
    specialty: str | None = Query(None, min_length=2),
    limit: int | None = Query(None, ge=1, le=200),
    cursor: str | None = Query(None),
    gateway = Depends(provider_gateway_dependency),
):
    try:
        page = await gateway.catalog_slots(provider_id, specialty, limit, cursor, uuid.uuid4())
    except ProviderGatewayError as exc:
        raise HTTPException(status_code=503, detail="Provider service is temporarily unavailable") from exc
    return CursorPage[SlotRead](items=page.items, next_cursor=page.next_cursor)

@app.post("/api/referrals/{referral_id}/process", response_model=ProcessingResult)
async def process_referral(referral_id: uuid.UUID, db: Session = Depends(get_db)):
    if db.get(Referral, referral_id) is None:
        raise HTTPException(status_code=404, detail="Referral not found")
    try:
        model = configured_model()
    except ModelResponseError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        return await ReferralCoordinator(db, model).process(referral_id)
    except ProviderGatewayTransientError as exc:
        raise HTTPException(status_code=503, detail="Provider service is temporarily unavailable; retry this referral") from exc
    except InvalidTransition as exc:
        # Re-processing a referral that has already advanced is a client error, not
        # a server fault. The command endpoints already answer 409 for this; leaving
        # it as an unhandled 500 here also polluted the service error rate and would
        # trip the gateway alert for something no operator needs to see.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.post("/api/referrals/{referral_id}/process/durable", response_model=DurableProcessRead, status_code=status.HTTP_202_ACCEPTED)
async def process_referral_durably(referral_id: uuid.UUID, payload: DurableProcessCreate, db: Session = Depends(get_db)):
    referral = db.get(Referral, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found")
    existing = db.scalar(select(WorkflowRun).where(WorkflowRun.engine_run_id == payload.event_id))
    if existing and existing.referral_id != referral.id:
        raise HTTPException(status_code=409, detail="Event ID was already used for another referral")
    workflow = existing or WorkflowRun(referral_id=referral.id, state=referral.state, engine_run_id=payload.event_id)
    if existing is None:
        db.add(workflow)
        db.commit()
        db.refresh(workflow)
    try:
        await send_event("careroute/referral.received", payload.event_id, {"referral_id": str(referral.id), "workflow_run_id": str(workflow.id)})
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Durable workflow event could not be delivered; retry with the same event_id") from exc
    return DurableProcessRead(workflow_run_id=workflow.id, referral_id=referral.id, event_id=payload.event_id)

def _referral_or_404(db: Session, referral_id: uuid.UUID) -> Referral:
    referral = db.get(Referral, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found")
    return referral

def _command_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))

@app.post("/api/referrals/{referral_id}/documents", response_model=CommandRead)
async def add_referral_document(referral_id: uuid.UUID, payload: DocumentCreate, db: Session = Depends(get_db)):
    referral = _referral_or_404(db, referral_id)
    try:
        _, duplicate = receive_document(db, referral, payload.event_id, payload.document_type, payload.storage_locator)
    except (CommandConflict, InvalidTransition) as exc:
        raise _command_error(exc) from exc
    await send_event("careroute/document.received", payload.event_id, {"referral_id": str(referral.id), "document_type": payload.document_type})
    db.refresh(referral)
    return CommandRead(referral_id=referral.id, state=referral.state, duplicate=duplicate, event_id=payload.event_id)

@app.post("/api/referrals/{referral_id}/slot-selection", response_model=CommandRead)
async def choose_referral_slot(referral_id: uuid.UUID, payload: SlotSelection, db: Session = Depends(get_db), gateway = Depends(provider_gateway_dependency)):
    referral = _referral_or_404(db, referral_id)
    try:
        duplicate = await select_slot(db, referral, payload.event_id, payload.slot_id, gateway)
    except (CommandConflict, InvalidTransition) as exc:
        raise _command_error(exc) from exc
    await send_event("careroute/slot.selected", payload.event_id, {"referral_id": str(referral.id), "slot_id": str(payload.slot_id)})
    return CommandRead(referral_id=referral.id, state=referral.state, duplicate=duplicate, event_id=payload.event_id)

@app.post("/api/referrals/{referral_id}/confirmation", response_model=CommandRead, status_code=status.HTTP_202_ACCEPTED)
async def confirm_referral_slot(referral_id: uuid.UUID, payload: Confirmation, db: Session = Depends(get_db)):
    referral = _referral_or_404(db, referral_id)
    try:
        duplicate = confirm_selection(db, referral, payload.event_id)
    except (CommandConflict, InvalidTransition) as exc:
        raise _command_error(exc) from exc
    await send_event("careroute/booking.confirmed", payload.event_id, {"referral_id": str(referral.id), "slot_id": str(referral.selected_slot_id)})
    return CommandRead(referral_id=referral.id, state=referral.state, duplicate=duplicate, event_id=payload.event_id)

@app.post("/api/referrals/{referral_id}/cancel", response_model=CommandRead)
async def cancel_referral(referral_id: uuid.UUID, payload: Cancellation, db: Session = Depends(get_db)):
    referral = _referral_or_404(db, referral_id)
    try:
        duplicate = cancel(db, referral, payload.event_id, payload.reason)
    except (CommandConflict, InvalidTransition) as exc:
        raise _command_error(exc) from exc
    await send_event("careroute/referral.cancelled", payload.event_id, {"referral_id": str(referral.id), "reason": payload.reason})
    return CommandRead(referral_id=referral.id, state=referral.state, duplicate=duplicate, event_id=payload.event_id)

@app.get("/api/referrals/{referral_id}/appointments", response_model=list[AppointmentRead])
async def list_referral_appointments(referral_id: uuid.UUID, db: Session = Depends(get_db), gateway = Depends(provider_gateway_dependency)):
    _referral_or_404(db, referral_id)
    try:
        result = await gateway.appointments_for_referral(referral_id, referral_id)
    except ProviderGatewayError as exc:
        raise HTTPException(status_code=503, detail="Provider service is temporarily unavailable") from exc
    return result.items

@app.get("/api/workflows/{workflow_id}", response_model=WorkflowRead)
def get_workflow(workflow_id: uuid.UUID, db: Session = Depends(get_db)):
    workflow = db.get(WorkflowRun, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow

@app.get("/api/workflows/{workflow_id}/events", response_model=list[AgentEventRead])
def get_workflow_events(workflow_id: uuid.UUID, db: Session = Depends(get_db)):
    if db.get(WorkflowRun, workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return db.scalars(select(AgentEvent).where(AgentEvent.workflow_run_id == workflow_id).order_by(AgentEvent.created_at, AgentEvent.id)).all()


configure_telemetry(app, "careroute-api", engine)
configure_metrics("careroute-api")
