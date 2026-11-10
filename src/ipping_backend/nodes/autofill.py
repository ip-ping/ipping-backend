"""Filling in a node's details from the address it connects from.

A node's country, region, city, position, provider and AS number can come from the IP data (MaxMind) instead of being typed.
The rule is simple and never surprising:

* a field that is empty gets the data's value, and is recorded in `node.auto_fields`;
* a field in `auto_fields` follows the data: it is refreshed when the address or the database changes;
* a field that is neither (an administrator typed it, or the node reported it when it enrolled) is never touched.

An administrator who edits a field makes it theirs (it leaves `auto_fields`); clearing it makes it automatic again. A position
that comes from the data is a city's, not the node's: `location_verified` is never set here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..geolocation.geoip import GeoIp
from ..ipdata.lookup import lookup
from ..ipdata.options import load_options
from ..ipdata.schemas import IpInfoOut
from ..models import Node

AUTO_FIELDS = ("country", "region", "city", "latitude", "longitude", "provider", "asn")
# latitude and longitude are one fact: both or neither
_GROUPS = (("country",), ("region",), ("city",), ("latitude", "longitude"), ("provider",), ("asn",))


def values_from(info: IpInfoOut) -> dict[str, Any]:
    """The fields of a node that the data can supply (a missing value is left out)."""
    found = {
        "country": info.country,
        "region": info.region[:100] if info.region else None,
        "city": info.city[:100] if info.city else None,
        "latitude": info.latitude,
        "longitude": info.longitude,
        "provider": info.organization[:100] if info.organization else None,
        "asn": info.asn,
    }
    return {k: v for k, v in found.items() if v is not None}


def _empty(value: Any) -> bool:
    return value is None or value == ""


def fill(node: Node, geoip: GeoIp) -> list[str]:
    """Apply the data for `node.remote_addr` to the node. Returns the fields that changed."""
    if not node.remote_addr:
        return []
    try:
        info = lookup(geoip, node.remote_addr)
    except ValueError:
        return []
    if not info.found:
        return []
    values = values_from(info)
    auto = list(node.auto_fields or [])
    changed: list[str] = []
    for group in _GROUPS:
        if any(f not in values for f in group):
            continue  # the data does not have all of it
        following = all(f in auto for f in group)
        empty = all(_empty(getattr(node, f)) for f in group)
        if not (following or empty):
            continue  # typed or reported: theirs
        for field in group:
            if getattr(node, field) != values[field]:
                setattr(node, field, values[field])
                changed.append(field)
            if field not in auto:
                auto.append(field)
    if sorted(auto) != sorted(node.auto_fields or []):
        node.auto_fields = [f for f in AUTO_FIELDS if f in auto]
        if not changed:
            changed.append("auto_fields")
    return changed


def apply_edit(node: Node, edits: dict[str, Any]) -> list[str]:
    """An administrator's edit: a typed value makes the field theirs, an empty one hands it back to the data.

    Returns the fields now waiting for the data (the caller fills them).
    """
    auto = set(node.auto_fields or [])
    handed_back: list[str] = []
    for field in AUTO_FIELDS:
        if field not in edits:
            continue
        if _empty(edits[field]):
            auto.discard(field)
            node_value = None
            setattr(node, field, node_value)
            handed_back.append(field)
        else:
            auto.discard(field)
            setattr(node, field, edits[field])
    # a position is a pair: if one half is theirs, so is the other
    if "latitude" not in auto or "longitude" not in auto:
        auto.discard("latitude")
        auto.discard("longitude")
    node.auto_fields = [f for f in AUTO_FIELDS if f in auto]
    return handed_back


async def fill_if_enabled(db: AsyncSession, node: Node, geoip: GeoIp) -> list[str]:
    """`fill`, unless the administrator turned auto-fill off."""
    return fill(node, geoip) if (await load_options(db)).node_autofill else []


async def refresh_all(sessionmaker: async_sessionmaker[AsyncSession], geoip: GeoIp) -> int:
    """Fill every node that has an address (the data changed, or auto-fill was just turned on). Returns how many changed."""
    changed = 0
    async with sessionmaker() as db:
        if not (await load_options(db)).node_autofill:
            return 0
        for node in (await db.execute(select(Node).where(Node.remote_addr.is_not(None)))).scalars():
            if fill(node, geoip):
                changed += 1
        await db.commit()
    return changed
