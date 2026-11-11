"""The admin endpoints for IP data: every source (the same for each), the lookup API's settings, node details."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..deps import AdminUser, Db, client_ip, csrf_protect
from ..errors import ApiError
from ..models import AuditEvent
from ..nodes.autofill import refresh_all
from . import downloader, runtime
from .catalog import BY_ID, CATALOG, fetchable
from .options import IpDataUpdate, environment_account, load_keys, load_options, save
from .schemas import (
    CredentialOut,
    IpDataOut,
    LookupSettingsOut,
    SourceAdminOut,
    SourceFileOut,
)

router = APIRouter(
    prefix="/api/v1/admin/ip-data", tags=["admin"], dependencies=[Depends(csrf_protect)]
)


def _when(value: Any) -> datetime | None:
    return downloader.parse_time(value)


async def _sources(request: Request, db: Db) -> list[SourceAdminOut]:
    state = request.app.state
    service = state.ipdata
    options = await load_options(db)
    keys = await load_keys(db, state.settings)
    from_environment = environment_account(state.settings)
    status = state.dataupdater.status()["sources"]
    out = []
    for info in CATALOG:
        status_name, detail = service.state(info.id)
        have = keys.get(info.id, {})
        credentials = [
            CredentialOut(
                name=field.name,
                label=field.label,
                secret=field.secret,
                required=field.required,
                set=bool(have.get(field.name)),
                value=have.get(field.name) if not field.secret else None,
                from_environment=from_environment and info.id == "maxmind",
            )
            for field in info.credentials
        ]
        entry = status.get(info.id, {})
        enabled = options.enabled(info.id)
        waiting_for_a_check = (
            enabled
            and not service.offline
            and service.directory is not None
            and fetchable(info.id, keys)
        )
        out.append(
            SourceAdminOut(
                id=info.id,
                name=info.name,
                homepage=info.homepage,
                summary=info.summary,
                cost=info.cost,
                attribution=info.attribution,
                enabled=enabled,
                state=status_name,
                detail=detail,
                credentials=credentials,
                files=[SourceFileOut(**f) for f in service.local.files(info.id)],
                running=bool(entry.get("running")),
                last_attempt=_when(entry.get("last_attempt")),
                last_success=_when(entry.get("last_success")),
                next_check=downloader.next_check(info.id, options, entry)
                if waiting_for_a_check
                else None,
                error=(entry.get("error") or {}).get("message"),
            )
        )
    return out


async def _build(request: Request, db: Db) -> IpDataOut:
    state = request.app.state
    options = await load_options(db)
    return IpDataOut(
        lookup=LookupSettingsOut(
            enabled=options.lookup_enabled,
            access=options.lookup_access,
            per_minute=options.lookup_per_minute,
        ),
        node_autofill=options.node_autofill,
        directory_configured=state.ipdata.directory is not None,
        databases_loaded=state.ipdata.available(),
        sources=await _sources(request, db),
        offline=state.ipdata.offline,
        data_update_hours=options.data_update_hours,
    )


@router.get("", response_model=IpDataOut)
async def get_ip_data(request: Request, admin: AdminUser, db: Db) -> IpDataOut:
    """Every IP data source with its switch, what it needs and how it is doing, and the settings of the lookup API. Keys are never sent."""
    return await _build(request, db)


@router.put("", response_model=IpDataOut)
async def put_ip_data(body: IpDataUpdate, request: Request, admin: AdminUser, db: Db) -> IpDataOut:
    """Change any of the settings. Only what is sent changes. A key or token that is sent replaces the saved one."""
    state = request.app.state
    before = await load_options(db)
    keys_before = await load_keys(db, state.settings)
    await save(db, body)
    credentials_changed = any(change.credentials for change in (body.sources or {}).values())
    db.add(
        AuditEvent(
            actor=admin.username,
            action="ip_data.updated",
            subject=None,
            data={
                "fields": sorted(body.model_fields_set),
                "credentials": "changed" if credentials_changed else "unchanged",
            },
            source_ip=client_ip(request),
        )
    )
    await db.commit()
    after = await load_options(db)
    keys_after = await load_keys(db, state.settings)
    await runtime.reload(state)  # the sources use the new switches and keys at once
    turned_on = [s for s in BY_ID if after.enabled(s) and not before.enabled(s)]
    given = [s for s in BY_ID if keys_after.get(s) != keys_before.get(s)]
    if turned_on or given:
        state.dataupdater.start()  # a source that was just turned on, or given its key, is not waited for
    if after.node_autofill and not before.node_autofill:
        await refresh_all(
            state.sessionmaker, state.ipdata
        )  # it has just been turned on: fill what is empty now
    return await _build(request, db)


@router.post("/update", response_model=IpDataOut, status_code=202)
async def update_now(request: Request, admin: AdminUser, db: Db) -> IpDataOut:
    """Check every source that is on for a newer file now, and download what is newer (it runs in the background: ask again to see how it went)."""
    state = request.app.state
    updater = state.dataupdater
    if updater.directory is None:
        raise ApiError(
            409,
            "no_directory",
            "The backend has no directory to keep the databases in (IPPING_GEOIP_DIR).",
        )
    if updater.offline:
        raise ApiError(
            409,
            "offline",
            "This server may not call out (IPPING_IPDATA_OFFLINE), so nothing is downloaded.",
        )
    options = await load_options(db)
    keys = await load_keys(db, state.settings)
    if not updater.wanted(options, keys):
        raise ApiError(
            409,
            "nothing_to_update",
            "No source that is on can be downloaded yet: turn one on, and give each source that needs a key its key.",
        )
    updater.start()
    db.add(
        AuditEvent(
            actor=admin.username,
            action="ip_data.update_requested",
            data={},
            source_ip=client_ip(request),
        )
    )
    await db.commit()
    return await _build(request, db)
