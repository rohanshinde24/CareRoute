from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _yaml(path: str):
    return yaml.safe_load((PROJECT_ROOT / path).read_text())


def test_apps_export_to_collector_and_images_are_pinned():
    compose = _yaml("docker-compose.yml")
    services = compose["services"]

    assert "jaeger" not in services
    assert "http://otel-collector:4318/v1/traces" in services["api"]["environment"]["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
    assert "http://otel-collector:4318/v1/traces" in services["provider-service"]["environment"]["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
    assert services["otel-collector"]["image"] == "otel/opentelemetry-collector-contrib:0.160.0"
    assert services["tempo"]["image"] == "grafana/tempo:2.10.7"
    assert services["grafana"]["image"] == "grafana/grafana:13.2.1"


def test_collector_has_bounded_vendor_neutral_trace_pipeline():
    config = _yaml("observability/otel-collector.yml")
    pipeline = config["service"]["pipelines"]["traces"]

    assert pipeline == {
        "receivers": ["otlp"],
        "processors": ["memory_limiter", "batch"],
        "exporters": ["otlp_http/tempo"],
    }
    assert config["exporters"]["otlp_http/tempo"]["endpoint"] == "http://tempo:4318"
    assert config["processors"]["memory_limiter"]["limit_mib"] == 128


def test_tempo_owns_trace_storage_and_otlp_ingestion():
    config = _yaml("observability/tempo.yml")

    assert set(config["distributor"]["receivers"]["otlp"]["protocols"]) == {"grpc", "http"}
    assert config["storage"]["trace"]["backend"] == "local"
    assert config["storage"]["trace"]["local"]["path"] == "/var/tempo/blocks"
    # P4B.1 owns "retention is configured"; P4C owns the value it is set to.
    assert config["compactor"]["compaction"]["block_retention"]


def test_grafana_provisions_tempo_without_manual_setup():
    config = _yaml("observability/grafana/provisioning/datasources/tempo.yml")
    datasource = config["datasources"][0]

    assert datasource["uid"] == "tempo"
    assert datasource["type"] == "tempo"
    assert datasource["url"] == "http://tempo:3200"
    assert datasource["isDefault"] is True
    assert datasource["editable"] is False


def test_collector_has_a_bounded_metrics_pipeline_exporting_to_prometheus():
    collector = _yaml("observability/otel-collector.yml")
    pipeline = collector["service"]["pipelines"]["metrics"]

    assert pipeline["receivers"] == ["otlp"]
    assert pipeline["processors"] == ["memory_limiter", "resource/strip_instance_id", "batch"]
    assert pipeline["exporters"] == ["prometheus"]
    assert collector["exporters"]["prometheus"]["endpoint"] == "0.0.0.0:8889"

    # service.instance.id is a per-process UUID. Exported as a label it makes every
    # restart a new time series, so it must be deleted before export.
    strip = collector["processors"]["resource/strip_instance_id"]["attributes"]
    assert {"key": "service.instance.id", "action": "delete"} in strip
    assert "resource_to_telemetry_conversion" not in collector["exporters"]["prometheus"]


def test_prometheus_scrapes_only_the_collector_and_apps_expose_no_endpoint():
    prometheus = _yaml("observability/prometheus.yml")
    targets = [target for job in prometheus["scrape_configs"] for config in job["static_configs"] for target in config["targets"]]

    assert targets == ["otel-collector:8889"]
    compose = _yaml("docker-compose.yml")
    for service in ("api", "provider-service"):
        environment = compose["services"][service]["environment"]
        assert "http://otel-collector:4318/v1/metrics" in environment["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"]
        assert "9090" not in str(compose["services"][service].get("ports", []))


def test_prometheus_service_is_pinned_durable_and_retained():
    service = _yaml("docker-compose.yml")["services"]["prometheus"]

    assert service["image"] == "prom/prometheus:v3.7.3"
    assert any("--storage.tsdb.retention.time=15d" == argument for argument in service["command"])
    assert any(volume.endswith(":ro") and "prometheus.yml" in volume for volume in service["volumes"])
    assert any(volume.startswith("prometheus_data:") for volume in service["volumes"])
    assert "prometheus_data" in _yaml("docker-compose.yml")["volumes"]


def test_grafana_provisions_prometheus_with_exemplar_linkage_to_tempo():
    datasource = _yaml("observability/grafana/provisioning/datasources/prometheus.yml")["datasources"][0]

    assert datasource["uid"] == "prometheus"
    assert datasource["type"] == "prometheus"
    assert datasource["url"] == "http://prometheus:9090"
    assert datasource["editable"] is False
    assert datasource["jsonData"]["exemplarTraceIdDestinations"][0]["datasourceUid"] == "tempo"


def test_alert_rules_cover_safety_and_distribution_signals():
    groups = _yaml("observability/rules.yml")["groups"]
    alerts = {rule["alert"] for group in groups for rule in group["rules"]}

    assert {
        "CareRouteOutboxNotDraining",
        "CareRouteRelayCannotReachBroker",
        "CareRouteConsumerLag",
        "CareRouteConsumerStalled",
        "CareRouteReconcilerRepairingSteadily",
        "CareRouteFailClosedRateHigh",
        "CareRouteDuplicateBooking",
        "CareRouteProviderGatewayErrors",
        "CareRouteProviderRetryExhaustion",
        "CareRouteModelLatencyHigh",
        "CareRouteWorkflowsNeedingReview",
    } <= alerts


def test_trace_evidence_outlives_a_phase():
    # P4B.1 evidence expired under the original 24h retention.
    assert _yaml("observability/tempo.yml")["compactor"]["compaction"]["block_retention"] == "336h"


def test_delivery_alerts_watch_age_and_lag_not_just_depth():
    """Depth alone is the wrong signal.

    A large backlog draining steadily is healthy; a single event stuck for
    minutes means the relay is dead. The age and stalled-consumer rules are the
    ones that distinguish those, so their absence would leave a silent failure.
    """
    groups = {group["name"]: group for group in _yaml("observability/rules.yml")["groups"]}
    delivery = groups["careroute-delivery"]
    rules = {rule["alert"]: rule for rule in delivery["rules"]}

    assert "careroute_outbox_age_seconds" in rules["CareRouteOutboxNotDraining"]["expr"]
    assert rules["CareRouteOutboxNotDraining"]["labels"]["severity"] == "critical"

    # A stalled consumer must only fire when something is actually being
    # published, or a quiet system pages someone at 3am for nothing.
    stalled = rules["CareRouteConsumerStalled"]["expr"]
    assert "careroute_relay_dispatched_total" in stalled and "> 0" in stalled
    assert rules["CareRouteConsumerStalled"]["labels"]["severity"] == "critical"

    # The reconciler repairing at all means an event was lost somewhere.
    assert "reconciled" in rules["CareRouteReconcilerRepairingSteadily"]["expr"]
