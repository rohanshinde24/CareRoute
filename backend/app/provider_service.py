import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import engine, get_db
from .models import Provider
from .schemas import SpecialtyName
from .tools import ProviderListResult, ProviderSpecialtyListResult, SlotListResult, invoke_tool
from .telemetry import configure_telemetry
from .metrics import configure_metrics


app = FastAPI(title="CareRoute Provider Service", version="0.1.0")


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
    db: Session = Depends(get_db),
):
    _correlate(response, x_careroute_correlation_id)
    if include_evaluation and not _trusted(x_careroute_internal_token):
        raise HTTPException(status_code=403, detail="Evaluation provider access is restricted")
    result = invoke_tool("findProviders", db, {"specialty": specialty, "include_evaluation": include_evaluation})
    assert isinstance(result, ProviderListResult)
    return result


@app.get("/internal/specialties", response_model=ProviderSpecialtyListResult)
def list_specialties(
    response: Response,
    x_careroute_correlation_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    _correlate(response, x_careroute_correlation_id)
    items = list(db.scalars(select(Provider.specialty).where(Provider.accepting_new_patients.is_(True), Provider.is_synthetic.is_(True), Provider.is_evaluation.is_(False)).distinct().order_by(Provider.specialty)))
    return ProviderSpecialtyListResult(items=items)


@app.get("/internal/providers/{provider_id}/slots", response_model=SlotListResult)
def get_available_slots(
    provider_id: uuid.UUID,
    response: Response,
    x_careroute_correlation_id: str | None = Header(default=None),
    x_careroute_internal_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    _correlate(response, x_careroute_correlation_id)
    provider = db.get(Provider, provider_id)
    if provider is None or not provider.is_synthetic:
        raise HTTPException(status_code=404, detail="Synthetic provider not found")
    if provider.is_evaluation and not _trusted(x_careroute_internal_token):
        raise HTTPException(status_code=404, detail="Synthetic provider not found")
    result = invoke_tool("getAvailableSlots", db, {"provider_id": provider_id})
    assert isinstance(result, SlotListResult)
    return result


configure_telemetry(app, "careroute-provider-service", engine)
configure_metrics("careroute-provider-service")
