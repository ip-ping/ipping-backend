"""Place tables (ipapi.is, IP2Location LITE): flattening overlapping ranges, reading the two CSV files, the table itself."""

from __future__ import annotations

import io
import random
import socket
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ipping_backend.ipdata import places
from ipping_backend.ipdata.places import (
    PlaceDb,
    PlaceTable,
    build,
    csv_lines,
    flatten,
    ip2location_spans,
    ipapi_spans,
    place_record,
)
from ipping_backend.ipdata.ranges import RangeError
from ipping_backend.ipdata.records import Record

# ---------------------------------------------------------------------------------------------
# Flattening: the narrowest range wins wherever ranges overlap
# ---------------------------------------------------------------------------------------------


def _expected(spans: list[tuple[int, int, int]], address: int) -> int | None:
    """The place of the narrowest range holding the address (the later one if they are equally wide)."""
    holding = [
        (end - start, -order, place)
        for order, (start, end, place) in enumerate(spans)
        if start <= address <= end
    ]
    return min(holding)[2] if holding else None


def _lookup(flat: list[tuple[int, int, int]], address: int) -> int | None:
    for start, end, place in flat:
        if start <= address <= end:
            return place
    return None


def test_the_narrowest_range_wins_where_ranges_overlap() -> None:
    # 1 is 101 wide, 2 is 11, 3 is 16 (it overlaps 2 in part), 4 is 11, 5 is 51 (it starts where 4 does)
    spans = [(0, 100, 1), (10, 20, 2), (15, 30, 3), (50, 60, 4), (50, 100, 5)]
    assert list(flatten(spans)) == [
        (0, 9, 1),
        (10, 20, 2),  # narrower than 1, and narrower than 3 where they overlap
        (21, 30, 3),
        (31, 49, 1),
        (50, 60, 4),
        (61, 100, 5),  # narrower than 1
    ]


def test_flattening_agrees_with_the_brute_force_on_random_ranges() -> None:
    rng = random.Random(20261111)
    for _ in range(300):
        count = rng.randint(1, 25)
        spans = []
        for _ in range(count):
            start = rng.randint(0, 120)
            spans.append((start, start + rng.randint(0, 60), rng.randint(1, 6)))
        spans.sort(key=lambda s: s[0])  # what the readers give: in order of start
        flat = list(flatten(spans))
        for address in range(0, 190):
            assert _lookup(flat, address) == _expected(spans, address), (spans, address)
        # flat: in order, never overlapping, and neighbours with one place are one range
        for (a, b, p), (c, d, q) in zip(flat, flat[1:], strict=False):
            assert a <= b < c <= d
            assert not (b + 1 == c and p == q)


def test_a_gap_between_ranges_stays_a_gap() -> None:
    assert list(flatten([(0, 9, 1), (20, 29, 2)])) == [(0, 9, 1), (20, 29, 2)]
    assert list(flatten([])) == []


def test_neighbours_with_the_same_place_become_one_range() -> None:
    assert list(flatten([(0, 9, 1), (10, 19, 1), (20, 29, 2)])) == [(0, 19, 1), (20, 29, 2)]


def test_addresses_need_more_than_64_bits_to_flatten() -> None:
    big = 1 << 120
    flat = list(flatten([(big, big + 1000, 1), (big + 10, big + 20, 2)]))
    assert flat == [(big, big + 9, 1), (big + 10, big + 20, 2), (big + 21, big + 1000, 1)]


# ---------------------------------------------------------------------------------------------
# The two CSV files
# ---------------------------------------------------------------------------------------------

IPAPI_HEADER = "ip_version,start_ip,end_ip,continent,country_code,country,state,city,zip,timezone,latitude,longitude,accuracy,source"
IPAPI_V4 = [
    IPAPI_HEADER,
    "4,1.0.4.0,1.0.4.255,OC,AU,Australia,Victoria,Narre Warren,3805,Australia/Melbourne,-38.03333,145.3,5,whoisDescrAttr",
    "4,1.0.4.0,1.0.7.255,OC,AU,Australia,Victoria,Berwick,3806,Australia/Melbourne,-38.03,145.34,5,whoisDescrAttr",
    "4,8.8.8.0,8.8.8.255,NA,US,United States,California,Mountain View,94035,America/Los_Angeles,37.38605,-122.08385,5,whoisArin",
    "4,9.9.9.0,9.9.9.255,NA,US,United States,,,,,,,1,whoisArin",  # a country and nothing else
    "4,not an address,9.9.9.9,,,,,,,,,,,",
    "4,5.5.5.5",  # a short line
    "4,6.0.0.9,6.0.0.0,NA,US,United States,Ohio,Columbus,43004,America/New_York,39.96,-82.99,5,x",  # backwards
]
IPAPI_V6 = [
    IPAPI_HEADER,
    "6,2001:4860:0000:0000:0000:0000:0000:0000,2001:4860:ffff:ffff:ffff:ffff:ffff:ffff,NA,US,United States,California,Mountain View,94035,America/Los_Angeles,37.38605,-122.08385,5,whoisArin",
]


