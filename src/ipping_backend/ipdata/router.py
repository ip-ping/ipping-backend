"""The IP lookup API: `GET /api/v1/ip` (the caller's own address) and `GET /api/v1/ip/{address}`.

Who may use it, and how often, is set in the admin page (IP data): off, public, an API key (or an administrator's
session), or administrators only, and a limit per client address a minute.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..deps import Db, OptionalApiKey, OptionalSession, client_ip
from ..errors import ApiError
from ..security.ratelimit import allow
from .lookup import lookup
from .options import load_options
from .schemas import IpInfoOut

router = APIRouter(prefix="/api/v1/ip", tags=["ip"])


async def _gate(request: Request, db: Db, session: OptionalSession, key: OptionalApiKey) -> None:
    options = await load_options(db)
    if not options.lookup_enabled:
        raise ApiError(404, "ip_lookup_disabled", "The IP lookup API is turned off.")
    administrator = session is not None and session.user_id is not None
    if options.lookup_access == "admin" and not administrator:
        raise ApiError(403, "admin_required", "The IP lookup API is for administrators only.")
    if options.lookup_access == "api_key" and key is None and not administrator:
        raise ApiError(
            401,
            "api_key_required",
            "The IP lookup API needs an API key (Authorization: Bearer <key>).",
        )
    if not administrator:
        who = client_ip(request) or "unknown"
        if not await allow(db, f"ip-lookup:{who}", options.lookup_per_minute, 60):
            raise ApiError(429, "rate_limited", "Too many lookups. Wait a minute.")


def _answer(request: Request, address: str) -> IpInfoOut:
    geoip = request.app.state.geoip
    if not geoip.databases():
        raise ApiError(
            503,
            "no_database",
            "No IP database is installed. An administrator can turn on MaxMind in the admin page (IP data).",
        )
    try:
        return lookup(geoip, address)
    except ValueError:
        raise ApiError(422, "invalid_address", "That is not an IP address.") from None


@router.get("", response_model=IpInfoOut)
async def my_address(
    request: Request, db: Db, session: OptionalSession, key: OptionalApiKey
) -> IpInfoOut:
    """What the databases say about the address this request comes from."""
    await _gate(request, db, session, key)
    return _answer(request, client_ip(request) or "")


@router.get("/{address}", response_model=IpInfoOut)
async def address_info(
    address: str, request: Request, db: Db, session: OptionalSession, key: OptionalApiKey
) -> IpInfoOut:
    """What the databases say about an IPv4 or IPv6 address: its network, place, time zone and AS."""
    await _gate(request, db, session, key)
    return _answer(request, address)
