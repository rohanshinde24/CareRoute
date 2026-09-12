import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx
from opentelemetry.propagate import inject
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, settings
from .models import Provider
from .tools import ProviderListResult, ProviderSpecialtyListResult, SlotListResult, invoke_tool
from .telemetry import operation, set_span_attributes


class ProviderGatewayError(RuntimeError):
    pass


class ProviderGatewayTransientError(ProviderGatewayError):
    pass


class ProviderGatewayProtocolError(ProviderGatewayError):
    pass


class ProviderGateway(Protocol):
    async def list_specialties(self, correlation_id: uuid.UUID) -> ProviderSpecialtyListResult: ...
    async def find_providers(self, specialty: str, include_evaluation: bool, correlation_id: uuid.UUID) -> ProviderListResult: ...
    async def get_available_slots(self, provider_id: uuid.UUID, correlation_id: uuid.UUID) -> SlotListResult: ...


class LocalProviderGateway:
    def __init__(self, db: Session):
        self.db = db

    async def list_specialties(self, correlation_id: uuid.UUID) -> ProviderSpecialtyListResult:
        items = list(self.db.scalars(select(Provider.specialty).where(Provider.accepting_new_patients.is_(True), Provider.is_synthetic.is_(True), Provider.is_evaluation.is_(False)).distinct().order_by(Provider.specialty)))
        return ProviderSpecialtyListResult(items=items)

    async def find_providers(self, specialty: str, include_evaluation: bool, correlation_id: uuid.UUID) -> ProviderListResult:
        result = invoke_tool("findProviders", self.db, {"specialty": specialty, "include_evaluation": include_evaluation})
        assert isinstance(result, ProviderListResult)
        return result

    async def get_available_slots(self, provider_id: uuid.UUID, correlation_id: uuid.UUID) -> SlotListResult:
        result = invoke_tool("getAvailableSlots", self.db, {"provider_id": provider_id})
        assert isinstance(result, SlotListResult)
        return result


class HttpProviderGateway:
    retryable_statuses = {429, 500, 502, 503, 504}

    def __init__(
        self,
        config: Settings = settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not config.provider_service_url:
            raise ValueError("PROVIDER_SERVICE_URL is required for the HTTP provider gateway")
        if config.provider_retry_attempts < 1:
            raise ValueError("PROVIDER_RETRY_ATTEMPTS must be at least 1")
        self.config = config
        self.transport = transport
        self.sleep = sleep

    async def list_specialties(self, correlation_id: uuid.UUID) -> ProviderSpecialtyListResult:
        payload = await self._get("/internal/specialties", operation_name="list_specialties", correlation_id=correlation_id)
        return self._validate(ProviderSpecialtyListResult, payload)

    async def find_providers(self, specialty: str, include_evaluation: bool, correlation_id: uuid.UUID) -> ProviderListResult:
        payload = await self._get(
            "/internal/providers",
            operation_name="find_providers",
            params={"specialty": specialty, "include_evaluation": str(include_evaluation).lower()},
            correlation_id=correlation_id,
            trusted=include_evaluation,
        )
        return self._validate(ProviderListResult, payload)

    async def get_available_slots(self, provider_id: uuid.UUID, correlation_id: uuid.UUID) -> SlotListResult:
        payload = await self._get(f"/internal/providers/{provider_id}/slots", operation_name="get_available_slots", correlation_id=correlation_id, trusted=True)
        return self._validate(SlotListResult, payload)

    async def _get(self, path: str, *, operation_name: str, correlation_id: uuid.UUID, params: dict[str, str] | None = None, trusted: bool = False) -> dict:
        headers = {"X-CareRoute-Correlation-ID": str(correlation_id)}
        if trusted:
            headers["X-CareRoute-Internal-Token"] = self.config.provider_internal_token
        inject(headers)
        last_error: Exception | None = None
        timeout = httpx.Timeout(self.config.provider_timeout_seconds)
        async with httpx.AsyncClient(base_url=self.config.provider_service_url, timeout=timeout, transport=self.transport) as client:
            for attempt in range(self.config.provider_retry_attempts):
                try:
                    with operation(
                        "careroute.provider.request.attempt",
                        {
                            "careroute.provider.operation": operation_name,
                            "careroute.provider.attempt": attempt + 1,
                            "careroute.provider.max_attempts": self.config.provider_retry_attempts,
                            "careroute.workflow.run_id": str(correlation_id),
                        },
                    ) as attempt_span:
                        response = await client.get(path, params=params, headers=headers)
                        set_span_attributes(attempt_span, {"http.response.status_code": response.status_code})
                        if response.status_code in self.retryable_statuses:
                            set_span_attributes(attempt_span, {"careroute.provider.outcome": "transient_failure"})
                            raise ProviderGatewayTransientError(f"Provider service returned retryable HTTP {response.status_code}")
                        if response.is_error:
                            set_span_attributes(attempt_span, {"careroute.provider.outcome": "protocol_failure"})
                            raise ProviderGatewayProtocolError(f"Provider service returned HTTP {response.status_code}")
                        if response.headers.get("X-CareRoute-Correlation-ID") != str(correlation_id):
                            set_span_attributes(attempt_span, {"careroute.provider.outcome": "protocol_failure"})
                            raise ProviderGatewayProtocolError("Provider service did not preserve the correlation ID")
                        try:
                            payload = response.json()
                        except ValueError as exc:
                            set_span_attributes(attempt_span, {"careroute.provider.outcome": "protocol_failure"})
                            raise ProviderGatewayProtocolError("Provider service returned invalid JSON") from exc
                        set_span_attributes(attempt_span, {"careroute.provider.outcome": "success"})
                        return payload
                except (httpx.TimeoutException, httpx.NetworkError, ProviderGatewayTransientError) as exc:
                    last_error = exc
                    if attempt + 1 < self.config.provider_retry_attempts:
                        await self.sleep(self.config.provider_retry_backoff_seconds * (2**attempt))
            raise ProviderGatewayTransientError(f"Provider service unavailable after {self.config.provider_retry_attempts} attempts") from last_error

    @staticmethod
    def _validate(model_type, payload: dict):
        try:
            return model_type.model_validate(payload)
        except ValidationError as exc:
            raise ProviderGatewayProtocolError("Provider service returned an invalid response") from exc


def configured_provider_gateway(db: Session, config: Settings = settings) -> ProviderGateway:
    if config.provider_service_url:
        return HttpProviderGateway(config)
    return LocalProviderGateway(db)
