"""IP data in the admin page and the API: every source the same, the lookup API, and nodes filled in from their address."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from ipping_backend.config import Settings
from ipping_backend.geolocation.geoip import GeoIp
from ipping_backend.ipdata import runtime
from ipping_backend.ipdata.catalog import IDS
from ipping_backend.ipdata.downloader import DataUpdater
from ipping_backend.ipdata.service import IpDataService
from ipping_backend.models import AppSetting, Node
from ipping_backend.nodes.autofill import fill, refresh_all

from .conftest import Api
from .test_api_keys import _key
from .test_geoip import asn_db, city_db, service_over
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
OTHERS = [source for source in IDS if source != "maxmind"]


def _geoip(api: Api, directory: Path, place: dict[str, Any] = FRANKFURT) -> GeoIp:
    """Databases in the directory, as the downloader leaves them, and the app reading them."""
    directory.mkdir(parents=True, exist_ok=True)
    city_db(directory / "GeoLite2-City.mmdb", place)
    asn_db(directory / "GeoLite2-ASN.mmdb")
    geoip = GeoIp(directory=directory)
    api.client.app.state.geoip = geoip  # type: ignore[attr-defined]
    return geoip


def _updater(api: Api, directory: Path | None, fake: FakeMaxMind | None = None) -> DataUpdater:
    """The app set up as it is with a data directory: a service and a downloader (against the pretend MaxMind) for it."""
    state = api.client.app.state  # type: ignore[attr-defined]
    settings = Settings(
        geoip_dir=str(directory) if directory else None, database_url="sqlite+aiosqlite://"
    )
    state.geoip = GeoIp(directory=directory)
    state.settings = settings
    state.ipdata = IpDataService(lambda: state.geoip, directory)
    state.dataupdater = DataUpdater(
        state.sessionmaker,
        settings,
        directory,
        lambda: state.geoip,
        transport=fake.transport() if fake else None,
    )
    api.client.portal.call(runtime.reload, state)
    return state.dataupdater  # type: ignore[no-any-return]


def _only_maxmind(api: Api) -> None:
    """Every other source off: a test of MaxMind's download talks to the pretend MaxMind only."""
    body = {"sources": {source: {"enabled": False} for source in OTHERS}}
    assert api.request("PUT", ADMIN, json=body).status_code == 200


def _give_maxmind(api: Api, account: str = ACCOUNT, key: str = KEY) -> Any:
    body = {"sources": {"maxmind": {"credentials": {"account_id": account, "license_key": key}}}}
    return api.request("PUT", ADMIN, json=body)


def _source(page: dict[str, Any], source: str) -> dict[str, Any]:
    return next(s for s in page["sources"] if s["id"] == source)  # type: ignore[no-any-return]


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
    assert "admin page" in response.json()["error"]["message"]


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
# The admin page: one list, every source the same
# ---------------------------------------------------------------------------------------------


def test_the_page_starts_empty_and_asks_an_administrator(api: Api, tmp_path: Path) -> None:
    assert api.get(ADMIN).status_code in (401, 403)
    api.become_admin()
    _updater(api, tmp_path / "geoip")
    page = api.get(ADMIN).json()
    assert "maxmind" not in page and "databases" not in page, "MaxMind has no block of its own"
    assert [s["id"] for s in page["sources"]] == list(IDS)
    assert all(s["enabled"] for s in page["sources"]), "all on by default"
    assert all("kind" not in s for s in page["sources"]), "every source is a database"
    assert page["lookup"] == {"enabled": True, "access": "api_key", "per_minute": 60}
    assert page["node_autofill"] is True and page["databases_loaded"] is False
    assert page["directory_configured"] is True and page["offline"] is False
    assert page["data_update_hours"] == 24
    # a source that needs an account says so, in the same way whichever it is
    needs = {
        s["id"]: [c["name"] for c in s["credentials"]] for s in page["sources"] if s["credentials"]
    }
    assert needs == {
        "maxmind": ["account_id", "license_key"],
        "ipinfo": ["token"],
        "ip2location": ["download_token"],
    }
    for source in needs:
        assert _source(page, source)["state"] == "needs_setup"
        assert all(
            c["set"] is False and c["required"] for c in _source(page, source)["credentials"]
        )
    for source in ("dbip", "iplocate", "ipapi_is", "iptoasn", "ipdeny", "ip2region"):
        assert _source(page, source)["state"] == "waiting" and not _source(page, source)["files"]


