from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://careroute:careroute@localhost:5432/careroute"
    cors_origins: str = "http://localhost:3000"
    model_provider: str = "deterministic"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    model_timeout_seconds: float = 30.0
    inngest_is_production: bool = False
    inngest_event_key: str | None = None
    inngest_signing_key: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
