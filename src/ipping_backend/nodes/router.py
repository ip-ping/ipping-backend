"""Node enrollment, the public node list and the node WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Request, WebSocket
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update

from ..deps import Db, client_ip
from ..errors import ApiError
from ..models import AuditEvent, EnrollmentToken, Node, new_id, utcnow
from ..security.ratelimit import allow
from ..security.tokens import hash_secret, new_secret, secrets_match
from . import protocol as proto
from .autofill import fill_if_enabled
from .registry import NodeConnection

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])

HELLO_TIMEOUT_S = 10
IDLE_TIMEOUT_S = 45  # three missed 15-second heartbeats
MAX_INVALID_FRAMES = 10
LAST_SEEN_WRITE_INTERVAL_S = 60


class EnrollRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    name: str = Field(
        default="",
        max_length=100,
        description="Optional. Without one the node is called after its place and its network (`City, CC - Provider · ASn`).",
    )
    version: str = Field(default="unknown", max_length=32)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    region: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    provider: str | None = Field(default=None, max_length=100)
    asn: int | None = Field(default=None, ge=0, le=4_294_967_295)


class EnrollResponse(BaseModel):
    node_id: str
    credential: str = Field(description="Shown once. Store it with file mode 0600.")
    status: Literal["pending", "approved"]


class PublicNode(BaseModel):
    id: str
    name: str
    country: str | None
    region: str | None
    city: str | None
    latitude: float | None
    longitude: float | None
    provider: str | None
    asn: int | None
    online: bool
    ipv4: bool
    ipv6: bool
    kinds: list[str]
    tier: Literal["official", "community"] = "official"


@router.post("/enroll", response_model=EnrollResponse, status_code=201)
async def enroll(body: EnrollRequest, request: Request, db: Db) -> EnrollResponse:
    """Exchange an enrollment token for this node's own credential (a single-use token is taken by the first node)."""
    ip = client_ip(request) or "unknown"
    if not await allow(db, f"enroll:{ip}", 10, 60):
        raise ApiError(429, "rate_limited", "Too many enrollment attempts. Wait a minute.")

    token = (
        await db.execute(
            select(EnrollmentToken).where(EnrollmentToken.token_hash == hash_secret(body.token))
        )
    ).scalar_one_or_none()
    now = utcnow()
    if (
        token is None
        or token.revoked_at
        or token.expires_at <= now
        or (token.used_at and not token.reusable)
    ):
        raise ApiError(403, "invalid_token", "The enrollment token is invalid, used or expired.")

    node_id = new_id()
    if token.reusable:
        # Any number of nodes may enroll with it: count them, keep the latest (each waits for approval all the same).
        await db.execute(
            update(EnrollmentToken)
            .where(EnrollmentToken.id == token.id)
            .values(used_at=now, node_id=node_id, uses=EnrollmentToken.uses + 1)
        )
    else:
        # Claim the token atomically so two simultaneous enrollments cannot both win.
        claimed = await db.execute(
            update(EnrollmentToken)
            .where(EnrollmentToken.id == token.id, EnrollmentToken.used_at.is_(None))
            .values(used_at=now, node_id=node_id, uses=1)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            raise ApiError(
                403, "invalid_token", "The enrollment token is invalid, used or expired."
            )

    secret = new_secret()
    node = Node(
        id=node_id,
        name=body.name.strip(),
        status="pending",
        credential_hash=hash_secret(secret),
        country=body.country.upper() if body.country else None,
        region=body.region,
        city=body.city,
        latitude=body.latitude,
        longitude=body.longitude,
        provider=body.provider,
        asn=body.asn,
        version=body.version,
        remote_addr=ip,
        tier=token.tier,
    )
    db.add(node)
    # what the node did not say about itself is filled in from the address it came from (see nodes/autofill.py)
    await fill_if_enabled(db, node, request.app.state.geoip)
    db.add(
        AuditEvent(
            actor=f"node:{node_id}",
            action="node.enrolled",
            subject=node_id,
            data={"name": body.name.strip(), "token_label": token.label},
            source_ip=ip,
        )
    )
    await db.commit()
    return EnrollResponse(node_id=node_id, credential=f"{node_id}.{secret}", status="pending")


@router.get("", response_model=list[PublicNode])
async def list_nodes(request: Request, db: Db) -> list[PublicNode]:
    """Approved nodes only, with nothing that identifies the host behind them."""
    online = await request.app.state.cluster.online_nodes(db)
    nodes = (
        await db.execute(
            select(Node)
            .where(Node.status == "approved")
            .order_by(Node.country, Node.city, Node.name)
        )
    ).scalars()
    result = []
    for node in nodes:
        connection = online.get(node.id)
        caps = (
            connection.capabilities
            if connection
            else proto.Capabilities.model_validate(
                {
                    k: v
                    for k, v in (node.capabilities or {}).items()
                    if k in proto.Capabilities.model_fields
                }
            )
        )
        result.append(
            PublicNode(
                id=node.id,
                name=node.display_name,
                country=node.country,
                region=node.region,
                city=node.city,
                latitude=node.latitude,
                longitude=node.longitude,
                provider=node.provider,
                asn=node.asn,
                online=connection is not None,
                ipv4=caps.ipv4,
                ipv6=caps.ipv6,
                kinds=caps.kinds,
                tier=node.tier,
            )
        )
    return result


async def _authenticate(websocket: WebSocket) -> Node | None:
    """Credentials travel in the Authorization header, never in the URL."""
    header = websocket.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    node_id, _, secret = value.strip().partition(".")
    async with websocket.app.state.sessionmaker() as db:
        node: Node | None = (
            await db.get(Node, node_id) if scheme.lower() == "bearer" and node_id else None
        )
        expected = node.credential_hash if node else hash_secret("no-such-node")
        matches = secrets_match(hash_secret(secret), expected)
        if node is None or not matches or node.status == "revoked":
            return None
        return node


@router.websocket("/connect")
async def connect(websocket: WebSocket) -> None:
    state = websocket.app.state
    node = await _authenticate(websocket)
    if node is None:
        await websocket.close(code=4401)  # before accept: the client sees an HTTP 403
        return
    await websocket.accept()

    try:
        hello_raw = await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT_S)
        hello = proto.node_to_backend.validate_python(json.loads(hello_raw))
        if not isinstance(hello, proto.Hello):
            raise ValueError("first frame must be hello")
    except (TimeoutError, ValueError, ValidationError):
        await websocket.close(code=4400)
        return
    except Exception:  # noqa: BLE001 - disconnected before saying hello
        return

    versions = set(hello.protocol_versions) & set(proto.SUPPORTED_PROTOCOL_VERSIONS)
    if not versions:
        error = proto.ErrorFrame(
            code="unsupported_protocol",
            message=f"Backend supports protocol versions {list(proto.SUPPORTED_PROTOCOL_VERSIONS)}.",
        )
        await websocket.send_text(error.model_dump_json())
        await websocket.close(code=4426)
        return

    connection = NodeConnection(
        node_id=node.id,
        websocket=websocket,
        protocol_version=max(versions),
        capabilities=hello.capabilities,
    )
    registry, engine, cluster = state.registry, state.engine, state.cluster

    previous = registry.get(node.id)
    if previous is not None:  # a reconnect that overtook the old socket's cleanup
        registry.unregister(previous)
        await previous.close(4409, "replaced by a newer connection")
        await engine.on_node_disconnected(node.id)
    registry.register(connection)
    await cluster.node_connected(node.id, hello.capabilities, connection.protocol_version)

    async with state.sessionmaker() as db:
        row = await db.get(Node, node.id)
        if row is not None:
            row.version = hello.node_version
            row.protocol_version = connection.protocol_version
            row.capabilities = hello.capabilities.model_dump()
            row.last_seen_at = utcnow()
            row.remote_addr = websocket.client.host if websocket.client else None
            await fill_if_enabled(db, row, state.geoip)
            node_status = row.status
            await db.commit()
        else:
            node_status = "pending"

    log.info("node connected", extra={"node": node.id, "node_version": hello.node_version})
    try:
        await connection.send(
            proto.Welcome(
                protocol_version=connection.protocol_version,
                node_id=node.id,
                node_status="approved" if node_status == "approved" else "pending",
                server_time=utcnow(),
            )
        )
        await _serve(connection, engine, state)
    except Exception as exc:  # noqa: BLE001 - disconnects surface as many exception types
        log.info("node connection ended", extra={"node": node.id, "reason": type(exc).__name__})
    finally:
        if registry.unregister(connection):
            engine.schedule_node_disconnected(node.id)
        log.info("node disconnected", extra={"node": node.id})


