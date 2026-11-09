"""API-facing shapes for measurements. These end up in contracts/openapi.json."""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..nodes.protocol import DnsDetail, DnsRecordType, HopDetail, HttpDetail, ProbeKind, TlsDetail

TASK_ACTIVE = ("pending", "dispatched", "running")
TASK_TERMINAL = (
    "completed",
    "failed",
    "timed_out",
    "node_disconnected",
    "interrupted",
    "cancelled",
)


class CreateMeasurementRequest(BaseModel):
    target: str = Field(min_length=1, max_length=2048, description="IP address, hostname or URL")
    kind: ProbeKind = Field(
        default="ping",
        description=(
            "ping: ICMP echo. tcp: time to open a TCP connection to `port`. http: one request, "
            "with timings and the TLS certificate. traceroute: the routers on the way. dns: ask "
            "each node's resolver (or a public one) about a name and compare the answers. "
            "tls: a TLS handshake with no request after it, and what the certificate says."
        ),
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description=(
            "Destination port of a tcp or tls check. It can also be given as host:port or in a "
            "URL. An http check takes its port from the URL."
        ),
    )
    follow_redirects: bool = Field(
        default=False, description="http only: follow redirects (up to the deployment's limit)"
    )
    dns_mode: Literal["fixed", "regional"] = Field(
        default="fixed",
        description=(
            "fixed: resolve once and have every node probe the same address (use this for "
            "location estimates). regional: each node resolves the hostname itself."
        ),
    )
    family: Literal["auto", "4", "6"] = "auto"
    record_type: DnsRecordType | None = Field(
        default=None, description="dns only: the record type to ask for (default A)"
    )
    resolver: Literal["system", "cloudflare", "google", "quad9"] | None = Field(
        default=None,
        description=(
            "dns only: system asks each node's own resolvers (what people near the node get); "
            "the others ask that public resolver from every node (default system)"
        ),
    )
    transport: Literal["udp", "doh"] | None = Field(
        default=None,
        description=(
            "dns only: udp (the default) or doh, DNS over HTTPS (cloudflare and google; not the nodes' "
            "own resolvers, and not quad9, which answers only over HTTP/2)"
        ),
    )
    count: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description=(
            "ping and tcp: probes per node. traceroute: how many times to trace the route "
            "(default 1; more shows the loss at each hop, like mtr)"
        ),
    )
    node_ids: list[str] | None = Field(
        default=None, max_length=1000, description="Limit to these nodes; omit for all eligible"
    )
    countries: list[str] | None = Field(
        default=None, max_length=300, description="Limit to nodes in these ISO 3166 countries"
    )

    @field_validator("countries")
    @classmethod
    def _upper(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [c.strip().upper() for c in value]
        if any(len(c) != 2 or not c.isalpha() for c in cleaned):
            raise ValueError("countries must be two-letter ISO 3166 codes")
        return cleaned


class NodeInfo(BaseModel):
    """What a result needs to say about the node that produced it (frozen at task creation)."""

    id: str
    name: str
    country: str | None = None
    region: str | None = None
    city: str | None = None
    provider: str | None = None
    asn: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_verified: bool = False


class Summary(BaseModel):
    """ping and tcp: probes sent and answered. http: the one request. traceroute: hops probed and
    hops that answered, with the timings of the destination when it was reached."""

    sent: int
    received: int
    loss_pct: float
    min_ms: float | None = None
    median_ms: float | None = None
    avg_ms: float | None = None
    max_ms: float | None = None
    reached: bool | None = Field(
        default=None, description="traceroute only: whether the destination answered"
    )


class SampleOut(BaseModel):
    seq: int
    status: Literal["ok", "timeout", "error"]
    rtt_ms: float | None = None
    error_code: str | None = None
    http: HttpDetail | None = Field(default=None, description="http: what the request found")
    hop: HopDetail | None = Field(default=None, description="traceroute: the router at this hop")
    dns: DnsDetail | None = Field(default=None, description="dns: what the question found")
    tls: TlsDetail | None = Field(
        default=None, description="tls: the certificate and the handshake"
    )


class TaskStateOut(BaseModel):
    id: str
    node: NodeInfo
    status: str = Field(description="pending, dispatched, running or a terminal status")
    attempts: int = Field(
        default=1,
        description="Above 1 if the task was sent again because a backend instance died",
    )
    error_code: str | None = None
    error_message: str | None = None
    tested_ip: str | None = None
    resolved_ips: list[str] = []
    summary: Summary | None = None


class TaskOut(TaskStateOut):
    samples: list[SampleOut] = []


class MeasurementOptions(BaseModel):
    """The settings a measurement was run with; which ones are present depends on its kind."""

    count: int
    interval_ms: int
    timeout_ms: int
    port: int | None = None
    scheme: Literal["http", "https"] | None = None
    path: str | None = None
    max_redirects: int | None = None
    max_hops: int | None = None
    record_type: DnsRecordType | None = None
    resolver: Literal["system", "cloudflare", "google", "quad9"] | None = None
    transport: Literal["udp", "doh"] | None = None


class TargetOut(BaseModel):
    host: str
    ip: str | None = Field(description="The address every node probes (fixed mode)")
    resolved_ips: list[str]
    input_kind: Literal["ip", "hostname", "url"]


class MeasurementStats(BaseModel):
    nodes_total: int
    nodes_responding: int
    nodes_failed: int
    fastest_task_id: str | None = None
    fastest_median_ms: float | None = None
    slowest_task_id: str | None = None
    slowest_median_ms: float | None = None


class MeasurementOut(BaseModel):
    id: str
    kind: str
    status: Literal["running", "completed", "cancelled", "interrupted"]
    target: TargetOut
    dns_mode: Literal["fixed", "regional"]
    family: Literal["auto", "4", "6"]
    options: MeasurementOptions
    shared: bool
    created_at: datetime
    deadline_at: datetime
    finished_at: datetime | None
    last_event_seq: int
    stats: MeasurementStats
    tasks: list[TaskOut]


class ShareRequest(BaseModel):
    shared: bool


def summarize(samples: list[SampleOut], kind: str = "ping") -> Summary | None:
    if not samples:
        return None
    if kind == "traceroute":
        return _summarize_route(samples)
    rtts = [s.rtt_ms for s in samples if s.status == "ok" and s.rtt_ms is not None]
    sent = len(samples)
    received = len(rtts)
    summary = Summary(
        sent=sent, received=received, loss_pct=round((sent - received) * 100 / sent, 1)
    )
    _fill_timings(summary, rtts)
    return summary


def _fill_timings(summary: Summary, rtts: list[float]) -> None:
    if rtts:
        summary.min_ms = round(min(rtts), 3)
        summary.max_ms = round(max(rtts), 3)
        summary.avg_ms = round(statistics.fmean(rtts), 3)
        summary.median_ms = round(statistics.median(rtts), 3)


def _summarize_route(samples: list[SampleOut]) -> Summary:
    """Hops that answered are not "received": silent routers are normal. What matters is whether
    the destination was reached, and how long it took."""
    answered = sum(1 for s in samples if s.status == "ok")
    destination = next((s for s in samples if s.hop is not None and s.hop.reached), None)
    summary = Summary(
        sent=len(samples),
        received=answered,
        loss_pct=round((len(samples) - answered) * 100 / len(samples), 1),
        reached=destination is not None,
    )
    if destination is not None and destination.hop is not None:
        _fill_timings(summary, [rtt / 1000 for rtt in destination.hop.rtts_us])
    return summary


def compute_stats(tasks: list[TaskStateOut]) -> MeasurementStats:
    responding = [
        t for t in tasks if t.summary and t.summary.received > 0 and t.summary.reached is not False
    ]
    failed = [
        t for t in tasks if t.status in ("failed", "timed_out", "node_disconnected", "interrupted")
    ]
    with_median = [t for t in responding if t.summary and t.summary.median_ms is not None]
    fastest = min(with_median, key=lambda t: t.summary.median_ms or 0, default=None)  # type: ignore[union-attr]
    slowest = max(with_median, key=lambda t: t.summary.median_ms or 0, default=None)  # type: ignore[union-attr]
    return MeasurementStats(
        nodes_total=len(tasks),
        nodes_responding=len(responding),
        nodes_failed=len(failed),
        fastest_task_id=fastest.id if fastest else None,
        fastest_median_ms=fastest.summary.median_ms if fastest and fastest.summary else None,
        slowest_task_id=slowest.id if slowest else None,
        slowest_median_ms=slowest.summary.median_ms if slowest and slowest.summary else None,
    )
