from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from ..deps import (
    CurrentSession,
    Db,
    Engine,
    OptionalApiKey,
    OptionalSession,
    client_ip,
    csrf_protect,
)
from ..errors import ApiError
from ..geolocation.geoip import valid_ip
from ..geolocation.path import path_for
from ..geolocation.schemas import LocationEstimateOut, PathOut
from ..geolocation.service import UnknownAddress, location_for
from ..ipdata.schemas import IpInfoOut
from ..models import (
    ApiKey,
    Measurement,
    MeasurementEvent,
    MeasurementTask,
    User,
    WebSession,
)
from . import schemas
from .engine import Owner

STREAM_POLL_S = 3.0
KEEPALIVE_S = 15.0

router = APIRouter(
    prefix="/api/v1/measurements",
    tags=["measurements"],
    dependencies=[Depends(csrf_protect)],
)


async def _is_admin(db: Db, session: WebSession | None) -> bool:
    if session is None or session.user_id is None:
        return False
    return await db.get(User, session.user_id) is not None


async def _authorized(
    db: Db,
    measurement_id: str,
    session: WebSession | None,
    key: ApiKey | None = None,
    *,
    owner_only: bool = False,
) -> Measurement:
    """Owner (a browser session, or the API key that started it), administrator, or (for read access)
    anyone when the owner shared it.

    Everything else gets the same 404 as a measurement that does not exist.
    """
    measurement = await db.get(Measurement, measurement_id)
    allowed = False
    if measurement is not None:
        is_owner = (session is not None and measurement.owner_session_id == session.id) or (
            key is not None and measurement.owner_api_key_id == key.id
        )
        allowed = is_owner or await _is_admin(db, session)
        if not owner_only and measurement.shared:
            allowed = True
    if measurement is None or not allowed:
        raise ApiError(404, "not_found", "Measurement not found.")
    return measurement


@router.post("", response_model=schemas.MeasurementOut, status_code=201)
async def create_measurement(
    body: schemas.CreateMeasurementRequest,
    request: Request,
    session: OptionalSession,
    key: OptionalApiKey,
    engine: Engine,
    db: Db,
) -> schemas.MeasurementOut:
    if key is not None:
        # A script: its own quotas apply, and the measurement is readable with the same key.
        owner = Owner(None, None, key.id, key.per_minute, key.per_day)
    elif session is not None:
        owner = Owner(session_id=session.id, user_id=session.user_id)
    else:
        raise ApiError(401, "session_required", "Load the page again to start a session.")
    measurement_id = await engine.create(body, owner, client_ip(request))
    result = await engine.load(db, measurement_id)
    assert result is not None  # noqa: S101 - it was committed a moment ago
    return result


