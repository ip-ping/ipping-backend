"""Tables of address ranges with a place for each range (ipapi.is, IP2Location LITE), as a SQLite file.

Both services publish a free geolocation database as a CSV file: a range of addresses and the country, region, city, postal
code and position of it (and, for ipapi.is, the time zone). Those files are large (4.8 million IPv4 ranges for ipapi.is), so the
table keeps each place once however many ranges share it (93 000 places for those 4.8 million ranges) and a range is a start, an
end and a place number.

The ipapi.is ranges overlap: a /22 holds a /24 that says something else, and some ranges overlap only in part. A table that
answers "the last range that starts at or before this address" cannot cope with that, so the table is built flat. Wherever ranges
overlap the narrowest one wins (the later one in the file when they are equally wide), which is the most specific thing the
file says about an address, and what is stored does not overlap.

Nothing is unpacked to disk and nothing is held in memory but the places and the ranges currently open: the CSV is read through
the zip file, a line at a time.
"""

from __future__ import annotations

import csv
import heapq
import io
import socket
import sqlite3
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import records
from .ranges import RangeError, Table

# country, region, city, postal code, latitude, longitude, time zone
Place = tuple[
    str | None, str | None, str | None, str | None, float | None, float | None, str | None
]
Span = tuple[int, int, int]  # start, end (both included) as numbers, and the place's number

