"""Cookie sessions for browsers: anonymous visitors and signed-in administrators.

The cookie holds a random value; the database holds only its hash. Signing in always issues a
fresh session, so a session id planted before login is useless afterwards.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import Response
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import WebSession, utcnow
from .tokens import hash_secret, new_secret

SESSION_COOKIE = "ipping_session"
ANONYMOUS_TTL = timedelta(days=30)
ADMIN_TTL = timedelta(hours=12)


async def create_session(
    db: AsyncSession, response: Response, settings: Settings, user_id: int | None = None
) -> WebSession:
    token = new_secret()
    ttl = ADMIN_TTL if user_id is not None else ANONYMOUS_TTL
    session = WebSession(
        id=hash_secret(token),
        user_id=user_id,
        csrf_token=new_secret(),
        expires_at=utcnow() + ttl,
    )
    db.add(session)
    await db.flush()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(ttl.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return session


async def find_session(db: AsyncSession, token: str | None) -> WebSession | None:
    if not token:
        return None
    session = await db.get(WebSession, hash_secret(token))
    if session is None or session.expires_at <= utcnow():
        return None
    return session


async def destroy_session(
    db: AsyncSession, session: WebSession, response: Response | None = None
) -> None:
    await db.execute(delete(WebSession).where(WebSession.id == session.id))
    if response is not None:
        response.delete_cookie(SESSION_COOKIE, path="/")
