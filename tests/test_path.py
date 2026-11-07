"""The networks and places of the routers on a traced path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ipping_backend.geolocation.geoip import GeoIp

from .conftest import Api
from .test_api import connect
from .test_geoip import make_db
from .test_probe_kinds import ALL_KINDS, _complete, _send, _start, _task

STOCKHOLM = {
    "city": {"names": {"en": "Stockholm"}},
    "country": {"iso_code": "SE", "names": {"en": "Sweden"}},
    "location": {"latitude": 59.33, "longitude": 18.07},
}
ARELION = {"autonomous_system_number": 1299, "autonomous_system_organization": "Arelion"}
EDGECAST = {"autonomous_system_number": 15133, "autonomous_system_organization": "Edgecast"}


def _databases(directory: Path) -> GeoIp:
    city = make_db(
        directory / "dbip-city-lite.mmdb",
        "City",
        {"62.115.0.0/16": STOCKHOLM, "93.184.216.0/24": STOCKHOLM},
    )
    asn = make_db(
        directory / "dbip-asn-lite.mmdb",
        "ASN",
        {"62.115.0.0/16": ARELION, "93.184.216.0/24": EDGECAST, "80.239.0.0/16": ARELION},
    )
    return GeoIp(city, asn)


def _trace(api: Api) -> str:
    """A finished four-hop trace: a private hop, two public routers (one unknown to the databases)
    and the destination."""
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        measurement = _start(api, target="example.com", kind="traceroute")
        task = _task(session)
        hops: list[dict[str, Any]] = [
            {"ttl": 1, "ip": "192.168.1.1", "rtts_us": [800]},
            {"ttl": 2, "ip": "62.115.1.1", "rtts_us": [9000]},
            {"ttl": 3, "ip": "81.1.2.3", "rtts_us": [11000]},
            {"ttl": 4, "ip": "93.184.216.34", "rtts_us": [20_000], "reached": True},
        ]
        for seq, detail in enumerate(hops):
            _send(
                session,
                task,
                type="sample",
                seq=seq,
                status="ok",
                rtt_us=detail["rtts_us"][0],
                detail=detail,
            )
        _complete(session, task)
        return str(measurement["id"])
    finally:
        ws.__exit__(None, None, None)


def test_without_databases_the_path_has_nothing_to_add(api: Api) -> None:
    mid = _trace(api)
    path = api.get(f"/api/v1/measurements/{mid}/path").json()
    assert path == {
        "source": None,
        "attribution": None,
        "city_database": False,
        "asn_database": False,
        "hops": {},
    }


def test_each_public_router_gets_its_network_and_place(api: Api, tmp_path: Path) -> None:
    api.client.app.state.geoip = _databases(tmp_path)  # type: ignore[attr-defined]
    mid = _trace(api)
    path = api.get(f"/api/v1/measurements/{mid}/path").json()
    assert path["city_database"] is True and path["asn_database"] is True
    assert path["source"] == "DB-IP Lite" and "DB-IP" in path["attribution"]
    # The private hop has no address to look up, and 81.1.2.3 is in neither database.
    assert sorted(path["hops"]) == ["62.115.1.1", "93.184.216.34"]
    assert path["hops"]["62.115.1.1"] == {
        "asn": 1299,
        "as_org": "Arelion",
        "country": "SE",
        "place": "Stockholm",
        "latitude": 59.33,
        "longitude": 18.07,
    }
    assert path["hops"]["93.184.216.34"]["asn"] == 15133


def test_only_a_network_is_enough_when_there_is_no_city_database(api: Api, tmp_path: Path) -> None:
    only_asn = make_db(tmp_path / "dbip-asn-lite.mmdb", "ASN", {"62.115.0.0/16": ARELION})
    api.client.app.state.geoip = GeoIp(None, only_asn)  # type: ignore[attr-defined]
    mid = _trace(api)
    path = api.get(f"/api/v1/measurements/{mid}/path").json()
    assert path["city_database"] is False and path["asn_database"] is True
    assert path["hops"] == {
        "62.115.1.1": {
            "asn": 1299,
            "as_org": "Arelion",
            "country": None,
            "place": None,
            "latitude": None,
            "longitude": None,
        }
    }


def test_a_ping_has_no_path_but_still_says_which_databases_exist(api: Api, tmp_path: Path) -> None:
    api.client.app.state.geoip = _databases(tmp_path)  # type: ignore[attr-defined]
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential)
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        path = api.get(f"/api/v1/measurements/{mid}/path").json()
        assert path["hops"] == {} and path["city_database"] is True
    finally:
        ws.__exit__(None, None, None)


def test_a_path_is_as_private_as_its_measurement(api: Api, tmp_path: Path) -> None:
    api.client.app.state.geoip = _databases(tmp_path)  # type: ignore[attr-defined]
    mid = _trace(api)
    owner_cookies = dict(api.client.cookies)
    api.client.cookies.clear()
    stranger = Api(api.client)
    stranger.start_session()
    assert stranger.get(f"/api/v1/measurements/{mid}/path").status_code == 404

    api.client.cookies.clear()
    api.client.cookies.update(owner_cookies)
    assert api.post(f"/api/v1/measurements/{mid}/share", {"shared": True}).status_code == 200
    api.client.cookies.clear()
    stranger.start_session()
    assert stranger.get(f"/api/v1/measurements/{mid}/path").status_code == 200
