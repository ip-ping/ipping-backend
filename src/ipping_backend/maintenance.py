"""Periodic housekeeping: retention and expired credentials."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    EnrollmentToken,
    Measurement,
    MeasurementEvent,
    MeasurementTask,
    Sample,
    WebSession,
    utcnow,
)
from .runtime_settings import load_settings

log = logging.getLogger(__name__)

INTERVAL_S = 600


async def run_once(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Raw samples and event logs are short-lived; per-node summaries live as long as the
    measurement row does."""
    now = utcnow()
    removed: dict[str, int] = {}
    async with sessionmaker() as db:
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
        removed["sessions"] = (
            await db.execute(delete(WebSession).where(WebSession.expires_at < now))
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
