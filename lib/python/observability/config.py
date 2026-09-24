"""Configuration for the standalone observability process (no main-app imports)."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("conf/.env", "conf/.env.observability"),
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )

    enabled: bool = Field(False, alias="OBSERVABILITY_ENABLED")
    host: str = Field("127.0.0.1", alias="OBSERVABILITY_HOST", min_length=1, max_length=253)
    port: int = Field(38080, alias="OBSERVABILITY_PORT", ge=1, le=65535)
    store_dir: str = Field("./runtime/observability", alias="OBSERVABILITY_STORE_DIR")
    retention_days: int = Field(30, alias="OBSERVABILITY_RETENTION_DAYS", ge=1, le=90)
    password_hash: str = Field("", alias="OBSERVABILITY_PASSWORD_HASH", repr=False, max_length=256)
    read_token: str = Field("", alias="OBSERVABILITY_READ_TOKEN", repr=False, max_length=512)
    ingest_token: str = Field("", alias="OBSERVABILITY_INGEST_TOKEN", repr=False, max_length=512)
    hmac_key: str = Field("", alias="OBSERVABILITY_HMAC_KEY", repr=False)
    cookie_secure: bool = Field(False, alias="OBSERVABILITY_COOKIE_SECURE")
    session_ttl_seconds: int = Field(86400, alias="OBSERVABILITY_SESSION_TTL_SECONDS", ge=1, le=86400)

    # Native pi usage is a separate, non-expiring ledger, not the event journal.
    pi_usage_enabled: bool = Field(False, alias="OBSERVABILITY_PI_USAGE_ENABLED")
    pi_session_dirs: list[str] = Field(default_factory=list, alias="OBSERVABILITY_PI_SESSION_DIRS", max_length=16)
    pi_scan_interval_seconds: int = Field(30, alias="OBSERVABILITY_PI_SCAN_INTERVAL_SECONDS", ge=5, le=3600)
    pi_agent_dir: str = Field("", alias="PI_CODING_AGENT_DIR", repr=False)

    @property
    def resolved_pi_session_dirs(self) -> list[str]:
        if self.pi_session_dirs:
            return [str(Path(value).expanduser().resolve()) for value in self.pi_session_dirs]
        agent = Path(self.pi_agent_dir).expanduser() if self.pi_agent_dir else Path.home() / ".pi" / "agent"
        return [str((agent / "sessions").resolve())]

    @model_validator(mode="after")
    def separate_tokens(self):
        if any(not value.strip() for value in self.pi_session_dirs):
            raise ValueError("Pi source directories cannot contain empty paths")
        if self.read_token and self.read_token == self.ingest_token:
            raise ValueError("Read and ingest credentials must be independent")
        return self


@lru_cache(maxsize=1)
def get_observability_settings() -> ObservabilitySettings:
    return ObservabilitySettings()