def _zip(tmp_path: Path, name: str, member: str, lines: list[str], *, extra: bool = True) -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(member, "\n".join(lines) + "\n")
        if extra:
            bundle.writestr("LICENSE.txt", "CC BY-SA 4.0")
    return path


def _reader(
    archive: Path, family: int, spans: Callable[..., Iterator[places.Span]]
) -> Callable[[PlaceTable], Iterator[places.Span]]:
    def read(table: PlaceTable) -> Iterator[places.Span]:
        with csv_lines(archive) as lines:
            yield from spans(lines, table, family)

    return read


def _table(tmp_path: Path, v4: Path, v6: Path | None = None) -> PlaceDb:
    def reader(archive: Path, family: int) -> Callable[[PlaceTable], Iterator[places.Span]]:
        return _reader(archive, family, ipapi_spans)

    passes = [(4, reader(v4, 4))] + ([(6, reader(v6, 6))] if v6 else [])
    path = tmp_path / "ipapi.sqlite"
    build(path, passes, {"built_at": "2026-11-11T00:00:00+00:00"})
    return PlaceDb(path)


def test_the_ipapi_files_are_read_flattened_and_found(tmp_path: Path) -> None:
    db = _table(
        tmp_path,
        _zip(tmp_path, "v4.zip", "geolocationDatabaseIPv4.csv", IPAPI_V4),
        _zip(tmp_path, "v6.zip", "geolocationDatabaseIPv6.csv", IPAPI_V6),
    )
    found = place_record(db.find("8.8.8.8"))
    assert found == Record(
        country="US",
        region="California",
        city="Mountain View",
        postal_code="94035",
        latitude=37.386,
        longitude=-122.0838,
        time_zone="America/Los_Angeles",
    )
    # the narrower range wins inside the wider one, and the wider one holds the rest
    assert place_record(db.find("1.0.4.9")).city == "Narre Warren"  # type: ignore[union-attr]
    assert place_record(db.find("1.0.5.9")).city == "Berwick"  # type: ignore[union-attr]
    assert place_record(db.find("1.0.7.255")).city == "Berwick"  # type: ignore[union-attr]
    assert db.find("1.0.8.0") is None
    # a country and nothing else is a country and nothing else
    assert place_record(db.find("9.9.9.9")) == Record(country="US")
    assert place_record(db.find("2001:4860:4860::8888")).city == "Mountain View"  # type: ignore[union-attr]
    # lines that are not ranges say nothing, and addresses of no range are none
    for address in ("5.5.5.5", "6.0.0.5", "2.2.2.2", "2606:4700::1", "not an address"):
        assert db.find(address) is None
    meta = db.meta()
    assert (
        meta["rows"] == "5" and meta["places"] == "4" and db.size() > 0
    )  # the nested range is two, and IPv6 shares a place with IPv4


def test_each_place_is_kept_once(tmp_path: Path) -> None:
    rows = [IPAPI_HEADER] + [
        f"4,10.0.{i}.0,10.0.{i}.255,NA,US,United States,Ohio,Columbus,43004,America/New_York,39.96,-82.99,5,x"
        for i in range(0, 200, 2)  # a gap after each, so no two become one range
    ]
    db = _table(tmp_path, _zip(tmp_path, "v4.zip", "v4.csv", rows))
    assert db.meta()["rows"] == "100" and db.meta()["places"] == "1"


def test_a_file_that_is_not_the_ipapi_csv_is_refused(tmp_path: Path) -> None:
    other = _zip(tmp_path, "x.zip", "x.csv", ["a,b,c", "1,2,3"])
    with pytest.raises(RangeError, match="ipapi.is"):
        _table(tmp_path, other)
    empty = _zip(tmp_path, "e.zip", "e.csv", [])
    with pytest.raises(RangeError):
        _table(tmp_path, empty)


