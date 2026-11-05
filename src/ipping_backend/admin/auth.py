"""Browser sessions, first-run setup and administrator sign-in."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..deps import CurrentSession, Db, OptionalSession, client_ip, csrf_protect, require_admin
from ..errors import ApiError
from ..models import AppSetting, AuditEvent, User
from ..security.passwords import hash_password, verify_password
from ..security.ratelimit import allow
from ..security.sessions import create_session, destroy_session
from ..security.tokens import secrets_match

router = APIRouter(prefix="/api/v1", tags=["session"])

SETUP_DONE_KEY = "setup_completed"
SETUP_TOKEN_KEY = "setup_token_pending"  # noqa: S105 - a settings key, not a secret


class SessionOut(BaseModel):
    csrf_token: str
    authenticated: bool
    username: str | None = None


class SetupStatus(BaseModel):
    needs_setup: bool


class SetupRequest(BaseModel):
    setup_token: str = Field(min_length=1, max_length=200)
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.get("/session", response_model=SessionOut)
async def get_session(
    request: Request, response: Response, session: OptionalSession, db: Db
) -> SessionOut:
    """Start (or resume) a browser session and hand out its CSRF token.

    The frontend calls this on load and sends the token back as `X-CSRF-Token` on every write.
    """
    if session is None:
        session = await create_session(db, response, request.app.state.settings)
        await db.commit()
    user = await db.get(User, session.user_id) if session.user_id is not None else None
    return SessionOut(
        csrf_token=session.csrf_token,
        authenticated=user is not None,
        username=user.username if user else None,
    )


@router.get("/setup/status", response_model=SetupStatus)
async def setup_status(db: Db) -> SetupStatus:
    return SetupStatus(needs_setup=(await db.scalar(select(func.count()).select_from(User))) == 0)


@router.post("/setup", response_model=SessionOut, dependencies=[Depends(csrf_protect)])
async def run_setup(
    body: SetupRequest, request: Request, response: Response, session: OptionalSession, db: Db
) -> SessionOut:
    """One-time creation of the first administrator, gated by the setup token."""
    ip = client_ip(request) or "unknown"
    if not await allow(db, f"setup:{ip}", 5, 60):
        raise ApiError(429, "rate_limited", "Too many attempts. Wait a minute.")
    expected = request.app.state.setup_token
    if (await db.scalar(select(func.count()).select_from(User))) != 0:
        raise ApiError(409, "already_set_up", "An administrator already exists.")
    if not expected or not secrets_match(body.setup_token, expected):
        raise ApiError(403, "invalid_setup_token", "The setup token is not correct.")
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.add(AuditEvent(actor=body.username, action="admin.setup", source_ip=ip))
    # Two requests may race past the check above, in this process or in another one. Only one can
    # insert this row (it is a primary key), so only one administrator is ever created this way.
    db.add(AppSetting(key=SETUP_DONE_KEY, value=True))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise ApiError(409, "already_set_up", "An administrator already exists.") from None
    if session is not None:
        await destroy_session(db, session)
    new_session = await create_session(db, response, request.app.state.settings, user.id)
    await db.execute(delete(AppSetting).where(AppSetting.key == SETUP_TOKEN_KEY))
    await db.commit()
    request.app.state.setup_token = None
    return SessionOut(csrf_token=new_session.csrf_token, authenticated=True, username=user.username)


@router.post("/admin/login", response_model=SessionOut, dependencies=[Depends(csrf_protect)])
async def login(
    body: LoginRequest, request: Request, response: Response, session: OptionalSession, db: Db
) -> SessionOut:
    ip = client_ip(request) or "unknown"
    if not await allow(db, f"login:{ip}", 10, 60):
        raise ApiError(429, "rate_limited", "Too many sign-in attempts. Wait a minute.")
    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if not verify_password(user.password_hash if user else None, body.password) or user is None:
        db.add(AuditEvent(actor=body.username[:64], action="admin.login_failed", source_ip=ip))
        await db.commit()
        raise ApiError(401, "invalid_credentials", "Wrong username or password.")
    if session is not None:
        await destroy_session(db, session)  # never reuse a pre-login session id
    new_session = await create_session(db, response, request.app.state.settings, user.id)
    db.add(AuditEvent(actor=user.username, action="admin.login", source_ip=ip))
    await db.commit()
    return SessionOut(csrf_token=new_session.csrf_token, authenticated=True, username=user.username)


@router.post("/admin/logout", status_code=204, dependencies=[Depends(csrf_protect)])
async def logout(response: Response, session: CurrentSession, db: Db) -> None:
    await destroy_session(db, session, response)
    await db.commit()


@router.get("/admin/me", response_model=SessionOut)
async def me(session: CurrentSession, user: User = Depends(require_admin)) -> SessionOut:
    return SessionOut(csrf_token=session.csrf_token, authenticated=True, username=user.username)
