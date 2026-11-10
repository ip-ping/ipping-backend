"""Geofeeds (RFC 8805): where the operator of a network says its addresses are.

A geofeed is a CSV file a network publishes at a URL of its choosing: `prefix,country,region,city,postal`. This is
the one source of a place that comes from the network's owner rather than from a vendor's guess, so it is shown next
to the GeoIP database's, and checked against the measurements the same way. It never changes the estimate.

Opt-in and bounded, because the files are fetched from servers somebody else runs:

* only the URLs the administrator lists are fetched (`IPPING_GEOFEED_URLS`, or `--url`), by `ipping-backend
  geofeed-update`, never while serving a request and never because a measurement named an address. Nothing about a
  measurement's target leaves the server;
* https only, redirects only to https, a host whose addresses are all public, a size limit and a time limit;
* a line that is not a valid, public, not-absurdly-broad prefix with a two-letter country is skipped, not trusted.

What a feed says is a claim: the cross-check can rule a place out (a node heard from the address too soon for it to
be there) but cannot confirm it.
"""

from __future__ import annotations

import csv
import gzip
import io
import ipaddress
import re
import socket
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

from .gazetteer import City, cities
from .geoip import GeoPlace

FILE_NAME = "geofeeds.csv.gz"
# The most one feed may be: larger files exist (a few operators publish hundreds of thousands of lines) but a
# self-hosted backend loads every line into memory in every worker.
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES_PER_FEED = 500_000
MAX_ENTRIES_TOTAL = 1_000_000
FETCH_TIMEOUT_S = 30
# A feed that claims the whole internet is wrong, not generous.
MIN_PREFIX_LENGTH = {4: 8, 6: 16}

_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_REGION = re.compile(r"[A-Z]{2}-[A-Z0-9]{1,3}\Z")
_TEXT = re.compile(r"[\x20-\x7e -￿]*\Z")


@dataclass(frozen=True, slots=True)
class Entry:
    country: str  # ISO 3166-1 alpha-2, or "" when the operator left it out
    region: str  # ISO 3166-2 such as "DE-HE", or ""
    city: str
    source: str  # the URL of the feed it came from


def _text(value: str, limit: int) -> str:
    value = value.strip()
    return value[:limit] if _TEXT.match(value) else ""


def parse(
    text: str, source: str
) -> tuple[list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, Entry]], int]:
    """The entries of a feed, and how many lines were skipped (comments and blank lines are not counted)."""
    entries: list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, Entry]] = []
    skipped = 0
    lines = (
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    for row in csv.reader(lines):
        if len(entries) >= MAX_ENTRIES_PER_FEED:
            skipped += 1
            continue
        try:
            network = ipaddress.ip_network(row[0].strip(), strict=True)
        except (ValueError, IndexError):
            skipped += 1
            continue
        if network.prefixlen < MIN_PREFIX_LENGTH[network.version] or not network.is_global:
            skipped += 1
            continue
        country = row[1].strip().upper() if len(row) > 1 else ""
        if country and not _COUNTRY.match(country):
            skipped += 1
            continue
        region = row[2].strip().upper() if len(row) > 2 else ""
        if region and not _REGION.match(region):
            region = ""  # a bad region does not make the country and city wrong
        city = _text(row[3], 80) if len(row) > 3 else ""
        if not (country or city):
            skipped += 1  # "do not geolocate" lines say nothing
            continue
        entries.append((network, Entry(country, region, city, source)))
    return entries, skipped


class Table:
    """Prefixes to entries; the longest prefix that holds an address wins."""

    def __init__(self) -> None:
        self._by_length: dict[tuple[int, int], dict[int, tuple[Entry, str]]] = {}
        self._lengths: dict[int, list[int]] = {4: [], 6: []}
        self.size = 0

    def add(self, network: ipaddress.IPv4Network | ipaddress.IPv6Network, entry: Entry) -> None:
        key = (network.version, network.prefixlen)
        bucket = self._by_length.get(key)
        if bucket is None:
            bucket = self._by_length[key] = {}
            self._lengths[network.version] = sorted(
                [*self._lengths[network.version], network.prefixlen], reverse=True
            )
        address = int(network.network_address)
        if address not in bucket:
            self.size += 1
        bucket[address] = (entry, str(network))

    def lookup(self, ip: str) -> tuple[Entry, str] | None:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return None
        bits = address.max_prefixlen
        value = int(address)
        for length in self._lengths[address.version]:
            shift = bits - length
            found = self._by_length[(address.version, length)].get((value >> shift) << shift)
            if found is not None:
                return found
        return None


# ---------------------------------------------------------------------------------------------
# Storage: one file with every feed's lines, replaced atomically
# ---------------------------------------------------------------------------------------------


def write(path: Path, rows: list[tuple[str, Entry]]) -> None:
    """`rows` are (prefix, entry). Written to a temporary file and moved into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as out:
        temporary = Path(out.name)
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            for prefix, entry in rows:
                writer.writerow([prefix, entry.country, entry.region, entry.city, entry.source])
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read(path: Path) -> list[tuple[str, Entry]]:
    rows: list[tuple[str, Entry]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            for row in csv.reader(handle):
                if len(row) == 5:
                    rows.append((row[0], Entry(row[1], row[2], row[3], row[4])))
    except (OSError, EOFError):
        return []
    return rows


def host_of(source: str) -> str:
    return urlsplit(source).hostname or source[:60]


# ---------------------------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------------------------

Opener = Callable[[str], IO[bytes]]


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not newurl.lower().startswith("https://"):
            raise urllib.error.URLError("a redirect to something other than https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_host(host: str) -> None:
    """Refuse a host that is, or resolves to, anything but public addresses."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"{host} does not resolve") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"{host} is not a public host")


def _open(url: str) -> IO[bytes]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("only https addresses are fetched")
    check_host(parts.hostname)
    request = urllib.request.Request(  # noqa: S310 - https checked above
        url, headers={"User-Agent": "ipping-backend geofeed-update", "Accept": "text/csv, */*"}
    )
    response: IO[bytes] = urllib.request.build_opener(_HttpsOnly).open(
        request, timeout=FETCH_TIMEOUT_S
    )
    return response


def fetch(url: str, opener: Opener = _open) -> str:
    """The feed as text. Raises if it cannot be fetched, is too long, or is not text."""
    with opener(url) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"longer than {MAX_BYTES // 1_048_576} MB")
    if data[:2] == b"\x1f\x8b":  # some publishers serve it compressed regardless of what was asked
        data = gzip.GzipFile(fileobj=io.BytesIO(data)).read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError(f"longer than {MAX_BYTES // 1_048_576} MB when unpacked")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("not UTF-8 text") from exc


