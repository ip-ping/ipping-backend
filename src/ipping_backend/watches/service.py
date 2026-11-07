"""Running watches on their schedule and acting on how their measurements turn out.

Every backend instance runs the scheduler. They do not coordinate through a lock: a due watch is
*claimed* with a conditional update (move `next_run_at` forward only if it still has the value that was
read), and only the instance whose update changed the row starts the measurement, so a watch runs once
per interval however many instances there are, on SQLite and PostgreSQL alike. The instance that
finalises a measurement judges it, so each run is judged once.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..measurements.engine import MeasurementEngine, MeasurementError, Owner
from ..measurements.schemas import CreateMeasurementRequest
from ..models import Watch, WatchRun, utcnow
from ..runtime_settings import load_settings
from ..security.targets import Resolver, system_resolver
from . import evaluate
from .notify import Alert, deliver, make_alert

log = logging.getLogger(__name__)

# What a watch may carry into the request it makes (everything else on a request is the watch's own).
REQUEST_OPTIONS = (
    "port",
    "count",
    "follow_redirects",
    "dns_mode",
    "family",
    "record_type",
    "resolver",
)


def request_for(watch: Watch) -> CreateMeasurementRequest:
    extra: dict[str, Any] = {k: v for k, v in (watch.options or {}).items() if k in REQUEST_OPTIONS}
    return CreateMeasurementRequest(
        target=watch.target,
        kind=watch.kind,
        node_ids=watch.node_ids,
        **extra,
    )


class WatchScheduler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        engine: MeasurementEngine,
        *,
        resolver: Resolver = system_resolver,
        poll_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._engine = engine
        self._resolver = resolver
        self._poll_s = poll_s
        self._transport = transport
        engine.on_watch_finished = self.on_finished

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_s)
            try:
                await self.tick()
            except Exception:
                log.exception("watch scheduler failed")

    async def tick(self) -> int:
        """Start every watch that is due; returns how many this instance started."""
        started = 0
        async with self._sessionmaker() as db:
            due = list(
                (
                    await db.execute(
                        select(Watch)
                        .where(Watch.enabled.is_(True), Watch.next_run_at <= utcnow())
                        .order_by(Watch.next_run_at)
                        .limit(25)
                    )
                ).scalars()
            )
            for watch in due:
                claimed = await db.execute(
                    update(Watch)
                    .where(Watch.id == watch.id, Watch.next_run_at == watch.next_run_at)
                    .values(next_run_at=utcnow() + timedelta(seconds=watch.interval_s))
                )
                await db.commit()
                if claimed.rowcount == 1:  # type: ignore[attr-defined]
                    await self.start(watch.id)
                    started += 1
        return started

    async def start(self, watch_id: str) -> str | None:
        """Start a measurement for a watch now. Returns its id, or None when it could not start (the
        reason is recorded as an `error` run: it says nothing about the target)."""
        async with self._sessionmaker() as db:
            watch = await db.get(Watch, watch_id)
            if watch is None:
                return None
            request = request_for(watch)
        try:
            return await self._engine.create(request, Owner(None), None, watch_id=watch_id)
        except MeasurementError as exc:
            await self._record_error(watch_id, exc.message)
        except Exception:
            log.exception("could not start a watch's measurement", extra={"watch": watch_id})
            await self._record_error(watch_id, "The check could not be started.")
        return None

    async def _record_error(self, watch_id: str, note: str) -> None:
        async with self._sessionmaker() as db:
            watch = await db.get(Watch, watch_id)
            if watch is None:
                return
            now = utcnow()
            watch.last_run_at, watch.last_status = now, "error"
            db.add(WatchRun(watch_id=watch_id, at=now, status="error", note=note[:200]))
            await db.commit()

    async def on_finished(self, measurement_id: str) -> None:
        """Judge a finished measurement, remember the run, and alert if that is due."""
        async with self._sessionmaker() as db:
            result = await self._engine.load(db, measurement_id)
            if result is None:
                return
            from ..models import Measurement

            row = await db.get(Measurement, measurement_id)
            watch = await db.get(Watch, row.watch_id) if row and row.watch_id else None
            if watch is None:
                return
            assessment = evaluate.assess(result, watch.min_ok_pct)
            now = utcnow()
            db.add(
                WatchRun(
                    watch_id=watch.id,
                    at=now,
                    measurement_id=measurement_id,
                    status=assessment.status,
                    nodes_total=assessment.nodes_total,
                    nodes_ok=assessment.nodes_ok,
                    median_ms=assessment.median_ms,
                    min_ms=assessment.min_ms,
                    max_ms=assessment.max_ms,
                    note=assessment.note,
                )
            )
            streak = evaluate.advance(
                watch.bad_streak, watch.alerting, assessment.status, watch.alert_after
            )
            watch.last_run_at, watch.last_status = now, assessment.status
            watch.bad_streak, watch.alerting = streak.bad_streak, streak.alerting
            alert: Alert | None = None
            if streak.event is not None:
                alert = make_alert(
                    streak.event, watch.id, watch.name, watch.kind, watch.target, assessment.status,
                    assessment.nodes_ok, assessment.nodes_total, assessment.median_ms, measurement_id,
                )  # fmt: skip
            url, fmt, watch_id = watch.alert_url, watch.alert_format, watch.id
            await db.commit()
        if alert is not None and url:
            await self._send(watch_id, url, fmt, alert)

    async def _send(self, watch_id: str, url: str, fmt: str, alert: Alert) -> str | None:
        async with self._sessionmaker() as db:
            allow_private = (await load_settings(db)).allow_private_webhooks
        error = await deliver(
            url,
            fmt,
            alert,
            allow_private=allow_private,
            resolver=self._resolver,
            transport=self._transport,
        )
        async with self._sessionmaker() as db:
            watch = await db.get(Watch, watch_id)
            if watch is not None:
                watch.last_alert_at = utcnow()
                watch.last_alert_error = error
                await db.commit()
        if error:
            log.warning("alert not delivered", extra={"watch": watch_id, "reason": error})
        return error

    async def test_alert(self, watch_id: str) -> str | None:
        """Send a test message to the watch's address. Returns None on success, else the reason."""
        async with self._sessionmaker() as db:
            watch = await db.get(Watch, watch_id)
            if watch is None or not watch.alert_url:
                return "This watch has no alert address."
            alert = make_alert(
                "test", watch.id, watch.name, watch.kind, watch.target, watch.last_status or "ok",
                0, 0, None, None,
            )  # fmt: skip
            url, fmt = watch.alert_url, watch.alert_format
        return await self._send(watch_id, url, fmt, alert)
