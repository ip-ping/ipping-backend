from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..deps import (
    CurrentSession,
    Db,
    Engine,
    OptionalSession,
    client_ip,
    csrf_protect,
)
from ..errors import ApiError
from ..geolocation.schemas import LocationEstimateOut
from ..geolocation.service import location_for
from ..models import Measurement, MeasurementEvent, User, WebSession
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
    db: Db, measurement_id: str, session: WebSession | None, *, owner_only: bool = False
) -> Measurement:
    """Owner, administrator, or (for read access) anyone when the owner shared it.

    Everything else gets the same 404 as a measurement that does not exist.
    """
    measurement = await db.get(Measurement, measurement_id)
    allowed = False
    if measurement is not None:
        is_owner = session is not None and measurement.owner_session_id == session.id
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
    session: CurrentSession,
    engine: Engine,
    db: Db,
) -> schemas.MeasurementOut:
    measurement_id = await engine.create(
        body, Owner(session_id=session.id, user_id=session.user_id), client_ip(request)
    )
    result = await engine.load(db, measurement_id)
    assert result is not None  # noqa: S101 - it was committed a moment ago
    return result


@router.get("", response_model=list[schemas.MeasurementOut])
async def list_my_measurements(
    session: CurrentSession,
    engine: Engine,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[schemas.MeasurementOut]:
    ids = (
        await db.execute(
            select(Measurement.id)
            .where(Measurement.owner_session_id == session.id)
            .order_by(Measurement.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    results = [await engine.load(db, mid) for mid in ids]
    return [r for r in results if r is not None]


@router.get("/{measurement_id}", response_model=schemas.MeasurementOut)
async def get_measurement(
    measurement_id: str, session: OptionalSession, engine: Engine, db: Db
) -> schemas.MeasurementOut:
    await _authorized(db, measurement_id, session)
    result = await engine.load(db, measurement_id)
    assert result is not None  # noqa: S101
    return result


@router.get("/{measurement_id}/location", response_model=LocationEstimateOut)
async def estimated_location(
    measurement_id: str, request: Request, session: OptionalSession, db: Db
) -> LocationEstimateOut:
    """Where the target address appears to be, from the nodes' round-trip times (experimental)."""
    measurement = await _authorized(db, measurement_id, session)
    return await location_for(db, measurement, request.app.state.geoip)


@router.post("/{measurement_id}/cancel", status_code=202)
async def cancel_measurement(
    measurement_id: str, session: CurrentSession, engine: Engine, db: Db
) -> dict[str, str]:
    await _authorized(db, measurement_id, session, owner_only=True)
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
    engine: Engine,
    db: Db,
    after: Annotated[int | None, Query(ge=0, description="Resume after this event id")] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-Sent Events. Reconnecting with `Last-Event-ID` (browsers do this on their own)
    replays whatever was missed, so a dropped connection never loses a result."""
    await _authorized(db, measurement_id, session)
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
