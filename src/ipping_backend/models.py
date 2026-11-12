"""Database schema.

Measurements are stored so that they stay interpretable after the fact: every task keeps a
snapshot of the node it ran on, so renaming or moving a node never rewrites history.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from .nodes.naming import node_label


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def new_measurement_id() -> str:
    # Unguessable: knowing a measurement id is part of what grants access to its results.
    return secrets.token_urlsafe(16)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetimes on every backend (SQLite drops the offset otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to the database; use timezone-aware values")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="admin")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WebSession(Base):
    """Browser session. `id` is the SHA-256 of the cookie value, never the value itself."""

    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # What a node that enrolls with this token becomes: official | community (run by a volunteer).
    tier: Mapped[str] = mapped_column(String(16), default="official", server_default="official")
    # Whether a node that enrolls with this token is sponsored (see `Node.sponsored`).
    sponsored: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # A reusable token enrolls any number of nodes until it expires or is revoked; a single-use one is claimed by the first.
    reusable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # How many nodes have enrolled with it (`node_id` is the latest).
    uses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    # pending (awaiting approval) | approved | revoked
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    credential_hash: Mapped[str] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    asn: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Which of country, region, city, latitude, longitude, provider and asn were filled in automatically from the
    # address the node connects from (see `nodes/autofill.py`). Those are refreshed with the data; the others were
    # typed by an administrator or reported by the node, and are left alone.
    auto_fields: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    # Set by an administrator after checking the node's declared location.
    location_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # official | community. Community nodes are run by volunteers: they are left out of a measurement
    # unless it asks for them, never feed a location estimate until their place is verified, and are
    # rate limited (see `nodes/audit.py` and the README's "Community nodes").
    tier: Mapped[str] = mapped_column(String(16), default="official", server_default="official")
    # A sponsored node's provider is named where the node is shown: the interface makes the provider's name stand out. It
    # changes nothing else: a sponsored node is official or community as it was, and is measured and trusted the same.
    sponsored: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # What the last location audit found: {status, summary, checked_at, address, evidence: [...]}.
    location_audit: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    protocol_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_addr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    @property
    def display_name(self) -> str:
        """The name it is shown by: its own, or one made of its place and its network (see `nodes/naming.py`)."""
        return node_label(
            self.name,
            city=self.city,
            region=self.region,
            country=self.country,
            provider=self.provider,
            asn=self.asn,
            node_id=self.id,
        )


class Measurement(Base):
    __tablename__ = "measurements"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_measurement_id)
    kind: Mapped[str] = mapped_column(String(16), default="ping")
    # running | completed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(16), default="running")
    # Only the host is kept; credentials, paths and query strings in a pasted URL are dropped.
    input_kind: Mapped[str] = mapped_column(String(16))
    target_host: Mapped[str] = mapped_column(String(255))
    target_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    resolved_ips: Mapped[list[str]] = mapped_column(JSON, default=list)
    dns_mode: Mapped[str] = mapped_column(String(16), default="regional")
    # The node that resolved the name for everyone (a "fixed" measurement asked to be resolved by one node), as it
    # was when the measurement began. None: the nodes resolved it themselves, or the backend did, or the target is an
    # address.
    resolved_by: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    family: Mapped[str] = mapped_column(String(8), default="auto")
    options: Mapped[dict[str, Any]] = mapped_column(JSON)
    owner_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    # The scheduled check (watch) that started this measurement, if one did.
    watch_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # The API key that started it, if one did (it can then read and cancel it).
    owner_api_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        Index("ix_measurements_owner_status", "owner_session_id", "status"),
        Index("ix_measurements_status_deadline", "status", "deadline_at"),
    )


class MeasurementTask(Base):
    """One node's share of a measurement. Persisted before it is sent to the node."""

    __tablename__ = "measurement_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    measurement_id: Mapped[str] = mapped_column(
        ForeignKey("measurements.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str] = mapped_column(String(32))
    node_info: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempt_id: Mapped[str] = mapped_column(String(32), default=new_id)
    # pending | dispatched | running | completed | failed | timed_out | node_disconnected |
    # interrupted | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    tested_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    resolved_ips: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # The backend instance that sent the task to the node (and holds that node's connection). If
    # that instance stops answering, its unfinished tasks are interrupted.
    owner_instance: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # How many times this task has been handed to a node. It goes up only when the backend instance
    # that held the task died before the node had produced anything, so starting over loses nothing.
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # When the task last (re)started waiting for its node; None means "since the measurement began".
    queued_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_tasks_node_status", "node_id", "status"),)


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("measurement_tasks.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    # ok | timeout | error
    status: Mapped[str] = mapped_column(String(10))
    rtt_us: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # What only some kinds have: an HTTP response's timings and certificate, a traceroute hop.
    # Already validated against protocol.HttpDetail / HopDetail before it is stored.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_samples_task_seq"),)


class MeasurementEvent(Base):
    """Ordered log behind the live stream. `seq` doubles as the SSE event id."""

    __tablename__ = "measurement_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    measurement_id: Mapped[str] = mapped_column(ForeignKey("measurements.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("measurement_id", "seq", name="uq_events_measurement_seq"),)


class LocationEstimate(Base):
    """The stored estimate for a finished measurement (see geolocation/)."""

    __tablename__ = "location_estimates"

    measurement_id: Mapped[str] = mapped_column(
        ForeignKey("measurements.id", ondelete="CASCADE"), primary_key=True
    )
    model_version: Mapped[str] = mapped_column(String(40))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BackendInstance(Base):
    """One running backend process. It proves it is alive by refreshing `heartbeat_at`."""

    __tablename__ = "backend_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class NodeConnection(Base):
    """Which backend instance currently holds each node's WebSocket (shared view of "online")."""

    __tablename__ = "node_connections"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(32), index=True)
    connected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    protocol_version: Mapped[int] = mapped_column(Integer)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RateEvent(Base):
    """One counted hit for a rate limit, in the database so every instance sees the same count."""

    __tablename__ = "rate_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_rate_events_key_at", "key", "at"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)


