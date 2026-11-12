"""The IP data sources and their vote: the records, the voting rules, and the readers of each source's file."""

from __future__ import annotations

import gzip
import io
import random
import socket
import struct
import tarfile
from pathlib import Path
from typing import Any

import pytest

from ipping_backend.geolocation.geoip import GeoIp
from ipping_backend.ipdata import consensus, records
from ipping_backend.ipdata.consensus import RELIABILITY, decide, reliability
from ipping_backend.ipdata.local import LocalSources, ipinfo_record, iplocate_record
from ipping_backend.ipdata.ranges import (
    RangeDb,
    as_record,
    build,
    ipdeny_rows,
    iptoasn_from_gzip,
)
from ipping_backend.ipdata.records import Record
from ipping_backend.ipdata.xdb import XdbError, XdbFile, read_header, record_of

from .test_geoip import make_db

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


def test_sources_that_share_their_mistakes_are_one_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    """IPtoASN and IPdeny both say where a block is registered: a German data centre on an American block is `US` to both. Their
    agreement is one witness of about 80 %, so one geolocation database that says `DE` outweighs it."""
    ballots = {"iptoasn": R(country="US"), "ipdeny": R(country="US"), "dbip": R(country="DE")}
    voted = decide(ballots)
    assert voted.record.country == "DE" and voted.percent["country"] > 50
    assert voted.agree["country"] == ["dbip"] and voted.differ["country"] == [
        "iptoasn",
        "ipdeny",
    ]  # all of them are still shown
    # the same ballots with the two counted as two would have had the registries win: the rule is what decides this
    monkeypatch.setattr(consensus, "FAMILIES", ())
    assert decide(ballots).record.country == "US"


def test_a_family_that_is_the_only_one_to_speak_is_still_an_answer() -> None:
    voted = decide({"iptoasn": R(country="US"), "ipdeny": R(country="US")})
    assert voted.record.country == "US"
    assert voted.percent["country"] == round(
        100 * reliability("iptoasn", "country")
    )  # as sure as the better of the two, not more


def test_the_answer_is_the_value_with_the_highest_percentage() -> None:
    """Never a winner that the percentage itself rates below another value (a count of votes used to be able to do that)."""
    rng = random.Random(7)
    voters = [s for s in RELIABILITY if "country" in RELIABILITY[s]]
    for _ in range(400):
        chosen = rng.sample(voters, rng.randint(2, len(voters)))
        said = {s: R(country=rng.choice(["US", "DE", "SG"])) for s in chosen}
        voted = decide(said)
        groups: dict[str, list[str]] = {}
        for s, r in said.items():
            groups.setdefault(r.country or "", []).append(s)
        evidence = {c: consensus._evidence(members, "country") for c, members in groups.items()}
        best = max(evidence.values())
        assert evidence[voted.record.country or ""] == pytest.approx(best), said
        assert voted.percent["country"] == round(
            100 * best / (1 + sum(evidence.values()))
        ) or voted.percent["country"] in (1, 99)


def test_a_source_that_could_not_be_measured_is_trusted_no_more_than_the_best_that_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(consensus, "MEASURED", {"dbip": {"country": 0.9}})
    table = consensus.merged()
    assert table["dbip"]["country"] == 0.9  # a measurement replaces the prior
    assert (
        table["maxmind"]["country"] == 0.9
    )  # claimed 0.98, never measured: no better than the best that was
    assert table["ipdeny"]["country"] == 0.9  # (its prior 0.93 is capped the same way)
    assert (
        table["maxmind"]["region"] == consensus.PRIORS["maxmind"]["region"]
    )  # nothing measured for it: the prior stays
    monkeypatch.setattr(consensus, "MEASURED", {"dbip": {"country": 0.97}})
    assert (
        consensus.merged()["ipdeny"]["country"] == consensus.PRIORS["ipdeny"]["country"]
    )  # never raised above its prior


def test_the_measured_table_holds_numbers_a_vote_can_use() -> None:
    from ipping_backend.ipdata.catalog import IDS
    from ipping_backend.ipdata.measured import MEASURED

    assert MEASURED, "the table is generated by scripts/evaluate_ipdata.py --fit"
    for source, fields in MEASURED.items():
        assert source in IDS
        for field, value in fields.items():
            assert field in {"country", "location"}, (
                field
            )  # the fields the anchors can say anything about
            assert consensus.FLOOR <= value <= consensus.CEILING, (source, field, value)
    assert set(MEASURED["dbip"]) == {"country", "location"}


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


def test_the_many_ways_to_write_one_organization_are_one_vote() -> None:
    google = decide(
        {
            "maxmind": R(asn=15169, organization="Google LLC"),
            "iptoasn": R(asn=15169, organization="GOOGLE - Google LLC"),
            "ipinfo": R(asn=15169, organization="Google LLC"),
            "dbip": R(asn=15169, organization="Google Cloud"),
        }
    )
    assert google.record.organization == "Google LLC" and google.differ["organization"] == []
    assert google.percent["organization"] >= 97
    hetzner = decide(
        {
            "ipinfo": R(asn=24940, organization="Hetzner Online GmbH"),
            "iptoasn": R(asn=24940, organization="HETZNER-AS Hetzner Online GmbH"),
        }
    )
    assert hetzner.agree["organization"] == ["ipinfo", "iptoasn"]
    apart = decide(
        {
            "ipinfo": R(asn=3356, organization="Level 3 Parent, LLC"),
            "iptoasn": R(asn=3356, organization="Lumen Technologies"),
        }
    )
    assert apart.differ["organization"] != [] or len(apart.agree["organization"]) == 1


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
# IPLocate and IPinfo Lite: database files with a layout of their own (the records of the real files, for 8.8.8.8 and 1.1.1.1)
# ---------------------------------------------------------------------------------------------

