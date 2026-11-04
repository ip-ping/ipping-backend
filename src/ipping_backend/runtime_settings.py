"""Settings an administrator can change while the service is running.

Stored one row per key in `app_settings`; keys with no row use the defaults below. These are the
safety valves of a public measurement service: the emergency stop, the per-user and global
budgets, and the target denylist.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AppSetting


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


async def load_settings(session: AsyncSession) -> RuntimeSettings:
    rows = (await session.execute(select(AppSetting))).scalars().all()
    stored: dict[str, Any] = {row.key: row.value for row in rows}
    known = {k: v for k, v in stored.items() if k in RuntimeSettings.model_fields}
    return RuntimeSettings.model_validate(known)


async def save_settings(session: AsyncSession, update: RuntimeSettingsUpdate) -> RuntimeSettings:
    changes = update.model_dump(exclude_none=True)
    for key, value in changes.items():
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    await session.flush()
    return await load_settings(session)