IP2LOCATION_V4 = [
    '"16843008","16843263","US","United States of America","California","Los Angeles","34.05223","-118.24368","90001","-07:00"',
    '"134744064","134744319","US","United States of America","California","Mountain View","37.38605","-122.08385","94039","-07:00"',
    '"167772160","167772415","-","-","-","-","0.000000","0.000000","-","-"',  # nothing known
    '"4294967295","4294967296","US","x","x","x","1","1","1","-07:00"',  # does not fit an IPv4 address
    "garbage",
    '"1","2","US"',
]
MAPPED = 0xFFFF_0000_0000 + (8 << 24) + (8 << 16) + (8 << 8)  # ::ffff:8.8.8.0
V6_START = int.from_bytes(socket.inet_pton(socket.AF_INET6, "2001:4860::"), "big")
IP2LOCATION_V6 = [
    f'"{MAPPED}","{MAPPED + 255}","US","United States of America","California","Mountain View","37.38605","-122.08385","94039","-07:00"',
    f'"{V6_START}","{V6_START + (1 << 96) - 1}","US","United States of America","Florida","Miami","25.77427","-80.19366","33101","-04:00"',
    '"1","2","US","United States of America","x","x","1","1","1","-07:00"',  # the IPv4-compatible corner
]


def test_the_ip2location_files_are_read_and_the_ipv4_ones_in_the_ipv6_file_are_left_to_the_ipv4_file(
    tmp_path: Path,
) -> None:
    def reader(archive: Path, family: int) -> Callable[[PlaceTable], Iterator[places.Span]]:
        return _reader(archive, family, ip2location_spans)

    v4 = _zip(tmp_path, "4.zip", "IP2LOCATION-LITE-DB11.CSV", IP2LOCATION_V4)
    v6 = _zip(tmp_path, "6.zip", "IP2LOCATION-LITE-DB11.IPV6.CSV", IP2LOCATION_V6)
    path = tmp_path / "ip2location.sqlite"
    rows = build(path, [(4, reader(v4, 4)), (6, reader(v6, 6))], {})
    db = PlaceDb(path)
    # three IPv4 ranges (one says nothing) and one IPv6 range: the IPv4-mapped and IPv4-compatible rows of the IPv6 file are not a second copy
    assert rows == 4
    found = place_record(db.find("8.8.8.8"))
    assert found == Record(
        country="US",
        region="California",
        city="Mountain View",
        postal_code="94039",
        latitude=37.386,
        longitude=-122.0838,
    )  # the time zone is an offset (-07:00), which cannot be compared: it is left out
    assert place_record(db.find("1.1.1.1")).city == "Los Angeles"  # type: ignore[union-attr]
    assert place_record(db.find("10.0.0.1")) == Record()  # "-" everywhere: nothing
    assert place_record(db.find("2001:4860:4860::8888")).city == "Miami"  # type: ignore[union-attr]
    assert db.find("::2") is None and db.find("2606:4700::1") is None


def test_a_file_that_comes_out_of_order_gives_the_same_table(tmp_path: Path) -> None:
    rows = [IPAPI_HEADER] + [
        f"4,10.{i}.0.0,10.{i}.255.255,NA,US,United States,State {i % 7},City {i},{i:05d},America/New_York,{30 + i / 10},-80,5,x"
        for i in range(60)
    ]
    shuffled = rows[:1] + random.Random(3).sample(rows[1:], len(rows) - 1)
    in_order = _table(tmp_path / "a", _zip(_dir(tmp_path / "a"), "v4.zip", "v4.csv", rows))
    out_of_order = _table(tmp_path / "b", _zip(_dir(tmp_path / "b"), "v4.zip", "v4.csv", shuffled))
    for i in range(60):
        assert in_order.find(f"10.{i}.9.9") == out_of_order.find(f"10.{i}.9.9") is not None
    assert in_order.meta()["rows"] == out_of_order.meta()["rows"]
    assert not list((tmp_path / "b").glob(".*.sort")), "the scratch file is gone"


def _dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------------------------
# The zip file and the table file
# ---------------------------------------------------------------------------------------------


