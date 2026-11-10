from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .options import Edition


class IpInfoOut(BaseModel):
    """What the IP databases say about an address."""

    address: str
    version: Literal[4, 6]
    public: bool = Field(
        description="False for a private, reserved or otherwise non-routable address: nothing is looked up"
    )
    found: bool = Field(description="Whether the databases know anything about this address")
    network: str | None = Field(
        default=None, description="The network the databases' record covers"
    )
    country: str | None = Field(default=None, description="ISO 3166-1 alpha-2")
    country_name: str | None = None
    continent: str | None = Field(default=None, description="Continent code")
    region: str | None = None
    region_code: str | None = None
    city: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy_radius_km: int | None = Field(
        default=None, description="How far from the point the address may be"
    )
    time_zone: str | None = None
    asn: int | None = None
    organization: str | None = Field(default=None, description="Who the AS number belongs to")
    source: str | None = Field(default=None, description="Which databases answered")
    database_built: datetime | None = None
    attribution: str | None = None


class DatabaseOut(BaseModel):
    edition: Edition
    present: bool
    file: str | None = None
    type: str | None = None
    built: datetime | None = Field(default=None, description="When MaxMind built the database")
    size: int | None = None
    downloaded_at: datetime | None = None
    checked_at: datetime | None = None
    error: str | None = None


class StatusErrorOut(BaseModel):
    code: str
    message: str


class MaxMindOut(BaseModel):
    account_id: str
    license_key_set: bool = Field(
        description="Whether a license key is saved. The key itself is never sent back."
    )
    credentials_source: Literal["admin", "environment", "none"]
    editions: list[Edition]
    auto_update: bool
    update_hours: int
    directory_configured: bool = Field(
        description="Whether the backend has a directory to keep the databases in"
    )
    running: bool
    last_attempt: datetime | None
    last_success: datetime | None
    next_check: datetime | None
    error: StatusErrorOut | None
    databases: list[DatabaseOut]


class LookupSettingsOut(BaseModel):
    enabled: bool
    access: Literal["public", "api_key", "admin"]
    per_minute: int


class IpDataOut(BaseModel):
    maxmind: MaxMindOut
    lookup: LookupSettingsOut
    node_autofill: bool
    databases_loaded: bool = Field(
        description="Whether any IP database is in use, from MaxMind or another source"
    )
    source: str | None = None
    attribution: str | None = None