SCHEMA = """
CREATE TABLE ranges (
    family INTEGER NOT NULL,
    start BLOB NOT NULL,
    "end" BLOB NOT NULL,
    place INTEGER NOT NULL,
    PRIMARY KEY (family, start)
) WITHOUT ROWID;
CREATE TABLE places (
    id INTEGER PRIMARY KEY,
    country TEXT,
    region TEXT,
    city TEXT,
    postal TEXT,
    latitude REAL,
    longitude REAL,
    time_zone TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

MAX_ROWS = 12_000_000
MAX_CSV_BYTES = 3_000_000_000
BATCH = 20_000
_BEYOND_ANY_ADDRESS = 1 << 130


class _Unsorted(Exception):
    """The ranges did not come in order of their start."""


# ---- Places ----------------------------------------------------------------------------------------------------------


class PlaceTable:
    """The distinct places of a table, numbered from 1. Cleaning a place is not cheap, so a place is cleaned once per distinct row."""

    def __init__(self) -> None:
        self._by_raw: dict[tuple[str, ...], int] = {}
        self._by_place: dict[Place, int] = {}
        self.pending: list[tuple[Any, ...]] = []  # rows not yet written to the file

    def intern(self, raw: tuple[str, ...], clean: Callable[[tuple[str, ...]], Place]) -> int:
        found = self._by_raw.get(raw)
        if found is not None:
            return found
        place = clean(raw)
        number = self._by_place.get(place)
        if number is None:
            number = self._by_place[place] = len(self._by_place) + 1
            self.pending.append((number, *place))
        self._by_raw[raw] = number
        return number

    def __len__(self) -> int:
        return len(self._by_place)


# An address family and the function that reads its ranges, given the table to put their places in
Passes = Sequence[tuple[int, Callable[[PlaceTable], Iterator[Span]]]]


def _place(row: tuple[str, ...]) -> Place:
    """(country, region, city, postal code, time zone, latitude, longitude) as written in the CSV, cleaned."""
    country, region, city, postal, zone, lat, lon = row
    latitude, longitude = records.position(lat, lon)
    return (
        records.country(country),
        records.text(region),
        records.text(city),
        records.text(postal, 20),
        latitude,
        longitude,
        records.time_zone(zone),
    )


# ---- Flattening ------------------------------------------------------------------------------------------------------


class _Sweep:
    """Ranges in order of their start go in; ranges that do not overlap come out, the narrowest of any that overlap winning."""

    def __init__(self) -> None:
        self.open: list[tuple[int, int, int, int]] = []  # (width, -order, end, place)
        self.position = 0  # every address below this one is decided
        self.run: tuple[int, int, int] | None = (
            None  # the last range, not handed out yet: a neighbour with the same place joins it
        )
        self.out: list[tuple[int, int, int]] = []

    def add(self, start: int, end: int, place: int, order: int) -> None:
        self._decide_below(start)
        heapq.heappush(self.open, (end - start, -order, end, place))

    def finish(self) -> None:
        self._decide_below(_BEYOND_ANY_ADDRESS)
        if self.run is not None:
            self.out.append(self.run)
            self.run = None

    def _decide_below(self, limit: int) -> None:
        open_ranges = self.open
        while self.position < limit and open_ranges:
            _, _, end, place = open_ranges[0]
            if end < self.position:
                heapq.heappop(open_ranges)
                continue
            stop = min(end, limit - 1)
            self._emit(self.position, stop, place)
            self.position = stop + 1
        if self.position < limit < _BEYOND_ANY_ADDRESS:
            self.position = limit  # nothing says anything about the addresses in between

    def _emit(self, start: int, end: int, place: int) -> None:
        run = self.run
        if run is not None and run[1] + 1 == start and run[2] == place:
            self.run = (run[0], end, place)
            return
        if run is not None:
            self.out.append(run)
        self.run = (start, end, place)


def flatten(spans: Iterable[Span]) -> Iterator[Span]:
    """`spans` in order of start; the same addresses as ranges that do not overlap (the narrowest of those that did overlap wins)."""
    sweep = _Sweep()
    for order, (start, end, place) in enumerate(spans):
        sweep.add(start, end, place, order)
        if sweep.out:
            yield from sweep.out
            sweep.out.clear()
    sweep.finish()
    yield from sweep.out


def _in_order(spans: Iterable[Span]) -> Iterator[Span]:
    previous = -1
    for span in spans:
        if span[0] < previous:
            raise _Unsorted
        previous = span[0]
        yield span


def _sorted_on_disk(spans: Iterable[Span], width: int, scratch: Path) -> Iterator[Span]:
    """The same spans in order of start, sorted in a scratch file (for a file that did not come in order)."""
    scratch.unlink(missing_ok=True)
    connection = sqlite3.connect(scratch)
    try:
        connection.executescript(
            "PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;"
            "CREATE TABLE spans (start BLOB NOT NULL, n INTEGER NOT NULL, stop BLOB NOT NULL, place INTEGER NOT NULL,"
            " PRIMARY KEY (start, n)) WITHOUT ROWID;"
        )
        batch: list[tuple[bytes, int, bytes, int]] = []
        for n, (start, end, place) in enumerate(spans):
            batch.append((start.to_bytes(width, "big"), n, end.to_bytes(width, "big"), place))
            if len(batch) >= BATCH:
                connection.executemany("INSERT INTO spans VALUES (?,?,?,?)", batch)
                batch.clear()
        connection.executemany("INSERT INTO spans VALUES (?,?,?,?)", batch)
        for start_bytes, _, end_bytes, place in connection.execute(
            "SELECT start, n, stop, place FROM spans ORDER BY start, n"
        ):
            yield int.from_bytes(start_bytes, "big"), int.from_bytes(end_bytes, "big"), place
    finally:
        connection.close()
        scratch.unlink(missing_ok=True)


# ---- Building --------------------------------------------------------------------------------------------------------


def build(path: Path, passes: Passes, meta: dict[str, str]) -> int:
    """Write a table of `passes` (an address family and a function that reads its ranges) to a new file; returns how many ranges.

    Each function is given the `PlaceTable` to put its places in, and returns the ranges as (start, end, place number).
    """
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + SCHEMA)
        table = PlaceTable()
        total = 0
        for family, read in passes:
            width = 4 if family == 4 else 16
            try:
                total += _write(connection, family, width, flatten(_in_order(read(table))), table)
            except (
                _Unsorted
            ):  # not the usual case: sort the file's ranges first, and write this family again
                connection.execute("DELETE FROM ranges WHERE family = ?", (family,))
                scratch = path.with_name(f".{path.name}.sort")
                total += _write(
                    connection,
                    family,
                    width,
                    flatten(_sorted_on_disk(read(table), width, scratch)),
                    table,
                )
            if total > MAX_ROWS:
                raise RangeError("more ranges than any table of this kind has")
        connection.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [*meta.items(), ("rows", str(total)), ("places", str(len(table)))],
        )
        connection.commit()
    finally:
        connection.close()
    return total


def _write(
    connection: sqlite3.Connection,
    family: int,
    width: int,
    flat: Iterable[Span],
    table: PlaceTable,
) -> int:
    batch: list[tuple[int, bytes, bytes, int]] = []
    count = 0
    for start, end, place in flat:
        batch.append((family, start.to_bytes(width, "big"), end.to_bytes(width, "big"), place))
        count += 1
        if len(batch) >= BATCH:
            _insert(connection, table, batch)
            batch.clear()
        if count > MAX_ROWS:
            raise RangeError("more ranges than any table of this kind has")
    _insert(connection, table, batch)
    return count


def _insert(
    connection: sqlite3.Connection, table: PlaceTable, batch: list[tuple[int, bytes, bytes, int]]
) -> None:
    if table.pending:
        connection.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?,?)", table.pending)
        table.pending.clear()
    connection.executemany("INSERT INTO ranges VALUES (?,?,?,?)", batch)


# ---- The two CSV files -----------------------------------------------------------------------------------------------


class _Capped(io.RawIOBase):
    """A binary stream that refuses to give more than `limit` bytes (a zip file can say it holds less than it does)."""

    def __init__(self, inner: Any, limit: int) -> None:
        self._inner, self._left = inner, limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        got = self._inner.readinto(buffer) or 0
        self._left -= got
        if self._left < 0:
            raise RangeError("The CSV file is larger than any file of this kind should be.")
        return int(got)


@contextmanager
def csv_lines(archive: Path) -> Iterator[Iterator[str]]:
    """The lines of the one CSV file in a zip file, read where they are: nothing is unpacked."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = [
                i
                for i in bundle.infolist()
                if i.filename.lower().endswith(".csv") and not i.is_dir()
            ]
            if not members:
                raise RangeError("The zip file holds no CSV file.")
            member = max(members, key=lambda i: i.file_size)
            if member.file_size > MAX_CSV_BYTES:
                raise RangeError("The CSV file is larger than any file of this kind should be.")
            with bundle.open(member) as raw:
                capped = io.BufferedReader(_Capped(raw, MAX_CSV_BYTES), 1 << 20)
                yield iter(io.TextIOWrapper(capped, encoding="utf-8", errors="replace", newline=""))
    except zipfile.BadZipFile as exc:
        raise RangeError("The file is not a zip file.") from exc