def test_the_csv_is_read_out_of_the_zip_whichever_case_and_whatever_else_is_in_it(
    tmp_path: Path,
) -> None:
    archive = _zip(tmp_path, "x.zip", "IP2LOCATION-LITE-DB11.CSV", ["a,b", "c,d"])
    with csv_lines(archive) as lines:
        assert [line.strip() for line in lines] == ["a,b", "c,d"]


def test_a_zip_without_a_csv_or_not_a_zip_is_refused(tmp_path: Path) -> None:
    only_text = tmp_path / "t.zip"
    with zipfile.ZipFile(only_text, "w") as bundle:
        bundle.writestr("README.TXT", "no data here")
    with pytest.raises(RangeError, match="no CSV"), csv_lines(only_text):
        pass
    liar = tmp_path / "liar.zip"
    liar.write_bytes(b"NO PERMISSION")
    with pytest.raises(RangeError, match="not a zip"), csv_lines(liar):
        pass


def test_a_csv_larger_than_it_should_be_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip(tmp_path, "x.zip", "big.csv", ["x" * 100] * 50)
    monkeypatch.setattr(places, "MAX_CSV_BYTES", 1000)
    with pytest.raises(RangeError, match="larger"), csv_lines(archive):
        pass  # the zip says how big the file is: refused before a byte is read


def test_a_stream_that_gives_more_than_it_should_is_stopped_as_it_is_read() -> None:
    """A zip file can say it holds less than it does: the reading itself is capped."""
    capped = places._Capped(io.BytesIO(b"x" * 100), 60)  # noqa: SLF001
    buffer = bytearray(40)
    assert capped.readinto(buffer) == 40
    with pytest.raises(RangeError, match="larger"):
        capped.readinto(buffer)


def test_a_missing_or_broken_table_answers_nothing(tmp_path: Path) -> None:
    assert PlaceDb(tmp_path / "nope.sqlite").find("8.8.8.8") is None
    (tmp_path / "bad.sqlite").write_bytes(b"not sqlite" * 100)
    assert PlaceDb(tmp_path / "bad.sqlite").find("8.8.8.8") is None
    assert place_record(None) is None


def test_a_replaced_table_is_read_again(tmp_path: Path) -> None:
    first = _table(tmp_path / "a", _zip(_dir(tmp_path / "a"), "v4.zip", "v4.csv", IPAPI_V4))
    assert place_record(first.find("8.8.8.8")).city == "Mountain View"  # type: ignore[union-attr]
    other = [IPAPI_HEADER, IPAPI_V4[3].replace("Mountain View", "Palo Alto")]
    second = _table(tmp_path / "b", _zip(_dir(tmp_path / "b"), "v4.zip", "v4.csv", other))
    second.path.replace(first.path)
    assert place_record(first.find("8.8.8.8")).city == "Palo Alto"  # type: ignore[union-attr]


def test_a_table_with_more_ranges_than_any_has_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(places, "MAX_ROWS", 1)
    archive = _zip(tmp_path, "v4.zip", "v4.csv", IPAPI_V4)
    with pytest.raises(RangeError, match="more ranges"):
        _table(tmp_path, archive)


def test_the_places_of_a_row_are_cleaned_once_per_distinct_row() -> None:
    table = PlaceTable()
    calls: list[tuple[str, ...]] = []

    def clean(raw: tuple[str, ...]) -> places.Place:
        calls.append(raw)
        return (raw[0] or None, None, None, None, None, None, None)

    a = table.intern(("US", "x"), clean)
    b = table.intern(("US", "x"), clean)
    c = table.intern(("US", "y"), clean)  # a different row that cleans to the same place
    d = table.intern(("DE", "x"), clean)
    assert a == b == c and d != a and len(table) == 2 and len(calls) == 3
    assert [row[0] for row in table.pending] == [1, 2]


def test_a_big_file_is_read_through_the_zip(tmp_path: Path) -> None:
    def address(n: int) -> str:
        return socket.inet_ntoa(n.to_bytes(4, "big"))

    rows = [IPAPI_HEADER] + [
        f"4,{address(167772160 + i * 256)},{address(167772160 + i * 256 + 255)},NA,US,United States,Ohio,Columbus,43004,America/New_York,39.96,-82.99,5,x"
        for i in range(0, 2000, 2)
    ]
    db = _table(tmp_path, _zip(tmp_path, "v4.zip", "v4.csv", rows))
    assert db.meta()["rows"] == "1000"
    assert (
        place_record(db.find("10.0.1.9")) is None and place_record(db.find("10.0.0.9")) is not None
    )
