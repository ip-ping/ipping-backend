"""An optional cross-check of a measured location against a GeoIP / ASN database.

The estimate itself is built from round-trip times only and is calibrated on real data; a GeoIP database
does not change it. What a database adds is a second opinion that can be *checked*: if it says the
address is in Tokyo and a node in Frankfurt heard from it in 4 ms, light cannot have made that trip, and
the result says so. It also names the network (AS number and organisation).

Databases are MaxMind-format files (`.mmdb`): MaxMind GeoLite2 or DB-IP Lite (CC BY 4.0, which asks for
attribution, shown with the result). They are read from disk when configured and picked up again when
the file changes, so a monthly update needs no restart. Nothing is fetched on a request.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import maxminddb

from .estimate import KM_PER_RTT_MS_HARD, TOLERANCE_KM, haversine_km

log = logging.getLogger(__name__)

# How often a directory is looked at again for new or newer database files.
SCAN_EVERY_S = 10.0

ATTRIBUTIONS = {
    "dbip": "IP geolocation by DB-IP (db-ip.com), licensed CC BY 4.0",
    "maxmind": "This product includes GeoLite2 data created by MaxMind (maxmind.com)",
}


@dataclass(frozen=True)
class GeoPlace:
    name: str
    country: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class AsnInfo:
    number: int
    organization: str


@dataclass(frozen=True)
class RuledOut:
    """The node whose measurement makes a claimed place impossible."""

    node_id: str
    node_name: str
    rtt_ms: float
    distance_km: float
    max_distance_km: float


def _first(*values: Any) -> Any:
    return next((v for v in values if v), None)


def flavour_of(name: str) -> str:
    """Whose database a file is, by its name: `maxmind` (GeoLite2 and GeoIP2, and anything else MaxMind-format) or `dbip`."""
    lowered = name.lower()
    if lowered.startswith(("geolite2", "geoip2")):
        return "maxmind"
    return "dbip" if "dbip" in lowered or "db-ip" in lowered else "maxmind"


class _Database:
    """One .mmdb file, reopened when it changes on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._reader: maxminddb.Reader | None = None
        self._stamp: tuple[int, int] | None = None

    def get(self, ip: str) -> dict[str, Any] | None:
        return self.get_with_prefix(ip)[0]

    def get_with_prefix(self, ip: str) -> tuple[dict[str, Any] | None, int | None]:
        """The record for the address, and the length of the network it covers (in the address's own family)."""
        reader = self._current()
        if reader is None:
            return None, None
        try:
            record, prefix = reader.get_with_prefix_len(ip)
        except (ValueError, maxminddb.InvalidDatabaseError):
            return None, None
        if not isinstance(record, dict):
            return None, None
        # an IPv4 address in an IPv6 tree reports its prefix counted from the start of the 128 bits
        if "." in ip and prefix > 32:
            prefix -= 96
        return record, prefix

    def info(self) -> dict[str, Any] | None:
        """What the file says about itself: its type, when it was built, its size."""
        reader = self._current()
        if reader is None:
            return None
        meta = reader.metadata()
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "file": self.path.name,
            "type": meta.database_type,
            "build_epoch": meta.build_epoch,
            "size": size,
        }

    def _current(self) -> maxminddb.Reader | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._reader is None or stamp != self._stamp:
                try:
                    new = maxminddb.open_database(str(self.path))
                except (OSError, ValueError, maxminddb.InvalidDatabaseError):
                    log.warning("could not open GeoIP database %s", self.path)
                    return self._reader
                old, self._reader, self._stamp = self._reader, new, stamp
                if old is not None:
                    old.close()
            return self._reader

    @property
    def description(self) -> str:
        reader = self._current()
        return reader.metadata().database_type if reader else ""


