"""MaxMind-format IP databases (`.mmdb`) on disk, and the check of a claimed place against measurements.

The estimate itself is built from round-trip times only and is calibrated on real data; IP data does not
change it. What the IP data adds is a second opinion that can be *checked*: if it says the address is in
Tokyo and a node in Frankfurt heard from it in 4 ms, light cannot have made that trip, and the result says so
(`rule_out`).

MaxMind GeoLite2 and DB-IP Lite are `.mmdb` files in one layout, which `GeoIp` finds in a directory by their
names and opens again when the file changes, so an update needs no restart. They are two sources among
nine (see `ipdata/`): what they say is voted on with the rest, never preferred. Nothing is fetched on a request.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import maxminddb

from .estimate import KM_PER_RTT_MS_HARD, TOLERANCE_KM, haversine_km

log = logging.getLogger(__name__)

# How often a directory is looked at again for new or newer database files.
SCAN_EVERY_S = 10.0


@dataclass(frozen=True)
class GeoPlace:
    name: str
    country: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class RuledOut:
    """The node whose measurement makes a claimed place impossible."""

    node_id: str
    node_name: str
    rtt_ms: float
    distance_km: float
    max_distance_km: float


def flavour_of(name: str) -> str:
    """Whose database a file is, by its name: `maxmind` (GeoLite2 and GeoIP2, and anything else MaxMind-format), `dbip`, or
    `iplocate` or `ipinfo`, whose files have a layout of their own and are read by those sources only."""
    lowered = name.lower()
    if lowered.startswith(("geolite2", "geoip2")):
        return "maxmind"
    for own in ("iplocate", "ipinfo"):
        if lowered.startswith(own):
            return own
    return "dbip" if "dbip" in lowered or "db-ip" in lowered else "maxmind"


class _Database:
    """One .mmdb file, reopened when it changes on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._reader: maxminddb.Reader | None = None
        self._stamp: tuple[int, int] | None = None

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


class GeoIp:
    """The MaxMind-format databases of a directory (and any given by path), one of each kind per flavour.

    With a directory, the files in it whose name has `city`, `asn` or `country` in it (any case) are used, the newest of each
    flavour (`maxmind`, `dbip`), and the directory is looked at again from time to time: a first download after the server started
    is found without a restart, and so is a newer file. An explicit path is fixed.
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
        }
        # one file per (flavour, kind): the sources that are read apart (MaxMind's, DB-IP's)
        self._found: dict[tuple[str, str], _Database | None] = {}
        self._next_scan: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, directory: str | None, city: str | None, asn: str | None) -> GeoIp:
        """Explicit paths win; otherwise the newest files in the directory."""
        return cls(
            Path(city) if city else None,
            Path(asn) if asn else None,
            directory=Path(directory) if directory else None,
        )

    def rescan(self) -> None:
        """Look at the directory again at the next lookup (a download has just replaced a file)."""
        with self._lock:
            for key in self._next_scan:
                self._next_scan[key] = 0.0

    def flavoured(self, flavour: str, kind: str) -> _Database | None:
        """The database of this kind that belongs to one source (`maxmind` or `dbip`).

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
            if now >= self._next_scan.get(key, 0.0):
                self._next_scan[key] = now + self._scan_every_s
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
                current = self._found.get(key)
                if newest is None:
                    self._found[key] = None
                elif current is None or current.path != newest:
                    self._found[key] = _Database(newest)
            return self._found.get(key)


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
