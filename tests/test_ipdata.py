"""IP data in the admin page and the API: the MaxMind account, the lookup API, and nodes filled in from their address."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from ipping_backend.config import Settings
from ipping_backend.geolocation.geoip import GeoIp
from ipping_backend.ipdata.updater import MaxMindUpdater
from ipping_backend.models import Node
from ipping_backend.nodes.autofill import fill, refresh_all

from .conftest import Api
from .test_api_keys import _key
from .test_geoip import asn_db, city_db
from .test_maxmind import ACCOUNT, KEY, FakeMaxMind

FRANKFURT = {
    "city": {"names": {"en": "Frankfurt am Main"}},
    "country": {"iso_code": "DE", "names": {"en": "Germany"}},
    "continent": {"code": "EU", "names": {"en": "Europe"}},
    "subdivisions": [{"iso_code": "HE", "names": {"en": "Hesse"}}],
    "postal": {"code": "60313"},
    "location": {
        "latitude": 50.11,
        "longitude": 8.68,
        "accuracy_radius": 20,
        "time_zone": "Europe/Berlin",
    },
}
MADRID = {
    "city": {"names": {"en": "Madrid"}},
    "country": {"iso_code": "ES", "names": {"en": "Spain"}},
    "location": {"latitude": 40.42, "longitude": -3.7},
}
PUBLIC = "93.184.216.34"
ADMIN = "/api/v1/admin/ip-data"


def _geoip(api: Api, directory: Path, place: dict[str, Any] = FRANKFURT) -> GeoIp:
    """Databases in the directory, as the updater leaves them, and the app reading them."""
    directory.mkdir(parents=True, exist_ok=True)
    city_db(directory / "GeoLite2-City.mmdb", place)
    asn_db(directory / "GeoLite2-ASN.mmdb")
    geoip = GeoIp(directory=directory)
    api.client.app.state.geoip = geoip  # type: ignore[attr-defined]
    return geoip


def _updater(api: Api, directory: Path, fake: FakeMaxMind | None = None) -> MaxMindUpdater:
    """An updater (against the pretend MaxMind) for this directory, which the app reads its databases from."""
    state = api.client.app.state  # type: ignore[attr-defined]
    settings = Settings(geoip_dir=str(directory), database_url="sqlite+aiosqlite://")
    state.geoip = GeoIp(directory=directory)
    updater = MaxMindUpdater(
        state.sessionmaker, settings, state.geoip, transport=fake.transport() if fake else None
    )
    state.maxmind = updater
    state.settings = settings
    return updater


def _wait(api: Api, done: Any, timeout: float = 10.0) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        data = api.get(ADMIN).json()
        if done(data):
            return data  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"timed out: {data}")


def _set_options(api: Api, **options: Any) -> None:
    response = api.request("PUT", ADMIN, json=options)
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------------------------
# The lookup API
# ---------------------------------------------------------------------------------------------


def test_an_address_is_looked_up_for_an_administrator(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    info = api.get(f"/api/v1/ip/{PUBLIC}").json()
    assert info["found"] and info["public"] and info["version"] == 4
    assert (info["country"], info["country_name"], info["continent"]) == ("DE", "Germany", "EU")
    assert (info["region"], info["region_code"], info["city"], info["postal_code"]) == (
        "Hesse",
        "HE",
        "Frankfurt am Main",
        "60313",
    )
    assert (info["latitude"], info["longitude"], info["accuracy_radius_km"]) == (50.11, 8.68, 20)
    assert info["time_zone"] == "Europe/Berlin" and info["network"] == "93.184.216.0/24"
    assert (info["asn"], info["organization"]) == (15133, "Edgecast")
    assert (
        info["source"] == "MaxMind GeoLite2"
        and "MaxMind" in info["attribution"]
        and info["database_built"]
    )


def test_what_is_not_an_address_a_public_one_or_known_is_said_so(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    private = api.get("/api/v1/ip/10.1.2.3").json()
    assert private["public"] is False and private["found"] is False and private["country"] is None
    unknown = api.get("/api/v1/ip/1.2.3.4").json()
    assert unknown["public"] and not unknown["found"] and unknown["country"] is None
    mapped = api.get(f"/api/v1/ip/::ffff:{PUBLIC}").json()
    assert mapped["address"] == PUBLIC and mapped["found"]
    bad = api.get("/api/v1/ip/not-an-address")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_address"


def test_without_a_database_the_lookup_says_how_to_get_one(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    api.client.app.state.geoip = GeoIp(directory=tmp_path / "none")  # type: ignore[attr-defined]
    response = api.get(f"/api/v1/ip/{PUBLIC}")
    assert response.status_code == 503 and response.json()["error"]["code"] == "no_database"
    assert "MaxMind" in response.json()["error"]["message"]


def test_the_callers_own_address(api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    monkeypatch.setattr("ipping_backend.ipdata.router.client_ip", lambda request: PUBLIC)
    mine = api.get("/api/v1/ip").json()
    assert mine["address"] == PUBLIC and mine["city"] == "Frankfurt am Main"


def test_who_may_look_addresses_up_is_a_setting(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    key = _key(api)["key"]
    owner = dict(api.client.cookies)
    url = f"/api/v1/ip/{PUBLIC}"

    def attempt(
        headers: dict[str, str] | None = None, as_admin: bool = False
    ) -> tuple[int, str | None]:
        api.client.cookies.clear()
        if as_admin:
            api.client.cookies.update(owner)
        response = api.client.get(url, headers=headers or {})
        return response.status_code, (response.json().get("error") or {}).get("code")

    # the default: an API key (or an administrator)
    assert attempt() == (401, "api_key_required")
    assert attempt({"Authorization": f"Bearer {key}"}) == (200, None)
    assert attempt({"Authorization": "Bearer ipk_nope"})[0] == 401
    assert attempt(as_admin=True) == (200, None)

    api.client.cookies.update(owner)
    _set_options(api, lookup_access="public")
    assert attempt() == (200, None)

    api.client.cookies.update(owner)
    _set_options(api, lookup_access="admin")
    assert attempt() == (403, "admin_required")
    assert attempt({"Authorization": f"Bearer {key}"}) == (403, "admin_required")
    assert attempt(as_admin=True) == (200, None)

    api.client.cookies.update(owner)
    _set_options(api, lookup_enabled=False, lookup_access="public")
    assert attempt() == (404, "ip_lookup_disabled")
    assert attempt(as_admin=True) == (404, "ip_lookup_disabled")  # off is off


def test_lookups_are_limited_a_minute_for_everyone_but_administrators(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    _set_options(api, lookup_access="public", lookup_per_minute=3)
    owner = dict(api.client.cookies)
    api.client.cookies.clear()
    codes = [api.client.get(f"/api/v1/ip/{PUBLIC}").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    api.client.cookies.update(owner)
    assert all(
        api.get(f"/api/v1/ip/{PUBLIC}").status_code == 200 for _ in range(5)
    )  # an administrator is not limited


# ---------------------------------------------------------------------------------------------
# The admin page
# ---------------------------------------------------------------------------------------------


def test_the_page_starts_empty_and_asks_an_administrator(api: Api, tmp_path: Path) -> None:
    assert api.get(ADMIN).status_code in (401, 403)
    api.become_admin()
    _updater(api, tmp_path / "geoip")
    page = api.get(ADMIN).json()
    assert (
        page["maxmind"]["license_key_set"] is False
        and page["maxmind"]["credentials_source"] == "none"
    )
    assert (
        page["maxmind"]["editions"] == ["GeoLite2-City", "GeoLite2-ASN"]
        and page["maxmind"]["auto_update"] is True
    )
    assert page["maxmind"]["directory_configured"] is True and page["maxmind"]["running"] is False
    assert [d["edition"] for d in page["maxmind"]["databases"]] == [
        "GeoLite2-City",
        "GeoLite2-Country",
        "GeoLite2-ASN",
    ]
    assert not any(d["present"] for d in page["maxmind"]["databases"])
    assert page["lookup"] == {"enabled": True, "access": "api_key", "per_minute": 60}
    assert page["node_autofill"] is True and page["databases_loaded"] is False


def test_the_license_key_is_kept_and_used_but_never_sent_back(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _updater(api, tmp_path / "geoip")
    response = api.request(
        "PUT", ADMIN, json={"account_id": ACCOUNT, "license_key": KEY, "maxmind_auto_update": False}
    )
    assert response.status_code == 200
    page = response.json()
    assert page["maxmind"]["account_id"] == ACCOUNT and page["maxmind"]["license_key_set"] is True
    assert page["maxmind"]["credentials_source"] == "admin"
    assert KEY not in response.text and KEY not in api.get(ADMIN).text
    assert (
        KEY not in api.get("/api/v1/admin/audit").text
        and KEY not in api.get("/api/v1/admin/settings").text
    )
    # changing the account ID keeps the key; an empty key means "keep it"
    page = api.request("PUT", ADMIN, json={"account_id": "654321", "license_key": ""}).json()
    assert page["maxmind"]["account_id"] == "654321" and page["maxmind"]["license_key_set"] is True
    # removing them
    page = api.request("PUT", ADMIN, json={"clear_credentials": True}).json()
    assert page["maxmind"] == {
        **page["maxmind"],
        "account_id": "",
        "license_key_set": False,
        "credentials_source": "none",
    }
    audit = api.get("/api/v1/admin/audit").json()
    changed = [e for e in audit if e["action"] == "ip_data.updated"]
    assert changed and all(KEY not in str(e) for e in changed)


def test_what_cannot_be_a_key_an_account_or_a_choice_is_refused(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _updater(api, tmp_path / "geoip")
    for body in (
        {"account_id": "my account"},
        {"license_key": "short"},
        {"license_key": "has spaces in it, so no no no no"},
        {"maxmind_editions": []},
        {"maxmind_editions": ["GeoIP2-City"]},
        {"maxmind_update_hours": 1},
        {"lookup_access": "everyone"},
        {"lookup_per_minute": 0},
    ):
        assert api.request("PUT", ADMIN, json=body).status_code == 422, body


def test_credentials_in_the_environment_win_over_the_page(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    updater = _updater(api, tmp_path / "geoip")
    updater._settings = Settings(  # noqa: SLF001
        geoip_dir=str(tmp_path / "geoip"),
        maxmind_account_id="42",
        maxmind_license_key="envkey_envkey_envkey_1234",
    )
    api.client.app.state.settings = updater._settings  # type: ignore[attr-defined]  # noqa: SLF001
    page = api.get(ADMIN).json()["maxmind"]
    assert (
        page["credentials_source"] == "environment"
        and page["account_id"] == "42"
        and page["license_key_set"]
    )
    assert "envkey" not in api.get(ADMIN).text


def test_update_now_downloads_in_the_background_and_the_page_shows_how_it_went(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    fake = FakeMaxMind(tmp_path)
    directory = tmp_path / "geoip"
    directory.mkdir()
    _updater(api, directory, fake)
    none = api.post(f"{ADMIN}/update")
    assert none.status_code == 409 and none.json()["error"]["code"] == "no_credentials"
    api.request(
        "PUT", ADMIN, json={"account_id": ACCOUNT, "license_key": KEY, "maxmind_auto_update": False}
    )
    assert api.post(f"{ADMIN}/update").status_code == 202
    page = _wait(api, lambda d: not d["maxmind"]["running"] and d["maxmind"]["last_success"])
    assert (
        page["maxmind"]["error"] is None
        and page["databases_loaded"]
        and page["source"] == "MaxMind GeoLite2"
    )
    present = {d["edition"]: d for d in page["maxmind"]["databases"] if d["present"]}
    assert sorted(present) == ["GeoLite2-ASN", "GeoLite2-City"]
    assert all(
        d["built"] and d["size"] and d["downloaded_at"] and d["error"] is None
        for d in present.values()
    )
    api.request("PUT", ADMIN, json={"lookup_access": "public"})
    assert api.get(f"/api/v1/ip/{PUBLIC}").json()["found"]  # used at once, without a restart


def test_a_key_that_is_given_starts_a_download_by_itself(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    fake = FakeMaxMind(tmp_path)
    directory = tmp_path / "geoip"
    directory.mkdir()
    _updater(api, directory, fake)
    api.request("PUT", ADMIN, json={"account_id": ACCOUNT, "license_key": KEY})
    page = _wait(api, lambda d: d["databases_loaded"])
    assert page["maxmind"]["last_success"]


def test_a_refused_key_is_shown_on_the_page(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    fake = FakeMaxMind(tmp_path)
    directory = tmp_path / "geoip"
    directory.mkdir()
    _updater(api, directory, fake)
    api.request(
        "PUT", ADMIN, json={"account_id": ACCOUNT, "license_key": "wrongwrongwrongwrong1234"}
    )
    page = _wait(api, lambda d: d["maxmind"]["error"])
    assert page["maxmind"]["error"]["code"] == "refused" and "wrongwrong" not in api.get(ADMIN).text
    assert page["maxmind"]["next_check"] and not page["databases_loaded"]


def test_there_is_nowhere_to_download_to_without_a_directory(api: Api) -> None:
    api.become_admin()
    state = api.client.app.state  # type: ignore[attr-defined]
    state.maxmind = MaxMindUpdater(
        state.sessionmaker, Settings(database_url="sqlite+aiosqlite://"), state.geoip
    )
    api.request(
        "PUT", ADMIN, json={"account_id": ACCOUNT, "license_key": KEY, "maxmind_auto_update": False}
    )
    response = api.post(f"{ADMIN}/update")
    assert response.status_code == 409 and response.json()["error"]["code"] == "no_directory"
    assert api.get(ADMIN).json()["maxmind"]["directory_configured"] is False


# ---------------------------------------------------------------------------------------------
# Nodes filled in from their address
# ---------------------------------------------------------------------------------------------


def _node(api: Api, **fields: Any) -> str:
    node_id, _ = api.enroll_node("Falkenstein", approve=False, **fields)
    return node_id


def _address(api: Api, node_id: str, address: str | None = PUBLIC) -> None:
    async def go() -> None:
        async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            await db.execute(update(Node).where(Node.id == node_id).values(remote_addr=address))
            await db.commit()

    api.client.portal.call(go)


def _admin_node(api: Api, node_id: str) -> dict[str, Any]:
    return next(n for n in api.get("/api/v1/admin/nodes").json() if n["id"] == node_id)  # type: ignore[no-any-return]


def test_a_node_that_said_nothing_about_itself_is_filled_in_from_its_address(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    node = _node(api, country=None, city=None)
    _address(api, node)
    filled = api.post(f"/api/v1/admin/nodes/{node}/autofill").json()
    assert (filled["country"], filled["region"], filled["city"]) == (
        "DE",
        "Hesse",
        "Frankfurt am Main",
    )
    assert (filled["latitude"], filled["longitude"]) == (50.11, 8.68)
    assert (filled["provider"], filled["asn"]) == ("Edgecast", 15133)
    assert filled["auto_fields"] == [
        "country",
        "region",
        "city",
        "latitude",
        "longitude",
        "provider",
        "asn",
    ]
    assert filled["location_verified"] is False  # a city's position is not where the node is


def test_what_the_node_reported_or_an_administrator_typed_is_not_overwritten(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    node = _node(api, country="DE", city="Hanau", provider="My own network")  # said by the node
    _address(api, node)
    filled = api.post(f"/api/v1/admin/nodes/{node}/autofill").json()
    assert (filled["country"], filled["city"], filled["provider"]) == (
        "DE",
        "Hanau",
        "My own network",
    )
    assert filled["region"] == "Hesse" and filled["asn"] == 15133  # the rest comes from the data
    assert filled["auto_fields"] == ["region", "latitude", "longitude", "asn"]


def test_the_datas_place_is_not_used_where_it_contradicts_the_place_given(
    api: Api, tmp_path: Path
) -> None:
    """The data says Frankfurt. A node that said another country, or a city far from there, keeps its own place: the
    estimate trusts a node's position, and a hosting address is often placed at its provider's headquarters."""
    api.become_admin()
    _geoip(api, tmp_path / "geoip")  # Frankfurt am Main, DE
    other_country = _node(api, country="NL", city="Utrecht")
    far_city = _node(
        api, country="DE", city="Berlin"
    )  # the gazetteer knows Berlin: about 420 km away
    near_city = _node(api, country="DE", city="Frankfurt am Main")
    for node in (other_country, far_city, near_city):
        _address(api, node)
        api.post(f"/api/v1/admin/nodes/{node}/autofill")
    for node in (other_country, far_city):
        said = _admin_node(api, node)
        assert (said["region"], said["latitude"], said["longitude"]) == (None, None, None)
        assert (said["provider"], said["asn"]) == (
            "Edgecast",
            15133,
        )  # about the address, not a place
        assert said["auto_fields"] == ["provider", "asn"]
    agreeing = _admin_node(api, near_city)
    assert (agreeing["region"], agreeing["latitude"]) == ("Hesse", 50.11)
    assert agreeing["auto_fields"] == ["region", "latitude", "longitude", "provider", "asn"]


def test_a_typed_position_far_from_the_data_is_kept_and_the_data_keeps_out(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    far, near = _node(api, country=None, city=None), _node(api, country=None, city=None)
    for node, (lat, lon) in ((far, (52.52, 13.40)), (near, (50.0, 8.5))):
        _address(api, node)
        api.patch(f"/api/v1/admin/nodes/{node}", {"latitude": lat, "longitude": lon})
    kept = _admin_node(api, far)
    assert (kept["latitude"], kept["longitude"]) == (52.52, 13.4)
    assert (kept["country"], kept["city"], kept["region"]) == (None, None, None)
    assert kept["auto_fields"] == ["provider", "asn"]
    close = _admin_node(api, near)
    assert (close["latitude"], close["longitude"]) == (50.0, 8.5)  # still theirs
    assert (close["country"], close["city"]) == ("DE", "Frankfurt am Main")  # agreeing: filled in
    assert "latitude" not in close["auto_fields"]


def test_typing_a_place_that_disagrees_takes_back_what_the_data_supplied(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    node = _node(api, country=None, city=None)
    _address(api, node)
    api.post(f"/api/v1/admin/nodes/{node}/autofill")
    typed = api.patch(f"/api/v1/admin/nodes/{node}", {"country": "NL"}).json()
    assert typed["country"] == "NL"
    assert (typed["region"], typed["city"], typed["latitude"], typed["longitude"]) == (None,) * 4
    assert typed["auto_fields"] == ["provider", "asn"]
    back = api.patch(f"/api/v1/admin/nodes/{node}", {"country": None}).json()
    assert (back["country"], back["city"], back["latitude"]) == ("DE", "Frankfurt am Main", 50.11)
    assert back["auto_fields"] == [
        "country",
        "region",
        "city",
        "latitude",
        "longitude",
        "provider",
        "asn",
    ]


def test_a_record_with_no_city_is_not_a_position_for_a_node(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(
        api,
        tmp_path / "geoip",
        {
            "country": {"iso_code": "DE", "names": {"en": "Germany"}},
            "location": {"latitude": 51.0, "longitude": 9.0, "accuracy_radius": 1000},
        },
    )
    node = _node(api, country=None, city=None)
    _address(api, node)
    filled = api.post(f"/api/v1/admin/nodes/{node}/autofill").json()
    assert filled["country"] == "DE"
    assert (filled["city"], filled["latitude"], filled["longitude"]) == (None, None, None)


def test_typing_a_value_makes_it_yours_and_clearing_it_hands_it_back(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    node = _node(api, country=None, city=None)
    _address(api, node)
    api.post(f"/api/v1/admin/nodes/{node}/autofill")
    typed = api.patch(
        f"/api/v1/admin/nodes/{node}", {"city": "Hanau", "provider": "Acme Hosting"}
    ).json()
    assert (typed["city"], typed["provider"]) == ("Hanau", "Acme Hosting")
    assert typed["auto_fields"] == ["country", "region", "latitude", "longitude", "asn"]
    assert (
        api.post(f"/api/v1/admin/nodes/{node}/autofill").json()["city"] == "Hanau"
    )  # still theirs
    # the data changes: only the automatic fields follow
    _geoip(api, tmp_path / "geoip2", MADRID)
    followed = api.post(f"/api/v1/admin/nodes/{node}/autofill").json()
    assert (followed["country"], followed["city"], followed["provider"]) == (
        "ES",
        "Hanau",
        "Acme Hosting",
    )
    assert followed["region"] == "Hesse"  # the new data has no region: the old value stays
    # clearing a field makes it automatic again
    back = api.patch(f"/api/v1/admin/nodes/{node}", {"city": None}).json()
    assert back["city"] == "Madrid" and "city" in back["auto_fields"]
    # typing one half of a position makes the pair theirs
    pair = api.patch(f"/api/v1/admin/nodes/{node}", {"latitude": 1.5}).json()
    assert (
        pair["latitude"] == 1.5
        and "latitude" not in pair["auto_fields"]
        and "longitude" not in pair["auto_fields"]
    )


def test_a_node_without_a_public_address_or_a_database_is_left_alone(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    node = _node(api, country=None, city=None)
    no_address = api.post(f"/api/v1/admin/nodes/{node}/autofill")
    assert (
        no_address.status_code == 200 and no_address.json()["auto_fields"] == []
    )  # "testclient" is not an address
    _address(api, node, "10.0.0.5")
    assert api.post(f"/api/v1/admin/nodes/{node}/autofill").json()["country"] is None  # private
    _address(api, node, "1.2.3.4")
    assert (
        api.post(f"/api/v1/admin/nodes/{node}/autofill").json()["country"] is None
    )  # not in the databases
    _address(api, node, None)
    gone = api.post(f"/api/v1/admin/nodes/{node}/autofill")
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "no_address"


def test_a_node_is_filled_in_when_it_enrolls_and_when_the_data_changes(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    monkeypatch.setattr("ipping_backend.nodes.router.client_ip", lambda request: PUBLIC)
    node = _node(api, country=None, city=None)
    enrolled = _admin_node(api, node)
    assert (enrolled["country"], enrolled["city"], enrolled["asn"]) == (
        "DE",
        "Frankfurt am Main",
        15133,
    )

    # the database is replaced by a newer one: every automatic field follows, at once
    state = api.client.app.state  # type: ignore[attr-defined]
    state.geoip = GeoIp(directory=_geoip_dir(tmp_path / "newer", MADRID))
    assert api.client.portal.call(refresh_all, state.sessionmaker, state.geoip) == 1
    after = _admin_node(api, node)
    assert (after["country"], after["city"]) == ("ES", "Madrid") and after["asn"] == 15133


def _geoip_dir(directory: Path, place: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    city_db(directory / "GeoLite2-City.mmdb", place)
    asn_db(directory / "GeoLite2-ASN.mmdb")
    return directory


def test_turning_auto_fill_off_leaves_nodes_alone_and_turning_it_on_fills_them(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    _set_options(api, node_autofill=False)
    node = _node(api, country=None, city=None)
    _address(api, node)
    state = api.client.app.state  # type: ignore[attr-defined]
    assert api.client.portal.call(refresh_all, state.sessionmaker, state.geoip) == 0
    assert _admin_node(api, node)["country"] is None
    assert (
        api.post(f"/api/v1/admin/nodes/{node}/autofill").json()["country"] == "DE"
    )  # asked for by hand: it works
    api.patch(
        f"/api/v1/admin/nodes/{node}", {"country": None, "city": None}
    )  # cleared, and off: stays empty
    assert _admin_node(api, node)["country"] is None
    _set_options(api, node_autofill=True)  # on: what is empty is filled now
    assert _admin_node(api, node)["country"] == "DE"


def test_fill_follows_the_data_only_for_fields_it_filled(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    geoip = _geoip(api, tmp_path / "geoip")
    state = api.client.app.state  # type: ignore[attr-defined]

    async def run() -> tuple[list[str], list[str]]:
        async with state.sessionmaker() as db:
            node = (await db.execute(select(Node).limit(1))).scalar_one_or_none()
            assert node is None
            fresh = Node(
                id="n" * 32, name="x", credential_hash="x", remote_addr=PUBLIC, auto_fields=[]
            )
            first = fill(fresh, geoip)
            second = fill(fresh, geoip)  # nothing new: nothing changed
            return first, second

    first, second = api.client.portal.call(run)
    assert "country" in first and "asn" in first and second == []


# ---------------------------------------------------------------------------------------------
# The addresses a measurement tested, with what the databases say: part of the measurement, not of the lookup API
# ---------------------------------------------------------------------------------------------


def _answer_with(session: Any, address: str, rtt_us: int = 9_000) -> None:
    import json

    task = json.loads(session.receive_text())
    ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
    session.send_text(json.dumps({"type": "task_ack", **ids}))
    session.send_text(
        json.dumps({"type": "sample", **ids, "seq": 0, "status": "ok", "rtt_us": rtt_us})
    )
    session.send_text(
        json.dumps({"type": "task_result", **ids, "status": "completed", "tested_ip": address})
    )
    from .test_api import barrier

    barrier(session)


def test_a_measurement_says_what_the_databases_know_of_every_address_it_tested(
    api: Api, tmp_path: Path
) -> None:
    from .test_api import connect

    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    # the lookup API itself is closed to everybody but administrators: the measurement's own addresses are not
    assert api.request("PUT", ADMIN, json={"lookup_access": "admin"}).status_code == 200
    sessions = []
    for name in ("A", "B", "C"):
        _, credential = api.enroll_node(name)
        sessions.append(connect(api, credential))
    try:
        mid = api.post(
            "/api/v1/measurements", {"target": "example.com", "dns_mode": "regional"}
        ).json()["id"]
        for (_, session), address in zip(sessions, [PUBLIC, PUBLIC, "93.184.216.35"], strict=True):
            _answer_with(session, address)
        stranger = Api(api.client)
        owner = dict(api.client.cookies)
        got = api.get(f"/api/v1/measurements/{mid}/addresses")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["databases_loaded"] is True
        assert [(a["address"], a["nodes"]) for a in body["addresses"]] == [
            (PUBLIC, 2),
            ("93.184.216.35", 1),
        ]
        first = body["addresses"][0]["info"]
        assert first["city"] == "Frankfurt am Main" and first["country"] == "DE" and first["asn"]
        # a stranger (no session of the owner) cannot read it, like the measurement; once shared, anyone can
        api.client.cookies.clear()
        stranger.start_session()
        assert stranger.get(f"/api/v1/measurements/{mid}/addresses").status_code == 404
        api.client.cookies.update(owner)
        assert api.post(f"/api/v1/measurements/{mid}/share", {"shared": True}).status_code == 200
        api.client.cookies.clear()
        stranger.start_session()
        shared = stranger.get(f"/api/v1/measurements/{mid}/addresses")
        assert shared.status_code == 200 and shared.json()["addresses"][0]["info"]["asn"]
        # while the standalone lookup stays closed to the same visitor
        assert stranger.get(f"/api/v1/ip/{PUBLIC}").status_code in (401, 403)
    finally:
        for ws, _ in sessions:
            ws.__exit__(None, None, None)


def test_without_a_database_the_addresses_are_listed_and_say_so(api: Api) -> None:
    from .test_api import connect

    api.become_admin()
    api.client.app.state.geoip = GeoIp()  # type: ignore[attr-defined]
    _, credential = api.enroll_node("A")
    ws, session = connect(api, credential)
    try:
        mid = api.post("/api/v1/measurements", {"target": PUBLIC}).json()["id"]
        _answer_with(session, PUBLIC)
        body = api.get(f"/api/v1/measurements/{mid}/addresses").json()
        assert body["databases_loaded"] is False
        assert body["addresses"] == [{"address": PUBLIC, "nodes": 1, "info": None}]
    finally:
        ws.__exit__(None, None, None)


def test_a_private_address_a_node_reports_is_never_kept_so_never_listed(
    api: Api, tmp_path: Path
) -> None:
    from .test_api import connect

    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    _, credential = api.enroll_node("A")
    ws, session = connect(api, credential)
    try:
        mid = api.post(
            "/api/v1/measurements", {"target": "example.com", "dns_mode": "regional"}
        ).json()["id"]
        _answer_with(session, "10.1.2.3")  # a node's report is never trusted to be public
        body = api.get(f"/api/v1/measurements/{mid}/addresses").json()
        assert body["databases_loaded"] is True and body["addresses"] == []
    finally:
        ws.__exit__(None, None, None)
