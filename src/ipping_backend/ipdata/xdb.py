"""A reader for ip2region's `.xdb` files (https://github.com/lionsoul2014/ip2region, Apache-2.0).

The format (structure 3, with IPv4 and IPv6 files): a 256-byte header, a *vector index* of 256 x 256 entries (one for each pair
of leading address bytes) that gives where in the *segment index* to search, the segment index (sorted `start, end, data
length, data pointer` entries: 14 bytes for IPv4, 38 for IPv6) and the data, a string for each segment. This reads only what a
lookup touches, from a memory-mapped file, and is written from the format description and the project's reference searcher.

The string of a segment, in the current data files, is `country|province|city|ISP|ISO country code`, with `0` for what is not known.
"""

from __future__ import annotations

import ipaddress
import mmap
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

from .records import Record, country, text

HEADER = 256
VECTOR_ENTRY = 8
VECTOR_COLS = 256
VECTOR_BYTES = VECTOR_COLS * VECTOR_COLS * VECTOR_ENTRY


class XdbError(ValueError):
    """A file that is not a usable xdb."""


@dataclass(frozen=True)
class Header:
    structure: int
    created_at: int
    version: int  # 4 or 6: the IP version of the file


def read_header(buffer: bytes | mmap.mmap) -> Header:
    if len(buffer) < HEADER + VECTOR_BYTES:
        raise XdbError("too small to be an xdb file")
    structure, _policy, created, start, end = struct.unpack_from("<HHIII", buffer, 0)
    version = 4
    if structure >= 3:
        version = struct.unpack_from("<H", buffer, 16)[0]
    if version not in (4, 6) or not (HEADER + VECTOR_BYTES <= start <= end <= len(buffer)):
        raise XdbError("its header does not describe an xdb file")
    return Header(structure=structure, created_at=created, version=version)


def search(buffer: bytes | mmap.mmap, header: Header, address: str) -> str | None:
    """The data string of the segment holding the address, or `None`."""
    ip = ipaddress.ip_address(address)
    if ip.version != header.version:
        return None
    packed = ip.packed
    width = len(packed)
    entry = 14 if header.version == 4 else 38
    cell = HEADER + packed[0] * VECTOR_COLS * VECTOR_ENTRY + packed[1] * VECTOR_ENTRY
    first, last = struct.unpack_from("<II", buffer, cell)
    if first == 0 or last == 0 or last < first:
        return None

    def compare(block: int, offset: int) -> int:
        stored = buffer[block + offset : block + offset + width]
        if header.version == 4:
            stored = bytes(reversed(stored))  # IPv4 segments are stored little-endian
        return (packed > stored) - (packed < stored)

    low, high = 0, (last - first) // entry
    while low <= high:
        middle = (low + high) >> 1
        block = first + middle * entry
        if block + entry > len(buffer):
            return None
        if compare(block, 0) < 0:
            high = middle - 1
        elif compare(block, width) > 0:
            low = middle + 1
        else:
            length, pointer = struct.unpack_from("<HI", buffer, block + 2 * width)
            if length == 0 or pointer + length > len(buffer):
                return None
            return bytes(buffer[pointer : pointer + length]).decode("utf-8", errors="replace")
    return None


def record_of(data: str | None) -> Record | None:
    """`Australia|Queensland|0|0|AU` -> a record. Reserved ranges and unknown fields give nothing."""
    if not data:
        return None
    parts = data.split("|")
    if len(parts) < 5:
        return None
    name, province, city, _isp, iso = (p.strip() for p in parts[:5])
    code = country(iso) or country(name)
    if code is None:
        return None
    return Record(country=code, region=text(province), city=text(city))


class XdbFile:
    """One xdb file, mapped into memory and mapped again when the file is replaced."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._map: mmap.mmap | None = None
        self._header: Header | None = None
        self._stamp: tuple[int, int] | None = None

    def _current(self) -> tuple[mmap.mmap, Header] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._map is None or stamp != self._stamp:
            try:
                with self.path.open("rb") as handle:
                    mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                header = read_header(mapped)
            except (OSError, ValueError, XdbError):
                return (self._map, self._header) if self._map is not None and self._header else None
            old, self._map, self._header, self._stamp = self._map, mapped, header, stamp
            if old is not None:
                old.close()
        return (self._map, self._header) if self._map is not None and self._header else None

    @property
    def version(self) -> int | None:
        with self._lock:
            current = self._current()
        return current[1].version if current else None

    def find(self, address: str) -> str | None:
        with self._lock:
            current = self._current()
            if current is None:
                return None
            try:
                return search(current[0], current[1], address)
            except (ValueError, struct.error):
                return None

    def created_at(self) -> int | None:
        with self._lock:
            current = self._current()
        return current[1].created_at if current else None

    def close(self) -> None:
        with self._lock:
            if self._map is not None:
                self._map.close()
                self._map = None