@dataclass(frozen=True, slots=True)
class FeedResult:
    url: str
    entries: int
    skipped: int
    error: str | None = None
    kept_previous: bool = False


def update(path: Path, urls: list[str], opener: Opener = _open) -> list[FeedResult]:
    """Fetch every feed and replace the stored file. A feed that fails keeps what it had; a feed that is no longer
    listed is dropped."""
    previous = read(path)
    results: list[FeedResult] = []
    rows: list[tuple[str, Entry]] = []
    for url in dict.fromkeys(urls):
        try:
            entries, skipped = parse(fetch(url, opener), url)
            if not entries:
                raise ValueError("no usable lines in it")
        except (OSError, ValueError, EOFError) as exc:  # URLError is an OSError
            old = [(p, e) for p, e in previous if e.source == url]
            rows.extend(old)
            results.append(FeedResult(url, len(old), 0, str(exc)[:200], kept_previous=bool(old)))
            continue
        rows.extend((str(network), entry) for network, entry in entries)
        results.append(FeedResult(url, len(entries), skipped))
    if len(rows) > MAX_ENTRIES_TOTAL:
        raise ValueError(
            f"{len(rows)} lines in all, more than the {MAX_ENTRIES_TOTAL} that are kept: list fewer feeds"
        )
    write(path, rows)
    return results


# ---------------------------------------------------------------------------------------------
# Use
# ---------------------------------------------------------------------------------------------


def _index() -> dict[tuple[str, str], City]:
    best: dict[tuple[str, str], City] = {}
    for city in cities():
        key = (city.country, city.name.lower())
        if key not in best or city.population > best[key].population:
            best[key] = city
    return best


class Geofeeds:
    """The stored feeds, loaded from `path` and loaded again when the file is replaced."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._stamp: tuple[int, int] | None = None
        self._table = Table()
        self._cities: dict[tuple[str, str], City] | None = None

    def _current(self) -> Table:
        if self._path is None:
            return self._table
        try:
            stat = self._path.stat()
        except OSError:
            if self._stamp is not None:
                self._stamp, self._table = None, Table()
            return self._table
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self._stamp:
            table = Table()
            for prefix, entry in read(self._path):
                try:
                    table.add(ipaddress.ip_network(prefix), entry)
                except ValueError:
                    continue
            self._table, self._stamp = table, stamp
        return self._table

    @property
    def entries(self) -> int:
        return self._current().size

    def lookup(self, ip: str) -> tuple[Entry, str] | None:
        return self._current().lookup(ip)

    def place(self, entry: Entry) -> GeoPlace | None:
        """Where the entry's city is, if the gazetteer knows it (a country alone is no place to check against)."""
        if not (entry.country and entry.city):
            return None
        if self._cities is None:
            self._cities = _index()
        wanted = entry.city.lower()
        city = self._cities.get((entry.country, wanted))
        if (
            city is None
        ):  # "Frankfurt" for "Frankfurt am Main": the biggest city of that country that starts with it
            starting = [
                c
                for (country, name), c in self._cities.items()
                if country == entry.country and name.startswith(wanted + " ")
            ]
            city = max(starting, key=lambda c: c.population, default=None)
        if city is None:
            return None
        return GeoPlace(
            name=city.name, country=city.country, latitude=city.latitude, longitude=city.longitude
        )