IPAPI_COLUMNS = (
    "start_ip",
    "end_ip",
    "country_code",
    "state",
    "city",
    "zip",
    "timezone",
    "latitude",
    "longitude",
)


def ipapi_spans(lines: Iterable[str], table: PlaceTable, family: int) -> Iterator[Span]:
    """The ipapi.is free geolocation CSV: `ip_version,start_ip,end_ip,continent,country_code,country,state,city,zip,timezone,latitude,longitude,...`."""
    reader = csv.reader(lines)
    header = next(reader, None)
    try:
        at = {name: (header or []).index(name) for name in IPAPI_COLUMNS}
    except ValueError as exc:
        raise RangeError("This is not the ipapi.is geolocation CSV.") from exc
    pton_family = socket.AF_INET if family == 4 else socket.AF_INET6
    columns = (
        at["country_code"], at["state"], at["city"], at["zip"], at["timezone"], at["latitude"], at["longitude"]
    )  # fmt: skip
    first, last = at["start_ip"], at["end_ip"]
    for row in reader:
        try:
            start = int.from_bytes(socket.inet_pton(pton_family, row[first]), "big")
            end = int.from_bytes(socket.inet_pton(pton_family, row[last]), "big")
            raw = tuple(row[i] for i in columns)
        except (OSError, ValueError, IndexError):  # not a range of this family
            continue
        if end >= start:
            yield start, end, table.intern(raw, _place)


def ip2location_spans(lines: Iterable[str], table: PlaceTable, family: int) -> Iterator[Span]:
    """IP2Location LITE DB11: `"ip_from","ip_to","country_code","country_name","region_name","city_name","latitude","longitude","zip_code","time_zone"`, no header.

    The IPv6 file holds the IPv4 ranges as IPv4-mapped IPv6 ones (and may hold more at the bottom of the space): the IPv4 file has
    those, so they are left out of the IPv6 table.
    """
    for row in csv.reader(lines):
        try:
            start, end = int(row[0]), int(row[1])
            raw = (row[2], row[4], row[5], row[8], row[9], row[6], row[7])
        except (ValueError, IndexError):
            continue
        if family == 4:
            if end >= 1 << 32:
                continue
        elif start < 1 << 32 or start >> 32 == 0xFFFF:
            continue
        if end >= start:
            yield start, end, table.intern(raw, _place)


# ---- Reading ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Located:
    start: bytes
    end: bytes
    country: str | None
    region: str | None
    city: str | None
    postal_code: str | None
    latitude: float | None
    longitude: float | None
    time_zone: str | None


class PlaceDb(Table[Located]):
    """The place of a range, from a table `build` wrote."""

    _PROBE = "SELECT 1 FROM places LIMIT 1"
    _SELECT = (
        'SELECT r.start, r."end", p.country, p.region, p.city, p.postal, p.latitude, p.longitude, p.time_zone '
        'FROM (SELECT start, "end", place FROM ranges WHERE family = ? AND start <= ? ORDER BY start DESC LIMIT 1) AS r '
        "JOIN places AS p ON p.id = r.place"
    )

    def _found(self, row: tuple[Any, ...]) -> Located:
        return Located(*row)


def place_record(found: Located | None) -> records.Record | None:
    if found is None:
        return None
    return records.Record(
        country=found.country,
        region=found.region,
        city=found.city,
        postal_code=found.postal_code,
        latitude=found.latitude,
        longitude=found.longitude,
        time_zone=found.time_zone,
    )
