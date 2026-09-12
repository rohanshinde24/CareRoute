import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx
from opentelemetry.propagate import inject
from pydantic import ValidationError
from sqlalchemy.orm import Session

from . import provider_queries

from .config import Settings, settings
from .provider_contracts import AppointmentListResult, BookingRequest, BookingResult, ProviderListResult, ProviderPage, ProviderSpecialtyListResult, SlotDetail, SlotListResult, SlotPage
from .telemetry import operation, set_span_attributes
from .metrics import record_provider_request, record_provider_retry_exhausted


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
    async def describe_slot(self, slot_id: uuid.UUID, correlation_id: uuid.UUID) -> SlotDetail | None: ...
    async def catalog_providers(self, specialty: str | None, accepting_new_patients: bool, limit: int | None, cursor: str | None, correlation_id: uuid.UUID) -> ProviderPage: ...
    async def catalog_slots(self, provider_id: uuid.UUID | None, specialty: str | None, limit: int | None, cursor: str | None, correlation_id: uuid.UUID) -> SlotPage: ...
    async def appointments_for_referral(self, referral_id: uuid.UUID, correlation_id: uuid.UUID) -> AppointmentListResult: ...
    async def book(self, request: BookingRequest, correlation_id: uuid.UUID) -> BookingResult: ...