class GeoIp:
    """The configured databases (either, both or neither).

    With a directory, the files in it whose name has `city`, `asn` or `country` in it (any case) are used, MaxMind's own
    (GeoLite2-City.mmdb) before others and then the newest, and the directory is looked at again from time to time: a
    first download after the server started is found without a restart, and so is a newer file. An explicit path is
    fixed.
    """

    def __init__(
        self,
        city: Path | None = None,
        asn: Path | None = None,
        *,
        directory: Path | None = None,
        scan_every_s: float = SCAN_EVERY_S,
    ) -> None:
        self._dir = directory
        self._scan_every_s = scan_every_s
        self._fixed: dict[str, _Database | None] = {
            "city": _Database(city) if city else None,
            "asn": _Database(asn) if asn else None,
            "country": None,
        }
        self._found: dict[str, _Database | None] = {"city": None, "asn": None, "country": None}
        self._next_scan = {"city": 0.0, "asn": 0.0, "country": 0.0}
        # one file per (flavour, kind) for the sources that are read apart (MaxMind's, DB-IP's)
        self._found_flavoured: dict[tuple[str, str], _Database | None] = {}
        self._next_scan_flavoured: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        # Whether a flavour (`maxmind`, `dbip`) may be used: the administrator can turn a source off
        self.enabled: Callable[[str], bool] = lambda flavour: True

    @classmethod
    def from_settings(cls, directory: str | None, city: str | None, asn: str | None) -> GeoIp:
        """Explicit paths win; otherwise the newest files in the directory."""
        return cls(
            Path(city) if city else None,
            Path(asn) if asn else None,
            directory=Path(directory) if directory else None,
        )

    def _best(self, kind: str) -> Path | None:
        """The file to use for this kind: MaxMind's own (GeoLite2, GeoIP2) before any other, then the newest."""
        assert self._dir is not None  # noqa: S101
        best: tuple[bool, float] | None = None
        chosen: Path | None = None
        for path in self._dir.iterdir():
            name = path.name.lower()
            if not name.endswith(".mmdb") or kind not in name or not self.enabled(flavour_of(name)):
                continue
            try:
                rank = (name.startswith(("geolite2", "geoip2")), path.stat().st_mtime)
            except OSError:
                continue
            if best is None or rank > best:
                best, chosen = rank, path
        return chosen

    def rescan(self) -> None:
        """Look at the directory again at the next lookup (a download has just replaced a file)."""
        with self._lock:
            for kind in self._next_scan:
                self._next_scan[kind] = 0.0
            for key in self._next_scan_flavoured:
                self._next_scan_flavoured[key] = 0.0

    def flavoured(self, flavour: str, kind: str) -> _Database | None:
        """The database of this kind that belongs to one source (`maxmind` or `dbip`), whether or not it is the one in use.

        An explicit path counts for the flavour its name says; in a directory it is the newest file of that flavour.
        """
        fixed = self._fixed.get(kind)
        if fixed is not None and flavour_of(fixed.path.name) == flavour:
            return fixed
        if self._dir is None:
            return None
        key = (flavour, kind)
        now = time.monotonic()
        with self._lock:
            if now >= self._next_scan_flavoured.get(key, 0.0):
                self._next_scan_flavoured[key] = now + self._scan_every_s
                newest: Path | None = None
                newest_at = -1.0
                try:
                    for path in self._dir.iterdir():
                        name = path.name.lower()
                        if name.endswith(".mmdb") and kind in name and flavour_of(name) == flavour:
                            try:
                                modified = path.stat().st_mtime
                            except OSError:
                                continue
                            if modified > newest_at:
                                newest, newest_at = path, modified
                except OSError:
                    newest = None
                current = self._found_flavoured.get(key)
                if newest is None:
                    self._found_flavoured[key] = None
                elif current is None or current.path != newest:
                    self._found_flavoured[key] = _Database(newest)
            return self._found_flavoured.get(key)

    def _database(self, kind: str) -> _Database | None:
        fixed = self._fixed[kind]
        if fixed is not None or self._dir is None:
            return fixed if fixed is None or self.enabled(flavour_of(fixed.path.name)) else None
        now = time.monotonic()
        with self._lock:
            if now >= self._next_scan[kind]:
                self._next_scan[kind] = now + self._scan_every_s
                try:
                    newest = self._best(kind)
                except OSError:
                    newest = None
                current = self._found[kind]
                if newest is None:
                    self._found[kind] = None
                elif current is None or current.path != newest:
                    self._found[kind] = _Database(newest)
            return self._found[kind]

    @property
    def _country(self) -> _Database | None:
        return self._database("country")

    def databases(self) -> list[dict[str, Any]]:
        """The databases in use: which file, what it is, when it was built."""
        found = []
        for kind in ("city", "country", "asn"):
            database = self._database(kind)
            info = database.info() if database is not None else None
            if info is not None:
                found.append({"kind": kind, **info})
        return found

    def record(self, kind: str, ip: str) -> tuple[dict[str, Any] | None, int | None]:
        """The raw record of the `city`, `country` or `asn` database for an address, with its network's prefix length."""
        database = self._database(kind)
        return database.get_with_prefix(ip) if database is not None else (None, None)

    @property
    def _city(self) -> _Database | None:
        return self._database("city")

    @property
    def _asn(self) -> _Database | None:
        return self._database("asn")

    @property
    def has_city(self) -> bool:
        db = self._city
        return db is not None and db.path.exists()

    @property
    def has_asn(self) -> bool:
        db = self._asn
        return db is not None and db.path.exists()

    @property
    def _flavour(self) -> str:
        names = [str(db.path).lower() for db in (self._city, self._asn) if db is not None]
        return "dbip" if any("dbip" in n for n in names) else "maxmind"

    @property
    def attribution(self) -> str | None:
        return ATTRIBUTIONS[self._flavour] if self.has_city or self.has_asn else None

    @property
    def source(self) -> str:
        return "DB-IP Lite" if self._flavour == "dbip" else "MaxMind GeoLite2"

    def place(self, ip: str) -> GeoPlace | None:
        database = self._city
        record = database.get(ip) if database else None
        if not record:
            return None
        location = record.get("location") or {}
        lat, lon = location.get("latitude"), location.get("longitude")
        if lat is None or lon is None:
            return None
        names = (record.get("city") or {}).get("names") or {}
        country = record.get("country") or {}
        name = _first(names.get("en"), (country.get("names") or {}).get("en"), "Unknown")
        return GeoPlace(
            name=str(name)[:80],
            country=str(country.get("iso_code") or "")[:2],
            latitude=float(lat),
            longitude=float(lon),
        )

    def asn(self, ip: str) -> AsnInfo | None:
        database = self._asn
        record = database.get(ip) if database else None
        if not record or not record.get("autonomous_system_number"):
            return None
        return AsnInfo(
            number=int(record["autonomous_system_number"]),
            organization=str(record.get("autonomous_system_organization") or "")[:120],
        )


def valid_ip(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def rule_out(
    place: GeoPlace, evidence: list[tuple[str, str, float, float, float]]
) -> RuledOut | None:
    """The node that makes `place` impossible, if any.

    `evidence` is (node id, name, latitude, longitude, round-trip ms) of nodes whose position is
    verified. A node that heard from the target after `r` ms cannot be further from it than 150 km per
    round-trip millisecond, whatever the path, so a place beyond that (with the position tolerance the
    estimate uses) is ruled out. The node with the largest excess is named.
    """
    worst: RuledOut | None = None
    worst_excess = 0.0
    for node_id, name, lat, lon, rtt in evidence:
        distance = haversine_km(lat, lon, place.latitude, place.longitude)
        limit = rtt * KM_PER_RTT_MS_HARD + TOLERANCE_KM
        if distance - limit > worst_excess:
            worst_excess = distance - limit
            worst = RuledOut(node_id, name, round(rtt, 3), distance, limit)
    return worst
