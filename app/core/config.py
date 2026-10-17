from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SERVICE_KEY = "local-internal-service-key"


class Settings(BaseSettings):
    """Runtime configuration. Every field can be overridden with a PB_-prefixed environment variable."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="PB_", extra="ignore")

    app_name: str = "pulseboard-api"
    environment: str = "local"  # local | test | staging | production
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "sqlite+aiosqlite:///./pulseboard.db"
    db_auto_create: bool = True  # local convenience; deployed environments run `alembic upgrade head`

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Tokens are issued by pulseboard-identity (RS256). The API only verifies them, using the identity
    # service's JWKS (jwks_url) or, for tests and air-gapped setups, a pinned public key.
    jwks_url: str | None = "http://localhost:8100/.well-known/jwks.json"
    jwt_public_key_b64: str | None = None
    jwt_issuer: str = "pulseboard-identity"
    jwt_audience: str = "pulseboard"
    service_key: str = _DEFAULT_SERVICE_KEY  # X-Service-Key for /internal/v1

    # Review queue. "sqs" talks to AWS SQS (a local moto server by default); "azure" uses Azure Storage Queues.
    queue_backend: Literal["sqs", "azure"] = "sqs"
    review_queue_name: str = "board-reviews"
    aws_endpoint_url: str | None = "http://localhost:5000"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "testing"
    aws_secret_access_key: str = "testing"
    azure_queue_account_url: str | None = None  # https://<account>.queue.core.windows.net (managed identity)
    azure_queue_connection_string: str | None = None  # alternative, e.g. Azurite locally

    run_background_workers: bool = True
    review_scheduler_interval_seconds: int = 30
    review_interval_minutes: int = 60
    max_delivery_attempts: int = 5

    regions: list[str] = ["NA", "EMEA", "APAC", "LATAM"]
    units: list[str] = ["USD", "%", "x", "pts", "days", "tickets"]
    count_units: list[str] = ["tickets"]  # shown as whole numbers
    volume_units: list[str] = ["USD", "tickets"]  # split across regions; other units are rates
    page_sizes: list[int] = [5, 10, 25, 50]
    # redis  = shared Redis at redis_url (Docker, Azure Managed Redis) - correct with any number of workers/instances
    # memory = in-process fake Redis - only for a single process (local `uvicorn`, tests)
    # none   = no caching - safe default for multi-worker deployments without Redis
    cache_backend: Literal["redis", "memory", "none"] = "memory"
    redis_url: str | None = None  # e.g. rediss://:<key>@<host>:10000/0
    cache_ttl_seconds: int = 300

    claim_seconds: int = 120
    setup_build_delay_seconds: float = 2.0  # simulated assistant think-time so the UI shows its building state

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def uses_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @model_validator(mode="after")
    def _no_default_secrets_in_production(self) -> "Settings":
        if self.is_production and (self.service_key == _DEFAULT_SERVICE_KEY or not (self.jwks_url or self.jwt_public_key_b64)):
            raise ValueError("PB_SERVICE_KEY and PB_JWKS_URL (or PB_JWT_PUBLIC_KEY_B64) must be set in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
