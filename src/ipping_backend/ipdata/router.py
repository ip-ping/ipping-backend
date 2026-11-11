"""The IP lookup API: `GET /api/v1/ip` (the caller's own address) and `GET /api/v1/ip/{address}`.

Who may use it, and how often, is set in the admin page (IP data): off, public, an API key (or an administrator's
session), or administrators only, and a limit per client address a minute.

An answer is the vote of every IP data source that is on (all of them databases kept on disk: nothing is asked of a service), with
how sure each field is: see `ipdata/service.py` and `ipdata/consensus.py`.
"""

from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Request

from ..deps import Db, OptionalApiKey, OptionalSession, client_ip
from ..errors import ApiError
from ..security.ratelimit import allow
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
    service = request.app.state.ipdata
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise ApiError(422, "invalid_address", "That is not an IP address.") from None
    if not service.available():
        raise ApiError(
            503,
            "no_database",
            "No IP data source can answer yet: turn one on, and give each source that needs a key its key, in the admin page (IP data).",
        )
    return service.lookup(address)  # type: ignore[no-any-return]


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
