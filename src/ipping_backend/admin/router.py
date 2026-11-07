"""Administration: enrollment tokens, node approval, runtime settings, audit trail."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..cluster import OnlineNode
from ..deps import AdminUser, Db, Engine, client_ip, csrf_protect
from ..errors import ApiError
from ..measurements.schemas import TASK_ACTIVE
from ..models import (
    AuditEvent,
    EnrollmentToken,
    Measurement,
    MeasurementTask,
    Node,
    new_id,
    utcnow,
)
from ..runtime_settings import (
    RuntimeSettings,
    RuntimeSettingsUpdate,
    load_settings,
    save_settings,
)
from ..security.tokens import hash_secret, new_secret

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(csrf_protect)],
)


async def _audit(
    db: Db,
    admin: AdminUser,
    request: Request,
    action: str,
    subject: str | None = None,
    **data: object,
) -> None:
    db.add(
        AuditEvent(
            actor=admin.username,
            action=action,
            subject=subject,
            data=dict(data),
            source_ip=client_ip(request),
        )
    )


# ------------------------------------------------------------------------------------------
# Enrollment tokens
# ------------------------------------------------------------------------------------------


class CreateTokenRequest(BaseModel):
    label: str | None = Field(default=None, max_length=100)
    ttl_minutes: int = Field(default=60, ge=1, le=24 * 60)


class TokenCreated(BaseModel):
    id: str
    token: str = Field(description="Shown once")
    expires_at: datetime


class TokenOut(BaseModel):
    id: str
    label: str | None
    created_by: str
    created_at: datetime
    expires_at: datetime
    state: Literal["active", "used", "expired", "revoked"]
    node_id: str | None


def _token_state(token: EnrollmentToken) -> str:
    if token.revoked_at:
        return "revoked"
    if token.used_at:
        return "used"
    return "expired" if token.expires_at <= utcnow() else "active"


@router.post("/enrollment-tokens", response_model=TokenCreated, status_code=201)
async def create_enrollment_token(
    body: CreateTokenRequest, request: Request, admin: AdminUser, db: Db
) -> TokenCreated:
    plaintext = new_secret("ipt_")
    row = EnrollmentToken(
        id=new_id(),
        token_hash=hash_secret(plaintext),
        label=body.label,
        created_by=admin.username,
        expires_at=utcnow() + timedelta(minutes=body.ttl_minutes),
    )
    db.add(row)
    await _audit(db, admin, request, "enrollment_token.created", row.id, label=body.label)
    await db.commit()
    return TokenCreated(id=row.id, token=plaintext, expires_at=row.expires_at)


@router.get("/enrollment-tokens", response_model=list[TokenOut])
async def list_enrollment_tokens(admin: AdminUser, db: Db) -> list[TokenOut]:
    rows = (
        await db.execute(
            select(EnrollmentToken).order_by(EnrollmentToken.created_at.desc()).limit(100)
        )
    ).scalars()
    return [
        TokenOut(
            id=r.id,
            label=r.label,
            created_by=r.created_by,
            created_at=r.created_at,
            expires_at=r.expires_at,
            state=_token_state(r),
            node_id=r.node_id,
        )
        for r in rows
    ]


@router.delete("/enrollment-tokens/{token_id}", status_code=204)
async def revoke_enrollment_token(
    token_id: str, request: Request, admin: AdminUser, db: Db
) -> None:
    row = await db.get(EnrollmentToken, token_id)
    if row is None:
        raise ApiError(404, "not_found", "Token not found.")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        await _audit(db, admin, request, "enrollment_token.revoked", token_id)
        await db.commit()


# ------------------------------------------------------------------------------------------
# Nodes
# ------------------------------------------------------------------------------------------


class AdminNode(BaseModel):
    id: str
    name: str
    status: Literal["pending", "approved", "revoked"]
    online: bool
    country: str | None
    region: str | None
    city: str | None
    latitude: float | None
    longitude: float | None
    provider: str | None
    asn: int | None
    location_verified: bool
    version: str | None
    protocol_version: int | None
    remote_addr: str | None
    ipv4: bool | None
    ipv6: bool | None
    kinds: list[str] | None
    created_at: datetime
    last_seen_at: datetime | None


class UpdateNodeRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    region: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    provider: str | None = Field(default=None, max_length=100)
    asn: int | None = Field(default=None, ge=0, le=4_294_967_295)
    location_verified: bool | None = None


class RotatedCredential(BaseModel):
    node_id: str
    credential: str = Field(description="Shown once. The node must be reconfigured with it.")


async def _online(request: Request, db: Db) -> dict[str, OnlineNode]:
    return await request.app.state.cluster.online_nodes(db)  # type: ignore[no-any-return]


def _admin_node(node: Node, online: dict[str, OnlineNode]) -> AdminNode:
    connection = online.get(node.id)
    caps = connection.capabilities if connection else None
    return AdminNode(
        id=node.id,
        name=node.name,
        status=node.status,
        online=connection is not None,
        country=node.country,
        region=node.region,
        city=node.city,
        latitude=node.latitude,
        longitude=node.longitude,
        provider=node.provider,
        asn=node.asn,
        location_verified=node.location_verified,
        version=node.version,
        protocol_version=node.protocol_version,
        remote_addr=node.remote_addr,
        ipv4=caps.ipv4 if caps else None,
        ipv6=caps.ipv6 if caps else None,
        kinds=caps.kinds if caps else None,
        created_at=node.created_at,
        last_seen_at=node.last_seen_at,
    )


async def _get_node(db: Db, node_id: str) -> Node:
    node = await db.get(Node, node_id)
    if node is None:
        raise ApiError(404, "not_found", "Node not found.")
    return node


@router.get("/nodes", response_model=list[AdminNode])
async def admin_list_nodes(request: Request, admin: AdminUser, db: Db) -> list[AdminNode]:
    nodes = (await db.execute(select(Node).order_by(Node.created_at))).scalars()
    online = await _online(request, db)
    return [_admin_node(n, online) for n in nodes]


@router.patch("/nodes/{node_id}", response_model=AdminNode)
async def update_node(
    node_id: str, body: UpdateNodeRequest, request: Request, admin: AdminUser, db: Db
) -> AdminNode:
    node = await _get_node(db, node_id)
    changes = body.model_dump(exclude_unset=True)
    if "country" in changes and changes["country"]:
        changes["country"] = changes["country"].upper()
    for key, value in changes.items():
        setattr(node, key, value)
    await _audit(db, admin, request, "node.updated", node_id, fields=sorted(changes))
    await db.commit()
    return _admin_node(node, await _online(request, db))


@router.post("/nodes/{node_id}/approve", response_model=AdminNode)
async def approve_node(node_id: str, request: Request, admin: AdminUser, db: Db) -> AdminNode:
    node = await _get_node(db, node_id)
    if node.status == "revoked":
        raise ApiError(409, "node_revoked", "A revoked node must enroll again.")
    node.status = "approved"
    node.approved_at = utcnow()
    await _audit(db, admin, request, "node.approved", node_id)
    await db.commit()
    return _admin_node(node, await _online(request, db))


@router.post("/nodes/{node_id}/revoke", response_model=AdminNode)
async def revoke_node(
    node_id: str, request: Request, admin: AdminUser, db: Db, engine: Engine
) -> AdminNode:
    node = await _get_node(db, node_id)
    node.status = "revoked"
    await _audit(db, admin, request, "node.revoked", node_id)
    await db.commit()
    # The node's socket may be held by any backend instance; tell them all.
    await request.app.state.cluster.command(
        {"type": "close", "node_id": node_id, "code": 4403, "reason": "revoked"}
    )
    return _admin_node(node, await _online(request, db))


@router.post("/nodes/{node_id}/rotate-credential", response_model=RotatedCredential)
async def rotate_credential(
    node_id: str, request: Request, admin: AdminUser, db: Db
) -> RotatedCredential:
    node = await _get_node(db, node_id)
    if node.status == "revoked":
        raise ApiError(409, "node_revoked", "A revoked node must enroll again.")
    secret = new_secret()
    node.credential_hash = hash_secret(secret)
    await _audit(db, admin, request, "node.credential_rotated", node_id)
    await db.commit()
    await request.app.state.cluster.command(
        {"type": "close", "node_id": node_id, "code": 4403, "reason": "credential rotated"}
    )
    return RotatedCredential(node_id=node_id, credential=f"{node_id}.{secret}")


# ------------------------------------------------------------------------------------------
# Settings, status, audit
# ------------------------------------------------------------------------------------------


@router.get("/settings", response_model=RuntimeSettings)
async def get_settings(admin: AdminUser, db: Db) -> RuntimeSettings:
    return await load_settings(db)


@router.patch("/settings", response_model=RuntimeSettings)
async def patch_settings(
    body: RuntimeSettingsUpdate, request: Request, admin: AdminUser, db: Db
) -> RuntimeSettings:
    updated = await save_settings(db, body)
    await _audit(
        db, admin, request, "settings.updated", fields=sorted(body.model_dump(exclude_none=True))
    )
    await db.commit()
    return updated


class GeoIpStatus(BaseModel):
    city: bool
    asn: bool
    source: str | None = None


class ServiceStatus(BaseModel):
    geoip: GeoIpStatus
    nodes_online: int
    nodes_approved: int
    nodes_pending: int
    measurements_running: int
    tasks_active: int
    live_streams: int


@router.get("/status", response_model=ServiceStatus)
async def service_status(request: Request, admin: AdminUser, db: Db) -> ServiceStatus:
    status_rows = (await db.execute(select(Node.status, func.count()).group_by(Node.status))).all()
    by_status: dict[str, int] = {row[0]: row[1] for row in status_rows}
    running = await db.scalar(
        select(func.count()).select_from(Measurement).where(Measurement.status == "running")
    )
    active = await db.scalar(
        select(func.count())
        .select_from(MeasurementTask)
        .where(MeasurementTask.status.in_(TASK_ACTIVE))
    )
    geoip = request.app.state.geoip
    return ServiceStatus(
        geoip=GeoIpStatus(
            city=geoip.has_city,
            asn=geoip.has_asn,
            source=geoip.source if geoip.has_city or geoip.has_asn else None,
        ),
        nodes_online=len(await _online(request, db)),
        nodes_approved=by_status.get("approved", 0),
        nodes_pending=by_status.get("pending", 0),
        measurements_running=running or 0,
        tasks_active=active or 0,
        live_streams=request.app.state.cluster.hub.count("events:"),
    )


class AuditOut(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    subject: str | None
    data: dict[str, object]
    source_ip: str | None


@router.get("/audit", response_model=list[AuditOut])
async def audit_log(
    admin: AdminUser, db: Db, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[AuditOut]:
    rows = (
        await db.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit))
    ).scalars()
    return [
        AuditOut(
            id=r.id,
            at=r.at,
            actor=r.actor,
            action=r.action,
            subject=r.subject,
            data=r.data,
            source_ip=r.source_ip,
        )
        for r in rows
    ]
