"""`GET /api/v1/text`: start a measurement and get the answer as text, for `curl` and shell scripts.

It needs an API key (the request has a side effect, so it must never be something a web page can make a
visitor's browser do), waits for the measurement to finish (up to `wait` seconds) and prints the same report
the command-line client does.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from ..deps import Db, Engine, OptionalApiKey, client_ip
from ..errors import ApiError
from ..nodes.protocol import DnsRecordType, ProbeKind
from .engine import Owner
from .render import render_text
from .schemas import CreateMeasurementRequest

router = APIRouter(prefix="/api/v1", tags=["measurements"])

POLL_S = 0.4


@router.get("/text", response_class=PlainTextResponse)
async def text_measurement(
    request: Request,
    key: OptionalApiKey,
    engine: Engine,
    db: Db,
    target: Annotated[str, Query(min_length=1, max_length=2048)],
    kind: ProbeKind = "ping",
    port: Annotated[int | None, Query(ge=1, le=65535)] = None,
    count: Annotated[int | None, Query(ge=1, le=10)] = None,
    record_type: DnsRecordType | None = None,
    resolver: Literal["system", "cloudflare", "google", "quad9"] | None = None,
    transport: Literal["udp", "doh"] | None = None,
    follow_redirects: bool = False,
    family: Literal["auto", "4", "6"] = "auto",
    dns_mode: Literal["fixed", "regional"] = "fixed",
    countries: Annotated[str | None, Query(description="Comma-separated ISO country codes")] = None,
    wait: Annotated[float, Query(ge=1, le=30, description="Seconds to wait for the result")] = 20,
) -> PlainTextResponse:
    if key is None:
        raise ApiError(
            401,
            "api_key_required",
            "Send an API key: Authorization: Bearer ipk_... (see the admin page).",
        )
    try:
        body = CreateMeasurementRequest(
            target=target,
            kind=kind,
            port=port,
            count=count,
            record_type=record_type,
            resolver=resolver,
            transport=transport,
            follow_redirects=follow_redirects,
            family=family,
            dns_mode=dns_mode,
            countries=countries.split(",") if countries else None,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ApiError(422, "validation_error", f"{first['loc'][0]}: {first['msg']}") from exc
    owner = Owner(None, None, key.id, key.per_minute, key.per_day)
    measurement_id = await engine.create(body, owner, client_ip(request))
    deadline = time.monotonic() + wait
    result = await engine.load(db, measurement_id)
    while result is not None and result.status == "running" and time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        db.expire_all()
        result = await engine.load(db, measurement_id)
    assert result is not None  # noqa: S101 - it was committed a moment ago
    return PlainTextResponse(
        render_text(result),
        headers={"X-Measurement-Id": measurement_id, "X-Measurement-Status": result.status},
    )
