import uuid

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app import metrics
from app.config import Settings


def _collect(callback):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics._instruments.bind(provider)
    try:
        callback()
        return reader.get_metrics_data()
    finally:
        metrics._instruments.bind(None)
        provider.shutdown()


def _points(data, name):
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    return list(metric.data.data_points)
    return []


def test_identifier_and_free_text_labels_never_reach_an_instrument():
    referral_id = str(uuid.uuid4())
    labels = metrics._safe_labels(
        {
            "outcome": "rejected",
            "referral_id": referral_id,
            "patient_name": "Maya Rivera",
            "reason": "Intermittent palpitations; specialist consultation requested",
            "provider_id": str(uuid.uuid4()),
            "member_id": "SYN-1001",
            "prompt": "You are a referral assistant",
        }
    )
    assert labels == {"outcome": "rejected"}
    assert referral_id not in str(labels)


def test_label_allowlist_is_closed_and_excludes_identifier_shaped_keys():
    for key in ("referral_id", "patient_id", "provider_id", "slot_id", "workflow_run_id", "member_id", "trace_id"):
        assert key not in metrics.SAFE_LABEL_KEYS


def test_unbounded_label_values_are_dropped_rather_than_exported():
    assert metrics._safe_labels({"specialty": "x" * (metrics.MAX_LABEL_LENGTH + 1)}) == {}
    assert metrics._safe_labels({"specialty": "Cardiology"}) == {"specialty": "Cardiology"}
    assert metrics._safe_labels({"outcome": {"nested": "object"}}) == {}
    assert metrics._safe_labels(None) == {}


def test_domain_instruments_record_expected_points():
    data = _collect(
        lambda: (
            metrics.record_policy_validation("accepted", "provider"),
            metrics.record_policy_validation("rejected", "provider"),
            metrics.record_workflow_run("NEEDS_HUMAN_REVIEW"),
            metrics.record_provider_request("find_providers", "success"),
            metrics.record_provider_retry_exhausted("find_providers"),
            metrics.record_booking_attempt("duplicate_suppressed"),
            metrics.record_agent_turns(2, "provider"),
            metrics.record_model_call(8.8, "ollama"),
            metrics.record_model_confidence(1.0, "ollama"),
        )
    )
    validations = _points(data, "careroute.policy.validations")
    assert {point.attributes["outcome"] for point in validations} == {"accepted", "rejected"}
    assert _points(data, "careroute.workflow.runs")[0].attributes == {"terminal_state": "NEEDS_HUMAN_REVIEW"}
    assert _points(data, "careroute.provider.retry_exhausted")[0].value == 1
    assert _points(data, "careroute.booking.attempts")[0].attributes == {"result": "duplicate_suppressed"}
    assert _points(data, "careroute.agent.turns")[0].sum == 2
    assert _points(data, "careroute.model.latency")[0].sum == 8.8


def test_recorded_points_carry_only_allowlisted_attribute_keys():
    data = _collect(
        lambda: (
            metrics.record_policy_validation("accepted", "specialty"),
            metrics.record_model_call(1.0, "deterministic"),
        )
    )
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    assert set(point.attributes).issubset(metrics.SAFE_LABEL_KEYS)


def test_metrics_are_disabled_by_default_and_recording_is_safe():
    assert Settings().metrics_enabled is False
    assert metrics.configure_metrics("careroute-api", Settings()) is None
    # Recording without a configured provider must never raise.
    metrics.record_policy_validation("accepted", "specialty")
    metrics.record_booking_attempt("booked")


def test_configured_provider_carries_service_identity():
    settings = Settings(metrics_enabled=True, otel_exporter_otlp_metrics_endpoint="http://localhost:4318/v1/metrics")
    provider = metrics.configure_metrics("careroute-provider-service", settings)
    try:
        assert provider is not None
        attributes = provider._sdk_config.resource.attributes
        assert attributes["service.name"] == "careroute-provider-service"
        assert attributes["service.namespace"] == "careroute"
    finally:
        provider.shutdown()
        metrics._instruments.bind(None)


def test_service_identity_is_an_explicit_label_not_a_promoted_resource_attribute():
    settings = Settings(metrics_enabled=True)
    provider = metrics.configure_metrics("careroute-api", settings)
    try:
        assert metrics._safe_labels({"outcome": "accepted"}) == {"service": "careroute-api", "outcome": "accepted"}
        assert "service" in metrics.SAFE_LABEL_KEYS
        # service.instance.id is a per-process UUID; it must never become a label.
        assert metrics._safe_labels({"service.instance.id": str(uuid.uuid4())}) == {"service": "careroute-api"}
    finally:
        provider.shutdown()
        metrics._service_name = None
        metrics._instruments.bind(None)