class Watch(Base):
    """A measurement that runs on a schedule, and tells someone when the target stops answering."""

    __tablename__ = "watches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))
    # As entered; parsed again at every run, so a change to the blocklist applies to it too.
    target: Mapped[str] = mapped_column(String(2048))
    # What the request carried besides the target: port, count, record_type, resolver, dns_mode, ...
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    node_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    interval_s: Mapped[int] = mapped_column(Integer)
    # The check is "bad" when fewer than this share of the nodes that took part got a good answer.
    min_ok_pct: Mapped[int] = mapped_column(Integer, default=80)
    # Bad runs in a row before an alert is sent.
    alert_after: Mapped[int] = mapped_column(Integer, default=2)
    alert_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    alert_format: Mapped[str] = mapped_column(String(8), default="json")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # ok | degraded | down | error (the check itself could not run)
    last_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    bad_streak: Mapped[int] = mapped_column(Integer, default=0)
    # An alert was sent and the target has not recovered since.
    alerting: Mapped[bool] = mapped_column(Boolean, default=False)
    last_alert_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_alert_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class WatchRun(Base):
    """One run of a watch, kept in short form for the history strip; the measurement is separate."""

    __tablename__ = "watch_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[str] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    # Not a foreign key: measurements are deleted by retention, runs by their own (longer) one.
    measurement_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(10))
    nodes_total: Mapped[int] = mapped_column(Integer, default=0)
    nodes_ok: Mapped[int] = mapped_column(Integer, default=0)
    # Of the nodes' own medians: the middle one, the fastest and the slowest.
    median_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class ApiKey(Base):
    """A credential for scripts: starts measurements without a browser, within its own quotas.

    Only a hash of the key is stored (it has 256 bits of entropy, so a plain SHA-256 is enough); the key
    itself is shown once, when it is created.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80))
    # The start of the key, so an administrator can tell keys apart; not enough to use it.
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    per_minute: Mapped[int] = mapped_column(Integer, default=30)
    per_day: Mapped[int] = mapped_column(Integer, default=1000)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
