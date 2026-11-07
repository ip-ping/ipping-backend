"""Periodic housekeeping: retention and expired credentials."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    BackendInstance,
    EnrollmentToken,
    Measurement,
    MeasurementEvent,
    MeasurementTask,
    Sample,
    WatchRun,
    WebSession,
    utcnow,
)
from .runtime_settings import load_settings
from .security.ratelimit import prune as prune_rate_events

log = logging.getLogger(__name__)

INTERVAL_S = 600


async def run_once(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Raw samples and event logs are short-lived; per-node summaries live as long as the
    measurement row does."""
    now = utcnow()
    removed: dict[str, int] = {}
    async with sessionmaker() as db:
        if db.bind.dialect.name == "postgresql":
            # Several processes run this loop; whoever holds the lock does this sweep (the lock
            # goes away with the transaction), the others skip it.
            got = await db.scalar(text("SELECT pg_try_advisory_xact_lock(7101)"))
            if not got:
                return removed
        settings = await load_settings(db)
        sample_cutoff = now - timedelta(days=settings.sample_retention_days)
        old = select(Measurement.id).where(
            Measurement.created_at < sample_cutoff, Measurement.status != "running"
        )
        old_tasks = select(MeasurementTask.id).where(MeasurementTask.measurement_id.in_(old))
        removed["samples"] = (
            await db.execute(delete(Sample).where(Sample.task_id.in_(old_tasks)))
        ).rowcount  # type: ignore[attr-defined]
        removed["events"] = (
            await db.execute(
                delete(MeasurementEvent).where(MeasurementEvent.measurement_id.in_(old))
            )
        ).rowcount  # type: ignore[attr-defined]
        removed["measurements"] = (
            await db.execute(
                delete(Measurement).where(
                    Measurement.created_at
                    < now - timedelta(days=settings.measurement_retention_days),
                    Measurement.status != "running",
                )
            )
        ).rowcount  # type: ignore[attr-defined]
        removed["watch_runs"] = (
            await db.execute(
                delete(WatchRun).where(
                    WatchRun.at < now - timedelta(days=settings.watch_history_days)
                )
            )
        ).rowcount  # type: ignore[attr-defined]
        removed["sessions"] = (
            await db.execute(delete(WebSession).where(WebSession.expires_at < now))
        ).rowcount  # type: ignore[attr-defined]
        removed["rate_events"] = await prune_rate_events(db)
        # Instances that stopped long ago (a crashed process leaves its row behind).
        removed["instances"] = (
            await db.execute(
                delete(BackendInstance).where(
                    BackendInstance.heartbeat_at < now - timedelta(days=1)
                )
            )
        ).rowcount  # type: ignore[attr-defined]
        removed["enrollment_tokens"] = (
            await db.execute(
                delete(EnrollmentToken).where(EnrollmentToken.expires_at < now - timedelta(days=7))
            )
        ).rowcount  # type: ignore[attr-defined]
        await db.commit()
    return removed


async def loop(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    while True:
        await asyncio.sleep(INTERVAL_S)
        try:
            removed = await run_once(sessionmaker)
            if any(removed.values()):
                log.info("retention sweep", extra=removed)
        except Exception:
            log.exception("retention sweep failed")
