"""Administration of API keys: scripts that start measurements without a browser."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..deps import API_KEY_PREFIX, AdminUser, Db, csrf_protect
from ..errors import ApiError
from ..models import ApiKey, Measurement, utcnow
from ..security.tokens import hash_secret, new_secret
from .router import _audit

router = APIRouter(
    prefix="/api/v1/admin/api-keys",
    tags=["api keys"],
    dependencies=[Depends(csrf_protect)],
)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    per_minute: int = Field(
        default=30, ge=1, le=600, description="Measurements it may start a minute"
    )
    per_day: int = Field(
        default=1000, ge=1, le=100_000, description="Measurements it may start a day"
    )


class ApiKeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    per_minute: int | None = Field(default=None, ge=1, le=600)
    per_day: int | None = Field(default=None, ge=1, le=100_000)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str = Field(
        description="The start of the key: enough to tell keys apart, not to use one"
    )
    per_minute: int
    per_day: int
    created_by: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    made_24h: int = Field(description="Measurements it started in the last day")


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The key. It is shown once; only a hash is kept.")


async def _out(db: Db, key: ApiKey) -> ApiKeyOut:
    made = await db.scalar(
        select(func.count())
        .select_from(Measurement)
        .where(
            Measurement.owner_api_key_id == key.id,
            Measurement.created_at > utcnow() - timedelta(days=1),
        )
    )
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        per_minute=key.per_minute,
        per_day=key.per_day,
        created_by=key.created_by,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        made_24h=made or 0,
    )


async def _get(db: Db, key_id: str) -> ApiKey:
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise ApiError(404, "not_found", "API key not found.")
    return key


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(admin: AdminUser, db: Db) -> list[ApiKeyOut]:
    keys = (await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars()
    return [await _out(db, k) for k in keys]


@router.post("", response_model=ApiKeyCreated, status_code=201)
async def create_key(
    body: ApiKeyCreate, request: Request, admin: AdminUser, db: Db
) -> ApiKeyCreated:
    secret = new_secret(API_KEY_PREFIX)
    key = ApiKey(
        name=body.name.strip(),
        prefix=secret[:12],
        key_hash=hash_secret(secret),
        per_minute=body.per_minute,
        per_day=body.per_day,
        created_by=admin.username,
    )
    db.add(key)
    await _audit(db, admin, request, "api_key.create", key.id, name=key.name, prefix=key.prefix)
    await db.commit()
    return ApiKeyCreated(**(await _out(db, key)).model_dump(), key=secret)


@router.patch("/{key_id}", response_model=ApiKeyOut)
async def change_key(
    key_id: str, body: ApiKeyPatch, request: Request, admin: AdminUser, db: Db
) -> ApiKeyOut:
    key = await _get(db, key_id)
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(key, field, value.strip() if isinstance(value, str) else value)
    await _audit(
        db, admin, request, "api_key.update", key.id, name=key.name, fields=sorted(changes)
    )
    await db.commit()
    return await _out(db, key)


@router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: str, request: Request, admin: AdminUser, db: Db) -> None:
    key = await _get(db, key_id)
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        await _audit(db, admin, request, "api_key.revoke", key.id, name=key.name, prefix=key.prefix)
        await db.commit()
