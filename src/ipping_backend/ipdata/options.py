"""What an administrator can set about IP data, stored in `app_settings`.

Every source has a switch (`source_enabled`: all on until turned off) and some take credentials. Credentials are accepted by the
admin API and used by the sources, and never sent back. The MaxMind license key can also come from the environment
(`IPPING_MAXMIND_ACCOUNT_ID`, `IPPING_MAXMIND_LICENSE_KEY`), which then wins.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import AppSetting
from .catalog import BY_ID

Edition = Literal["GeoLite2-City", "GeoLite2-Country", "GeoLite2-ASN"]
EDITIONS: tuple[Edition, ...] = ("GeoLite2-City", "GeoLite2-Country", "GeoLite2-ASN")
OPTIONS_KEY = "ip_data"
CREDENTIALS_KEY = "maxmind_credentials"
KEYS_KEY = "ip_data_keys"

_ACCOUNT_ID = re.compile(r"[0-9]{1,12}\Z")
# MaxMind keys are letters, digits and underscores; 16 to 64 of them
_LICENSE_KEY = re.compile(r"[A-Za-z0-9_]{16,64}\Z")
# the tokens and keys of the other sources: printable ASCII without spaces
_SECRET = re.compile(r"[\x21-\x7e]{1,200}\Z")


def _default_editions() -> list[Edition]:
    return ["GeoLite2-City", "GeoLite2-ASN"]


class IpDataOptions(BaseModel):
    # MaxMind
    maxmind_auto_update: bool = True
    maxmind_update_hours: int = Field(default=24, ge=6, le=168)
    maxmind_editions: list[Edition] = Field(
        default_factory=_default_editions, min_length=1, max_length=3
    )
    # The lookup API (`/api/v1/ip`): whether it is on, who may use it, and how much
    lookup_enabled: bool = True
    lookup_access: Literal["public", "api_key", "admin"] = "api_key"
    lookup_per_minute: int = Field(default=60, ge=1, le=6000)
    # Fill a node's country, city, place, provider and ASN from the address it connects from, where an
    # administrator has not typed one
    node_autofill: bool = True
    # Which sources are used. Only the ones turned off or on explicitly are here: a source that is not listed is on.
    source_enabled: dict[str, bool] = Field(default_factory=dict)
    # How often the databases that need no account (DB-IP, IPtoASN, IPdeny, ip2region) are asked whether there is a newer file
    data_update_hours: int = Field(default=24, ge=6, le=168)

    @field_validator("maxmind_editions")
    @classmethod
    def _unique(cls, value: list[Edition]) -> list[Edition]:
        return [e for e in EDITIONS if e in value]

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

    maxmind_auto_update: bool | None = None
    maxmind_update_hours: int | None = Field(default=None, ge=6, le=168)
    maxmind_editions: list[Edition] | None = Field(default=None, min_length=1, max_length=3)
    lookup_enabled: bool | None = None
    lookup_access: Literal["public", "api_key", "admin"] | None = None
    lookup_per_minute: int | None = Field(default=None, ge=1, le=6000)
    node_autofill: bool | None = None
    data_update_hours: int | None = Field(default=None, ge=6, le=168)
    sources: dict[str, SourceUpdate] | None = None
    account_id: str | None = Field(default=None, max_length=32)
    license_key: str | None = Field(default=None, max_length=128, repr=False)
    clear_credentials: bool = False

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
            if source == "maxmind":
                raise ValueError(
                    "The MaxMind account ID and license key are set with account_id and license_key."
                )
            allowed = {c.name for c in info.credentials}
            for name, secret in update.credentials.items():
                if name not in allowed:
                    raise ValueError(f"{info.name} takes no {name}.")
                secret = secret.strip()
                if secret and not _SECRET.match(secret):
                    raise ValueError(
                        f"That does not look like a key or token (letters, digits and symbols, no spaces): {info.name} {name}."
                    )
                update.credentials[name] = secret
        return value

    @field_validator("account_id")
    @classmethod
    def _account(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value and not _ACCOUNT_ID.match(value):
            raise ValueError("The account ID is the number shown in your MaxMind account.")
        return value

    @field_validator("license_key")
    @classmethod
    def _key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value and not _LICENSE_KEY.match(value):
            raise ValueError(
                "That does not look like a MaxMind license key (letters, digits and underscores)."
            )
        return value


class Credentials(BaseModel):
    account_id: str = ""
    license_key: str = Field(default="", repr=False)
    source: Literal["admin", "environment", "none"] = "none"

    @property
    def usable(self) -> bool:
        return bool(self.account_id and self.license_key)


async def load_options(db: AsyncSession) -> IpDataOptions:
    row = await db.get(AppSetting, OPTIONS_KEY)
    stored: dict[str, Any] = row.value if row is not None and isinstance(row.value, dict) else {}
    known = {k: v for k, v in stored.items() if k in IpDataOptions.model_fields}
    return IpDataOptions.model_validate(known)


async def load_credentials(db: AsyncSession, settings: Settings) -> Credentials:
    if settings.maxmind_account_id and settings.maxmind_license_key:
        return Credentials(
            account_id=settings.maxmind_account_id.strip(),
            license_key=settings.maxmind_license_key.strip(),
            source="environment",
        )
    row = await db.get(AppSetting, CREDENTIALS_KEY)
    if row is not None and isinstance(row.value, dict):
        account, key = str(row.value.get("account_id", "")), str(row.value.get("license_key", ""))
        if account or key:
            return Credentials(account_id=account, license_key=key, source="admin")
    return Credentials()


async def load_keys(db: AsyncSession) -> dict[str, dict[str, str]]:
    """The credentials of the sources that are not MaxMind: `{source: {field: value}}`."""
    row = await db.get(AppSetting, KEYS_KEY)
    stored = row.value if row is not None and isinstance(row.value, dict) else {}
    return {
        source: {k: str(v) for k, v in fields.items() if v}
        for source, fields in stored.items()
        if source in BY_ID and isinstance(fields, dict)
    }


async def save(db: AsyncSession, update: IpDataUpdate) -> None:
    """Apply an update (the caller commits). The credentials are replaced or cleared, never merged blindly."""
    changes = update.model_dump(
        exclude_none=True,
        exclude={"account_id", "license_key", "clear_credentials", "sources"},
    )
    if update.sources:
        enabled = dict((await load_options(db)).source_enabled)
        for source, change in update.sources.items():
            if change.enabled is not None:
                enabled[source] = change.enabled
        changes["source_enabled"] = enabled
        keys = await load_keys(db)
        touched = False
        for source, change in update.sources.items():
            for name, secret in (change.credentials or {}).items():
                touched = True
                if secret:
                    keys.setdefault(source, {})[name] = secret
                else:
                    keys.get(source, {}).pop(name, None)
        if touched:
            payload = {s: f for s, f in keys.items() if f}
            row = await db.get(AppSetting, KEYS_KEY)
            if row is None:
                db.add(AppSetting(key=KEYS_KEY, value=payload))
            else:
                row.value = payload
    if changes:
        current = (await load_options(db)).model_dump()
        current.update(changes)
        value = IpDataOptions.model_validate(current).model_dump()
        row = await db.get(AppSetting, OPTIONS_KEY)
        if row is None:
            db.add(AppSetting(key=OPTIONS_KEY, value=value))
        else:
            row.value = value
    row = await db.get(AppSetting, CREDENTIALS_KEY)
    if update.clear_credentials:
        if row is not None:
            await db.delete(row)
    elif update.account_id is not None or update.license_key is not None:
        stored = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
        if update.account_id is not None:
            stored["account_id"] = update.account_id
        if update.license_key:  # an empty key means "keep the one that is saved"
            stored["license_key"] = update.license_key
        if row is None:
            db.add(AppSetting(key=CREDENTIALS_KEY, value=stored))
        else:
            row.value = stored
    await db.flush()
