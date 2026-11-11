"""What an administrator can set about IP data, stored in `app_settings`.

Every source has a switch (`source_enabled`: all on until turned off) and some take credentials (an account's key, where the free
download needs one). Credentials are accepted by the admin API and used by the downloader, and never sent back. MaxMind's account ID and
license key can also come from the environment (`IPPING_MAXMIND_ACCOUNT_ID`, `IPPING_MAXMIND_LICENSE_KEY`), which then wins.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import AppSetting
from .catalog import BY_ID

OPTIONS_KEY = "ip_data"
KEYS_KEY = "ip_data_keys"
# Where MaxMind's account was kept before every source took its credentials the same way
LEGACY_MAXMIND_KEY = "maxmind_credentials"

_ACCOUNT_ID = re.compile(r"[0-9]{1,12}\Z")
# MaxMind keys are letters, digits and underscores; 16 to 64 of them
_LICENSE_KEY = re.compile(r"[A-Za-z0-9_]{16,64}\Z")
# the tokens of the other sources: printable ASCII without spaces
_SECRET = re.compile(r"[\x21-\x7e]{1,200}\Z")


class IpDataOptions(BaseModel):
    # The lookup API (`/api/v1/ip`): whether it is on, who may use it, and how much
    lookup_enabled: bool = True
    lookup_access: Literal["public", "api_key", "admin"] = "api_key"
    lookup_per_minute: int = Field(default=60, ge=1, le=6000)
    # Fill a node's country, city, place, provider and ASN from the address it connects from, where an
    # administrator has not typed one
    node_autofill: bool = True
    # Which sources are used. Only the ones turned off or on explicitly are here: a source that is not listed is on.
    source_enabled: dict[str, bool] = Field(default_factory=dict)
    # How often the sources are asked whether there is a newer file (a source that is renewed less often is asked less often)
    data_update_hours: int = Field(default=24, ge=6, le=168)

    @field_validator("source_enabled")
    @classmethod
    def _known(cls, value: dict[str, bool]) -> dict[str, bool]:
        return {
            k: bool(v) for k, v in value.items() if k in BY_ID
        }  # an id that is gone is forgotten

    def enabled(self, source: str) -> bool:
        return self.source_enabled.get(source, True)


class SourceUpdate(BaseModel):
    """A change to one source: switch it, or set or clear its credentials (`""` clears one)."""

    enabled: bool | None = None
    credentials: dict[str, str] | None = None


class IpDataUpdate(BaseModel):
    """Every field optional: only the ones that are sent change."""

    lookup_enabled: bool | None = None
    lookup_access: Literal["public", "api_key", "admin"] | None = None
    lookup_per_minute: int | None = Field(default=None, ge=1, le=6000)
    node_autofill: bool | None = None
    data_update_hours: int | None = Field(default=None, ge=6, le=168)
    sources: dict[str, SourceUpdate] | None = None

    @field_validator("sources")
    @classmethod
    def _sources(cls, value: dict[str, SourceUpdate] | None) -> dict[str, SourceUpdate] | None:
        if value is None:
            return None
        for source, update in value.items():
            info = BY_ID.get(source)
            if info is None:
                raise ValueError(f"There is no IP data source called {source}.")
            if update.credentials is None:
                continue
            allowed = {c.name for c in info.credentials}
            for name, secret in update.credentials.items():
                if name not in allowed:
                    raise ValueError(f"{info.name} takes no {name}.")
                secret = secret.strip()
                if secret and not _valid(source, name, secret):
                    raise ValueError(_why_not(source, name, info.name))
                update.credentials[name] = secret
        return value


def _valid(source: str, name: str, secret: str) -> bool:
    if source == "maxmind" and name == "account_id":
        return bool(_ACCOUNT_ID.match(secret))
    if source == "maxmind" and name == "license_key":
        return bool(_LICENSE_KEY.match(secret))
    return bool(_SECRET.match(secret))


def _why_not(source: str, name: str, label: str) -> str:
    if source == "maxmind" and name == "account_id":
        return "The account ID is the number shown in your MaxMind account."
    if source == "maxmind" and name == "license_key":
        return "That does not look like a MaxMind license key (letters, digits and underscores)."
    return f"That does not look like a key or token (letters, digits and symbols, no spaces): {label} {name}."


def environment_account(settings: Settings) -> bool:
    """Whether MaxMind's account ID and license key come from the environment (and so are not set in the admin page)."""
    return bool(settings.maxmind_account_id and settings.maxmind_license_key)


async def load_options(db: AsyncSession) -> IpDataOptions:
    row = await db.get(AppSetting, OPTIONS_KEY)
    stored: dict[str, Any] = row.value if row is not None and isinstance(row.value, dict) else {}
    known = {k: v for k, v in stored.items() if k in IpDataOptions.model_fields}
    return IpDataOptions.model_validate(known)


async def load_keys(
    db: AsyncSession, settings: Settings | None = None
) -> dict[str, dict[str, str]]:
    """The credentials of the sources: `{source: {field: value}}`, only the ones that are set.

    With `settings`, MaxMind's account from the environment replaces the one saved in the admin page.
    """
    row = await db.get(AppSetting, KEYS_KEY)
    stored = row.value if row is not None and isinstance(row.value, dict) else {}
    keys: dict[str, dict[str, str]] = {}
    for source, fields in stored.items():
        info = BY_ID.get(source)
        if info is None or not isinstance(fields, dict):
            continue
        names = {
            c.name for c in info.credentials
        }  # a credential a source no longer takes is dropped
        keys[source] = {k: str(v) for k, v in fields.items() if v and k in names}
    if "maxmind" not in keys:
        legacy = await db.get(AppSetting, LEGACY_MAXMIND_KEY)
        if legacy is not None and isinstance(legacy.value, dict):
            keys["maxmind"] = {
                name: str(legacy.value[name])
                for name in ("account_id", "license_key")
                if legacy.value.get(name)
            }
    if settings is not None and environment_account(settings):
        keys["maxmind"] = {
            "account_id": str(settings.maxmind_account_id).strip(),
            "license_key": str(settings.maxmind_license_key).strip(),
        }
    return {source: fields for source, fields in keys.items() if fields}


async def save(db: AsyncSession, update: IpDataUpdate) -> None:
    """Apply an update (the caller commits). A credential is replaced or cleared, never merged blindly."""
    changes = update.model_dump(exclude_none=True, exclude={"sources"})
    if update.sources:
        enabled = dict((await load_options(db)).source_enabled)
        for source, change in update.sources.items():
            if change.enabled is not None:
                enabled[source] = change.enabled
        changes["source_enabled"] = enabled
        if any(change.credentials for change in update.sources.values()):
            keys = await load_keys(db)
            for source, change in update.sources.items():
                for name, secret in (change.credentials or {}).items():
                    if secret:
                        keys.setdefault(source, {})[name] = secret
                    else:
                        keys.get(source, {}).pop(name, None)
            payload = {s: f for s, f in keys.items() if f}
            row = await db.get(AppSetting, KEYS_KEY)
            if row is None:
                db.add(AppSetting(key=KEYS_KEY, value=payload))
            else:
                row.value = payload
            legacy = await db.get(AppSetting, LEGACY_MAXMIND_KEY)
            if legacy is not None:  # now kept with the others
                await db.delete(legacy)
    if changes:
        current = (await load_options(db)).model_dump()
        current.update(changes)
        value = IpDataOptions.model_validate(current).model_dump()
        row = await db.get(AppSetting, OPTIONS_KEY)
        if row is None:
            db.add(AppSetting(key=OPTIONS_KEY, value=value))
        else:
            row.value = value
    await db.flush()
