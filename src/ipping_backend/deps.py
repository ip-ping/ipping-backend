"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import ApiError
from .measurements.engine import MeasurementEngine
from .models import User, WebSession
from .security.sessions import SESSION_COOKIE, find_session
from .security.tokens import secrets_match

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-csrf-token"


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Handlers that write call `await db.commit()` themselves, so a
    change is durable before the response is built rather than whenever cleanup happens to run."""
    async with request.app.state.sessionmaker() as session:
        yield session


Db = Annotated[AsyncSession, Depends(get_db)]


def get_engine(request: Request) -> MeasurementEngine:
    engine: MeasurementEngine = request.app.state.engine
    return engine


Engine = Annotated[MeasurementEngine, Depends(get_engine)]


def client_ip(request: Request | WebSocket) -> str | None:
    # Behind a reverse proxy, run uvicorn with --proxy-headers and --forwarded-allow-ips so that
    # this is the real client address; X-Forwarded-For is never read directly.
    return request.client.host if request.client else None


async def get_session_optional(request: Request, db: Db) -> WebSession | None:
    return await find_session(db, request.cookies.get(SESSION_COOKIE))


OptionalSession = Annotated[WebSession | None, Depends(get_session_optional)]


async def require_session(session: OptionalSession) -> WebSession:
    if session is None:
        raise ApiError(401, "session_required", "Load the page again to start a session.")
    return session


CurrentSession = Annotated[WebSession, Depends(require_session)]


def _origin_allowed(request: Request, origin: str) -> bool:
    settings = request.app.state.settings
    parts = urlsplit(origin)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    allowed = {o.rstrip("/") for o in settings.allowed_origins}
    if settings.public_url:
        allowed.add(settings.public_url.rstrip("/"))
    return parts.netloc == host or origin.rstrip("/") in allowed


async def csrf_protect(request: Request, session: OptionalSession) -> None:
    """Reject cross-site writes: a matching Origin, and the CSRF token when a session exists."""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None and not _origin_allowed(request, origin):
        raise ApiError(403, "bad_origin", "Cross-site requests are not allowed.")
    if session is not None:
        sent = request.headers.get(CSRF_HEADER, "")
        if not secrets_match(sent, session.csrf_token):
            raise ApiError(403, "bad_csrf_token", "Missing or invalid CSRF token.")


async def require_admin(session: CurrentSession, db: Db) -> User:
    user = await db.get(User, session.user_id) if session.user_id is not None else None
    if user is None:
        raise ApiError(401, "admin_required", "Sign in as an administrator.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
