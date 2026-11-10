"""The optional GeoIP / ASN cross-check: reading the databases, ruling places out, the API, the updater."""

from __future__ import annotations

import gzip
import io
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from mmdb_writer import MMDBWriter
from netaddr import IPSet

from ipping_backend.geolocation.geoip import GeoIp, GeoPlace, rule_out
from ipping_backend.geolocation.update import check_database, fetch, months, update

from .conftest import Api
from .test_location_api import SITES, answer, close, fleet

TARGET = "93.184.216.34"
TOKYO = {
    "city": {"names": {"en": "Tokyo"}},
    "country": {"iso_code": "JP", "names": {"en": "Japan"}},
    "location": {"latitude": 35.68, "longitude": 139.69},
}
AMSTERDAM = {
    "city": {"names": {"en": "Amsterdam"}},
    "country": {"iso_code": "NL"},
    "location": {"latitude": 52.37, "longitude": 4.9},
}


def make_db(path: Path, kind: str, records: dict[str, dict[str, Any]]) -> Path:
    writer = MMDBWriter(
        ip_version=6,
        ipv4_compatible=True,
        database_type=f"DBIP-{kind}-Lite",
        languages=["en"],
        description={"en": "test"},
    )
    for network, record in records.items():
        writer.insert_network(IPSet([network]), record)
    writer.to_db_file(str(path))
    return path


def city_db(path: Path, place: dict[str, Any] = TOKYO) -> Path:
    return make_db(path, "City", {"93.184.216.0/24": place, "8.8.8.0/24": place})


def asn_db(path: Path) -> Path:
    record = {"autonomous_system_number": 15133, "autonomous_system_organization": "Edgecast"}
    return make_db(path, "ASN", {"93.184.216.0/24": record, "8.8.8.0/24": record})


# ---------------------------------------------------------------------------------------------
# Reading the databases
# ---------------------------------------------------------------------------------------------


def test_a_place_and_a_network_are_read(tmp_path: Path) -> None:
    geoip = GeoIp(
        city_db(tmp_path / "dbip-city-lite-2026-11.mmdb"), asn_db(tmp_path / "dbip-asn-lite.mmdb")
    )
    assert geoip.place(TARGET) == GeoPlace("Tokyo", "JP", 35.68, 139.69)
    asn = geoip.asn(TARGET)
    assert asn is not None and (asn.number, asn.organization) == (15133, "Edgecast")
    assert geoip.place("1.1.1.1") is None and geoip.asn("1.1.1.1") is None
    assert geoip.place("not an address") is None
    assert geoip.has_city and geoip.has_asn
    assert geoip.source == "DB-IP Lite" and "DB-IP" in (geoip.attribution or "")


def test_nothing_configured_or_missing_files_are_harmless(tmp_path: Path) -> None:
    for geoip in (GeoIp(), GeoIp(tmp_path / "missing-city.mmdb", tmp_path / "missing-asn.mmdb")):
        assert not geoip.has_city and not geoip.has_asn and geoip.attribution is None
        assert geoip.place(TARGET) is None and geoip.asn(TARGET) is None


def test_a_file_that_is_not_a_database_is_ignored(tmp_path: Path) -> None:
    junk = tmp_path / "junk-city.mmdb"
    junk.write_bytes(b"this is not a MaxMind database")
    assert GeoIp(junk).place(TARGET) is None


