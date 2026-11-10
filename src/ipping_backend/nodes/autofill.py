"""Filling in a node's details from the address it connects from.

A node's country, region, city, position, provider and AS number can come from the IP data (MaxMind) instead of being typed.
The rule is simple and never surprising:

* a field that is empty gets the data's value, and is recorded in `node.auto_fields`;
* a field in `auto_fields` follows the data: it is refreshed when the address or the database changes;
* a field that is neither (an administrator typed it, or the node reported it when it enrolled) is never touched.

An administrator who edits a field makes it theirs (it leaves `auto_fields`); clearing it makes it automatic again. A position
that comes from the data is a city's, not the node's: `location_verified` is never set here.

The data's *place* (country, region, city, position) is only used where it agrees with the place somebody gave. A hosting
address is often placed at its provider's headquarters, and the location estimate trusts a node's position, so the data must
never put a position next to a country or city it contradicts:

* a country that was typed or reported, and is not the data's, is a conflict;
* so is a typed position more than `CONFLICT_KM` from the data's, and a typed or reported city that the gazetteer places that
  far from it (a city it does not know says nothing);
* on a conflict no place is taken from the data, and any place it supplied earlier is taken back (the provider and the AS
  number belong to the address, not to a place, and are still filled in);
* a record with no city name is a country's or region's centre, not a place a node can be: its position is not used.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..geolocation.estimate import haversine_km
from ..geolocation.gazetteer import cities
from ..geolocation.geoip import GeoIp
from ..ipdata.lookup import lookup
from ..ipdata.options import load_options
from ..ipdata.schemas import IpInfoOut
from ..models import Node

AUTO_FIELDS = ("country", "region", "city", "latitude", "longitude", "provider", "asn")
# latitude and longitude are one fact: both or neither
_GROUPS = (("country",), ("region",), ("city",), ("latitude", "longitude"), ("provider",), ("asn",))
_PLACE = {("country",), ("region",), ("city",), ("latitude", "longitude")}
# How far the data's position may be from the place somebody gave before the data is not used. City-level databases are
# right to a few tens of kilometres for the same metropolitan area; hosting addresses that are wrong are wrong by far more.
CONFLICT_KM = 300.0


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


@lru_cache(maxsize=1)
def _named() -> dict[tuple[str, str], tuple[float, float]]:
    """(country, lower-case city name) -> the position of the biggest city of that name."""
    best: dict[tuple[str, str], tuple[int, float, float]] = {}
    for city in cities():
        key = (city.country, city.name.lower())
        if key not in best or city.population > best[key][0]:
            best[key] = (city.population, city.latitude, city.longitude)
    return {key: (lat, lon) for key, (_, lat, lon) in best.items()}


def _conflicts(node: Node, auto: list[str], values: dict[str, Any]) -> bool:
    """Whether the data's place contradicts a place that was typed or reported (fields in `auto` are the data's own)."""

    def theirs(field: str) -> bool:
        return field not in auto and not _empty(getattr(node, field))

    country = values.get("country")
    if theirs("country") and country and str(node.country).upper() != str(country).upper():
        return True
    if "latitude" not in values:
        return False
    lat, lon = float(values["latitude"]), float(values["longitude"])
    typed_lat, typed_lon = node.latitude, node.longitude
    if (
        theirs("latitude")
        and theirs("longitude")
        and typed_lat is not None
        and typed_lon is not None
        and haversine_km(typed_lat, typed_lon, lat, lon) > CONFLICT_KM
    ):
        return True
    if theirs("city"):
        where = _named().get((str(node.country or country or "").upper(), str(node.city).lower()))
        if where is not None and haversine_km(where[0], where[1], lat, lon) > CONFLICT_KM:
            return True
    return False


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
    if not info.city:
        values.pop("latitude", None)  # no city: the centre of a country or region, which no node is
        values.pop("longitude", None)
    auto = list(node.auto_fields or [])
    changed: list[str] = []
    conflict = _conflicts(node, auto, values)
    for group in _GROUPS:
        if conflict and group in _PLACE:
            for field in (
                group
            ):  # somebody's own place disagrees with the data: what it supplied is taken back
                if field in auto:
                    auto.remove(field)
                    setattr(node, field, None)
                    changed.append(field)
            continue
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
