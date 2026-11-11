from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .options import Edition

SourceKind = Literal["database", "online"]
AnswerStatus = Literal[
    "ok", "empty", "error", "disabled", "needs_setup", "waiting", "rate_limited", "offline"
]


class SourceSaid(BaseModel):
    """What one source said about an address: only the fields it had."""

    country: str | None = None
    region: str | None = None
    city: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy_radius_km: int | None = None
    time_zone: str | None = None
    asn: int | None = None
    organization: str | None = None


class SourceAnswerOut(BaseModel):
    """One source's part in an answer."""

    id: str
    name: str
    kind: SourceKind
    status: AnswerStatus = Field(
        description="ok: it said something; empty: it knows nothing about this address; the others say why it did not answer"
    )
    message: str | None = Field(default=None, description="Why it did not answer, when it did not")
    said: SourceSaid = Field(description="What it said")
    agrees: list[str] = Field(
        description="The fields (country, region, city, location, postal_code, time_zone, asn, organization) where it voted for the winning value"
    )
    differs: list[str] = Field(description="The fields where it voted for something else")


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
    source: str | None = Field(default=None, description="Which sources answered")
    database_built: datetime | None = None
    attribution: str | None = None
    confidence: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "For each field that has an answer (country, region, city, location, postal_code, time_zone, asn, organization), "
            "how likely it is right as a percentage from 1 to 99: from how many sources voted for it, and how reliable each "
            "is for that kind of data. A value that only one source gave is still answered, with that source's reliability."
        ),
    )
    sources: list[SourceAnswerOut] = Field(
        default_factory=list,
        description="Every source, with what it said, or why it did not answer",
    )


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


class CredentialOut(BaseModel):
    name: str
    label: str
    secret: bool = Field(
        description="Whether it is a secret: a secret is never sent back, only whether it is set"
    )
    required: bool
    set: bool
    value: str | None = Field(default=None, description="The value, for what is not a secret")


class SourceFileOut(BaseModel):
    name: str
    size: int | None = None
    built: datetime | None = Field(
        default=None, description="When the data was built, where the file says"
    )
    rows: int | None = None


SourceState = Literal[
    "ready", "disabled", "needs_setup", "waiting", "offline", "no_directory", "paused"
]


class SourceAdminOut(BaseModel):
    """One IP data source as the admin page shows it."""

    id: str
    name: str
    kind: SourceKind
    homepage: str
    summary: str = Field(description="What it knows")
    cost: str = Field(description="What it needs from the administrator")
    attribution: str | None = None
    enabled: bool
    state: SourceState = Field(description="ready, or why the source does not answer now")
    detail: str | None = None
    credentials: list[CredentialOut]
    files: list[SourceFileOut] = Field(description="The files of a database that are in use")
    running: bool = Field(description="Whether a download is going on")
    last_attempt: datetime | None = Field(
        default=None, description="When it was last downloaded, or asked"
    )
    last_success: datetime | None = None
    next_check: datetime | None = None
    error: str | None = Field(default=None, description="How the last download or lookup failed")


class IpDataOut(BaseModel):
    maxmind: MaxMindOut
    lookup: LookupSettingsOut
    node_autofill: bool
    databases_loaded: bool = Field(
        description="Whether any IP data source can answer now (a database with data, or an online source)"
    )
    source: str | None = None
    attribution: str | None = None
    sources: list[SourceAdminOut] = Field(
        description="Every source, with its switch, what it needs and how it is doing"
    )
    offline: bool = Field(
        description="True when the server may not call out (IPPING_IPDATA_OFFLINE): no online lookups, no downloads"
    )
    data_update_hours: int = Field(
        description="How often the databases that need no account are asked for a newer file"
    )
