from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://careroute:careroute@localhost:5432/careroute"
    # Explicit rather than inherited from SQLAlchemy. Past pool_size + max_overflow
    # concurrent database-touching requests, work queues on checkout instead of
    # failing, so the ceiling shows up as latency with idle CPU and is invisible
    # unless it is written down.
    redis_url: str = "redis://localhost:6379/0"
    relay_interval_seconds: float = 1.0
    reconciler_interval_seconds: float = 30.0
    provider_database_url: str = "postgresql+psycopg://careroute:careroute@localhost:5433/careroute_provider"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # Fail a checkout fast. A thread waiting 30s for a connection is a threadpool
    # slot that cannot serve anything else, and FastAPI runs sync endpoints in a
    # fixed-size threadpool: enough slow waits and even /health stops responding.
    db_pool_timeout: float = 5.0
    db_pool_recycle_seconds: int = 1800
    # PostgreSQL terminates a transaction left idle this long, so an abandoned
    # session cannot hold a pool slot forever.
    db_idle_transaction_timeout_seconds: float = 30.0
    cors_origins: str = "http://localhost:3000"
    model_provider: str = "deterministic"
    # Orchestration for the provider-ranking investigation: "legacy" is the
    # handwritten loop, "langgraph" the state graph. Both share one policy.
    agent_runtime: str = "legacy"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    model_timeout_seconds: float = 30.0
    provider_service_url: str | None = None
    provider_timeout_seconds: float = 3.0
    provider_retry_attempts: int = 3
    provider_retry_backoff_seconds: float = 0.1
    provider_internal_token: str = "careroute-local-synthetic"
    telemetry_enabled: bool = False
    otel_exporter_otlp_traces_endpoint: str = "http://localhost:4318/v1/traces"
    otel_export_timeout_seconds: float = 2.0
    metrics_enabled: bool = False
    otel_exporter_otlp_metrics_endpoint: str = "http://localhost:4318/v1/metrics"
    otel_metric_export_interval_seconds: float = 15.0
    inngest_is_production: bool = False
    inngest_event_key: str | None = None
    inngest_signing_key: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def max_pooled_connections(self) -> int:
        """Ceiling on concurrent database work for one process."""
        return self.db_pool_size + self.db_max_overflow

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
