import functools
import inspect
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .pagination import InvalidCursor
from .provider_database import get_provider_db, provider_engine
from .provider_models import Provider
from .schemas import SpecialtyName
from .provider_contracts import AppointmentListResult, BookingRequest, BookingResult, ProviderListResult, ProviderPage, ProviderSpecialtyListResult, SlotDetail, SlotListResult, SlotPage
from . import provider_queries
from .telemetry import configure_telemetry
from .metrics import configure_metrics


class ReleasesSessionRoute(APIRoute):
    """Return a sync endpoint's database connection before the endpoint returns.

    FastAPI closes a `yield` dependency only after the response is serialized,
    and for a sync endpoint that serialization is a second hop onto the same
    40-thread pool the endpoints run on. Under a burst larger than the
    connection pool this deadlocks until pool timeouts break it: threads fill
    up waiting for a connection, while the requests holding the connections have
    already finished their queries and are queued for a thread to serialize on.
    After a 60-second freeze, 400 abandoned requests on the queue kept this
    service unavailable for 26 seconds on that alone.

    Closing the session in the endpoint's own thread means a request never holds
    a connection while waiting for a thread. Endpoints here return Pydantic
    contract objects rather than ORM rows, so nothing needs the session after.
    Applied as the route class so a new endpoint cannot forget it.
    """

    def __init__(self, path, endpoint, **kwargs):
        if not inspect.iscoroutinefunction(endpoint):
            endpoint = _releasing_sessions(endpoint)
        super().__init__(path, endpoint, **kwargs)


def _releasing_sessions(endpoint):
    @functools.wraps(endpoint)
    def call(*args, **kwargs):
        try:
            return endpoint(*args, **kwargs)
        finally:
            for value in kwargs.values():
                if isinstance(value, Session):
                    value.close()

    return call


app = FastAPI(title="CareRoute Provider Service", version="0.1.0")
app.router.route_class = ReleasesSessionRoute


def _correlate(response: Response, correlation_id: str | None) -> str:
    value = correlation_id or str(uuid.uuid4())
    if len(value) > 200:
        raise HTTPException(status_code=400, detail="Correlation ID is too long")
    response.headers["X-CareRoute-Correlation-ID"] = value
    return value


def _trusted(token: str | None) -> bool:
    return bool(token) and token == settings.provider_internal_token


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "provider-service"}


@app.get("/internal/providers", response_model=ProviderListResult)
def find_providers(
    response: Response,
    specialty: SpecialtyName,
    include_evaluation: bool = False,
    x_careroute_correlation_id: str | None = Header(default=None),
    x_careroute_internal_token: str | None = Header(default=None),
    db: Session = Depends(get_provider_db),
):
    _correlate(response, x_careroute_correlation_id)
    if include_evaluation and not _trusted(x_careroute_internal_token):
        raise HTTPException(status_code=403, detail="Evaluation provider access is restricted")
    return provider_queries.find_providers(db, specialty, include_evaluation)


@app.get("/internal/specialties", response_model=ProviderSpecialtyListResult)
def list_specialties(
    response: Response,
    x_careroute_correlation_id: str | None = Header(default=None),
    db: Session = Depends(get_provider_db),
):
    _correlate(response, x_careroute_correlation_id)
    return provider_queries.list_specialties(db)


@app.get("/internal/providers/{provider_id}/slots", response_model=SlotListResult)
def get_available_slots(
    provider_id: uuid.UUID,
    response: Response,
    x_careroute_correlation_id: str | None = Header(default=None),
    x_careroute_internal_token: str | None = Header(default=None),
    db: Session = Depends(get_provider_db),
):
    _correlate(response, x_careroute_correlation_id)
    provider = db.get(Provider, provider_id)
    if provider is None or not provider.is_synthetic:
        raise HTTPException(status_code=404, detail="Synthetic provider not found")
    if provider.is_evaluation and not _trusted(x_careroute_internal_token):
        raise HTTPException(status_code=404, detail="Synthetic provider not found")
    return provider_queries.available_slots(db, provider_id)


@app.get("/internal/catalog/providers", response_model=ProviderPage)
def catalog_providers(
    response: Response,
    specialty: str | None = None,
    accepting_new_patients: bool = True,
    limit: int | None = None,
    cursor: str | None = None,
    x_careroute_correlation_id: str | None = Header(default=None),
    db: Session = Depends(get_provider_db),
):
    """Paged provider catalogue for the public API. Excludes evaluation fixtures."""
    _correlate(response, x_careroute_correlation_id)
    try:
        return provider_queries.list_providers(db, specialty, accepting_new_patients, limit, cursor)
    except InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/internal/catalog/slots", response_model=SlotPage)
def catalog_slots(
    response: Response,
    provider_id: uuid.UUID | None = None,
    specialty: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    x_careroute_correlation_id: str | None = Header(default=None),
    db: Session = Depends(get_provider_db),
):
    _correlate(response, x_careroute_correlation_id)
    try:
        return provider_queries.list_free_slots(db, provider_id, specialty, limit, cursor)
    except InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/internal/referrals/{referral_id}/appointments", response_model=AppointmentListResult)
def referral_appointments(
    referral_id: uuid.UUID,
    response: Response,
    x_careroute_correlation_id: str | None = Header(default=None),
    db: Session = Depends(get_provider_db),
):
    _correlate(response, x_careroute_correlation_id)
    return provider_queries.appointments_for_referral(db, referral_id)


@app.get("/internal/slots/{slot_id}", response_model=SlotDetail)
def describe_slot(
    slot_id: uuid.UUID,
    response: Response,
    x_careroute_correlation_id: str | None = Header(default=None),
    db: Session = Depends(get_provider_db),
):
    _correlate(response, x_careroute_correlation_id)
    detail = provider_queries.describe_slot(db, slot_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Slot not found")
    return detail


@app.post("/internal/bookings", response_model=BookingResult)
def book(
    payload: BookingRequest,
    response: Response,
    x_careroute_correlation_id: str | None = Header(default=None),
    x_careroute_internal_token: str | None = Header(default=None),
    db: Session = Depends(get_provider_db),
):
    """Book a slot. Only trusted CareRoute services may call this.

    Confirmation gating and workflow state stay with the referral domain; this
    endpoint owns the slot, the appointment, and the exactly-once guarantee.
    """
    _correlate(response, x_careroute_correlation_id)
    if not _trusted(x_careroute_internal_token):
        raise HTTPException(status_code=403, detail="Booking is restricted to trusted services")
    return provider_queries.book_slot(db, payload)


configure_telemetry(app, "careroute-provider-service", provider_engine)
configure_metrics("careroute-provider-service")