def test_the_credentials_of_every_source_are_kept_and_used_but_never_sent_back(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _updater(api, tmp_path / "geoip")
    body = {
        "sources": {
            "maxmind": {"credentials": {"account_id": ACCOUNT, "license_key": KEY}},
            "ipinfo": {"credentials": {"token": "ipinfo_token_1234567890"}},
            "ip2location": {"credentials": {"download_token": "ip2location_token_1234567890"}},
        }
    }
    response = api.request("PUT", ADMIN, json=body)
    assert response.status_code == 200
    page = response.json()
    mm = {c["name"]: c for c in _source(page, "maxmind")["credentials"]}
    assert mm["account_id"]["set"] and mm["account_id"]["value"] == ACCOUNT  # not a secret
    assert mm["license_key"]["set"] and mm["license_key"]["value"] is None
    assert all(c["from_environment"] is False for c in mm.values())
    assert _source(page, "ipinfo")["credentials"][0]["set"] is True
    assert _source(page, "ip2location")["credentials"][0]["set"] is True
    secrets = (KEY, "ipinfo_token_1234567890", "ip2location_token_1234567890")
    for text in (response.text, api.get(ADMIN).text, api.get("/api/v1/admin/audit").text):
        assert not any(secret in text for secret in secrets)
    assert not any(secret in api.get("/api/v1/admin/settings").text for secret in secrets)
    # changing the account ID keeps the key; an empty one clears it
    page = api.request(
        "PUT", ADMIN, json={"sources": {"maxmind": {"credentials": {"account_id": "654321"}}}}
    ).json()
    mm = {c["name"]: c for c in _source(page, "maxmind")["credentials"]}
    assert mm["account_id"]["value"] == "654321" and mm["license_key"]["set"] is True
    page = api.request(
        "PUT",
        ADMIN,
        json={"sources": {"maxmind": {"credentials": {"account_id": "", "license_key": ""}}}},
    ).json()
    assert not any(c["set"] for c in _source(page, "maxmind")["credentials"])
    assert _source(page, "ipinfo")["credentials"][0]["set"] is True  # the others are as they were
    audit = [e for e in api.get("/api/v1/admin/audit").json() if e["action"] == "ip_data.updated"]
    assert audit and all(not any(secret in str(e) for secret in secrets) for e in audit)


def test_what_cannot_be_a_key_an_account_or_a_choice_is_refused(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _updater(api, tmp_path / "geoip")

    def credential(source: str, **fields: str) -> dict[str, Any]:
        return {"sources": {source: {"credentials": fields}}}

    for body in (
        credential("maxmind", account_id="my account"),
        credential("maxmind", license_key="short"),
        credential("maxmind", license_key="has spaces in it, so no no no no"),
        credential("ipinfo", token="has a space"),
        credential("dbip", token="a source that takes no key"),
        credential("ipinfo", api_key="not the name it takes"),
        {"sources": {"nobody": {"enabled": True}}},
        {"data_update_hours": 1},
        {"lookup_access": "everyone"},
        {"lookup_per_minute": 0},
    ):
        assert api.request("PUT", ADMIN, json=body).status_code == 422, body


def test_credentials_in_the_environment_win_over_the_page(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _updater(api, tmp_path / "geoip")
    api.client.app.state.settings = Settings(  # type: ignore[attr-defined]
        geoip_dir=str(tmp_path / "geoip"),
        maxmind_account_id="42",
        maxmind_license_key="envkey_envkey_envkey_1234",
    )
    page = api.get(ADMIN).json()
    mm = {c["name"]: c for c in _source(page, "maxmind")["credentials"]}
    assert mm["account_id"]["value"] == "42" and mm["license_key"]["set"]
    assert all(c["from_environment"] for c in mm.values())
    assert not any(c["from_environment"] for c in _source(page, "ipinfo")["credentials"])
    assert "envkey" not in api.get(ADMIN).text


def test_a_maxmind_account_saved_by_an_earlier_version_is_read_and_then_kept_with_the_others(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _updater(api, tmp_path / "geoip")

    async def legacy() -> None:
        async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            db.add(
                AppSetting(
                    key="maxmind_credentials", value={"account_id": "777", "license_key": KEY}
                )
            )
            await db.commit()

    api.client.portal.call(legacy)
    mm = {c["name"]: c for c in _source(api.get(ADMIN).json(), "maxmind")["credentials"]}
    assert mm["account_id"]["value"] == "777" and mm["license_key"]["set"]

    # any change to a credential moves the old one to where the others are kept
    api.request("PUT", ADMIN, json={"sources": {"ipinfo": {"credentials": {"token": "abc12345"}}}})

    async def where() -> tuple[bool, bool]:
        async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            return (
                await db.get(AppSetting, "maxmind_credentials") is not None,
                await db.get(AppSetting, "ip_data_keys") is not None,
            )

    assert api.client.portal.call(where) == (False, True)
    mm = {c["name"]: c for c in _source(api.get(ADMIN).json(), "maxmind")["credentials"]}
    assert mm["account_id"]["value"] == "777" and mm["license_key"]["set"]


def test_update_now_downloads_in_the_background_and_the_page_shows_how_it_went(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    fake = FakeMaxMind(tmp_path)
    directory = tmp_path / "geoip"
    directory.mkdir()
    _updater(api, directory, fake)
    _only_maxmind(api)
    none = api.post(f"{ADMIN}/update")
    assert none.status_code == 409 and none.json()["error"]["code"] == "nothing_to_update"
    assert _give_maxmind(api).status_code == 200  # giving the account starts the download too
    assert api.post(f"{ADMIN}/update").status_code == 202  # also while a check is going
    page = _wait(
        api,
        lambda d: not _source(d, "maxmind")["running"] and _source(d, "maxmind")["last_success"],
    )
    mm = _source(page, "maxmind")
    assert mm["error"] is None and page["databases_loaded"] and mm["state"] == "ready"
    assert sorted(f["name"] for f in mm["files"]) == ["GeoLite2-ASN.mmdb", "GeoLite2-City.mmdb"]
    assert all(f["built"] and f["size"] for f in mm["files"])
    assert mm["next_check"], "when it is looked at again"
    api.request("PUT", ADMIN, json={"lookup_access": "public"})
    assert api.get(f"/api/v1/ip/{PUBLIC}").json()["found"]  # used at once, without a restart


def test_a_key_that_is_given_starts_a_download_by_itself(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    fake = FakeMaxMind(tmp_path)
    directory = tmp_path / "geoip"
    directory.mkdir()
    _updater(api, directory, fake)
    _only_maxmind(api)
    _give_maxmind(api)
    page = _wait(api, lambda d: d["databases_loaded"])
    assert _source(page, "maxmind")["last_success"]


def test_turning_a_source_on_starts_its_download_by_itself(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    fake = FakeMaxMind(tmp_path)
    directory = tmp_path / "geoip"
    directory.mkdir()
    _updater(api, directory, fake)
    _only_maxmind(api)
    api.request("PUT", ADMIN, json={"sources": {"maxmind": {"enabled": False}}})
    _give_maxmind(api)
    assert not api.get(ADMIN).json()["databases_loaded"]
    api.request("PUT", ADMIN, json={"sources": {"maxmind": {"enabled": True}}})
    _wait(api, lambda d: d["databases_loaded"])


def test_a_refused_key_is_shown_on_the_page(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    fake = FakeMaxMind(tmp_path)
    directory = tmp_path / "geoip"
    directory.mkdir()
    _updater(api, directory, fake)
    _only_maxmind(api)
    _give_maxmind(api, key="wrongwrongwrongwrong1234")
    page = _wait(api, lambda d: _source(d, "maxmind")["error"])
    mm = _source(page, "maxmind")
    assert "MaxMind refused" in mm["error"] and "wrongwrong" not in api.get(ADMIN).text
    assert mm["next_check"] and not page["databases_loaded"]


def test_there_is_nowhere_to_download_to_without_a_directory(api: Api) -> None:
    api.become_admin()
    _updater(api, None)
    _give_maxmind(api)
    page = api.get(ADMIN).json()
    assert page["directory_configured"] is False
    assert _source(page, "maxmind")["state"] == "no_directory"
    response = api.post(f"{ADMIN}/update")
    assert response.status_code == 409 and response.json()["error"]["code"] == "no_directory"


def test_nothing_is_downloaded_in_offline_mode(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    updater = _updater(api, tmp_path / "geoip")
    updater.offline = True
    api.client.app.state.ipdata.offline = True  # type: ignore[attr-defined]
    page = api.get(ADMIN).json()
    assert page["offline"] is True and _source(page, "dbip")["state"] == "offline"
    response = api.post(f"{ADMIN}/update")
    assert response.status_code == 409 and response.json()["error"]["code"] == "offline"
    assert _source(page, "dbip")["next_check"] is None


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
    assert api.client.portal.call(refresh_all, state.sessionmaker, state.ipdata) == 1
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
    assert api.client.portal.call(refresh_all, state.sessionmaker, state.ipdata) == 0
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
    service = service_over(_geoip(api, tmp_path / "geoip"))
    state = api.client.app.state  # type: ignore[attr-defined]

    async def run() -> tuple[list[str], list[str]]:
        async with state.sessionmaker() as db:
            node = (await db.execute(select(Node).limit(1))).scalar_one_or_none()
            assert node is None
            fresh = Node(
                id="n" * 32, name="x", credential_hash="x", remote_addr=PUBLIC, auto_fields=[]
            )
            first = fill(fresh, service)
            second = fill(fresh, service)  # nothing new: nothing changed
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
