"""The admin endpoints for IP data: the MaxMind account, the databases, the lookup API's settings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..deps import AdminUser, Db, client_ip, csrf_protect
from ..errors import ApiError
from ..models import AuditEvent
from ..nodes.autofill import refresh_all
from .options import (
    EDITIONS,
    IpDataUpdate,
    load_credentials,
    load_options,
    save,
)
from .schemas import DatabaseOut, IpDataOut, LookupSettingsOut, MaxMindOut, StatusErrorOut
from .updater import MaxMindUpdater, _parse, due

router = APIRouter(
    prefix="/api/v1/admin/ip-data", tags=["admin"], dependencies=[Depends(csrf_protect)]
)


def _when(value: Any) -> datetime | None:
    return _parse(value)


async def _build(request: Request, db: Db) -> IpDataOut:
    state = request.app.state
    updater: MaxMindUpdater = state.maxmind
    options = await load_options(db)
    credentials = await load_credentials(db, state.settings)
    status = updater.status()
    loaded = {d["file"]: d for d in state.geoip.databases()}
    databases = []
    for edition in EDITIONS:
        info = loaded.get(f"{edition}.mmdb")
        entry = status["editions"].get(edition, {})
        built = info.get("build_epoch") if info else None
        databases.append(
            DatabaseOut(
                edition=edition,
                present=info is not None,
                file=info["file"] if info else None,
                type=info["type"] if info else None,
                built=datetime.fromtimestamp(built, UTC) if built else None,
                size=info["size"] if info else None,
                downloaded_at=_when(entry.get("downloaded_at")),
                checked_at=_when(entry.get("checked_at")),
                error=entry.get("error"),
            )
        )
    last_attempt = _when(status["last_attempt"])
    next_check = None
    if options.maxmind_auto_update and credentials.usable and updater.directory is not None:
        next_check = datetime.now(UTC) if due(options, status) else None
        if next_check is None and last_attempt is not None:
            error = status["error"]
            wait = (
                timedelta(hours=6 if error.get("code") in ("refused", "rate_limited") else 1)
                if error
                else timedelta(hours=options.maxmind_update_hours)
            )
            next_check = last_attempt + wait
    error = status["error"]
    return IpDataOut(
        maxmind=MaxMindOut(
            account_id=credentials.account_id,
            license_key_set=bool(credentials.license_key),
            credentials_source=credentials.source,
            editions=options.maxmind_editions,
            auto_update=options.maxmind_auto_update,
            update_hours=options.maxmind_update_hours,
            directory_configured=updater.directory is not None,
            running=bool(status["running"]),
            last_attempt=last_attempt,
            last_success=_when(status["last_success"]),
            next_check=next_check,
            error=StatusErrorOut(code=error["code"], message=error["message"]) if error else None,
            databases=databases,
        ),
        lookup=LookupSettingsOut(
            enabled=options.lookup_enabled,
            access=options.lookup_access,
            per_minute=options.lookup_per_minute,
        ),
        node_autofill=options.node_autofill,
        databases_loaded=bool(loaded),
        source=state.geoip.source if loaded else None,
        attribution=state.geoip.attribution,
    )


@router.get("", response_model=IpDataOut)
async def get_ip_data(request: Request, admin: AdminUser, db: Db) -> IpDataOut:
    """The MaxMind account (never the key), the databases in use, and the settings of the lookup API."""
    return await _build(request, db)


@router.put("", response_model=IpDataOut)
async def put_ip_data(body: IpDataUpdate, request: Request, admin: AdminUser, db: Db) -> IpDataOut:
    """Change any of the settings. Only what is sent changes. A license key that is sent replaces the saved one."""
    before = await load_options(db)
    credentials_changed = bool(
        body.license_key or body.account_id is not None or body.clear_credentials
    )
    await save(db, body)
    sent = sorted(k for k in body.model_fields_set if k not in ("license_key", "account_id"))
    db.add(
        AuditEvent(
            actor=admin.username,
            action="ip_data.updated",
            subject=None,
            data={"fields": sent, "credentials": "changed" if credentials_changed else "unchanged"},
            source_ip=client_ip(request),
        )
    )
    await db.commit()
    state = request.app.state
    after = await load_options(db)
    if after.node_autofill and not before.node_autofill:
        await refresh_all(
            state.sessionmaker, state.geoip
        )  # it has just been turned on: fill what is empty now
    credentials = await load_credentials(db, state.settings)
    if credentials_changed and credentials.usable and after.maxmind_auto_update:
        state.maxmind.start(force=False)  # a key was just given: do not wait for the next check
    return await _build(request, db)


@router.post("/update", response_model=IpDataOut, status_code=202)
async def update_now(request: Request, admin: AdminUser, db: Db) -> IpDataOut:
    """Download the databases now (it runs in the background: ask again to see how it went)."""
    state = request.app.state
    if state.maxmind.directory is None:
        raise ApiError(
            409,
            "no_directory",
            "The backend has no directory to keep the databases in (IPPING_GEOIP_DIR).",
        )
    if not (await load_credentials(db, state.settings)).usable:
        raise ApiError(409, "no_credentials", "Enter the MaxMind account ID and license key first.")
    state.maxmind.start(force=True)
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
