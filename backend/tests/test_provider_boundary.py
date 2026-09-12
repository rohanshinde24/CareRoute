import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import get_db
from app.models import AppointmentSlot, Provider, ProviderSchedule
from app.provider_gateway import HttpProviderGateway, ProviderGatewayProtocolError, ProviderGatewayTransientError
from app.provider_service import app as provider_app


def _provider(db, *, name: str, evaluation: bool = False):
    provider = Provider(name=name, specialty="Cardiology", location="San Francisco, CA", is_synthetic=True, is_evaluation=evaluation)
    db.add(provider)
    db.flush()
    schedule = ProviderSchedule(provider_id=provider.id, name=f"{name} schedule", timezone="UTC")
    db.add(schedule)
    db.flush()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    slot = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30))
    db.add(slot)
    db.commit()
    return provider, slot


def test_provider_service_enforces_evaluation_isolation_and_correlation(db):
    visible, _ = _provider(db, name="Visible Provider")
    hidden, _ = _provider(db, name="Hidden Provider", evaluation=True)
    provider_app.dependency_overrides[get_db] = lambda: db
    correlation_id = str(uuid.uuid4())
    try:
        with TestClient(provider_app) as client:
            response = client.get("/internal/providers", params={"specialty": "Cardiology"}, headers={"X-CareRoute-Correlation-ID": correlation_id})
            assert response.status_code == 200
            assert response.headers["X-CareRoute-Correlation-ID"] == correlation_id
            assert [item["id"] for item in response.json()["items"]] == [str(visible.id)]

            denied = client.get("/internal/providers", params={"specialty": "Cardiology", "include_evaluation": "true"})
            assert denied.status_code == 403

            trusted = client.get(
                "/internal/providers",
                params={"specialty": "Cardiology", "include_evaluation": "true"},
                headers={"X-CareRoute-Internal-Token": "careroute-local-synthetic"},
            )
            assert trusted.status_code == 200
            assert [item["id"] for item in trusted.json()["items"]] == [str(hidden.id)]
    finally:
        provider_app.dependency_overrides.clear()


def test_provider_service_hides_evaluation_slot_without_trusted_header(db):
    hidden, slot = _provider(db, name="Hidden Slot Provider", evaluation=True)
    provider_app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(provider_app) as client:
            assert client.get(f"/internal/providers/{hidden.id}/slots").status_code == 404
            response = client.get(
                f"/internal/providers/{hidden.id}/slots",
                headers={"X-CareRoute-Internal-Token": "careroute-local-synthetic"},
            )
            assert response.status_code == 200
            assert response.json()["items"][0]["id"] == str(slot.id)
    finally:
        provider_app.dependency_overrides.clear()


def _settings(**overrides):
    return Settings(
        provider_service_url="http://provider.test",
        provider_retry_attempts=overrides.pop("provider_retry_attempts", 3),
        provider_retry_backoff_seconds=0.01,
        provider_timeout_seconds=0.1,
        **overrides,
    )


def test_http_gateway_retries_transient_response_and_propagates_correlation():
    attempts = 0
    sleeps = []
    correlation_id = uuid.uuid4()

    def handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        assert request.headers["X-CareRoute-Correlation-ID"] == str(correlation_id)
        return httpx.Response(200, headers={"X-CareRoute-Correlation-ID": str(correlation_id)}, json={"items": []})

    async def no_wait(delay: float):
        sleeps.append(delay)

    gateway = HttpProviderGateway(_settings(), transport=httpx.MockTransport(handler), sleep=no_wait)
    result = asyncio.run(gateway.find_providers("Cardiology", False, correlation_id))

    assert result.items == []
    assert attempts == 2
    assert sleeps == [0.01]


def test_http_gateway_exhausts_bounded_retries():
    attempts = 0

    def handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async def no_wait(_: float):
        return None

    gateway = HttpProviderGateway(_settings(provider_retry_attempts=2), transport=httpx.MockTransport(handler), sleep=no_wait)
    with pytest.raises(ProviderGatewayTransientError, match="after 2 attempts"):
        asyncio.run(gateway.find_providers("Cardiology", False, uuid.uuid4()))
    assert attempts == 2


def test_http_gateway_does_not_retry_permanent_failure():
    attempts = 0

    def handler(_: httpx.Request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(403)

    gateway = HttpProviderGateway(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderGatewayProtocolError, match="HTTP 403"):
        asyncio.run(gateway.find_providers("Cardiology", False, uuid.uuid4()))
    assert attempts == 1


@pytest.mark.parametrize(
    ("headers", "payload", "message"),
    [
        ({}, {"items": []}, "correlation ID"),
        ({"X-CareRoute-Correlation-ID": "placeholder"}, {"unexpected": []}, "invalid response"),
    ],
)
def test_http_gateway_rejects_protocol_violations(headers, payload, message):
    correlation_id = uuid.uuid4()
    actual_headers = {key: str(correlation_id) if value == "placeholder" else value for key, value in headers.items()}

    def handler(_: httpx.Request):
        return httpx.Response(200, headers=actual_headers, json=payload)

    gateway = HttpProviderGateway(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderGatewayProtocolError, match=message):
        asyncio.run(gateway.find_providers("Cardiology", False, correlation_id))
