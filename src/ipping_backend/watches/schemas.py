"""API shapes for watches (scheduled checks)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..nodes.protocol import DnsRecordType, ProbeKind

Status = Literal["ok", "degraded", "down", "error"]


class WatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: ProbeKind
    target: str = Field(min_length=1, max_length=2048, description="Anything a measurement accepts")
    port: int | None = Field(default=None, ge=1, le=65535)
    count: int | None = Field(default=None, ge=1, le=10)
    follow_redirects: bool = False
    dns_mode: Literal["fixed", "regional"] = "fixed"
    family: Literal["auto", "4", "6"] = "auto"
    record_type: DnsRecordType | None = None
    resolver: Literal["system", "cloudflare", "google", "quad9"] | None = None
    transport: Literal["udp", "doh", "tcp", "quic"] | None = None
    node_ids: list[str] | None = Field(
        default=None, max_length=1000, description="Omit for all nodes"
    )
    interval_s: int = Field(default=300, ge=30, le=86_400, description="Seconds between runs")
    min_ok_pct: int = Field(
        default=80,
        ge=1,
        le=100,
        description="Bad when fewer than this share of nodes get a good answer",
    )
    alert_after: int = Field(
        default=2, ge=1, le=10, description="Bad runs in a row before an alert"
    )
    alert_url: str | None = Field(default=None, max_length=2048)
    alert_format: Literal["json", "ntfy"] = "json"
    enabled: bool = True


class WatchPatch(BaseModel):
    """Every field optional: only the ones that are sent change. The kind cannot change."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    target: str | None = Field(default=None, min_length=1, max_length=2048)
    port: int | None = Field(default=None, ge=1, le=65535)
    count: int | None = Field(default=None, ge=1, le=10)
    follow_redirects: bool | None = None
    dns_mode: Literal["fixed", "regional"] | None = None
    family: Literal["auto", "4", "6"] | None = None
    record_type: DnsRecordType | None = None
    resolver: Literal["system", "cloudflare", "google", "quad9"] | None = None
    transport: Literal["udp", "doh", "tcp", "quic"] | None = None
    node_ids: list[str] | None = Field(default=None, max_length=1000)
    all_nodes: bool | None = Field(default=None, description="true clears node_ids")
    interval_s: int | None = Field(default=None, ge=30, le=86_400)
    min_ok_pct: int | None = Field(default=None, ge=1, le=100)
    alert_after: int | None = Field(default=None, ge=1, le=10)
    alert_url: str | None = Field(
        default=None, max_length=2048, description="Empty string clears it"
    )
    alert_format: Literal["json", "ntfy"] | None = None
    enabled: bool | None = None


class WatchRunOut(BaseModel):
    at: datetime
    status: Status
    nodes_total: int
    nodes_ok: int
    median_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    measurement_id: str | None = None
    note: str | None = None


class WatchOut(BaseModel):
    id: str
    name: str
    kind: str
    target: str
    options: dict[str, Any]
    node_ids: list[str] | None
    interval_s: int
    min_ok_pct: int
    alert_after: int
    alert_url: str | None
    alert_format: str
    enabled: bool
    created_by: str
    created_at: datetime
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: Status | None
    bad_streak: int
    alerting: bool
    last_alert_at: datetime | None
    last_alert_error: str | None
    history: list[WatchRunOut] = Field(default=[], description="The latest runs, oldest first")


class RunNowOut(BaseModel):
    measurement_id: str | None


class TestAlertOut(BaseModel):
    ok: bool
    error: str | None = None
