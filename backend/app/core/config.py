"""Application configuration.

No secret has a default. The app refuses to start without ``jwt_secret`` and
``credential_key`` rather than generating them - a generated key that changes on
restart silently invalidates every stored device credential, and the failure
mode (all polls start failing auth an hour later) is very hard to diagnose.
"""

from __future__ import annotations

import base64
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DCIM_",
        env_file=(".env", "../deploy/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- identity -----------------------------------------------------------
    environment: str = "development"
    service_name: str = "dcim-backend"

    # --- datastores ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://dcim:dcim@localhost:5432/dcim"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    redis_url: str = "redis://localhost:6379/0"

    # --- security -----------------------------------------------------------
    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 60
    collector_token: SecretStr
    credential_key: SecretStr = Field(
        description="base64-encoded 32-byte key for device-credential encryption at rest")

    # --- ingest -------------------------------------------------------------
    ingest_group: str = "dcim-ingest"
    ingest_batch_size: int = 200
    ingest_block_ms: int = 1000
    ingest_claim_idle_ms: int = 60_000
    ingest_max_counter_gap_s: int = 900

    # --- websocket ----------------------------------------------------------
    ws_coalesce_ms: int = 1000
    ws_max_topics: int = 50
    ws_queue_size: int = 256

    # --- simulator (seed import) -------------------------------------------
    simulator_base_url: str = "http://127.0.0.1:8001"
    simulator_username: str = "admin"
    simulator_password: SecretStr | None = None
    # The gNMI server is one process serving every target, so its host is a
    # deployment fact rather than a device attribute. Real gear answers on its
    # own management IP and this is ignored.
    gnmi_server_host: str = "127.0.0.1"

    # --- api ----------------------------------------------------------------
    cors_origins: list[str] = ["http://localhost:5173"]
    api_prefix: str = "/api/v1"

    @field_validator("credential_key")
    @classmethod
    def _key_must_be_32_bytes(cls, v: SecretStr) -> SecretStr:
        try:
            raw = base64.b64decode(v.get_secret_value(), validate=True)
        except Exception as exc:
            raise ValueError("credential_key must be base64") from exc
        if len(raw) != 32:
            raise ValueError(f"credential_key must decode to 32 bytes, got {len(raw)}")
        return v

    @property
    def credential_key_bytes(self) -> bytes:
        return base64.b64decode(self.credential_key.get_secret_value())

    @property
    def sync_database_url(self) -> str:
        """Alembic runs synchronously, so swap asyncpg for psycopg (v3).

        Stripping the driver entirely would leave a bare ``postgresql://`` URL,
        which SQLAlchemy resolves to psycopg2 - a package this project does not
        depend on, producing a ModuleNotFoundError only when migrations run.
        """
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
