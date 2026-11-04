"""Deployment configuration, read from IPPING_* environment variables.

Anything an operator should be able to change while the service runs (limits, the emergency
stop, the target denylist) lives in the database instead; see `runtime_settings.py`.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IPPING_", extra="ignore")

    # PostgreSQL in production (postgresql://user:pass@host/db). SQLite is only meant for
    # development and tests.
    database_url: str = "sqlite+aiosqlite:///./ipping.db"

    # Required on the one-time /setup page so that only the operator can create the first admin.
    # When unset and no admin exists, a token is generated at startup and written to the log.
    setup_token: str | None = None

    # Public origin of the application (https://ping.example.com). Used for the same-origin check
    # on state-changing requests. Requests whose Origin matches their own Host are always allowed.
    public_url: str | None = None
    allowed_origins: list[str] = []

    # Browsers refuse to store Secure cookies over plain HTTP, so IP-only installs turn this off.
    cookie_secure: bool = True

    log_level: str = "INFO"
    log_json: bool = True

    host: str = "127.0.0.1"
    port: int = 8000

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, value: str) -> str:
        for prefix, replacement in (
            ("postgresql://", "postgresql+asyncpg://"),
            ("postgres://", "postgresql+asyncpg://"),
            ("sqlite://", "sqlite+aiosqlite://"),
        ):
            if value.startswith(prefix):
                return replacement + value[len(prefix) :]
        return value


def get_settings() -> Settings:
    return Settings()
