"""The IP data sources and their vote: the records, the voting rules, the readers, and each source's way of answering."""

from __future__ import annotations

import gzip
import io
import socket
import struct
import tarfile
from pathlib import Path

import pytest

from ipping_backend.ipdata import online, records
from ipping_backend.ipdata.consensus import RELIABILITY, decide, reliability
from ipping_backend.ipdata.ranges import (
    RangeDb,
    as_record,
    build,
    ipdeny_rows,
    iptoasn_from_gzip,
)
from ipping_backend.ipdata.records import Record
from ipping_backend.ipdata.xdb import XdbError, XdbFile, read_header, record_of

R = Record

# ---------------------------------------------------------------------------------------------
# Cleaning what sources say
# ---------------------------------------------------------------------------------------------


def test_countries_come_from_codes_and_names() -> None:
    assert records.country("de") == "DE" and records.country("DE") == "DE"
    assert (
        records.country("United States") == "US"
        and records.country("United States of America") == "US"
    )
    assert records.country("Russia") == "RU" and records.country("South Korea") == "KR"
    assert records.country("Atlantis") is None and records.country("ZZ") is None
    assert (
        records.country("0") is None
        and records.country(None) is None
        and records.country("") is None
    )


def test_as_numbers_come_in_all_their_forms() -> None:
    assert records.asn(15169) == 15169 and records.asn("15169") == 15169
    assert records.asn("AS15169") == 15169 and records.asn("as 15169") == 15169
    assert records.asn_and_name("AS15169 Google LLC") == (15169, "Google LLC")
    assert records.asn_and_name("Google LLC") == (None, "Google LLC")
    assert records.asn(0) is None and records.asn("0") is None and records.asn(2**32) is None
    assert records.asn(True) is None and records.asn("not a number") is None


def test_what_sources_write_for_unknown_is_nothing() -> None:
    for word in ("0", "-", "Unknown", "none", "N/A", "Reserved", "", "  "):
        assert records.text(word) is None
    assert records.text("  São   Paulo ") == "São Paulo"
    assert records.position(0, 0) == (None, None)  # null island
    assert records.position(91, 10) == (None, None) and records.position("x", 1) == (None, None)
    assert records.position("50.1109", "8.6821") == (50.1109, 8.6821)
    assert records.time_zone("Europe/Berlin") == "Europe/Berlin"
    assert records.time_zone("-08:00") is None and records.time_zone("UTC") is None


def test_places_and_organizations_are_compared_by_what_they_say_not_how() -> None:
    assert records.fold("São Paulo") == records.fold("sao paulo") == "saopaulo"
    assert records.organization_key("Google LLC") == records.organization_key("GOOGLE")
    assert records.organization_key("Cloudflare, Inc.") == "cloudflare"
    assert records.organization_key("OVH SAS") == records.organization_key("ovh")
    assert records.organization_key("LLC") != ""  # all noise: the words are kept


def test_a_record_with_nothing_to_vote_on_is_empty() -> None:
    assert R().empty() and R(network="1.0.0.0/24", accuracy_radius_km=5).empty()
    assert not R(country="DE").empty() and not R(latitude=1.0, longitude=2.0).empty()
    assert R(latitude=1.0).empty()  # half a position is none
    said = R(country="DE", city="Berlin", network="x", region_code="BE").fields()
    assert said == {"country": "DE", "city": "Berlin"}


# ---------------------------------------------------------------------------------------------
# The vote
# ---------------------------------------------------------------------------------------------


def test_one_source_with_something_is_still_an_answer() -> None:
    """One of five has data, the other four are empty: the one is answered, with its own reliability."""
    voted = decide(
        {
            "maxmind": R(),
            "dbip": R(),
            "ipinfo": R(country="DE", city="Frankfurt am Main", latitude=50.11, longitude=8.68),
            "iptoasn": R(),
            "ipdeny": R(),
        }
    )
    assert (voted.record.country, voted.record.city) == ("DE", "Frankfurt am Main")
    assert (voted.record.latitude, voted.record.longitude) == (50.11, 8.68)
    assert voted.percent["country"] == round(100 * reliability("ipinfo", "country"))
    assert voted.percent["city"] == round(100 * reliability("ipinfo", "city"))
    assert voted.contributors == ["ipinfo"] and voted.agree["country"] == ["ipinfo"]
    assert voted.differ["country"] == []


