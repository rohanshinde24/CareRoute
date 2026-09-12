from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from typing import Any, Mapping, Sequence

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.trace import Status
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult, SpanExporter
from sqlalchemy.engine import Engine

from .config import Settings, settings


# Export-time enforcement is intentional: automatic instrumentation may create
# attributes before application hooks run. Anything not listed here is removed.
SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "db.collection.name",
        "db.namespace",
        "db.operation.name",
        "db.system",
        "error.type",
        "http.request.method",
        "http.request.resend_count",
        "http.response.status_code",
        "http.route",
        "network.protocol.version",
        "server.address",
        "server.port",
        "url.scheme",
    }
)


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {
        key: value
        for key, value in attributes.items()
        if key in SAFE_ATTRIBUTE_KEYS or key.startswith("careroute.")
    }


def _sanitized_span(span: ReadableSpan) -> ReadableSpan:
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_safe_attributes(span.attributes),
        events=(),
        links=(),
        kind=span.kind,
        status=Status(status_code=span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class PrivacyFilteringSpanExporter(SpanExporter):
    """Drops non-allowlisted attributes before any span leaves the process."""

    def __init__(self, delegate: SpanExporter):
        self.delegate = delegate

    def export(self, spans: Sequence) -> SpanExportResult:
        return self.delegate.export([_sanitized_span(span) for span in spans])

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        force_flush = getattr(self.delegate, "force_flush", None)
        return True if force_flush is None else bool(force_flush(timeout_millis))


def build_tracer_provider(service_name: str, exporter: SpanExporter) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name, "service.namespace": "careroute"}))
    provider.add_span_processor(BatchSpanProcessor(PrivacyFilteringSpanExporter(exporter)))
    return provider


def configure_telemetry(app: FastAPI, service_name: str, engine: Engine, config: Settings = settings) -> TracerProvider | None:
    if not config.telemetry_enabled or getattr(app.state, "telemetry_configured", False):
        return None
    exporter = OTLPSpanExporter(
        endpoint=config.otel_exporter_otlp_traces_endpoint,
        timeout=config.otel_export_timeout_seconds,
    )
    provider = build_tracer_provider(service_name, exporter)
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="/health")
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provider)
    app.state.telemetry_configured = True
    app.state.tracer_provider = provider
    return provider


def tracer():
    return trace.get_tracer("careroute.domain")


@contextmanager
def operation(name: str, attributes: Mapping[str, Any] | None = None):
    with tracer().start_as_current_span(name) as span:
        set_span_attributes(span, attributes or {})
        yield span


def traced(name: str, attributes: Mapping[str, Any] | None = None):
    def decorator(function):
        @wraps(function)
        async def wrapper(*args, **kwargs):
            with operation(name, attributes):
                return await function(*args, **kwargs)

        return wrapper

    return decorator


def set_span_attributes(span, attributes: Mapping[str, Any]) -> None:
    for key, value in _safe_attributes(attributes).items():
        if value is not None:
            span.set_attribute(key, value)


def set_current_attributes(attributes: Mapping[str, Any]) -> None:
    set_span_attributes(trace.get_current_span(), attributes)


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}"
