"""Administration of watches: create, change, pause, run now, test the alert, read the history."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from ..admin.router import _audit
from ..deps import AdminUser, Db, csrf_protect
from ..errors import ApiError
from ..measurements.plan import PlanError, plan_measurement
from ..models import Watch, WatchRun, utcnow
from ..runtime_settings import RuntimeSettings, load_settings
from ..security.targets import TargetError, parse_target
from .notify import AlertAddressError, check_address
from .schemas import (
    RunNowOut,
    TestAlertOut,
    WatchIn,
    WatchOut,
    WatchPatch,
    WatchRunOut,
)
from .service import REQUEST_OPTIONS, WatchScheduler, request_for

router = APIRouter(
    prefix="/api/v1/admin/watches",
    tags=["watches"],
    dependencies=[Depends(csrf_protect)],
)

HISTORY_SHOWN = 48


def _scheduler(request: Request) -> WatchScheduler:
    scheduler: WatchScheduler = request.app.state.watches
    return scheduler


def _options(body: WatchIn | WatchPatch, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """The request settings a watch carries, from what was sent (and what it already had)."""
    options = dict(current or {})
    for key in REQUEST_OPTIONS:
        value = getattr(body, key, None)
        if value is not None:
            options[key] = value
    return options


async def _check_plan(watch: Watch, settings: RuntimeSettings) -> None:
    """A watch has to describe a measurement that could be made, so mistakes show up when it is saved
    and not an hour later as an error run."""
    try:
        request = request_for(watch)
        plan_measurement(request, parse_target(watch.target), settings)
    except TargetError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    except PlanError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    except ValueError as exc:
        raise ApiError(422, "invalid_option", str(exc)) from exc


async def _check_alert_url(url: str | None, settings: RuntimeSettings, request: Request) -> None:
    if not url:
        return
    try:
        await check_address(
            url,
            allow_private=settings.allow_private_webhooks,
            resolver=_scheduler(request)._resolver,  # noqa: SLF001 - the same resolver it will use
        )
    except AlertAddressError as exc:
        raise ApiError(422, "invalid_alert_address", str(exc)) from exc


async def _out(db: Db, watch: Watch) -> WatchOut:
    runs = list(
        (
            await db.execute(
                select(WatchRun)
                .where(WatchRun.watch_id == watch.id)
                .order_by(WatchRun.at.desc())
                .limit(HISTORY_SHOWN)
            )
        ).scalars()
    )
    return WatchOut(
        id=watch.id,
        name=watch.name,
        kind=watch.kind,
        target=watch.target,
        options=dict(watch.options or {}),
        node_ids=watch.node_ids,
        interval_s=watch.interval_s,
        min_ok_pct=watch.min_ok_pct,
        alert_after=watch.alert_after,
        alert_url=watch.alert_url,
        alert_format=watch.alert_format,
        enabled=watch.enabled,
        created_by=watch.created_by,
        created_at=watch.created_at,
        next_run_at=watch.next_run_at,
        last_run_at=watch.last_run_at,
        last_status=watch.last_status,
        bad_streak=watch.bad_streak,
        alerting=watch.alerting,
        last_alert_at=watch.last_alert_at,
        last_alert_error=watch.last_alert_error,
        history=[WatchRunOut.model_validate(r, from_attributes=True) for r in reversed(runs)],
    )


async def _get(db: Db, watch_id: str) -> Watch:
    watch = await db.get(Watch, watch_id)
    if watch is None:
        raise ApiError(404, "not_found", "Watch not found.")
    return watch


@router.get("", response_model=list[WatchOut])
async def list_watches(admin: AdminUser, db: Db) -> list[WatchOut]:
    watches = (await db.execute(select(Watch).order_by(Watch.created_at))).scalars()
    return [await _out(db, w) for w in watches]


@router.post("", response_model=WatchOut, status_code=201)
async def create_watch(body: WatchIn, request: Request, admin: AdminUser, db: Db) -> WatchOut:
    settings = await load_settings(db)
    if body.kind not in settings.enabled_kinds:
        raise ApiError(422, "kind_disabled", f"{body.kind} measurements are turned off.")
    if body.interval_s < settings.min_watch_interval_s:
        raise ApiError(
            422,
            "interval_too_short",
            f"Watches run at most every {settings.min_watch_interval_s} seconds.",
        )
    count = await db.scalar(select(func.count()).select_from(Watch))
    if (count or 0) >= settings.max_watches:
        raise ApiError(409, "watch_limit", f"At most {settings.max_watches} watches.")
    watch = Watch(
        name=body.name.strip(),
        kind=body.kind,
        target=body.target.strip(),
        options=_options(body),
        node_ids=body.node_ids,
        interval_s=body.interval_s,
        min_ok_pct=body.min_ok_pct,
        alert_after=body.alert_after,
        alert_url=(body.alert_url or "").strip() or None,
        alert_format=body.alert_format,
        enabled=body.enabled,
        created_by=admin.username,
        next_run_at=utcnow() if body.enabled else None,
    )
    await _check_plan(watch, settings)
    await _check_alert_url(watch.alert_url, settings, request)
    db.add(watch)
    # the address may hold a secret (a token in the path): the audit trail never carries it
    await _audit(db, admin, request, "watch.create", watch.id, name=watch.name, target=watch.target,
                 kind=watch.kind, alert=bool(watch.alert_url))  # fmt: skip
    await db.commit()
    return await _out(db, watch)


@router.get("/{watch_id}", response_model=WatchOut)
async def read_watch(watch_id: str, admin: AdminUser, db: Db) -> WatchOut:
    return await _out(db, await _get(db, watch_id))


@router.patch("/{watch_id}", response_model=WatchOut)
async def update_watch(
    watch_id: str, body: WatchPatch, request: Request, admin: AdminUser, db: Db
) -> WatchOut:
    watch = await _get(db, watch_id)
    settings = await load_settings(db)
    changes = body.model_dump(exclude_unset=True)
    if "interval_s" in changes and changes["interval_s"] < settings.min_watch_interval_s:
        raise ApiError(
            422,
            "interval_too_short",
            f"Watches run at most every {settings.min_watch_interval_s} seconds.",
        )
    if body.name is not None:
        watch.name = body.name.strip()
    if body.target is not None:
        watch.target = body.target.strip()
    watch.options = _options(body, watch.options)
    if body.all_nodes:
        watch.node_ids = None
    elif body.node_ids is not None:
        watch.node_ids = body.node_ids
    for field in ("interval_s", "min_ok_pct", "alert_after", "alert_format"):
        if getattr(body, field) is not None:
            setattr(watch, field, getattr(body, field))
    if "alert_url" in changes:
        watch.alert_url = (body.alert_url or "").strip() or None
    if body.enabled is not None and body.enabled != watch.enabled:
        watch.enabled = body.enabled
        watch.next_run_at = utcnow() if body.enabled else None
    elif watch.enabled and watch.next_run_at is None:
        watch.next_run_at = utcnow()
    await _check_plan(watch, settings)
    await _check_alert_url(watch.alert_url, settings, request)
    await _audit(db, admin, request, "watch.update", watch.id, name=watch.name,
                 fields=sorted(k for k in changes if k != "alert_url"))  # fmt: skip
    await db.commit()
    return await _out(db, watch)


@router.delete("/{watch_id}", status_code=204)
async def delete_watch(watch_id: str, request: Request, admin: AdminUser, db: Db) -> None:
    watch = await _get(db, watch_id)
    await _audit(db, admin, request, "watch.delete", watch.id, name=watch.name)
    from sqlalchemy import delete

    await db.execute(delete(WatchRun).where(WatchRun.watch_id == watch.id))
    await db.delete(watch)
    await db.commit()


@router.post("/{watch_id}/run", response_model=RunNowOut, status_code=202)
async def run_now(watch_id: str, request: Request, admin: AdminUser, db: Db) -> RunNowOut:
    await _get(db, watch_id)
    measurement_id = await _scheduler(request).start(watch_id)
    if measurement_id is None:
        raise ApiError(409, "could_not_start", "The check could not be started; see its history.")
    return RunNowOut(measurement_id=measurement_id)


@router.post("/{watch_id}/test-alert", response_model=TestAlertOut)
async def test_alert(watch_id: str, request: Request, admin: AdminUser, db: Db) -> TestAlertOut:
    watch = await _get(db, watch_id)
    if not watch.alert_url:
        raise ApiError(422, "no_alert_address", "This watch has no alert address.")
    error = await _scheduler(request).test_alert(watch_id)
    return TestAlertOut(ok=error is None, error=error)


@router.get("/{watch_id}/runs", response_model=list[WatchRunOut])
async def runs(
    watch_id: str,
    admin: AdminUser,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[WatchRunOut]:
    await _get(db, watch_id)
    rows = (
        await db.execute(
            select(WatchRun)
            .where(WatchRun.watch_id == watch_id)
            .order_by(WatchRun.at.desc())
            .limit(limit)
        )
    ).scalars()
    return [WatchRunOut.model_validate(r, from_attributes=True) for r in rows]
