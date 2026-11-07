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

    # Optional GeoIP / ASN databases (MaxMind-format .mmdb: GeoLite2 or DB-IP Lite). With a directory, the
    # newest *city*.mmdb and *asn*.mmdb in it are used; `ipping-backend geoip-update` fills it. They add a
    # second opinion to the location estimate that is checked against the measurements; nothing else.
    geoip_dir: str | None = None
    geoip_city_db: str | None = None
    geoip_asn_db: str | None = None

    log_level: str = "INFO"
    log_json: bool = True

    host: str = "127.0.0.1"
    port: int = 8000

    # More than one process (several workers, or several hosts) needs PostgreSQL, which is where
    # they agree on who is alive, which node is connected where, and what happened. Processes need
    # roughly synchronised clocks (NTP): "alive" is a recent heartbeat.
    workers: int = 1
    instance_heartbeat_s: float = 3.0
    instance_dead_after_s: float = 10.0
    reaper_interval_s: float = 1.0
    dispatch_poll_s: float = 1.0
    # How long a task may wait for a node that is not connected before it is given up on.
    pending_grace_s: float = 5.0

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
