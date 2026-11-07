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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import maxminddb

from .estimate import KM_PER_RTT_MS_HARD, TOLERANCE_KM, haversine_km

log = logging.getLogger(__name__)

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


class _Database:
    """One .mmdb file, reopened when it changes on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._reader: maxminddb.Reader | None = None
        self._stamp: tuple[int, int] | None = None

    def get(self, ip: str) -> dict[str, Any] | None:
        reader = self._current()
        if reader is None:
            return None
        try:
            record = reader.get(ip)
        except (ValueError, maxminddb.InvalidDatabaseError):
            return None
        return record if isinstance(record, dict) else None

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
    """The configured databases (either, both or neither)."""

    def __init__(self, city: Path | None = None, asn: Path | None = None) -> None:
        self._city = _Database(city) if city else None
        self._asn = _Database(asn) if asn else None
        self._flavour = (
            "dbip" if any("dbip" in str(p).lower() for p in (city, asn) if p) else "maxmind"
        )

    @classmethod
    def from_settings(cls, directory: str | None, city: str | None, asn: str | None) -> GeoIp:
        """Explicit paths win; otherwise the newest `*city*.mmdb` / `*asn*.mmdb` in the directory."""

        def newest(pattern: str) -> Path | None:
            if not directory:
                return None
            found = sorted(Path(directory).glob(pattern), key=lambda p: p.stat().st_mtime)
            return found[-1] if found else None

        return cls(
            Path(city) if city else newest("*city*.mmdb"),
            Path(asn) if asn else newest("*asn*.mmdb"),
        )

    @property
    def has_city(self) -> bool:
        return self._city is not None and self._city.path.exists()

    @property
    def has_asn(self) -> bool:
        return self._asn is not None and self._asn.path.exists()

    @property
    def attribution(self) -> str | None:
        return ATTRIBUTIONS[self._flavour] if self.has_city or self.has_asn else None

    @property
    def source(self) -> str:
        return "DB-IP Lite" if self._flavour == "dbip" else "MaxMind GeoLite2"

    def place(self, ip: str) -> GeoPlace | None:
        record = self._city.get(ip) if self._city else None
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
        record = self._asn.get(ip) if self._asn else None
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