class LocalProviderGateway:
    """In-process access to the provider domain, for tests and single-process runs.

    It holds a provider-database session, not the referral one: even in-process,
    the referral domain must not reach provider tables through its own session.
    """

    def __init__(self, db: Session):
        self.db = db

    async def list_specialties(self, correlation_id: uuid.UUID) -> ProviderSpecialtyListResult:
        return provider_queries.list_specialties(self.db)

    async def find_providers(self, specialty: str, include_evaluation: bool, correlation_id: uuid.UUID) -> ProviderListResult:
        return provider_queries.find_providers(self.db, specialty, include_evaluation)

    async def get_available_slots(self, provider_id: uuid.UUID, correlation_id: uuid.UUID) -> SlotListResult:
        return provider_queries.available_slots(self.db, provider_id)

    async def describe_slot(self, slot_id: uuid.UUID, correlation_id: uuid.UUID) -> SlotDetail | None:
        return provider_queries.describe_slot(self.db, slot_id)

    async def catalog_providers(self, specialty, accepting_new_patients, limit, cursor, correlation_id) -> ProviderPage:
        return provider_queries.list_providers(self.db, specialty, accepting_new_patients, limit, cursor)

    async def catalog_slots(self, provider_id, specialty, limit, cursor, correlation_id) -> SlotPage:
        return provider_queries.list_free_slots(self.db, provider_id, specialty, limit, cursor)

    async def appointments_for_referral(self, referral_id: uuid.UUID, correlation_id: uuid.UUID) -> AppointmentListResult:
        return provider_queries.appointments_for_referral(self.db, referral_id)

    async def book(self, request: BookingRequest, correlation_id: uuid.UUID) -> BookingResult:
        return provider_queries.book_slot(self.db, request)


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

    async def describe_slot(self, slot_id: uuid.UUID, correlation_id: uuid.UUID) -> SlotDetail | None:
        try:
            payload = await self._get(f"/internal/slots/{slot_id}", operation_name="describe_slot", correlation_id=correlation_id, trusted=True)
        except ProviderGatewayProtocolError:
            # A 404 is a legitimate answer here, not a broken contract.
            return None
        return self._validate(SlotDetail, payload)

    async def catalog_providers(self, specialty, accepting_new_patients, limit, cursor, correlation_id) -> ProviderPage:
        params = {"accepting_new_patients": str(accepting_new_patients).lower()}
        if specialty:
            params["specialty"] = specialty
        if limit is not None:
            params["limit"] = str(limit)
        if cursor:
            params["cursor"] = cursor
        payload = await self._get("/internal/catalog/providers", operation_name="catalog_providers", correlation_id=correlation_id, params=params)
        return self._validate(ProviderPage, payload)

    async def catalog_slots(self, provider_id, specialty, limit, cursor, correlation_id) -> SlotPage:
        params: dict[str, str] = {}
        if provider_id:
            params["provider_id"] = str(provider_id)
        if specialty:
            params["specialty"] = specialty
        if limit is not None:
            params["limit"] = str(limit)
        if cursor:
            params["cursor"] = cursor
        payload = await self._get("/internal/catalog/slots", operation_name="catalog_slots", correlation_id=correlation_id, params=params)
        return self._validate(SlotPage, payload)

    async def appointments_for_referral(self, referral_id: uuid.UUID, correlation_id: uuid.UUID) -> AppointmentListResult:
        payload = await self._get(f"/internal/referrals/{referral_id}/appointments", operation_name="appointments_for_referral", correlation_id=correlation_id)
        return self._validate(AppointmentListResult, payload)

    async def book(self, request: BookingRequest, correlation_id: uuid.UUID) -> BookingResult:
        payload = await self._request(
            "POST",
            "/internal/bookings",
            operation_name="book",
            correlation_id=correlation_id,
            json=request.model_dump(mode="json"),
            trusted=True,
        )
        return self._validate(BookingResult, payload)

    async def _get(self, path: str, *, operation_name: str, correlation_id: uuid.UUID, params: dict[str, str] | None = None, trusted: bool = False) -> dict:
        return await self._request("GET", path, operation_name=operation_name, correlation_id=correlation_id, params=params, trusted=trusted)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation_name: str,
        correlation_id: uuid.UUID,
        params: dict[str, str] | None = None,
        json: dict | None = None,
        trusted: bool = False,
    ) -> dict:
        """Issue one provider request with bounded, jittered retries.

        Retrying a POST is normally unsafe. It is safe here only because every
        mutating provider operation carries a caller-supplied idempotency key and
        the provider side records the decision against it, so a retry after a lost
        response returns the original outcome instead of acting twice.
        """
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
                        response = await client.request(method, path, params=params, json=json, headers=headers)
                        set_span_attributes(attempt_span, {"http.response.status_code": response.status_code})
                        if response.status_code in self.retryable_statuses:
                            set_span_attributes(attempt_span, {"careroute.provider.outcome": "transient_failure"})
                            record_provider_request(operation_name, "transient_failure")
                            raise ProviderGatewayTransientError(f"Provider service returned retryable HTTP {response.status_code}")
                        if response.is_error:
                            set_span_attributes(attempt_span, {"careroute.provider.outcome": "protocol_failure"})
                            record_provider_request(operation_name, "protocol_failure")
                            raise ProviderGatewayProtocolError(f"Provider service returned HTTP {response.status_code}")
                        if response.headers.get("X-CareRoute-Correlation-ID") != str(correlation_id):
                            set_span_attributes(attempt_span, {"careroute.provider.outcome": "protocol_failure"})
                            record_provider_request(operation_name, "protocol_failure")
                            raise ProviderGatewayProtocolError("Provider service did not preserve the correlation ID")
                        try:
                            payload = response.json()
                        except ValueError as exc:
                            set_span_attributes(attempt_span, {"careroute.provider.outcome": "protocol_failure"})
                            record_provider_request(operation_name, "protocol_failure")
                            raise ProviderGatewayProtocolError("Provider service returned invalid JSON") from exc
                        set_span_attributes(attempt_span, {"careroute.provider.outcome": "success"})
                        record_provider_request(operation_name, "success")
                        return payload
                except (httpx.TimeoutException, httpx.NetworkError, ProviderGatewayTransientError) as exc:
                    last_error = exc
                    if attempt + 1 < self.config.provider_retry_attempts:
                        await self.sleep(self._backoff_delay(attempt))
            record_provider_retry_exhausted(operation_name)
            raise ProviderGatewayTransientError(f"Provider service unavailable after {self.config.provider_retry_attempts} attempts") from last_error

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        Without jitter every client that failed on the same provider outage retries
        at the same instants, so recovery is met by a synchronised burst - the
        thundering herd that caused the outage to persist. Full jitter spreads the
        retries uniformly across the window instead of clustering them at its edge.
        """
        ceiling = self.config.provider_retry_backoff_seconds * (2**attempt)
        return random.uniform(0, ceiling)

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
