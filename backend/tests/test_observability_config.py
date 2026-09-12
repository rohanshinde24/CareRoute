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
    assert config["compactor"]["compaction"]["block_retention"] == "24h"


def test_grafana_provisions_tempo_without_manual_setup():
    config = _yaml("observability/grafana/provisioning/datasources/tempo.yml")
    datasource = config["datasources"][0]

    assert datasource["uid"] == "tempo"
    assert datasource["type"] == "tempo"
    assert datasource["url"] == "http://tempo:3200"
    assert datasource["isDefault"] is True
    assert datasource["editable"] is False
