import asyncio
import uuid

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import create_engine

from app import telemetry
from app.agent import ReferralCoordinator
from app.config import Settings
from app.evaluation import _records
from app.model_providers import DeterministicReferralModel
from app.provider_gateway import HttpProviderGateway
from app.telemetry import PrivacyFilteringSpanExporter, build_tracer_provider, configure_telemetry


def _recording_provider(service_name: str = "test-service"):
    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(PrivacyFilteringSpanExporter(memory)))
    return provider, memory


def _span_data(memory: InMemorySpanExporter):
    return list(memory.get_finished_spans())


def test_privacy_filter_exports_only_allowlisted_attributes_and_no_event_payloads():
    provider, memory = _recording_provider()
    test_tracer = provider.get_tracer("privacy-test")
    secret = "Synthetic Patient member-123 referral-reason"

    with test_tracer.start_as_current_span("safe.operation") as span:
        span.set_attribute("careroute.workflow.state", "MATCHING_PROVIDER")
        span.set_attribute("http.request.method", "POST")
        span.set_attribute("url.full", f"http://api/referrals/{secret}")
        span.set_attribute("db.statement", f"SELECT * FROM patients WHERE name = '{secret}'")
        span.set_attribute("patient.name", secret)
        span.add_event("exception", {"exception.message": secret, "exception.type": "ValueError"})
        span.set_status(Status(StatusCode.ERROR, secret))

    exported = _span_data(memory)[0]
    assert exported.attributes == {
        "careroute.workflow.state": "MATCHING_PROVIDER",
        "http.request.method": "POST",
    }
    assert exported.events == ()
    assert exported.status.description is None
    assert secret not in repr(exported)


def test_disabled_telemetry_is_a_safe_noop():
    app = FastAPI()
    engine = create_engine("sqlite:///:memory:")
    config = Settings(telemetry_enabled=False)

    assert configure_telemetry(app, "disabled-service", engine, config) is None
    assert not getattr(app.state, "telemetry_configured", False)


def test_tracer_provider_has_distinct_service_identity():
    provider = build_tracer_provider("careroute-provider-service", InMemorySpanExporter())
    try:
        assert provider.resource.attributes["service.name"] == "careroute-provider-service"
        assert provider.resource.attributes["service.namespace"] == "careroute"
    finally:
        provider.shutdown()


def test_provider_gateway_injects_w3c_trace_context(monkeypatch):
    provider, _ = _recording_provider()
    monkeypatch.setattr(telemetry, "tracer", lambda: provider.get_tracer("propagation-test"))
    captured = {}
    correlation_id = uuid.uuid4()

    async def request():
        with provider.get_tracer("propagation-test").start_as_current_span("parent"):
            gateway = HttpProviderGateway(
                Settings(provider_service_url="http://provider.test", provider_retry_attempts=1),
                transport=httpx.MockTransport(handler),
            )
            await gateway.list_specialties(correlation_id)

    def handler(request: httpx.Request):
        captured["traceparent"] = request.headers.get("traceparent")
        return httpx.Response(200, headers={"X-CareRoute-Correlation-ID": str(correlation_id)}, json={"items": []})

    asyncio.run(request())

    assert captured["traceparent"].startswith("00-")
    assert len(captured["traceparent"].split("-")[1]) == 32


def test_workflow_exports_model_and_completion_spans_without_sensitive_inputs(db, monkeypatch):
    provider, memory = _recording_provider("careroute-api-test")
    monkeypatch.setattr(telemetry, "tracer", lambda: provider.get_tracer("careroute-test"))
    referral, _ = _records(db, "telemetry-workflow", specialty="Cardiology", reason="private referral reason")

    result = asyncio.run(ReferralCoordinator(db, DeterministicReferralModel()).process(referral.id))
    spans = _span_data(memory)
    names = {span.name for span in spans}
    process = next(span for span in spans if span.name == "careroute.workflow.process")

    assert {"careroute.workflow.process", "careroute.model.interpret", "careroute.workflow.complete"} <= names
    assert process.attributes["careroute.workflow.run_id"] == str(result.workflow_run_id)
    assert "private referral reason" not in repr(spans)
    assert str(referral.id) not in repr(spans)


def test_provider_retry_attempts_are_individually_traced(monkeypatch):
    provider, memory = _recording_provider("careroute-api-test")
    monkeypatch.setattr(telemetry, "tracer", lambda: provider.get_tracer("careroute-test"))
    attempts = 0
    correlation_id = uuid.uuid4()

    def handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={"X-CareRoute-Correlation-ID": str(correlation_id)}, json={"items": []})

    async def no_wait(_: float):
        return None

    config = Settings(
        provider_service_url="http://provider.test",
        provider_retry_attempts=2,
        provider_retry_backoff_seconds=0,
    )
    gateway = HttpProviderGateway(config, transport=httpx.MockTransport(handler), sleep=no_wait)
    asyncio.run(gateway.find_providers("Cardiology", False, correlation_id))

    attempt_spans = [span for span in _span_data(memory) if span.name == "careroute.provider.request.attempt"]
    assert [span.attributes["careroute.provider.attempt"] for span in attempt_spans] == [1, 2]
    assert [span.attributes["careroute.provider.outcome"] for span in attempt_spans] == ["transient_failure", "success"]
    assert all(span.attributes["careroute.workflow.run_id"] == str(correlation_id) for span in attempt_spans)
