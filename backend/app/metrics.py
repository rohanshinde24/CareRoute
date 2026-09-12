from __future__ import annotations

from typing import Any, Mapping

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from .config import Settings, settings

# Metrics carry the same privacy boundary as spans, and one extra one: a label
# value with unbounded range is both a data leak and an unbounded time series.
# Privacy and cardinality are therefore enforced as a single constraint here.
# Every value admitted below must come from a closed set fixed in code.
SAFE_LABEL_KEYS = frozenset(
    {
        "agent_kind",
        "model_provider",
        "operation",
        "outcome",
        "result",
        "service",
        "specialty",
        "terminal_state",
    }
)

# Free text reaches a label only through a bug, so bound the damage: anything
# longer than this, or not a primitive, is dropped rather than exported.
MAX_LABEL_LENGTH = 64


# Set once at configuration time so every point can be attributed to a service
# without the Collector promoting resource attributes wholesale, which would also
# promote the per-process service.instance.id UUID.
_service_name: str | None = None


def _safe_labels(labels: Mapping[str, Any] | None) -> dict[str, str]:
    safe: dict[str, str] = {}
    if _service_name:
        safe["service"] = _service_name
    if not labels:
        return safe
    for key, value in labels.items():
        if key not in SAFE_LABEL_KEYS or value is None:
            continue
        if not isinstance(value, (str, bool, int)):
            continue
        text = str(value)
        if len(text) > MAX_LABEL_LENGTH:
            continue
        safe[key] = text
    return safe


def build_meter_provider(service_name: str, reader: MetricReader) -> MeterProvider:
    return MeterProvider(
        resource=Resource.create({"service.name": service_name, "service.namespace": "careroute"}),
        metric_readers=[reader],
    )


def configure_metrics(service_name: str, config: Settings = settings) -> MeterProvider | None:
    """Install the global meter provider. Disabled by default outside Compose."""
    if not config.metrics_enabled:
        return None
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=config.otel_exporter_otlp_metrics_endpoint,
            timeout=config.otel_export_timeout_seconds,
        ),
        export_interval_millis=int(config.otel_metric_export_interval_seconds * 1000),
    )
    global _service_name
    _service_name = service_name
    provider = build_meter_provider(service_name, reader)
    metrics.set_meter_provider(provider)
    _instruments.bind(provider)
    return provider


class _Instruments:
    """Lazily created so an unconfigured process records into the API no-op.

    The meter provider is held explicitly rather than read from the OpenTelemetry
    global, because the global can only be set once per process. Holding it here
    keeps a second configuration (and every test) honest instead of silently
    recording into the provider that happened to be installed first.
    """

    def __init__(self) -> None:
        self._provider = None
        self.reset()

    def bind(self, provider) -> None:
        self._provider = provider
        self.reset()

    def reset(self) -> None:
        # Drop cached instruments as well as the built flag. __getattr__ only
        # fires for attributes that are missing, so leaving them behind would
        # keep recording into the previous provider after a rebind.
        for name in [key for key in self.__dict__ if not key.startswith("_")]:
            del self.__dict__[name]
        self._built = False

    def _build(self) -> None:
        source = self._provider or metrics
        meter = source.get_meter("careroute.domain")
        self.policy_validations = meter.create_counter(
            "careroute.policy.validations",
            description="Deterministic validations of model proposals, by outcome.",
        )
        self.workflow_runs = meter.create_counter(
            "careroute.workflow.runs",
            description="Workflow runs reaching a terminal state.",
        )
        self.agent_turns = meter.create_histogram(
            "careroute.agent.turns",
            description="Turns used by a bounded investigator loop before it concluded.",
        )
        self.model_latency = meter.create_histogram(
            "careroute.model.latency",
            unit="s",
            description="Model call latency by configured model provider.",
        )
        self.model_confidence = meter.create_histogram(
            "careroute.model.confidence",
            description="Reported confidence of model interpretations.",
        )
        self.provider_requests = meter.create_counter(
            "careroute.provider.requests",
            description="Provider gateway requests by operation and outcome.",
        )
        self.provider_retry_exhausted = meter.create_counter(
            "careroute.provider.retry_exhausted",
            description="Provider gateway calls that exhausted their bounded retries.",
        )
        self.booking_attempts = meter.create_counter(
            "careroute.booking.attempts",
            description="Booking attempts by result, including idempotent duplicate suppression.",
        )
        self._built = True

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if not self.__dict__.get("_built"):
            self._build()
        return self.__dict__[name]


_instruments = _Instruments()


def _add(instrument_name: str, amount: int | float, labels: Mapping[str, Any] | None) -> None:
    getattr(_instruments, instrument_name).add(amount, _safe_labels(labels))


def _record(instrument_name: str, value: int | float, labels: Mapping[str, Any] | None) -> None:
    getattr(_instruments, instrument_name).record(value, _safe_labels(labels))


def record_policy_validation(outcome: str, agent_kind: str | None = None) -> None:
    _add("policy_validations", 1, {"outcome": outcome, "agent_kind": agent_kind})


def record_workflow_run(terminal_state: str) -> None:
    _add("workflow_runs", 1, {"terminal_state": terminal_state})


def record_agent_turns(turns: int, agent_kind: str) -> None:
    _record("agent_turns", turns, {"agent_kind": agent_kind})


def record_model_call(seconds: float, model_provider: str) -> None:
    _record("model_latency", seconds, {"model_provider": model_provider})


def record_model_confidence(confidence: float, model_provider: str) -> None:
    _record("model_confidence", confidence, {"model_provider": model_provider})


def record_provider_request(operation: str, outcome: str) -> None:
    _add("provider_requests", 1, {"operation": operation, "outcome": outcome})


def record_provider_retry_exhausted(operation: str) -> None:
    _add("provider_retry_exhausted", 1, {"operation": operation})


def record_booking_attempt(result: str) -> None:
    _add("booking_attempts", 1, {"result": result})