IPLOCATE_COUNTRY: dict[str, dict[str, Any]] = {
    "8.8.8.0/24": {"continent_code": "NA", "country_code": "US", "country_name": "United States"},
    "114.114.114.0/24": {"continent_code": "AS", "country_code": "CN", "country_name": "China"},
}
IPLOCATE_ASN: dict[str, dict[str, Any]] = {
    "8.8.8.0/24": {
        "asn": "15169",
        "country_code": "US",
        "domain": "google.com",
        "name": "GOOGLE",
        "network": "8.8.8.0/24",
        "org": "Google LLC",
    },
    "114.114.114.0/24": {
        "asn": "137702",
        "country_code": "DE",  # the network's own country, not where the address is
        "domain": "chinatelecom.cn",
        "name": "CHINATELECOM-JIANGSU-NANJING-IDC",
        "network": "not a network",
    },
}
IPINFO_LITE: dict[str, dict[str, Any]] = {
    "1.1.1.0/24": {
        "as_domain": "cloudflare.com",
        "as_name": "Cloudflare, Inc.",
        "asn": "AS13335",
        "continent": "Oceania",
        "continent_code": "OC",
        "country": "Australia",
        "country_code": "AU",
    },
    "9.9.9.0/24": {"asn": "AS19281", "as_name": "Quad9", "country": "Switzerland"},
}


def _database(tmp_path: Path, name: str, records_: dict[str, dict[str, Any]]) -> GeoIp:
    make_db(tmp_path / name, "City", records_)
    return GeoIp(directory=tmp_path)


def test_iplocate_two_databases_are_one_record(tmp_path: Path) -> None:
    _database(tmp_path, "iplocate-country.mmdb", IPLOCATE_COUNTRY)
    _database(tmp_path, "iplocate-asn.mmdb", IPLOCATE_ASN)
    reading = LocalSources(lambda: GeoIp(directory=tmp_path), tmp_path)
    assert reading.lookup("iplocate", "8.8.8.8") == R(
        country="US", asn=15169, organization="Google LLC", network="8.8.8.0/24"
    )
    # the country comes from the country database only: the ASN database's is the network's own
    chinese = reading.lookup("iplocate", "114.114.114.114")
    assert chinese == R(country="CN", asn=137702, organization="CHINATELECOM-JIANGSU-NANJING-IDC")
    assert reading.lookup("iplocate", "1.2.3.4") is None
    assert reading.present("iplocate") and [f["name"] for f in reading.files("iplocate")] == [
        "iplocate-country.mmdb",
        "iplocate-asn.mmdb",
    ]
    assert reading.knows("iplocate") == {"country", "asn", "organization"}


def test_iplocate_with_one_of_its_two_databases_still_answers_what_it_has(tmp_path: Path) -> None:
    _database(tmp_path, "iplocate-asn.mmdb", IPLOCATE_ASN)
    reading = LocalSources(lambda: GeoIp(directory=tmp_path), tmp_path)
    assert reading.lookup("iplocate", "8.8.8.8") == R(
        asn=15169, organization="Google LLC", network="8.8.8.0/24"
    )
    assert reading.knows("iplocate") == {"asn", "organization"}
    other = tmp_path / "other"
    other.mkdir()
    _database(other, "iplocate-country.mmdb", IPLOCATE_COUNTRY)
    only_country = LocalSources(lambda: GeoIp(directory=other), other)
    assert only_country.lookup("iplocate", "8.8.8.8") == R(country="US")
    assert only_country.knows("iplocate") == {"country"}


def test_ipinfo_lite_is_one_record(tmp_path: Path) -> None:
    _database(tmp_path, "ipinfo-lite.mmdb", IPINFO_LITE)
    reading = LocalSources(lambda: GeoIp(directory=tmp_path), tmp_path)
    assert reading.lookup("ipinfo", "1.1.1.1") == R(
        country="AU", asn=13335, organization="Cloudflare, Inc.", network="1.1.1.0/24"
    )
    # a record with only a country's name still has its country
    assert reading.lookup("ipinfo", "9.9.9.9") == R(
        country="CH", asn=19281, organization="Quad9", network="9.9.9.0/24"
    )
    assert reading.lookup("ipinfo", "8.8.8.8") is None
    assert reading.knows("ipinfo") == {"country", "asn", "organization"}


def test_nothing_on_disk_means_nothing_known(tmp_path: Path) -> None:
    reading = LocalSources(lambda: GeoIp(directory=tmp_path), tmp_path)
    for source in (
        "maxmind",
        "dbip",
        "ipinfo",
        "iplocate",
        "ipapi_is",
        "ip2location",
        "iptoasn",
        "ipdeny",
        "ip2region",
    ):
        assert reading.lookup(source, "8.8.8.8") is None
        assert not reading.present(source) and reading.knows(source) == frozenset()
    nowhere = LocalSources(lambda: GeoIp(), None)
    assert nowhere.lookup("ipinfo", "1.1.1.1") is None and not nowhere.present("iplocate")


def test_the_two_database_readers_tolerate_nothing_to_read() -> None:
    assert iplocate_record(None, None, "8.8.8.8") is None
    assert ipinfo_record(None, "8.8.8.8") is None
