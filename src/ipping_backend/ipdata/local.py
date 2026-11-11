"""The five sources that live on disk, read the same way whatever file they come from.

MaxMind and DB-IP are `.mmdb` files, read through `GeoIp` (which opens them again when they change); IPtoASN and IPdeny are
range tables (`ranges.py`); ip2region is a pair of `.xdb` files (`xdb.py`). A lookup here never touches the network.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..geolocation.geoip import GeoIp, _Database
from . import records
from .ranges import RangeDb, as_record
from .records import Record
from .xdb import XdbFile, record_of

IPTOASN_FILE = "iptoasn.sqlite"
IPDENY_FILE = "ipdeny.sqlite"
XDB_FILES = {4: "ip2region_v4.xdb", 6: "ip2region_v6.xdb"}
MMDB_FLAVOUR = {"maxmind": "maxmind", "dbip": "dbip"}


def _names(record: Any, *path: str) -> str | None:
    node: Any = record
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    names = node.get("names") if isinstance(node, dict) else None
    value = names.get("en") if isinstance(names, dict) else None
    return records.text(value)


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
    covering = prefix if geo else owner_prefix
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
        network=str(ipaddress.ip_network(f"{address}/{covering}", strict=False))
        if covering is not None
        else None,
    )


class LocalSources:
    def __init__(self, geoip_of: Callable[[], GeoIp], directory: Path | None) -> None:
        self._geoip_of = geoip_of
        self.directory = directory
        self._ranges: dict[str, RangeDb] = {}
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
        if source in ("iptoasn", "ipdeny"):
            table = self._table(source)
            return as_record(table.find(address)) if table is not None else None
        if source == "ip2region":
            version = ipaddress.ip_address(address).version
            reader = self._reader(version)
            return record_of(reader.find(address)) if reader is not None else None
        return None

    def _table(self, source: str) -> RangeDb | None:
        if self.directory is None:
            return None
        table = self._ranges.get(source)
        if table is None:
            name = IPTOASN_FILE if source == "iptoasn" else IPDENY_FILE
            table = self._ranges[source] = RangeDb(self.directory / name)
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
        if source in MMDB_FLAVOUR:
            geoip = self._geoip_of()
            for kind in ("city", "country", "asn"):
                database = geoip.flavoured(MMDB_FLAVOUR[source], kind)
                info = database.info() if database is not None else None
                if info is not None:
                    found.append(
                        {
                            "name": info["file"],
                            "size": info["size"],
                            "built": datetime.fromtimestamp(info["build_epoch"], UTC)
                            if info["build_epoch"]
                            else None,
                        }
                    )
        elif source in ("iptoasn", "ipdeny"):
            table = self._table(source)
            if table is not None and table.size():
                meta = table.meta()
                built = records.text(meta.get("built_at"), 40)
                found.append(
                    {
                        "name": table.path.name,
                        "size": table.size(),
                        "built": datetime.fromisoformat(built) if built else None,
                        "rows": int(meta["rows"]) if meta.get("rows", "").isdigit() else None,
                    }
                )
        elif source == "ip2region":
            for version in (4, 6):
                reader = self._reader(version)
                if reader is not None and reader.version is not None:
                    created = reader.created_at()
                    found.append(
                        {
                            "name": reader.path.name,
                            "size": reader.path.stat().st_size,
                            "built": datetime.fromtimestamp(created, UTC) if created else None,
                        }
                    )
        return found

    def present(self, source: str) -> bool:
        return bool(self.files(source))
