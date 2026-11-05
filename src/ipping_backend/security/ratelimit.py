"""Rate limits that every backend process agrees on.

The counts live in the database (`rate_events`), not in a process's memory: with several
processes behind one proxy an in-memory limiter would let through that many times the limit. A hit
is recorded the moment it is allowed, in its own committed transaction, so a request that fails
validation afterwards still counts (otherwise bad requests would be a free way around the limit).
Under heavy concurrency the limit can be exceeded by a request or two; it is an abuse brake, not an
accounting system.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RateEvent, utcnow


async def allow(db: AsyncSession, key: str, limit: int, window_s: float) -> bool:
    cutoff = utcnow() - timedelta(seconds=window_s)
    used = await db.scalar(
        select(func.count())
        .select_from(RateEvent)
        .where(RateEvent.key == key, RateEvent.at > cutoff)
    )
    if (used or 0) >= limit:
        return False
    db.add(RateEvent(key=key))
    await db.commit()
    return True


async def prune(db: AsyncSession, older_than_s: float = 3600) -> int:
    result = await db.execute(
        delete(RateEvent).where(RateEvent.at < utcnow() - timedelta(seconds=older_than_s))
    )
    return result.rowcount  # type: ignore[attr-defined,no-any-return]