def test_nothing_from_anyone_is_nothing() -> None:
    assert decide({}).record.empty() and decide({}).percent == {}
    assert decide({"maxmind": R(), "dbip": R()}).contributors == []


def test_the_majority_wins_and_the_dissenters_are_named() -> None:
    voted = decide(
        {
            "ipinfo": R(country="NL"),
            "dbip": R(country="NL"),
            "maxmind": R(country="NL"),
            "ip2region": R(country="DE"),
        }
    )
    assert voted.record.country == "NL"
    assert voted.agree["country"] == ["maxmind", "ipinfo", "dbip"] and voted.differ["country"] == [
        "ip2region"
    ]
    assert voted.percent["country"] >= 95


def test_two_against_one_wins_even_when_the_one_is_the_most_reliable_source() -> None:
    voted = decide(
        {
            "maxmind": R(city="Lyon", country="FR"),
            "dbip": R(city="Paris", country="FR"),
            "ip2region": R(city="Paris", country="FR"),
        }
    )
    assert voted.record.city == "Paris"
    assert voted.agree["city"] == ["dbip", "ip2region"] and voted.differ["city"] == ["maxmind"]


def test_a_tie_goes_to_the_more_reliable_source() -> None:
    voted = decide({"ipdeny": R(country="CA"), "maxmind": R(country="US")})
    assert voted.record.country == "US"
    assert 70 <= voted.percent["country"] <= 85  # a split vote is not a sure thing


def test_the_percentage_rises_with_agreement_and_falls_with_disagreement() -> None:
    one = decide({"dbip": R(city="Oslo")}).percent["city"]
    two = decide({"dbip": R(city="Oslo"), "ipinfo": R(city="Oslo")}).percent["city"]
    three = decide(
        {"dbip": R(city="Oslo"), "ipinfo": R(city="Oslo"), "maxmind": R(city="Oslo")}
    ).percent["city"]
    split = decide({"dbip": R(city="Oslo"), "ipinfo": R(city="Bergen")}).percent["city"]
    assert one < two < three <= 99
    assert split < one
    # the worked examples of the module's documentation, with one reliability for every source
    for source in RELIABILITY:
        assert 55 <= 100 * reliability(source, "city") <= 99


def test_the_percentage_is_never_zero_or_certain() -> None:
    everyone = {s: R(country="SE") for s in RELIABILITY if "country" in RELIABILITY[s]}
    voted = decide(everyone)
    assert voted.percent["country"] == 99
    assert 1 <= min(
        decide({"ipdeny": R(country="SE"), "ip2region": R(country="NO")}).percent.values()
    )


def test_a_city_is_voted_on_only_by_sources_that_agree_on_the_country() -> None:
    voted = decide(
        {
            "maxmind": R(country="US", city="Springfield"),
            "dbip": R(country="US", city="Springfield"),
            "ipinfo": R(
                country="GB", city="London"
            ),  # the wrong country: its city does not count against the others
        }
    )
    assert (voted.record.country, voted.record.city) == ("US", "Springfield")
    assert voted.percent["city"] >= 80 and "ipinfo" not in voted.differ["city"]


def test_a_source_without_a_country_votes_on_what_it_has() -> None:
    voted = decide(
        {
            "iptoasn": R(asn=15169, organization="GOOGLE"),
            "dbip": R(country="US", asn=15169, organization="Google LLC"),
        }
    )
    assert voted.record.asn == 15169 and voted.record.country == "US"
    assert voted.agree["asn"] == ["dbip", "iptoasn"]
    assert voted.agree["organization"] == [
        "dbip",
        "iptoasn",
    ]  # Google LLC and GOOGLE are one organization


def test_the_organization_is_voted_on_among_those_that_agree_on_the_number() -> None:
    voted = decide(
        {
            "iptoasn": R(asn=13335, organization="CLOUDFLARENET"),
            "ipinfo": R(asn=13335, organization="Cloudflare, Inc."),
            "ipapi_is": R(asn=64496, organization="Some Other Network"),
        }
    )
    assert voted.record.asn == 13335 and voted.record.organization in {
        "Cloudflare, Inc.",
        "CLOUDFLARENET",
    }
    assert voted.agree["asn"] == ["ipinfo", "iptoasn"] and voted.differ["asn"] == ["ipapi_is"]
    assert "ipapi_is" not in voted.differ["organization"]