async def _serve(connection: NodeConnection, engine, state) -> None:  # type: ignore[no-untyped-def]
    invalid = 0
    last_write = asyncio.get_running_loop().time()
    while True:
        try:
            raw = await asyncio.wait_for(connection.websocket.receive_text(), IDLE_TIMEOUT_S)
        except TimeoutError:
            await connection.close(4408, "heartbeat timeout")
            return
        if len(raw) > proto.MAX_FRAME_BYTES:
            await connection.close(1009, "frame too large")
            return
        try:
            frame = proto.node_to_backend.validate_python(json.loads(raw))
        except (ValueError, ValidationError):
            invalid += 1
            if invalid >= MAX_INVALID_FRAMES:
                await connection.close(4400, "too many invalid frames")
                return
            await connection.send(
                proto.ErrorFrame(code="invalid_frame", message="Could not parse frame.")
            )
            continue

        if isinstance(frame, proto.Ping):
            await connection.send(proto.Pong())
            now = asyncio.get_running_loop().time()
            if now - last_write >= LAST_SEEN_WRITE_INTERVAL_S:
                last_write = now
                async with state.sessionmaker() as db:
                    await db.execute(
                        update(Node)
                        .where(Node.id == connection.node_id)
                        .values(last_seen_at=utcnow())
                    )
                    await db.commit()
        elif isinstance(frame, proto.Hello):
            await connection.send(
                proto.ErrorFrame(code="unexpected_frame", message="Already said hello.")
            )
        else:
            await engine.handle_frame(connection.node_id, frame)
