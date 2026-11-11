"""The nine sources, read from disk the same way whatever file they come from.

MaxMind and DB-IP are `.mmdb` files in MaxMind's layout, read through `GeoIp` (which opens them again when they change). IPLocate
and IPinfo are `.mmdb` files with a layout of their own. IPtoASN and IPdeny are range tables (`ranges.py`), ipapi.is and
IP2Location LITE range tables with a place for each range (`places.py`), and ip2region a pair of `.xdb` files (`xdb.py`).
A lookup here never touches the network.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..geolocation.geoip import GeoIp, _Database
from . import records
from .catalog import BY_ID, GEO, NETWORK
from .places import PlaceDb, place_record
from .ranges import RangeDb, as_record
from .records import Record
from .xdb import XdbFile, record_of

IPTOASN_FILE = "iptoasn.sqlite"
IPDENY_FILE = "ipdeny.sqlite"
IPAPI_FILE = "ipapi-is.sqlite"
IP2LOCATION_FILE = "ip2location-lite.sqlite"
XDB_FILES = {4: "ip2region_v4.xdb", 6: "ip2region_v6.xdb"}
IPLOCATE_FILES = {"country": "iplocate-country.mmdb", "asn": "iplocate-asn.mmdb"}
IPINFO_FILE = "ipinfo-lite.mmdb"
MMDB_FLAVOUR = {"maxmind": "maxmind", "dbip": "dbip"}
RANGE_FILES = {"iptoasn": IPTOASN_FILE, "ipdeny": IPDENY_FILE}
PLACE_FILES = {"ipapi_is": IPAPI_FILE, "ip2location": IP2LOCATION_FILE}
OWN_MMDB = {"iplocate": tuple(IPLOCATE_FILES.values()), "ipinfo": (IPINFO_FILE,)}


def _names(record: Any, *path: str) -> str | None:
    node: Any = record
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    names = node.get("names") if isinstance(node, dict) else None
    value = names.get("en") if isinstance(names, dict) else None
    return records.text(value)


def _network(address: str, prefix: int | None) -> str | None:
    if prefix is None:
        return None
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def mmdb_record(
    city: _Database | None, country: _Database | None, system: _Database | None, address: str
) -> Record | None:
    """What a MaxMind-format city (or country) and AS database say about an address."""
    geo, prefix = city.get_with_prefix(address) if city is not None else (None, None)
    if geo is None and country is not None:
        geo, prefix = country.get_with_prefix(address)
    owner, owner_prefix = system.get_with_prefix(address) if system is not None else (None, None)
    if not geo and not owner:
        return None
    geo = geo or {}
    place = geo.get("location") or {}
    subdivision = (geo.get("subdivisions") or [{}])[0]
    in_country = geo.get("country") or geo.get("registered_country") or {}
    lat, lon = records.position(place.get("latitude"), place.get("longitude"))
    radius = place.get("accuracy_radius")
    return Record(
        country=records.country(in_country.get("iso_code")),
        region=_names({"s": subdivision}, "s"),
        region_code=records.text(subdivision.get("iso_code"), 8),
        city=_names(geo, "city"),
        postal_code=records.text((geo.get("postal") or {}).get("code"), 20),
        latitude=lat,
        longitude=lon,
        accuracy_radius_km=int(radius) if isinstance(radius, int | float) and radius > 0 else None,
        time_zone=records.time_zone(place.get("time_zone")),
        asn=records.asn((owner or {}).get("autonomous_system_number")),
        organization=records.text((owner or {}).get("autonomous_system_organization")),
        network=_network(address, prefix if geo else owner_prefix),
    )


def iplocate_record(
    country: _Database | None, system: _Database | None, address: str
) -> Record | None:
    """IPLocate's two databases: `{country_code, ...}` for where, `{asn: "15169", org, name, network, ...}` for who.

    The country of the ASN database is the network's own, not where the address is, so only the country database gives the country.
    """
    where, _ = country.get_with_prefix(address) if country is not None else (None, None)
    owner, _ = system.get_with_prefix(address) if system is not None else (None, None)
    if not where and not owner:
        return None
    where, owner = where or {}, owner or {}
    network = records.text(owner.get("network"), 64)
    try:
        network = str(ipaddress.ip_network(network, strict=False)) if network else None
    except ValueError:
        network = None
    return Record(
        country=records.country(where.get("country_code")),
        asn=records.asn(owner.get("asn")),
        organization=records.text(owner.get("org")) or records.text(owner.get("name")),
        network=network,
    )


def ipinfo_record(database: _Database | None, address: str) -> Record | None:
    """IPinfo Lite: `{country_code, asn: "AS13335", as_name, ...}` in one database."""
    found, prefix = database.get_with_prefix(address) if database is not None else (None, None)
    if not found:
        return None
    return Record(
        country=records.country(found.get("country_code")) or records.country(found.get("country")),
        asn=records.asn(found.get("asn")),
        organization=records.text(found.get("as_name")),
        network=_network(address, prefix),
    )


def _built(epoch: Any) -> datetime | None:
    return datetime.fromtimestamp(epoch, UTC) if epoch else None


class LocalSources:
    def __init__(self, geoip_of: Callable[[], GeoIp], directory: Path | None) -> None:
        self._geoip_of = geoip_of
        self.directory = directory
        self._ranges: dict[str, RangeDb] = {}
        self._places: dict[str, PlaceDb] = {}
        self._mmdb: dict[str, _Database] = {}
        self._xdb: dict[int, XdbFile] = {}

    # ---- lookups
    def lookup(self, source: str, address: str) -> Record | None:
        if source in MMDB_FLAVOUR:
            geoip = self._geoip_of()
            flavour = MMDB_FLAVOUR[source]
            return mmdb_record(
                geoip.flavoured(flavour, "city"),
                geoip.flavoured(flavour, "country"),
                geoip.flavoured(flavour, "asn"),
                address,
            )
        if source == "iplocate":
            return iplocate_record(
                self._database(IPLOCATE_FILES["country"]),
                self._database(IPLOCATE_FILES["asn"]),
                address,
            )
        if source == "ipinfo":
            return ipinfo_record(self._database(IPINFO_FILE), address)
        if source in RANGE_FILES:
            table = self._table(source)
            return as_record(table.find(address)) if table is not None else None
        if source in PLACE_FILES:
            places = self._place_table(source)
            return place_record(places.find(address)) if places is not None else None
        if source == "ip2region":
            version = ipaddress.ip_address(address).version
            reader = self._reader(version)
            return record_of(reader.find(address)) if reader is not None else None
        return None

    def _database(self, name: str) -> _Database | None:
        if self.directory is None:
            return None
        database = self._mmdb.get(name)
        if database is None:
            database = self._mmdb[name] = _Database(self.directory / name)
        return database

    def _table(self, source: str) -> RangeDb | None:
        if self.directory is None:
            return None
        table = self._ranges.get(source)
        if table is None:
            table = self._ranges[source] = RangeDb(self.directory / RANGE_FILES[source])
        return table

    def _place_table(self, source: str) -> PlaceDb | None:
        if self.directory is None:
            return None
        table = self._places.get(source)
        if table is None:
            table = self._places[source] = PlaceDb(self.directory / PLACE_FILES[source])
        return table

    def _reader(self, version: int) -> XdbFile | None:
        if self.directory is None:
            return None
        reader = self._xdb.get(version)
        if reader is None:
            reader = self._xdb[version] = XdbFile(self.directory / XDB_FILES[version])
        return reader

    # ---- what is on disk
    def files(self, source: str) -> list[dict[str, Any]]:
        """The files of a source that are in use: name, size and (where the file says) when it was built."""
        found: list[dict[str, Any]] = []
        databases: list[_Database | None] = []
        if source in MMDB_FLAVOUR:
            geoip = self._geoip_of()
            databases = [
                geoip.flavoured(MMDB_FLAVOUR[source], k) for k in ("city", "country", "asn")
            ]
        elif source in OWN_MMDB:
            databases = [self._database(name) for name in OWN_MMDB[source]]
        for database in databases:
            info = database.info() if database is not None else None
            if info is not None:
                found.append(
                    {
                        "name": info["file"],
                        "size": info["size"],
                        "built": _built(info["build_epoch"]),
                    }
                )
        tables = (
            self._table(source) if source in RANGE_FILES else None,
            self._place_table(source) if source in PLACE_FILES else None,
        )
        for table in tables:
            if table is not None and table.size():
                meta = table.meta()
                built = records.text(meta.get("built_at"), 40)
                rows = meta.get("rows", "")
                found.append(
                    {
                        "name": table.path.name,
                        "size": table.size(),
                        "built": datetime.fromisoformat(built) if built else None,
                        "rows": int(rows) if rows.isdigit() else None,
                    }
                )
        if source == "ip2region":
            for version in (4, 6):
                reader = self._reader(version)
                if reader is not None and reader.version is not None:
                    created = reader.created_at()
                    found.append(
                        {
                            "name": reader.path.name,
                            "size": reader.path.stat().st_size,
                            "built": _built(created),
                        }
                    )
        return found

    def present(self, source: str) -> bool:
        return bool(self.files(source))

    def knows(self, source: str) -> frozenset[str]:
        """The kinds of data the files that are on disk give (a source with only its AS database has no places)."""
        if source in MMDB_FLAVOUR:
            geoip = self._geoip_of()
            flavour = MMDB_FLAVOUR[source]
            found: set[str] = set()
            if geoip.flavoured(flavour, "city") is not None:
                found |= GEO
            elif geoip.flavoured(flavour, "country") is not None:
                found.add("country")
            if geoip.flavoured(flavour, "asn") is not None:
                found |= NETWORK
            return frozenset(found)
        if source == "iplocate":
            found = set()
            if self._database(IPLOCATE_FILES["country"]) is not None and self._exists(
                IPLOCATE_FILES["country"]
            ):
                found.add("country")
            if self._exists(IPLOCATE_FILES["asn"]):
                found |= NETWORK
            return frozenset(found)
        return BY_ID[source].provides if self.present(source) else frozenset()

    def _exists(self, name: str) -> bool:
        return self.directory is not None and (self.directory / name).exists()