def test_positions_in_one_place_are_one_vote_and_the_middle_is_taken() -> None:
    voted = decide(
        {
            "maxmind": R(country="DE", latitude=50.11, longitude=8.68, accuracy_radius_km=20),
            "dbip": R(country="DE", latitude=50.12, longitude=8.70, accuracy_radius_km=50),
            "ipinfo": R(country="DE", latitude=50.10, longitude=8.66),
            "ip2region": R(country="DE", latitude=52.52, longitude=13.40),  # Berlin: another place
        }
    )
    assert voted.agree["location"] == ["maxmind", "ipinfo", "dbip"] and voted.differ[
        "location"
    ] == ["ip2region"]
    assert (voted.record.latitude, voted.record.longitude) == (
        50.11,
        8.68,
    )  # the median of each coordinate
    assert voted.record.accuracy_radius_km == 50  # the median of the radii that were given (20, 50)


def test_a_place_is_a_region_then_a_city_in_it() -> None:
    voted = decide(
        {
            "maxmind": R(country="BR", region="São Paulo", city="São Paulo", region_code="SP"),
            "dbip": R(country="BR", region="Sao Paulo", city="Sao Paulo"),
            "ipinfo": R(country="BR", region="Rio de Janeiro", city="Rio de Janeiro"),
        }
    )
    assert (voted.record.region, voted.record.city, voted.record.region_code) == (
        "São Paulo",
        "São Paulo",
        "SP",
    )
    assert voted.agree["region"] == ["maxmind", "dbip"]


def test_the_time_zone_and_postal_code_are_voted_too() -> None:
    voted = decide(
        {
            "ipinfo": R(country="US", postal_code="94043", time_zone="America/Los_Angeles"),
            "iplocate": R(country="US", postal_code="94035", time_zone="America/Los_Angeles"),
            "ip2location": R(country="US", postal_code="94043"),
        }
    )
    assert voted.record.postal_code == "94043" and voted.record.time_zone == "America/Los_Angeles"
    assert voted.agree["postal_code"] == ["ipinfo", "ip2location"] and voted.differ[
        "postal_code"
    ] == ["iplocate"]
    assert (
        voted.percent["time_zone"] > voted.percent["postal_code"]
    )  # a time zone is surer than a postal code


# ---------------------------------------------------------------------------------------------
# Range tables (IPtoASN, IPdeny)
# ---------------------------------------------------------------------------------------------

IPTOASN = (
    b"0.0.0.0\t0.255.255.255\t0\tNone\tNot routed\n"
    b"8.8.8.0\t8.8.8.255\t15169\tUS\tGOOGLE\n"
    b"8.8.9.0\t8.8.9.127\t64500\tDE\tEXAMPLE-AS Example GmbH\n"
    b"this line is not data\n"
    b"2001:4860::\t2001:4860:ffff:ffff:ffff:ffff:ffff:ffff\t15169\tUS\tGOOGLE\n"
    b"9.0.0.0\t8.0.0.0\t1\tUS\tBACKWARDS\n"
)


def test_iptoasn_ranges_are_read_and_found(tmp_path: Path) -> None:
    rows = list(iptoasn_from_gzip(gzip.compress(IPTOASN)))
    assert len(rows) == 3  # the not-routed range, the bad line and the backwards range say nothing
    path = tmp_path / "iptoasn.sqlite"
    assert build(path, rows, {"built_at": "2026-11-01T00:00:00+00:00"}) == 3
    db = RangeDb(path)
    found = as_record(db.find("8.8.8.8"))
    assert found is not None and (found.asn, found.country, found.organization) == (
        15169,
        "US",
        "GOOGLE",
    )
    other = as_record(db.find("8.8.9.100"))
    assert other is not None and (other.asn, other.country) == (64500, "DE")
    six = as_record(db.find("2001:4860:4860::8888"))
    assert six is not None and six.asn == 15169
    assert db.find("8.8.9.200") is None  # between two ranges
    assert db.find("1.2.3.4") is None and db.find("not an address") is None
    assert db.meta()["rows"] == "3" and db.size() > 0