@router.get("", response_model=list[schemas.MeasurementOut])
async def list_my_measurements(
    session: OptionalSession,
    key: OptionalApiKey,
    engine: Engine,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[schemas.MeasurementOut]:
    if key is not None:
        mine = Measurement.owner_api_key_id == key.id
    elif session is not None:
        mine = Measurement.owner_session_id == session.id
    else:
        raise ApiError(401, "session_required", "Load the page again to start a session.")
    ids = (
        await db.execute(
            select(Measurement.id).where(mine).order_by(Measurement.created_at.desc()).limit(limit)
        )
    ).scalars()
    results = [await engine.load(db, mid) for mid in ids]
    return [r for r in results if r is not None]


@router.get("/{measurement_id}", response_model=schemas.MeasurementOut)
async def get_measurement(
    measurement_id: str, session: OptionalSession, key: OptionalApiKey, engine: Engine, db: Db
) -> schemas.MeasurementOut:
    await _authorized(db, measurement_id, session, key)
    result = await engine.load(db, measurement_id)
    assert result is not None  # noqa: S101
    return result


@router.get("/{measurement_id}/location", response_model=LocationEstimateOut)
async def estimated_location(
    measurement_id: str,
    request: Request,
    session: OptionalSession,
    key: OptionalApiKey,
    db: Db,
    ip: Annotated[
        str | None,
        Query(
            description=(
                "When the nodes resolved the name to different addresses (`groups` in the answer): "
                "estimate for this one instead of the one most nodes measured"
            )
        ),
    ] = None,
) -> LocationEstimateOut:
    """Where the target address appears to be, from the nodes' round-trip times (experimental)."""
    measurement = await _authorized(db, measurement_id, session, key)
    if ip is not None and valid_ip(ip) is None:
        raise ApiError(422, "invalid_address", "That is not an IP address.")
    try:
        return await location_for(
            db,
            measurement,
            request.app.state.ipdata,
            request.app.state.geofeeds,
            valid_ip(ip),
        )
    except UnknownAddress:
        raise ApiError(404, "unknown_address", "No node measured that address.") from None


MAX_ADDRESSES = 50


@router.get("/{measurement_id}/addresses", response_model=schemas.MeasurementAddressesOut)
async def tested_addresses(
    measurement_id: str,
    request: Request,
    session: OptionalSession,
    key: OptionalApiKey,
    db: Db,
) -> schemas.MeasurementAddressesOut:
    """What the IP databases say about each address the nodes tested (place, network, AS), most tested first.

    It belongs to the measurement: whoever may read the measurement may read this, whatever the IP lookup API's
    own access setting says, because the addresses are the ones the measurement itself tested (a name each node
    resolved for itself can be several) and nothing else can be asked for. `info` is null when no database is
    installed.
    """
    await _authorized(db, measurement_id, session, key)
    count = func.count()
    rows = (
        await db.execute(
            select(MeasurementTask.tested_ip, count)
            .where(
                MeasurementTask.measurement_id == measurement_id,
                MeasurementTask.tested_ip.is_not(None),
            )
            .group_by(MeasurementTask.tested_ip)
            .order_by(count.desc(), MeasurementTask.tested_ip)
            .limit(MAX_ADDRESSES)
        )
    ).all()
    service = request.app.state.ipdata
    loaded = service.available()

    def info_for(address: str) -> IpInfoOut | None:
        if not loaded:
            return None
        try:
            return service.lookup(address)  # type: ignore[no-any-return]
        except ValueError:  # not an address: a node's report is never trusted to be one
            return None

    infos = [info_for(address) for address, _ in rows]
    addresses = [
        schemas.AddressInfoOut(address=address, nodes=nodes, info=info)
        for (address, nodes), info in zip(rows, infos, strict=True)
    ]
    return schemas.MeasurementAddressesOut(databases_loaded=loaded, addresses=addresses)


@router.get("/{measurement_id}/path", response_model=PathOut)
async def traced_path(
    measurement_id: str,
    request: Request,
    session: OptionalSession,
    key: OptionalApiKey,
    engine: Engine,
    db: Db,
) -> PathOut:
    """The network (ASN) and the place a GeoIP database gives for each router a traceroute found.

    Empty unless a database is loaded (`ipping-backend geoip-update`). A router's place is a hint:
    check it against the round-trip time before believing it.
    """
    await _authorized(db, measurement_id, session, key)
    result = await engine.load(db, measurement_id)
    assert result is not None  # noqa: S101
    return path_for(result, request.app.state.ipdata)


@router.post("/{measurement_id}/cancel", status_code=202)
async def cancel_measurement(
    measurement_id: str, session: OptionalSession, key: OptionalApiKey, engine: Engine, db: Db
) -> dict[str, str]:
    await _authorized(db, measurement_id, session, key, owner_only=True)
    await engine.cancel(measurement_id)
    return {"status": "cancelling"}


@router.post("/{measurement_id}/share", response_model=schemas.ShareRequest)
async def share_measurement(
    measurement_id: str, body: schemas.ShareRequest, session: CurrentSession, db: Db
) -> schemas.ShareRequest:
    """Opt-in: anyone with the link can view a shared measurement."""
    measurement = await _authorized(db, measurement_id, session, owner_only=True)
    measurement.shared = body.shared
    await db.commit()
    return schemas.ShareRequest(shared=measurement.shared)


def _frame(seq: int, type_: str, data: dict[str, Any]) -> str:
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.get("/{measurement_id}/events")
async def stream_events(
    measurement_id: str,
    request: Request,
    session: OptionalSession,
    key: OptionalApiKey,
    engine: Engine,
    db: Db,
    after: Annotated[int | None, Query(ge=0, description="Resume after this event id")] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-Sent Events. Reconnecting with `Last-Event-ID` (browsers do this on their own)
    replays whatever was missed, so a dropped connection never loses a result."""
    await _authorized(db, measurement_id, session, key)
    await db.rollback()  # release the pooled connection: this response can stay open for minutes
    resume = after
    if resume is None and last_event_id and last_event_id.isdigit():
        resume = int(last_event_id)
    sessionmaker = request.app.state.sessionmaker
    hub = engine.cluster.hub

    async def generate() -> AsyncIterator[str]:
        # The event table is the source of truth and the stream just tails it. Being woken (by this
        # process or, through NOTIFY, another one) makes it prompt; the timer makes it correct even
        # if a wake-up is lost. Subscribe before the first read so nothing slips between the two.
        waiter = hub.waiter(f"events:{measurement_id}")
        try:
            last = resume or 0
            yield "retry: 3000\n\n"
            quiet = 0.0
            while True:
                async with sessionmaker() as s:
                    rows = (
                        (
                            await s.execute(
                                select(MeasurementEvent)
                                .where(
                                    MeasurementEvent.measurement_id == measurement_id,
                                    MeasurementEvent.seq > last,
                                )
                                .order_by(MeasurementEvent.seq)
                                .limit(500)
                            )
                        )
                        .scalars()
                        .all()
                    )
                for event in rows:
                    last = event.seq
                    quiet = 0.0
                    yield _frame(event.seq, event.type, event.data)
                    if event.type == "measurement.finished":
                        return
                if rows:
                    continue  # there may be more waiting
                if not await waiter.wait(STREAM_POLL_S):
                    quiet += STREAM_POLL_S
                    if quiet >= KEEPALIVE_S:
                        quiet = 0.0
                        yield ": keep-alive\n\n"
        finally:
            hub.release(waiter)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
