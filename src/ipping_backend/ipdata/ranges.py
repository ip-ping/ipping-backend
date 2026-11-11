"""Tables of address ranges (IPtoASN, IPdeny) as a small SQLite file, and the two readers that fill them.

A range table is a list of `start .. end` address ranges that do not overlap, each with a country and, for IPtoASN, an AS
number and its name. SQLite keeps it off the heap (700 000 ranges are about 40 MB on disk) and answers "which range holds
this address" from its index: the last range that starts at or before the address, then a look at where it ends.

Both tables are built from public data, into a temporary file that is checked before it replaces the old one.
"""

from __future__ import annotations

import gzip
import io
import ipaddress
import re
import sqlite3
import tarfile
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .records import Record, asn, country, text

SCHEMA = """
CREATE TABLE ranges (
    family INTEGER NOT NULL,
    start BLOB NOT NULL,
    "end" BLOB NOT NULL,
    asn INTEGER,
    country TEXT,
    name TEXT,
    PRIMARY KEY (family, start)
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

MAX_ROWS = 5_000_000
Row = tuple[int, bytes, bytes, int | None, str | None, str | None]


class RangeError(ValueError):
    """Data that is not a usable range table."""


def build(path: Path, rows: Iterable[Row], meta: dict[str, str]) -> int:
    """Write the rows into a new SQLite file at `path`; returns how many ranges it holds."""
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        batch: list[Row] = []
        count = 0
        for row in rows:
            batch.append(row)
            count += 1
            if count > MAX_ROWS:
                raise RangeError("more ranges than any table of this kind has")
            if len(batch) >= 20_000:
                connection.executemany("INSERT OR IGNORE INTO ranges VALUES (?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            connection.executemany("INSERT OR IGNORE INTO ranges VALUES (?,?,?,?,?,?)", batch)
        connection.executemany(
            "INSERT INTO meta VALUES (?, ?)", [*meta.items(), ("rows", str(count))]
        )
        connection.commit()
    finally:
        connection.close()
    return count


@dataclass(frozen=True)
class Found:
    asn: int | None
    country: str | None
    name: str | None
    start: bytes
    end: bytes


class Table[T]:
    """One table file, opened for reading and opened again when the file is replaced.

    A table of ranges that do not overlap: `find` answers with the range that holds an address. What is stored with a range, and so
    what `find` returns, is the subclass's (`RangeDb` here, `places.PlaceDb`): it gives the SQL that picks the last range
    starting at or before the address (its first two columns are `start` and `end`) and turns that row into an answer.
    """

    _PROBE = "SELECT 1 FROM ranges LIMIT 1"
    _SELECT = ""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._stamp: tuple[int, int] | None = None

    def _found(self, row: tuple[Any, ...]) -> T:
        raise NotImplementedError

    def _current(self) -> sqlite3.Connection | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._connection is None or stamp != self._stamp:
            try:
                connection = sqlite3.connect(
                    f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
                )
                connection.execute(self._PROBE)
            except sqlite3.Error:
                return self._connection
            old, self._connection, self._stamp = self._connection, connection, stamp
            if old is not None:
                old.close()
        return self._connection

    def find(self, address: str) -> T | None:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        packed = ip.packed
        with self._lock:
            connection = self._current()
            if connection is None:
                return None
            try:
                row = connection.execute(self._SELECT, (ip.version, packed)).fetchone()
            except sqlite3.Error:
                return None
        if row is None or packed > row[1]:
            return None
        return self._found(row)

    def meta(self) -> dict[str, str]:
        with self._lock:
            connection = self._current()
            if connection is None:
                return {}
            try:
                return dict(connection.execute("SELECT key, value FROM meta").fetchall())
            except sqlite3.Error:
                return {}

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


class RangeDb(Table[Found]):
    """The AS number, country and name of a range (IPtoASN, IPdeny)."""

    _SELECT = (
        'SELECT start, "end", asn, country, name FROM ranges WHERE family = ? AND start <= ? '
        "ORDER BY start DESC LIMIT 1"
    )

    def _found(self, row: tuple[Any, ...]) -> Found:
        return Found(asn=row[2], country=row[3], name=row[4], start=row[0], end=row[1])


# ---- IPtoASN ---------------------------------------------------------------------------------------------------------


def iptoasn_rows(lines: Iterable[str]) -> Iterator[Row]:
    """`range_start <TAB> range_end <TAB> AS number <TAB> country <TAB> description` (https://iptoasn.com/).

    A range that is not announced has AS number 0: it is left out, because it says nothing."""
    for line in lines:
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 5:
            continue
        number = asn(parts[2])
        if number is None:
            continue
        try:
            start, end = ipaddress.ip_address(parts[0]), ipaddress.ip_address(parts[1])
        except ValueError:
            continue
        if start.version != end.version or int(end) < int(start):
            continue
        yield (start.version, start.packed, end.packed, number, country(parts[3]), text(parts[4]))


def iptoasn_from_gzip(data: bytes) -> Iterator[Row]:
    with gzip.open(io.BytesIO(data), "rt", encoding="utf-8", errors="replace") as lines:
        yield from iptoasn_rows(lines)


# ---- IPdeny ----------------------------------------------------------------------------------------------------------

_ZONE_NAME = re.compile(r"(?:^|/)([a-z]{2})(?:-aggregated)?\.zone\Z", re.IGNORECASE)
MAX_ZONE_BYTES = 8 * 1024 * 1024


def ipdeny_rows(archive: bytes) -> Iterator[Row]:
    """The `xx.zone` files of an IPdeny `all-zones` archive: one CIDR per line, the country in the file name.

    Only regular files named like a zone are read, whatever else the archive holds, and none larger than `MAX_ZONE_BYTES`."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            match = _ZONE_NAME.search(member.name)
            if not member.isfile() or match is None or member.size > MAX_ZONE_BYTES:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            code = country(match.group(1))
            if code is None:
                continue
            for raw in handle.read().decode("ascii", errors="ignore").splitlines():
                try:
                    network = ipaddress.ip_network(raw.strip(), strict=False)
                except ValueError:
                    continue
                yield (
                    network.version,
                    network.network_address.packed,
                    network.broadcast_address.packed,
                    None,
                    code,
                    None,
                )


# ---- As records ------------------------------------------------------------------------------------------------------


def as_record(found: Found | None) -> Record | None:
    if found is None:
        return None
    return Record(country=found.country, asn=found.asn, organization=found.name)