def test_a_missing_or_broken_table_answers_nothing(tmp_path: Path) -> None:
    assert RangeDb(tmp_path / "nope.sqlite").find("8.8.8.8") is None
    (tmp_path / "bad.sqlite").write_bytes(b"not sqlite" * 100)
    assert RangeDb(tmp_path / "bad.sqlite").find("8.8.8.8") is None


def _zones(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("us.zone.link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    return out.getvalue()


def test_ipdeny_zones_are_read_by_file_name_and_nothing_else(tmp_path: Path) -> None:
    archive = _zones(
        {
            "us.zone": "8.8.8.0/24\n8.8.4.0/24\nnot a network\n",
            "de.zone": "93.184.216.0/24\n",
            "ipv6/fr.zone": "2a00:1450::/32\n",
            "README.txt": "1.1.1.0/24\n",
            "zz.zone": "5.5.5.0/24\n",  # not a country
        }
    )
    rows = list(ipdeny_rows(archive))
    assert len(rows) == 4
    path = tmp_path / "ipdeny.sqlite"
    build(path, rows, {})
    db = RangeDb(path)
    assert as_record(db.find("8.8.8.8")) == R(country="US")
    assert as_record(db.find("93.184.216.34")) == R(country="DE")
    assert as_record(db.find("2a00:1450:4001::1")) == R(country="FR")
    assert db.find("1.1.1.1") is None and db.find("5.5.5.5") is None


# ---------------------------------------------------------------------------------------------
# ip2region (xdb)
# ---------------------------------------------------------------------------------------------


def make_xdb(path: Path, version: int, segments: list[tuple[str, str, str]]) -> Path:
    """A small structure-3 xdb file: the segments must each stay inside one /16 (no splitting is done)."""
    entry = 14 if version == 4 else 38
    header, vector_size = 256, 256 * 256 * 8
    data = bytearray()
    blocks: list[tuple[bytes, bytes, int, int]] = []
    start_of_data = header + vector_size
    for first, last, text in segments:
        family = socket.AF_INET if version == 4 else socket.AF_INET6
        a, b = socket.inet_pton(family, first), socket.inet_pton(family, last)
        encoded = text.encode()
        blocks.append((a, b, start_of_data + len(data), len(encoded)))
        data += encoded
    start_of_index = start_of_data + len(data)
    index = bytearray()
    vector = [[0, 0] for _ in range(65536)]
    for position, (a, b, pointer, length) in enumerate(blocks):
        at = start_of_index + position * entry
        cell = a[0] * 256 + a[1]
        if vector[cell][0] == 0:
            vector[cell][0] = at
        vector[cell][1] = at
        if version == 4:
            index += a[::-1] + b[::-1]  # IPv4 segments are stored little-endian
        else:
            index += a + b
        index += struct.pack("<HI", length, pointer)
    head = struct.pack(
        "<HHIII", 3, 1, 1_700_000_000, start_of_index, start_of_index + len(index) - entry
    )
    head += struct.pack("<HH", version, 4)
    head = head.ljust(header, b"\0")
    table = b"".join(struct.pack("<II", s, e) for s, e in vector)
    path.write_bytes(head + table + bytes(data) + bytes(index))
    return path


V4 = [
    ("1.0.0.0", "1.0.255.255", "Australia|Queensland|Brisbane|0|AU"),
    ("8.8.0.0", "8.8.7.255", "United States|California|0|Google LLC|US"),
    ("8.8.8.0", "8.8.255.255", "United States|California|Mountain View|Google LLC|US"),
    ("10.0.0.0", "10.0.255.255", "Reserved|Reserved|Reserved|0|0"),
    ("114.114.0.0", "114.114.255.255", "中国|江苏省|南京市|0|CN"),
]


def test_ip2region_v4_is_searched(tmp_path: Path) -> None:
    reader = XdbFile(make_xdb(tmp_path / "v4.xdb", 4, V4))
    assert reader.version == 4 and reader.created_at() == 1_700_000_000
    assert reader.find("1.0.4.4") == "Australia|Queensland|Brisbane|0|AU"
    assert reader.find("8.8.8.8") == "United States|California|Mountain View|Google LLC|US"
    assert reader.find("8.8.1.1") == "United States|California|0|Google LLC|US"
    assert reader.find("8.8.0.0") is not None and reader.find("8.8.255.255") is not None
    assert reader.find("9.9.9.9") is None and reader.find("2.2.2.2") is None
    assert reader.find("2606:4700::1") is None  # the other IP version's file answers that


def test_ip2region_v6_is_searched(tmp_path: Path) -> None:
    reader = XdbFile(
        make_xdb(
            tmp_path / "v6.xdb",
            6,
            [
                (
                    "2001:4860::",
                    "2001:4860:ffff:ffff:ffff:ffff:ffff:ffff",
                    "United States|Florida|Miami|Google LLC|US",
                ),
                (
                    "2a00:1450::",
                    "2a00:1450:ffff:ffff:ffff:ffff:ffff:ffff",
                    "Germany|Hesse|Frankfurt am Main|0|DE",
                ),
            ],
        )
    )
    assert reader.version == 6
    assert reader.find("2001:4860:4860::8888") == "United States|Florida|Miami|Google LLC|US"
    assert reader.find("2a00:1450:4001:81b::200e") == "Germany|Hesse|Frankfurt am Main|0|DE"
    assert reader.find("2001:db8::1") is None and reader.find("8.8.8.8") is None


def test_ip2region_strings_become_records() -> None:
    assert record_of("Australia|Queensland|Brisbane|0|AU") == R(
        country="AU", region="Queensland", city="Brisbane"
    )
    assert record_of("中国|江苏省|南京市|0|CN") == R(country="CN", region="江苏省", city="南京市")
    assert record_of("United States|California|0|Google LLC|US") == R(
        country="US", region="California"
    )
    assert record_of("Reserved|Reserved|Reserved|0|0") is None
    assert record_of("") is None and record_of(None) is None and record_of("a|b") is None
    assert record_of("Germany|0|0|0|0") == R(country="DE")  # no code: the name is used


def test_a_file_that_is_not_an_xdb_is_refused(tmp_path: Path) -> None:
    with pytest.raises(XdbError):
        read_header(b"short")
    with pytest.raises(XdbError):
        read_header(b"\0" * (256 + 524288))
    broken = tmp_path / "broken.xdb"
    broken.write_bytes(b"x" * 600_000)
    assert (
        XdbFile(broken).find("8.8.8.8") is None
        and XdbFile(tmp_path / "missing.xdb").find("8.8.8.8") is None
    )


def test_a_replaced_xdb_file_is_read_again(tmp_path: Path) -> None:
    path = make_xdb(tmp_path / "v4.xdb", 4, V4)
    reader = XdbFile(path)
    assert reader.find("1.0.4.4") is not None
    make_xdb(
        path,
        4,
        [
            ("1.0.0.0", "1.0.255.255", "Japan|Tokyo|0|0|JP"),
            ("2.0.0.0", "2.0.255.255", "France|0|0|0|FR"),
        ],
    )
    assert reader.find("1.0.4.4") == "Japan|Tokyo|0|0|JP"


# ---------------------------------------------------------------------------------------------
# The four online services' answers (the real shapes, captured for 8.8.8.8)
# ---------------------------------------------------------------------------------------------

IPINFO = {
    "ip": "8.8.8.8", "hostname": "dns.google", "city": "Mountain View", "region": "California", "country": "US",
    "loc": "37.4056,-122.0775", "org": "AS15169 Google LLC", "postal": "94043", "timezone": "America/Los_Angeles",
    "anycast": True,
}  # fmt: skip
IPLOCATE = {
    "ip": "8.8.8.8", "country": "United States", "country_code": "US", "city": "Mountain View", "continent": "North America",
    "latitude": 37.38605, "longitude": -122.08385, "time_zone": "America/Los_Angeles", "postal_code": "94035",
    "subdivision": "California",
    "asn": {"asn": "AS15169", "route": "8.8.8.0/24", "netname": "GOOGLE", "name": "Google LLC", "country_code": "US"},
    "company": {"name": "Google LLC", "domain": "google.com"},
}  # fmt: skip
IPAPI_FREE = {
    "ip": "8.8.8.8", "is_bogon": False, "company": "Google LLC", "asn": "AS15169 Google LLC", "city": "Mountain View",
    "region": "California", "country": "United States", "lat": 37.38605, "lon": -122.08385, "timezone": "America/Los_Angeles",
}  # fmt: skip
IPAPI_KEYED = {
    "ip": "8.8.8.8", "is_bogon": False, "company": {"name": "Google LLC", "type": "hosting"},
    "asn": {"asn": 15169, "route": "8.8.8.0/24", "descr": "GOOGLE", "org": "Google LLC", "country": "us"},
    "location": {
        "country": "United States", "country_code": "US", "state": "California", "city": "Mountain View",
        "latitude": 37.38605, "longitude": -122.08385, "zip": "94043", "timezone": "America/Los_Angeles",
    },
}  # fmt: skip
IP2LOCATION = {
    "ip": "8.8.8.8", "country_code": "US", "country_name": "United States of America", "region_name": "California",
    "city_name": "Mountain View", "latitude": 37.38605, "longitude": -122.08385, "zip_code": "94043",
    "time_zone": "-08:00", "asn": "15169", "as": "Google LLC", "is_proxy": False,
}  # fmt: skip


def test_every_online_service_answer_is_read() -> None:
    expected = {
        "ipinfo": IPINFO,
        "iplocate": IPLOCATE,
        "ipapi_is": IPAPI_FREE,
        "ip2location": IP2LOCATION,
    }
    for source, body in expected.items():
        record = online.SOURCES[source].parse(body)
        assert (record.country, record.region, record.city) == (
            "US",
            "California",
            "Mountain View",
        ), source
        assert (record.asn, record.organization) == (15169, "Google LLC"), source
        assert record.latitude is not None and abs(record.latitude - 37.4) < 0.1, source


def test_the_details_that_differ_between_services() -> None:
    assert online.SOURCES["ipinfo"].parse(IPINFO).time_zone == "America/Los_Angeles"
    assert online.SOURCES["ipinfo"].parse(IPINFO).postal_code == "94043"
    assert online.SOURCES["iplocate"].parse(IPLOCATE).network == "8.8.8.0/24"
    assert (
        online.SOURCES["ip2location"].parse(IP2LOCATION).time_zone is None
    )  # an offset is not a zone
    keyed = online.SOURCES["ipapi_is"].parse(IPAPI_KEYED)
    assert (keyed.country, keyed.region, keyed.postal_code, keyed.asn, keyed.organization) == (
        "US", "California", "94043", 15169, "Google LLC",
    )  # fmt: skip
    assert keyed.network == "8.8.8.0/24" and keyed.time_zone == "America/Los_Angeles"
    free = online.SOURCES["ipapi_is"].parse(IPAPI_FREE)
    assert (
        free.country == "US" and free.postal_code is None
    )  # the free answer names the country in full


def test_an_online_answer_with_nothing_in_it_is_empty() -> None:
    assert online.SOURCES["ipinfo"].parse({"ip": "10.0.0.1", "bogon": True}).empty()
    assert online.SOURCES["ipapi_is"].parse({"ip": "10.0.0.1", "is_bogon": True}).empty()
    assert online.SOURCES["ip2location"].parse({"error": {"error_code": 10001}}).empty()
    assert online.SOURCES["iplocate"].parse({}).empty()
    assert online.SOURCES["ipinfo"].parse({"country": "ZZ", "loc": "0,0", "org": "0"}).empty()


def test_a_key_goes_where_the_service_wants_it() -> None:
    url, headers, params = online.SOURCES["ipinfo"].request("8.8.8.8", {"token": "abc"})
    assert (
        url == "https://ipinfo.io/8.8.8.8/json"
        and params == {"token": "abc"}
        and "abc" not in str(headers)
    )
    url, headers, params = online.SOURCES["iplocate"].request("8.8.8.8", {"api_key": "k1"})
    assert headers["X-API-Key"] == "k1" and params == {}
    url, headers, params = online.SOURCES["ipapi_is"].request("8.8.8.8", {"api_key": "k2"})
    assert params == {"q": "8.8.8.8", "key": "k2"}
    url, headers, params = online.SOURCES["ip2location"].request("8.8.8.8", {"api_key": "k3"})
    assert params["key"] == "k3" and params["ip"] == "8.8.8.8"
    for source in online.SOURCES.values():  # and none without one
        _, headers, params = source.request("8.8.8.8", {})
        assert "key" not in params and "token" not in params and "X-API-Key" not in headers
