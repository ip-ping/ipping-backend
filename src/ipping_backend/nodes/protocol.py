"""Node <-> backend wire protocol, version 1.

JSON text frames over a WebSocket that the node opens. Every frame has a `type`. Unknown fields
are ignored on receipt, so either side can add optional fields without a version bump; anything
that changes the meaning of an existing field needs a new protocol version.

This module is the source of truth. `scripts/export_contracts.py` turns it into
`contracts/node-protocol-v1.schema.json`, and `contracts/fixtures/` holds example frames that
the Rust node also parses in its tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = (1,)

MAX_FRAME_BYTES = 64 * 1024


class _Frame(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------------------------
# Node -> backend
# --------------------------------------------------------------------------------------------


class Capabilities(_Frame):
    kinds: list[str] = Field(default_factory=lambda: ["ping"])
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
    """One echo request: its round-trip time, or why there is none."""

    type: Literal["sample"] = "sample"
    task_id: str
    attempt_id: str
    seq: int = Field(ge=0, lt=1000)
    status: Literal["ok", "timeout", "error"]
    rtt_us: int | None = Field(default=None, ge=0, le=60_000_000)
    error_code: str | None = Field(default=None, max_length=40)


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
    """Exactly one of `ip` (probe this address) or `hostname` (resolve it on the node)."""

    ip: str | None = None
    hostname: str | None = None
    family: Literal["auto", "4", "6"] = "auto"

    @model_validator(mode="after")
    def _exactly_one(self) -> TaskTarget:
        if (self.ip is None) == (self.hostname is None):
            raise ValueError("exactly one of ip or hostname is required")
        return self


class PingOptions(_Frame):
    count: int = Field(ge=1, le=10)
    interval_ms: int = Field(ge=200, le=5000)
    timeout_ms: int = Field(ge=200, le=10000)


class TaskFrame(_Frame):
    type: Literal["task"] = "task"
    task_id: str
    attempt_id: str
    kind: Literal["ping"] = "ping"
    target: TaskTarget
    options: PingOptions
    expires_at: datetime


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
    "node_busy": "The node is at its concurrency limit.",
    "expired": "The task expired before the node could start it.",
    "internal_error": "The node failed unexpectedly.",
    "node_disconnected": "The node disconnected during the measurement.",
    "deadline_exceeded": "The node did not finish before the measurement deadline.",
    "interrupted": "The backend restarted during the measurement.",
    "cancelled": "The measurement was cancelled.",
}