def test_a_replaced_file_is_picked_up_without_a_restart(tmp_path: Path) -> None:
    path = city_db(tmp_path / "city.mmdb", TOKYO)
    geoip = GeoIp(path)
    assert geoip.place(TARGET).name == "Tokyo"  # type: ignore[union-attr]
    city_db(tmp_path / "next.mmdb", AMSTERDAM).replace(path)
    os.utime(path, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    assert geoip.place(TARGET).name == "Amsterdam"  # type: ignore[union-attr]


def test_databases_that_appear_after_the_server_started_are_found(tmp_path: Path) -> None:
    """The installer downloads them into a running server's directory: no restart should be needed."""
    geoip = GeoIp.from_settings(str(tmp_path), None, None)
    assert not geoip.has_city and not geoip.has_asn and geoip.place(TARGET) is None
    city_db(tmp_path / "dbip-city-lite-2026-11.mmdb", TOKYO)
    asn_db(tmp_path / "dbip-asn-lite-2026-11.mmdb")
    fast = GeoIp(directory=tmp_path, scan_every_s=0)  # a scan on every lookup, for the test
    assert fast.has_city and fast.has_asn
    assert fast.place(TARGET).name == "Tokyo"  # type: ignore[union-attr]
    # and a newer month's file replaces them
    newer = city_db(tmp_path / "dbip-city-lite-2026-12.mmdb", AMSTERDAM)
    os.utime(newer, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    assert fast.place(TARGET).name == "Amsterdam"  # type: ignore[union-attr]


def test_the_directory_is_not_scanned_on_every_lookup(tmp_path: Path) -> None:
    geoip = GeoIp.from_settings(str(tmp_path), None, None)
    assert geoip.place(TARGET) is None
    city_db(tmp_path / "dbip-city-lite.mmdb", TOKYO)
    # ten seconds have not passed: the scan that found nothing is not repeated yet
    assert geoip.place(TARGET) is None


def test_a_directory_uses_the_newest_files_in_it(tmp_path: Path) -> None:
    old = city_db(tmp_path / "dbip-city-lite-2026-10.mmdb", AMSTERDAM)
    os.utime(old, (1_000_000_000, 1_000_000_000))
    city_db(tmp_path / "dbip-city-lite-2026-11.mmdb", TOKYO)
    geoip = GeoIp.from_settings(str(tmp_path), None, None)
    assert geoip.place(TARGET).name == "Tokyo"  # type: ignore[union-attr]
    assert not geoip.has_asn
    explicit = GeoIp.from_settings(str(tmp_path), str(old), None)
    assert explicit.place(TARGET).name == "Amsterdam"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------------------------
# Ruling a place out
# ---------------------------------------------------------------------------------------------

FRANKFURT = ("n1", "Frankfurt", 50.11, 8.68)


def test_a_place_further_than_light_allows_is_ruled_out() -> None:
    tokyo = GeoPlace("Tokyo", "JP", 35.68, 139.69)
    found = rule_out(tokyo, [(*FRANKFURT, 4.0)])
    assert found is not None and found.node_name == "Frankfurt"
    assert found.distance_km > 9000 and found.max_distance_km == pytest.approx(4 * 150 + 60)
    # the same node with a slow round trip rules nothing out
    assert rule_out(tokyo, [(*FRANKFURT, 120.0)]) is None
    assert rule_out(tokyo, []) is None


def test_the_worst_offender_is_named() -> None:
    tokyo = GeoPlace("Tokyo", "JP", 35.68, 139.69)
    found = rule_out(
        tokyo, [("a", "London", 51.51, -0.13, 8.0), ("b", "Frankfurt", 50.11, 8.68, 3.0)]
    )
    # Frankfurt heard from the target in 3 ms (at most ~510 km) and is 9,000 km from Tokyo: the larger excess
    assert found is not None and found.node_name == "Frankfurt"


# ---------------------------------------------------------------------------------------------
# On a result
# ---------------------------------------------------------------------------------------------


def _measure(api: Api, verified: bool) -> dict[str, Any]:
    nodes = fleet(api, SITES, verified=verified)
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com", "count": 4}).json()["id"]
        for _, session, rtt in nodes:
            answer(session, rtt)
        return api.get(f"/api/v1/measurements/{mid}/location").json()  # type: ignore[no-any-return]
    finally:
        close(nodes)


def test_a_result_has_no_geoip_unless_a_database_is_set_up(api: Api) -> None:
    result = _measure(api, verified=True)
    assert result["geoip"] is None and result["asn"] is None


def test_a_database_that_names_a_place_the_measurements_rule_out_says_so(
    api: Api, tmp_path: Path
) -> None:
    api.client.app.state.geoip = GeoIp(  # type: ignore[attr-defined]
        city_db(tmp_path / "dbip-city-lite.mmdb", TOKYO), asn_db(tmp_path / "dbip-asn-lite.mmdb")
    )
    result = _measure(api, verified=True)
    geoip = result["geoip"]
    assert geoip["verdict"] == "ruled_out" and geoip["place"]["name"] == "Tokyo"
    # Frankfurt (6.5 ms) and London (8 ms) are both thousands of km from Tokyo; one of them is named
    assert geoip["ruled_out_by"]["node_name"] in {"Frankfurt", "London"}
    assert geoip["ruled_out_by"]["max_distance_km"] < geoip["ruled_out_by"]["distance_km"]
    assert "DB-IP" in geoip["attribution"] and geoip["source"] == "DB-IP Lite"
    assert result["asn"] == {"number": 15133, "organization": "Edgecast"}
    # the estimate itself is what it always was: a database does not move it
    assert result["status"] == "estimated" and result["likely"]["country"] != "JP"


def test_a_database_that_agrees_with_the_measurements_is_consistent(
    api: Api, tmp_path: Path
) -> None:
    api.client.app.state.geoip = GeoIp(city_db(tmp_path / "dbip-city-lite.mmdb", AMSTERDAM))  # type: ignore[attr-defined]
    geoip = _measure(api, verified=True)["geoip"]
    assert geoip["verdict"] == "consistent" and geoip["ruled_out_by"] is None
    assert geoip["distance_to_estimate_km"] < 600


def test_nodes_with_unverified_positions_are_not_believed_enough_to_rule_anything_out(
    api: Api, tmp_path: Path
) -> None:
    api.client.app.state.geoip = GeoIp(city_db(tmp_path / "dbip-city-lite.mmdb", TOKYO))  # type: ignore[attr-defined]
    geoip = _measure(api, verified=False)["geoip"]
    assert geoip["verdict"] == "not_checked" and geoip["ruled_out_by"] is None


def test_the_admin_can_see_whether_databases_are_loaded(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    assert api.get("/api/v1/admin/status").json()["geoip"] == {
        "city": False,
        "asn": False,
        "source": None,
        "geofeed_entries": 0,
    }
    api.client.app.state.geoip = GeoIp(  # type: ignore[attr-defined]
        city_db(tmp_path / "dbip-city-lite.mmdb"), asn_db(tmp_path / "dbip-asn-lite.mmdb")
    )
    assert api.get("/api/v1/admin/status").json()["geoip"] == {
        "city": True,
        "asn": True,
        "source": "DB-IP Lite",
        "geofeed_entries": 0,
    }


# ---------------------------------------------------------------------------------------------
# The updater (no network: the downloads are served by a function)
# ---------------------------------------------------------------------------------------------


def _served(directory: Path, kind: str) -> bytes:
    path = (city_db if kind == "city" else asn_db)(directory / f"served-{kind}.mmdb")
    return gzip.compress(path.read_bytes())


def test_months_are_this_one_and_the_last_across_a_new_year() -> None:
    assert months(date(2026, 11, 7)) == ["2026-11", "2026-10"]
    assert months(date(2027, 1, 3)) == ["2027-01", "2026-12"]


def test_the_updater_downloads_checks_and_replaces(tmp_path: Path) -> None:
    served = {kind: _served(tmp_path, kind) for kind in ("city", "asn")}
    asked: list[str] = []

    def opener(url: str) -> io.BytesIO:
        asked.append(url)
        kind = "city" if "-city-" in url else "asn"
        if "2026-11" in url:
            raise OSError("not published yet")  # this month's file is not out: use last month's
        return io.BytesIO(served[kind])

    target = tmp_path / "geoip"
    target.mkdir()
    (target / "dbip-city-lite-2026-09.mmdb").write_bytes(b"old")
    paths = update(target, date(2026, 11, 7), opener)
    assert sorted(p.name for p in paths) == [
        "dbip-asn-lite-2026-10.mmdb",
        "dbip-city-lite-2026-10.mmdb",
    ]
    assert sorted(p.name for p in target.iterdir()) == sorted(p.name for p in paths), (
        "old and partial files are gone"
    )
    assert any("2026-11" in u for u in asked) and all(
        u.startswith("https://download.db-ip.com/") for u in asked
    )
    assert GeoIp(target / "dbip-city-lite-2026-10.mmdb").place(TARGET).name == "Tokyo"  # type: ignore[union-attr]


def test_a_broken_or_wrong_download_changes_nothing(tmp_path: Path) -> None:
    keep = city_db(tmp_path / "dbip-city-lite-2026-10.mmdb", AMSTERDAM)
    before = keep.read_bytes()
    for body in (
        gzip.compress(b"not a database"),
        _served(tmp_path, "asn"),
    ):  # garbage; the wrong kind
        with pytest.raises(Exception):  # noqa: B017 - any failure is fine, the point is what is left
            fetch(tmp_path, "city", date(2026, 11, 7), lambda url, body=body: io.BytesIO(body))
        assert keep.read_bytes() == before
        assert not list(tmp_path.glob("*.part"))


def test_nothing_published_is_an_error(tmp_path: Path) -> None:
    def unavailable(url: str) -> io.BytesIO:
        raise OSError("no network")

    with pytest.raises(RuntimeError, match="could not download"):
        fetch(tmp_path, "city", date(2026, 11, 7), unavailable)


def test_a_database_of_the_wrong_kind_is_not_accepted(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not City"):
        check_database(asn_db(tmp_path / "a.mmdb"), "city")
