"""Node <-> backend wire protocol, version 1.

JSON text frames over a WebSocket that the node opens. Every frame has a `type`. Unknown fields
are ignored on receipt, so either side can add optional fields without a version bump; anything
that changes the meaning of an existing field needs a new protocol version.

This module is the source of truth. `scripts/export_contracts.py` turns it into
`contracts/node-protocol-v1.schema.json`, and `contracts/fixtures/` holds example frames that
the Rust node also parses in its tests.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = (1,)

MAX_FRAME_BYTES = 64 * 1024
# What a sample may carry besides its round trip. Bounded because it comes from a node.
MAX_DETAIL_BYTES = 4096

ProbeKind = Literal["ping", "tcp", "http", "traceroute"]
PROBE_KINDS: tuple[str, ...] = ("ping", "tcp", "http", "traceroute")


class _Frame(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------------------------
# Node -> backend
# --------------------------------------------------------------------------------------------


class Capabilities(_Frame):
    """What this node can run. A kind the backend does not know is simply never used."""

    kinds: list[str] = Field(default_factory=lambda: ["ping"], max_length=16)
    ipv4: bool = True
    ipv6: bool = False


class Hello(_Frame):
    type: Literal["hello"] = "hello"
    protocol_versions: list[int] = Field(min_length=1, max_length=16)
    node_version: str = Field(max_length=32)
    capabilities: Capabilities


class TaskAck(_Frame):
    """The node accepted the task and is starting it."""

    type: Literal["task_ack"] = "task_ack"
    task_id: str
    attempt_id: str


class SampleFrame(_Frame):
    """One probe: its round-trip time, or why there is none.

    What a sample is depends on the task's kind: an echo request (ping), a connection attempt
    (tcp), one request (http) or one hop (traceroute). `detail` carries what only some kinds have;
    see `HttpDetail` and `HopDetail`.
    """

    type: Literal["sample"] = "sample"
    task_id: str
    attempt_id: str
    seq: int = Field(ge=0, lt=1000)
    status: Literal["ok", "timeout", "error"]
    rtt_us: int | None = Field(default=None, ge=0, le=60_000_000)
    error_code: str | None = Field(default=None, max_length=40)
    detail: dict[str, Any] | None = None

    @field_validator("detail")
    @classmethod
    def _small(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        # Too much is dropped, not rejected: the round trip in the same frame is still good.
        if value is not None and len(json.dumps(value, separators=(",", ":"))) > MAX_DETAIL_BYTES:
            return None
        return value


class TaskResult(_Frame):
    type: Literal["task_result"] = "task_result"
    task_id: str
    attempt_id: str
    status: Literal["completed", "failed"]
    tested_ip: str | None = Field(default=None, max_length=45)
    resolved_ips: list[str] = Field(default_factory=list, max_length=32)
    error_code: str | None = Field(default=None, max_length=40)
    error_message: str | None = Field(default=None, max_length=300)


class Ping(_Frame):
    type: Literal["ping"] = "ping"


NodeToBackend = Annotated[
    Hello | TaskAck | SampleFrame | TaskResult | Ping,
    Field(discriminator="type"),
]
node_to_backend: TypeAdapter[NodeToBackend] = TypeAdapter(NodeToBackend)


# --------------------------------------------------------------------------------------------
# Backend -> node
# --------------------------------------------------------------------------------------------


class Limits(_Frame):
    max_concurrent_tasks: int = 8
    max_count: int = 10


class Welcome(_Frame):
    type: Literal["welcome"] = "welcome"
    protocol_version: int
    node_id: str
    node_status: Literal["pending", "approved"]
    server_time: datetime
    heartbeat_interval_s: int = 15
    limits: Limits = Field(default_factory=Limits)


class TaskTarget(_Frame):
    """Exactly one of `ip` (probe this address) or `hostname` (resolve it on the node).

    `port` is the destination of a tcp or http task. `server_name` is the name an http task
    presents (Host header, TLS server name) while connecting to a pinned `ip`.
    """

    ip: str | None = None
    hostname: str | None = None
    family: Literal["auto", "4", "6"] = "auto"
    port: int | None = Field(default=None, ge=1, le=65535)
    server_name: str | None = Field(default=None, max_length=253)

    @model_validator(mode="after")
    def _exactly_one(self) -> TaskTarget:
        if (self.ip is None) == (self.hostname is None):
            raise ValueError("exactly one of ip or hostname is required")
        return self


class TaskOptions(_Frame):
    """Settings of a task. Which fields matter depends on the kind:

    * ping: count, interval_ms, timeout_ms
    * tcp: count, interval_ms, timeout_ms (a port is in the target)
    * http: scheme, path, timeout_ms, max_redirects (one request, so no count)
    * traceroute: max_hops, timeout_ms (per hop; three probes are sent to each hop)
    """

    count: int = Field(default=1, ge=1, le=10)
    interval_ms: int = Field(default=1000, ge=200, le=5000)
    timeout_ms: int = Field(ge=200, le=10000)
    scheme: Literal["http", "https"] | None = None
    path: str | None = Field(default=None, max_length=256)
    max_redirects: int | None = Field(default=None, ge=0, le=5)
    max_hops: int | None = Field(default=None, ge=1, le=30)


class TaskFrame(_Frame):
    type: Literal["task"] = "task"
    task_id: str
    attempt_id: str
    kind: ProbeKind = "ping"
    target: TaskTarget
    options: TaskOptions
    expires_at: datetime

    @model_validator(mode="after")
    def _complete_for_kind(self) -> TaskFrame:
        if self.kind in ("tcp", "http") and self.target.port is None:
            raise ValueError(f"a {self.kind} task needs a port")
        if self.kind == "http" and (self.options.scheme is None or self.options.path is None):
            raise ValueError("an http task needs a scheme and a path")
        if self.kind == "traceroute" and self.options.max_hops is None:
            raise ValueError("a traceroute task needs max_hops")
        return self


class CancelFrame(_Frame):
    type: Literal["cancel"] = "cancel"
    task_id: str


class Pong(_Frame):
    type: Literal["pong"] = "pong"


class ErrorFrame(_Frame):
    type: Literal["error"] = "error"
    code: str
    message: str


BackendToNode = Annotated[
    Welcome | TaskFrame | CancelFrame | Pong | ErrorFrame,
    Field(discriminator="type"),
]
backend_to_node: TypeAdapter[BackendToNode] = TypeAdapter(BackendToNode)


# Error codes a task can end with. Kept in one place so the UI can explain each of them.
TASK_ERROR_CODES = {
    "dns_failure": "The node could not resolve the hostname.",
    "unsupported_family": "The node has no connectivity for this IP version.",
    "blocked_by_policy": "The destination is not allowed.",
    "permission_denied": "The node is not allowed to send ICMP echo requests.",
    "unsupported_kind": "The node cannot run this kind of probe.",
    "port_not_allowed": "The destination port is not allowed.",
    "node_busy": "The node is at its concurrency limit.",
    "expired": "The task expired before the node could start it.",
    "internal_error": "The node failed unexpectedly.",
    "node_disconnected": "The node disconnected during the measurement.",
    "deadline_exceeded": "The node did not finish before the measurement deadline.",
    "interrupted": "The backend restarted during the measurement.",
    "cancelled": "The measurement was cancelled.",
}


# Error codes a single sample can carry (the node reports one of these, or another short code).
SAMPLE_ERROR_CODES = {
    "timeout": "No answer arrived in time.",
    "connection_refused": "The host answered, but nothing accepts connections on that port.",
    "connection_reset": "The connection was reset.",
    "host_unreachable": "No route to the host.",
    "network_unreachable": "No route to the network.",
    "tls_error": "The TLS handshake failed.",
    "tls_certificate_invalid": "The server presented a certificate that could not be trusted.",
    "protocol_error": "The server did not answer with valid HTTP.",
    "too_many_redirects": "The redirect limit was reached.",
    "redirect_blocked": "A redirect pointed somewhere that is not allowed.",
    "permission_denied": "The node is not allowed to run this probe.",
    "send_failed": "The probe could not be sent.",
}


# --------------------------------------------------------------------------------------------
# Sample details
#
# Not frames themselves: they travel in `SampleFrame.detail`, which is a plain JSON object. The
# backend parses it with the model for the task's kind and drops anything that does not fit, so a
# node can never put more than this into a stored result.
# --------------------------------------------------------------------------------------------

_PRINTABLE = re.compile(r"^[\x20-\x7e]*$")


def _printable(value: str | None) -> str | None:
    if value is None:
        return None
    return value if _PRINTABLE.match(value) else re.sub(r"[^\x20-\x7e]", "?", value)


class TlsInfo(_Frame):
    version: str | None = Field(default=None, max_length=16)
    cipher: str | None = Field(default=None, max_length=64)
    subject: str | None = Field(default=None, max_length=200)
    issuer: str | None = Field(default=None, max_length=200)
    not_before: datetime | None = None
    not_after: datetime | None = None
    verified: bool = False
    verify_error: str | None = Field(default=None, max_length=200)

    @field_validator("version", "cipher", "subject", "issuer", "verify_error")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _printable(value)


class RedirectHop(_Frame):
    """A redirect that was followed or stopped at. Never carries a query string."""

    status_code: int = Field(ge=300, le=399)
    location_scheme: str | None = Field(default=None, max_length=8)
    location_host: str | None = Field(default=None, max_length=253)
    location_port: int | None = Field(default=None, ge=1, le=65535)
    location_path: str | None = Field(default=None, max_length=200)
    followed: bool = False

    @field_validator("location_scheme", "location_host", "location_path")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _printable(value)


class HttpDetail(_Frame):
    """What one HTTP request found. Times are microseconds, measured on the node."""

    status_code: int | None = Field(default=None, ge=100, le=599)
    http_version: str | None = Field(default=None, max_length=8)
    server: str | None = Field(default=None, max_length=100)
    content_type: str | None = Field(default=None, max_length=100)
    dns_us: int | None = Field(default=None, ge=0, le=60_000_000)
    connect_us: int | None = Field(default=None, ge=0, le=60_000_000)
    tls_us: int | None = Field(default=None, ge=0, le=60_000_000)
    ttfb_us: int | None = Field(default=None, ge=0, le=60_000_000)
    total_us: int | None = Field(default=None, ge=0, le=60_000_000)
    final_scheme: str | None = Field(default=None, max_length=8)
    final_host: str | None = Field(default=None, max_length=253)
    final_path: str | None = Field(default=None, max_length=200)
    tls: TlsInfo | None = None
    redirects: list[RedirectHop] = Field(default_factory=list, max_length=6)

    @field_validator(
        "http_version", "server", "content_type", "final_scheme", "final_host", "final_path"
    )
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _printable(value)


class HopDetail(_Frame):
    """One traceroute hop: the router that answered at this TTL.

    `ip` is only ever a public address. A router inside the node operator's own network (or any
    other non-public address) is reported as `private` with no address, so a result never
    discloses the layout of someone's LAN.
    """

    ttl: int = Field(ge=1, le=64)
    ip: str | None = Field(default=None, max_length=45)
    private: bool = False
    rtts_us: list[int] = Field(default_factory=list, max_length=3)
    reached: bool = False

    @field_validator("rtts_us")
    @classmethod
    def _bounded(cls, value: list[int]) -> list[int]:
        if any(v < 0 or v > 60_000_000 for v in value):
            raise ValueError("rtt out of range")
        return value


def parse_detail(kind: str, detail: dict[str, Any] | None) -> BaseModel | None:
    """The validated detail of a sample of this kind, or None if there is none or it is invalid."""
    if not detail:
        return None
    try:
        if kind == "http":
            return HttpDetail.model_validate(detail)
        if kind == "traceroute":
            return HopDetail.model_validate(detail)
    except ValueError:
        pass
    return None
