"""Settings an administrator can change while the service is running.

Stored one row per key in `app_settings`; keys with no row use the defaults below. These are the
safety valves of a public measurement service: the emergency stop, the per-user and global
budgets, and the target denylist.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AppSetting
from .nodes.protocol import PROBE_KINDS

# Ports a TCP or HTTP check may target. A short list, because a measurement service that connects
# to any port of any host is a port scanner for hire.
DEFAULT_ALLOWED_PORTS = [
    22, 25, 53, 80, 110, 143, 443, 465, 587, 853, 993, 995, 3306, 3389, 5432, 8080, 8443,
]  # fmt: skip


# The languages the web interface has translations for (`ipping` `src/lib/i18n`): the codes an administrator can choose from.
LANGUAGES: tuple[str, ...] = ("en", "zh", "es", "fr", "de", "pt", "ru", "ja", "ko")


def _check_languages(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if any(code not in LANGUAGES for code in value):
        raise ValueError(f"languages must be among {', '.join(LANGUAGES)}")
    if not value:
        raise ValueError("at least one language must be on")
    return [
        code for code in LANGUAGES if code in set(value)
    ]  # in the interface's own order, once each


def _check_kinds(value: list[str] | None) -> list[str] | None:
    if value is not None and any(k not in PROBE_KINDS for k in value):
        raise ValueError(f"kinds must be among {', '.join(PROBE_KINDS)}")
    return value


def _check_ports(value: list[int] | None) -> list[int] | None:
    if value is not None and any(not 0 < p < 65536 for p in value):
        raise ValueError("ports must be between 1 and 65535")
    return sorted(set(value)) if value is not None else None


class RuntimeSettings(BaseModel):
    # Emergency stop: when false, no new measurement is accepted.
    measurements_enabled: bool = True
    max_concurrent_per_session: int = Field(default=2, ge=1, le=50)
    max_concurrent_per_ip: int = Field(default=4, ge=1, le=200)
    creations_per_minute_per_ip: int = Field(default=10, ge=1, le=600)
    # nodes x echo requests one measurement may cost
    max_probes_per_measurement: int = Field(default=1000, ge=1, le=100_000)
    # echo requests in flight across all running measurements
    max_global_inflight_probes: int = Field(default=5000, ge=1, le=1_000_000)
    measurement_deadline_s: int = Field(default=30, ge=5, le=300)
    default_count: int = Field(default=4, ge=1, le=10)
    max_count: int = Field(default=10, ge=1, le=10)
    # CIDR ranges and hostnames (a hostname also blocks its subdomains).
    target_denylist: list[str] = Field(default_factory=list, max_length=1000)
    sample_retention_days: int = Field(default=7, ge=1, le=3650)
    measurement_retention_days: int = Field(default=90, ge=1, le=3650)
    # Which kinds of measurement can be started (a node must also support the kind).
    enabled_kinds: list[str] = Field(default_factory=lambda: list(PROBE_KINDS), max_length=8)
    # Destination ports for tcp and http checks.
    allowed_ports: list[int] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_PORTS), max_length=200
    )
    # Most redirects an http check may follow; 0 turns following off.
    max_redirects: int = Field(default=5, ge=0, le=5)
    traceroute_max_hops: int = Field(default=30, ge=5, le=30)
    # How many times a task whose backend instance died is sent again (only if the node had not
    # produced a result yet); 0 turns it off and such a task is reported as interrupted.
    task_retries: int = Field(default=1, ge=0, le=3)
    # Community nodes (run by volunteers) are asked for at most this many tasks a minute each, so a
    # measurement that includes them cannot hammer someone's home connection.
    community_tasks_per_minute: int = Field(default=20, ge=1, le=600)
    # Scheduled checks (watches): how many, how often at most, how long their short history is kept.
    max_watches: int = Field(default=20, ge=0, le=500)
    min_watch_interval_s: int = Field(default=60, ge=30, le=3600)
    watch_history_days: int = Field(default=30, ge=1, le=365)
    # Alerts go to an address an administrator typed; unless this is on it must be a public one.
    allow_private_webhooks: bool = False
    # The web interface's languages: which ones a visitor can choose, the one used when nothing says otherwise, and
    # whether the browser's own language is followed (until the visitor picks one, which the browser remembers).
    languages_enabled: list[str] = Field(
        default_factory=lambda: list(LANGUAGES), max_length=len(LANGUAGES)
    )
    default_language: str = "en"
    language_auto_detect: bool = True

    validate_kinds = field_validator("enabled_kinds")(_check_kinds)
    validate_ports = field_validator("allowed_ports")(_check_ports)
    validate_languages = field_validator("languages_enabled")(_check_languages)

    @model_validator(mode="after")
    def _default_is_on(self) -> RuntimeSettings:
        if self.default_language not in LANGUAGES:
            raise ValueError(f"default_language must be among {', '.join(LANGUAGES)}")
        if self.default_language not in self.languages_enabled:
            raise ValueError("default_language must be one of the languages that are on")
        return self


class RuntimeSettingsUpdate(BaseModel):
    """Every field optional: only the ones that are sent change."""

    measurements_enabled: bool | None = None
    max_concurrent_per_session: int | None = Field(default=None, ge=1, le=50)
    max_concurrent_per_ip: int | None = Field(default=None, ge=1, le=200)
    creations_per_minute_per_ip: int | None = Field(default=None, ge=1, le=600)
    max_probes_per_measurement: int | None = Field(default=None, ge=1, le=100_000)
    max_global_inflight_probes: int | None = Field(default=None, ge=1, le=1_000_000)
    measurement_deadline_s: int | None = Field(default=None, ge=5, le=300)
    default_count: int | None = Field(default=None, ge=1, le=10)
    max_count: int | None = Field(default=None, ge=1, le=10)
    target_denylist: list[str] | None = Field(default=None, max_length=1000)
    sample_retention_days: int | None = Field(default=None, ge=1, le=3650)
    measurement_retention_days: int | None = Field(default=None, ge=1, le=3650)
    enabled_kinds: list[str] | None = Field(default=None, max_length=8)
    allowed_ports: list[int] | None = Field(default=None, max_length=200)
    max_redirects: int | None = Field(default=None, ge=0, le=5)
    traceroute_max_hops: int | None = Field(default=None, ge=5, le=30)
    task_retries: int | None = Field(default=None, ge=0, le=3)
    community_tasks_per_minute: int | None = Field(default=None, ge=1, le=600)
    max_watches: int | None = Field(default=None, ge=0, le=500)
    min_watch_interval_s: int | None = Field(default=None, ge=30, le=3600)
    watch_history_days: int | None = Field(default=None, ge=1, le=365)
    allow_private_webhooks: bool | None = None
    languages_enabled: list[str] | None = Field(default=None, max_length=len(LANGUAGES))
    default_language: str | None = None
    language_auto_detect: bool | None = None

    validate_kinds = field_validator("enabled_kinds")(_check_kinds)
    validate_ports = field_validator("allowed_ports")(_check_ports)
    validate_languages = field_validator("languages_enabled")(_check_languages)


async def load_settings(session: AsyncSession) -> RuntimeSettings:
    rows = (await session.execute(select(AppSetting))).scalars().all()
    stored: dict[str, Any] = {row.key: row.value for row in rows}
    known = {k: v for k, v in stored.items() if k in RuntimeSettings.model_fields}
    return RuntimeSettings.model_validate(known)


async def save_settings(session: AsyncSession, update: RuntimeSettingsUpdate) -> RuntimeSettings:
    changes = update.model_dump(exclude_none=True)
    # What would be stored must hold together (a default language that is switched off, for one), checked before it is
    # written: the whole of it or none of it.
    RuntimeSettings.model_validate({**(await load_settings(session)).model_dump(), **changes})
    for key, value in changes.items():
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    await session.flush()
    return await load_settings(session)
