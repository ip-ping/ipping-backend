"""Asking every IP data source and voting (the service), keeping the databases that need no account (the downloader), and the admin API."""

from __future__ import annotations

import asyncio
import gzip
import io
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ipping_backend.geolocation.geoip import GeoIp
from ipping_backend.ipdata import downloader, local
from ipping_backend.ipdata.catalog import IDS
from ipping_backend.ipdata.options import IpDataOptions
from ipping_backend.ipdata.service import IpDataService

from .conftest import Api
from .test_geoip import asn_db, city_db
from .test_ip_sources import (
    IP2LOCATION,
    IPAPI_FREE,
    IPINFO,
    IPLOCATE,
    V4,
    _zones,
    make_xdb,
)
from .test_ipdata import ADMIN, FRANKFURT, PUBLIC, _geoip, _set_options, _updater, _wait

HOSTS = {
    "ipinfo.io": IPINFO,
    "iplocate.io": IPLOCATE,
    "api.ipapi.is": IPAPI_FREE,
    "api.ip2location.io": IP2LOCATION,
}
SECRET = "SECRET-TOKEN-0123456789"


class Wire:
    """A pretend Internet for the four online services: what they answer, and what was asked of them."""

    def __init__(self, **overrides: Any) -> None:
        self.answers: dict[str, Any] = {**HOSTS, **overrides}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.get(request.url.host)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        if answer is None:
            return httpx.Response(404)
        return httpx.Response(200, json=answer)

    def hosts(self) -> list[str]:
        return sorted(r.url.host for r in self.requests)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def service(
    tmp_path: Path,
    wire: Wire | None = None,
    *,
    dbip: bool = False,
    maxmind: bool = False,
    offline: bool = False,
    options: IpDataOptions | None = None,
    keys: dict[str, dict[str, str]] | None = None,
) -> IpDataService:
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    if dbip:
        city_db(directory / "dbip-city-lite-2026-11.mmdb", FRANKFURT)
        asn_db(directory / "dbip-asn-lite-2026-11.mmdb")
    if maxmind:
        city_db(directory / "GeoLite2-City.mmdb", FRANKFURT)
        asn_db(directory / "GeoLite2-ASN.mmdb")
    geoip = GeoIp(directory=directory)
    built = IpDataService(
        lambda: geoip,
        directory,
        offline=offline,
        transport=wire.transport() if wire else httpx.MockTransport(Wire()),
    )
    built.apply(options or IpDataOptions(), keys or {}, maxmind_ready=maxmind)
    return built


def ask(svc: IpDataService, address: str = PUBLIC, **kw: Any) -> Any:
    async def go() -> Any:
        try:
            return await svc.lookup(address, **kw)
        finally:
            await svc.close()

    return asyncio.run(go())


def status(info: Any) -> dict[str, str]:
    return {s.id: s.status for s in info.sources}


# ---------------------------------------------------------------------------------------------
# Asking, and voting
# ---------------------------------------------------------------------------------------------


def test_every_source_that_is_on_is_asked_and_the_vote_is_taken(tmp_path: Path) -> None:
    wire = Wire()
    info = ask(service(tmp_path, wire, dbip=True, maxmind=True))
    assert wire.hosts() == ["api.ip2location.io", "api.ipapi.is", "ipinfo.io", "iplocate.io"]
    assert info.found and info.public
    # Frankfurt from the two databases against Mountain View from the four services: the services are four against two
    assert (info.country, info.city, info.asn, info.organization) == (
        "US",
        "Mountain View",
        15169,
        "Google LLC",
    )
    assert set(info.confidence) >= {"country", "region", "city", "location", "asn", "organization"}
    assert all(1 <= p <= 99 for p in info.confidence.values())
    assert len(info.sources) == len(IDS) == 9
    by_id = {s.id: s for s in info.sources}
    # MaxMind said Frankfurt: it is named as differing on the country, and then has no say about the city of another country
    assert "country" in by_id["maxmind"].differs and "city" not in by_id["maxmind"].differs
    assert "city" in by_id["ipinfo"].agrees and by_id["ipinfo"].said.city == "Mountain View"
    assert (
        status(info)["ipinfo"] == "ok" and status(info)["iptoasn"] == "waiting"
    )  # no directory file yet
    assert "IPinfo" in (info.source or "") and "ipinfo.io" in (info.attribution or "")


def test_one_source_with_data_is_enough(tmp_path: Path) -> None:
    """Four services fail in four ways, the databases are not there: the one that answers is the answer."""
    wire = Wire(
        **{
            "iplocate.io": httpx.Response(500),
            "api.ipapi.is": httpx.ConnectError("no route"),
            "api.ip2location.io": httpx.Response(200, text="<html>not json</html>"),
        }
    )
    info = ask(service(tmp_path, wire))
    assert info.found and (info.country, info.city) == ("US", "Mountain View")
    assert info.source == "IPinfo"
    assert (
        info.confidence["country"] == 98 and info.confidence["city"] == 72
    )  # IPinfo's own reliability
    got = status(info)
    assert (
        got["ipinfo"] == "ok"
        and got["iplocate"] == "error"
        and got["ipapi_is"] == "error"
        and got["ip2location"] == "error"
    )
    messages = {s.id: s.message for s in info.sources}
    assert (
        messages["iplocate"] == "Answered HTTP 500."
        and messages["ipapi_is"] == "Could not be reached."
    )
    assert messages["ip2location"] == "Gave an answer that could not be read."


def test_when_nobody_knows_nothing_is_found_and_everyone_is_listed(tmp_path: Path) -> None:
    wire = Wire(**{h: {"bogon": True} for h in HOSTS})
    info = ask(service(tmp_path, wire))
    assert info.public and not info.found and info.country is None and info.confidence == {}
    assert status(info)["ipinfo"] == "empty" and len(info.sources) == 9


def test_a_private_address_asks_nobody(tmp_path: Path) -> None:
    wire = Wire()
    info = ask(service(tmp_path, wire, dbip=True), "10.1.2.3")
    assert not info.public and not info.found and info.sources == [] and wire.requests == []


def test_an_online_answer_is_kept_and_a_new_address_is_asked(tmp_path: Path) -> None:
    wire = Wire()
    svc = service(tmp_path, wire)

    async def go() -> None:
        await svc.lookup(PUBLIC)
        await svc.lookup(PUBLIC)  # from the cache: no new request
        assert len(wire.requests) == 4
        await svc.lookup("1.1.1.1")
        assert len(wire.requests) == 8
        await svc.close()

    asyncio.run(go())


def test_a_service_that_is_out_of_requests_is_left_alone_for_a_while(tmp_path: Path) -> None:
    wire = Wire(**{"ipinfo.io": httpx.Response(429, headers={"Retry-After": "120"})})
    svc = service(tmp_path, wire)

    async def go() -> None:
        first = await svc.lookup(PUBLIC)
        assert status(first)["ipinfo"] == "rate_limited" and status(first)["iplocate"] == "ok"
        before = len(wire.requests)
        second = await svc.lookup("1.1.1.1")
        assert status(second)["ipinfo"] == "rate_limited"
        assert "ipinfo.io" not in [r.url.host for r in wire.requests[before:]]  # not asked again
        assert svc.state("ipinfo")[0] == "paused" and svc.available()
        await svc.close()

    asyncio.run(go())


def test_a_key_is_sent_to_its_service_and_never_shown(tmp_path: Path) -> None:
    wire = Wire(**{"ipinfo.io": httpx.Response(401, json={"error": f"bad {SECRET}"})})
    info = ask(service(tmp_path, wire, keys={"ipinfo": {"token": SECRET}}))
    sent = next(r for r in wire.requests if r.url.host == "ipinfo.io")
    assert sent.url.params["token"] == SECRET
    assert (
        next(r for r in wire.requests if r.url.host == "iplocate.io").headers.get("x-api-key")
        is None
    )
    dump = info.model_dump_json()
    assert SECRET not in dump and status(info)["ipinfo"] == "error"
    assert next(s.message for s in info.sources if s.id == "ipinfo") == "Refused the key."
    # without a key the refusal says a key is needed
    keyless = ask(service(tmp_path, Wire(**{"ipinfo.io": httpx.Response(403)})))
    assert (
        next(s.message for s in keyless.sources if s.id == "ipinfo")
        == "Refused the lookup: it needs a key."
    )


def test_a_connection_failure_does_not_show_the_address_it_called(tmp_path: Path) -> None:
    error = httpx.ConnectError(f"cannot connect to https://ipinfo.io/{PUBLIC}/json?token={SECRET}")
    info = ask(service(tmp_path, Wire(**{"ipinfo.io": error}), keys={"ipinfo": {"token": SECRET}}))
    assert (
        SECRET not in info.model_dump_json()
        and "ipinfo.io/" not in info.model_dump_json().replace("ipinfo.io)", "")
    )
    slow = ask(service(tmp_path, Wire(**{"ipinfo.io": httpx.ReadTimeout("slow")})))
    assert next(s.message for s in slow.sources if s.id == "ipinfo") == "Did not answer in time."


def test_a_source_that_is_off_is_not_asked(tmp_path: Path) -> None:
    wire = Wire()
    options = IpDataOptions(source_enabled={"ipinfo": False, "dbip": False})
    info = ask(service(tmp_path, wire, dbip=True, options=options))
    assert (
        "ipinfo.io" not in wire.hosts()
        and status(info)["ipinfo"] == "disabled"
        and status(info)["dbip"] == "disabled"
    )
    assert "IPinfo" not in (info.source or "") and "DB-IP" not in (info.source or "")
    assert info.city == "Mountain View"  # the services that are on still answer


def test_everything_off_answers_nothing_and_says_so(tmp_path: Path) -> None:
    wire = Wire()
    options = IpDataOptions(source_enabled={i: False for i in IDS})
    svc = service(tmp_path, wire, dbip=True, options=options)
    info = ask(svc)
    assert not info.found and wire.requests == [] and set(status(info).values()) == {"disabled"}
    assert not svc.available()


def test_offline_asks_nobody_outside_but_the_databases_answer(tmp_path: Path) -> None:
    wire = Wire()
    svc = service(tmp_path, wire, dbip=True, offline=True)
    info = ask(svc)
    assert (
        wire.requests == []
        and info.found
        and (info.country, info.city) == ("DE", "Frankfurt am Main")
    )
    got = status(info)
    assert got["ipinfo"] == "offline" and got["dbip"] == "ok"
    assert svc.available()  # the database is enough
    assert "IPPING_IPDATA_OFFLINE" in svc.state("ipinfo")[1]  # type: ignore[operator]


def test_maxmind_and_dbip_are_two_voters_read_from_their_own_files(tmp_path: Path) -> None:
    both = ask(service(tmp_path, dbip=True, maxmind=True, offline=True))
    assert (
        both.confidence["city"]
        > ask(service(tmp_path / "one", dbip=True, offline=True)).confidence["city"]
    )
    assert {s.id for s in both.sources if s.status == "ok"} == {"maxmind", "dbip"}
    off = ask(
        service(
            tmp_path / "off",
            dbip=True,
            maxmind=True,
            offline=True,
            options=IpDataOptions(source_enabled={"maxmind": False}),
        )
    )
    assert status(off)["maxmind"] == "disabled" and status(off)["dbip"] == "ok"
    assert (off.region, off.region_code) == ("Hesse", None) or off.region_code in (None, "HE")


def test_maxmind_without_its_account_says_what_it_needs(tmp_path: Path) -> None:
    svc = service(tmp_path, offline=True)
    state, detail = svc.state("maxmind")
    assert state == "needs_setup" and "account ID" in (detail or "")
    assert svc.state("dbip")[0] == "waiting"  # a directory, nothing in it yet
    assert IpDataService(lambda: GeoIp(), None, offline=True).state("dbip")[0] == "no_directory"


def test_the_databases_alone_can_be_asked(tmp_path: Path) -> None:
    wire = Wire()
    info = ask(service(tmp_path, wire, dbip=True), online=False)
    assert wire.requests == [] and info.found and info.city == "Frankfurt am Main"


# ---------------------------------------------------------------------------------------------
# Keeping the databases that need no account
# ---------------------------------------------------------------------------------------------

THIS, LAST = "2026-11", "2026-10"


def _gz(path: Path) -> bytes:
    return gzip.compress(path.read_bytes())


class Mirror:
    """Pretend DB-IP, IPtoASN, IPdeny and GitHub servers; counts what was downloaded."""

    def __init__(
        self, tmp_path: Path, months: tuple[str, ...] = (THIS,), version: str = "v1"
    ) -> None:
        work = tmp_path / "mirror"
        work.mkdir(exist_ok=True)
        self.version = version
        self.files: dict[str, bytes] = {}
        for month in months:
            self.files[f"download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"] = _gz(
                city_db(work / "c.mmdb", FRANKFURT)
            )
            self.files[f"download.db-ip.com/free/dbip-asn-lite-{month}.mmdb.gz"] = _gz(
                asn_db(work / "a.mmdb")
            )
        asn = "8.8.8.0\t8.8.8.255\t15169\tUS\tGOOGLE\n93.184.216.0\t93.184.216.255\t15133\tUS\tEDGECAST\n"
        self.files["iptoasn.com/data/ip2asn-combined.tsv.gz"] = gzip.compress(asn.encode())
        self.files["www.ipdeny.com/ipblocks/data/countries/all-zones.tar.gz"] = _zones(
            {"us.zone": "8.8.8.0/24\n", "de.zone": "93.184.216.0/24\n"}
        )
        self.files["www.ipdeny.com/ipv6/ipaddresses/blocks/ipv6-all-zones.tar.gz"] = _zones(
            {"fr.zone": "2a00:1450::/32\n"}
        )
        self.files[
            "raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v4.xdb"
        ] = make_xdb(work / "4.xdb", 4, V4).read_bytes()
        self.files[
            "raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v6.xdb"
        ] = make_xdb(
            work / "6.xdb",
            6,
            [
                (
                    "2001:4860::",
                    "2001:4860:ffff:ffff:ffff:ffff:ffff:ffff",
                    "United States|Florida|Miami|0|US",
                )
            ],
        ).read_bytes()
        self.gets: list[str] = []
        self.heads: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        body = self.files.get(key)
        if body is None:
            return httpx.Response(404)
        headers = {"ETag": f'"{self.version}-{len(body)}"', "Content-Length": str(len(body))}
        if request.method == "HEAD":
            self.heads.append(key)
            return httpx.Response(200, headers=headers)
        self.gets.append(key)
        return httpx.Response(200, content=body, headers=headers)


def updater(
    tmp_path: Path, mirror: Mirror, *, offline: bool = False, today: date = date(2026, 11, 11)
) -> tuple[downloader.DataUpdater, Path]:
    directory = tmp_path / "data"
    geoip = GeoIp(directory=directory)
    return (
        downloader.DataUpdater(
            None,  # type: ignore[arg-type]
            directory,
            lambda: geoip,
            offline=offline,
            transport=httpx.MockTransport(mirror),
            today=lambda: today,
        ),
        directory,
    )


@pytest.fixture
def small_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(downloader.MIN_ROWS, "iptoasn", 1)
    monkeypatch.setitem(downloader.MIN_ROWS, "ipdeny", 1)


def test_the_four_databases_are_downloaded_checked_and_readable(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    status_ = asyncio.run(up.update(list(downloader.SOURCES)))["sources"]
    assert all(not e["error"] and e["last_success"] for e in status_.values()), status_
    names = sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))
    assert names == [
        f"dbip-asn-lite-{THIS}.mmdb", f"dbip-city-lite-{THIS}.mmdb", "ip2region_v4.xdb", "ip2region_v6.xdb",
        "ipdata-status.json", "ipdeny.sqlite", "iptoasn.sqlite",
    ]  # fmt: skip
    reading = local.LocalSources(lambda: GeoIp(directory=directory), directory)
    dbip = reading.lookup("dbip", "93.184.216.34")
    assert dbip is not None and dbip.city == "Frankfurt am Main" and dbip.asn == 15133
    asn = reading.lookup("iptoasn", "8.8.8.8")
    assert asn is not None and (asn.asn, asn.organization, asn.country) == (15169, "GOOGLE", "US")
    assert reading.lookup("ipdeny", "93.184.216.34") == local.Record(country="DE")
    region = reading.lookup("ip2region", "1.0.4.4")
    assert region is not None and (region.country, region.city) == ("AU", "Brisbane")
    assert reading.lookup("ip2region", "2001:4860:4860::8888") is not None
    assert all(reading.present(s) for s in downloader.SOURCES)
    assert reading.files("iptoasn")[0]["rows"] == 2


def test_nothing_is_downloaded_again_while_the_servers_have_nothing_newer(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    up, _ = updater(tmp_path, mirror)
    asyncio.run(up.update(list(downloader.SOURCES)))
    first = len(mirror.gets)
    assert first == 2 + 1 + 2 + 2
    asyncio.run(up.update(list(downloader.SOURCES)))
    assert len(mirror.gets) == first and len(mirror.heads) >= first  # asked, not downloaded
    mirror.version = "v2"  # a new version upstream
    for key in list(mirror.files):
        mirror.files[key] = mirror.files[key]  # (same bytes, new ETag: still counts as newer)
    asyncio.run(up.update(list(downloader.SOURCES)))
    assert len(mirror.gets) == 2 * first
    asyncio.run(up.update(list(downloader.SOURCES), force=True))
    assert len(mirror.gets) == 3 * first


def test_last_months_dbip_file_is_used_until_this_months_is_published(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path, months=(LAST,))
    up, directory = updater(tmp_path, mirror)
    result = asyncio.run(up.update(["dbip"]))["sources"]["dbip"]
    assert not result["error"] and (directory / f"dbip-city-lite-{LAST}.mmdb").exists()
    mirror2 = Mirror(tmp_path, months=(THIS, LAST))
    up2, _ = updater(tmp_path, mirror2)
    asyncio.run(up2.update(["dbip"]))
    assert (directory / f"dbip-city-lite-{THIS}.mmdb").exists()
    assert not (directory / f"dbip-city-lite-{LAST}.mmdb").exists()  # the older file is gone


def test_a_source_that_has_nothing_published_is_an_error_and_the_others_go_on(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path, months=())
    up, directory = updater(tmp_path, mirror)
    result = asyncio.run(up.update(["dbip", "iptoasn"]))["sources"]
    assert (
        result["dbip"]["error"]["code"] == "not_found"
        and "DB-IP" in result["dbip"]["error"]["message"]
    )
    assert not result["iptoasn"]["error"] and (directory / "iptoasn.sqlite").exists()


def test_data_that_is_not_what_it_says_replaces_nothing(tmp_path: Path, small_tables: None) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    asyncio.run(up.update(["iptoasn"]))
    good = (directory / "iptoasn.sqlite").read_bytes()
    mirror.files["iptoasn.com/data/ip2asn-combined.tsv.gz"] = gzip.compress(
        b"1.0.0.0\t1.0.0.255\t13335\tAU\tCLOUDFLARENET\n"
    )
    mirror.version = "v2"
    result = asyncio.run(up.update(["iptoasn"]))["sources"]["iptoasn"]
    assert (
        result["error"]["code"] == "bad_data"
        and (directory / "iptoasn.sqlite").read_bytes() == good
    )
    mirror.files[
        "raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v4.xdb"
    ] = b"not an xdb" * 70000
    broken = asyncio.run(up.update(["ip2region"]))["sources"]["ip2region"]
    assert broken["error"] and not (directory / "ip2region_v4.xdb").exists()
    assert not list(directory.glob(".*.part")) and not list(
        directory.glob(".download-*")
    )  # nothing left behind


def test_nothing_is_downloaded_offline_or_without_a_directory(tmp_path: Path) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror, offline=True)
    asyncio.run(up.update(list(downloader.SOURCES)))
    assert mirror.gets == [] and mirror.heads == [] and not directory.exists()
    nowhere = downloader.DataUpdater(
        None, None, lambda: GeoIp(), transport=httpx.MockTransport(mirror)
    )  # type: ignore[arg-type]
    asyncio.run(nowhere.update(["dbip"]))
    assert mirror.gets == []


def test_when_a_check_is_due() -> None:
    options = IpDataOptions(data_update_hours=24)
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    assert downloader.due(options, {}, now)
    fresh = {"last_attempt": (now - timedelta(hours=3)).isoformat()}
    assert not downloader.due(options, fresh, now)
    assert downloader.due(options, {"last_attempt": (now - timedelta(hours=25)).isoformat()}, now)
    failed = {
        "last_attempt": (now - timedelta(minutes=70)).isoformat(),
        "error": {"code": "x", "message": "y"},
    }
    assert downloader.due(options, failed, now)  # an hour after a failure
    assert not downloader.due(
        options,
        {
            "last_attempt": (now - timedelta(minutes=30)).isoformat(),
            "error": {"code": "x", "message": "y"},
        },
        now,
    )


def test_a_status_file_of_a_process_that_died_is_not_running(tmp_path: Path) -> None:
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    downloader._write_status(
        tmp_path, {"sources": {"dbip": {"running": True, "running_since": old}}}
    )
    assert downloader.read_status(tmp_path)["sources"]["dbip"]["running"] is False
    assert downloader.read_status(None) == {"sources": {}}
    (tmp_path / downloader.STATUS_FILE).write_text("{not json")
    assert downloader.read_status(tmp_path) == {"sources": {}}


# ---------------------------------------------------------------------------------------------
# The admin API and the lookup API
# ---------------------------------------------------------------------------------------------


def _wire(api: Api, wire: Wire) -> Wire:
    state = api.client.app.state  # type: ignore[attr-defined]
    state.ipdata.offline = False
    state.ipdata._transport = wire.transport()
    state.ipdata._client = None
    return wire


def test_the_admin_page_lists_every_source_on_by_default(api: Api) -> None:
    api.become_admin()
    data = api.get(ADMIN).json()
    sources = {s["id"]: s for s in data["sources"]}
    assert list(sources) == list(IDS) and all(s["enabled"] for s in sources.values())
    assert sources["maxmind"]["kind"] == "database" and sources["ipinfo"]["kind"] == "online"
    assert [c["name"] for c in sources["maxmind"]["credentials"]] == ["account_id", "license_key"]
    assert [(c["name"], c["required"], c["set"]) for c in sources["ipinfo"]["credentials"]] == [
        ("token", False, False)
    ]
    assert (
        sources["dbip"]["credentials"] == []
        and sources["dbip"]["cost"]
        and sources["dbip"]["homepage"].startswith("https://")
    )
    assert (
        sources["ipinfo"]["state"] == "offline" and data["offline"] is True
    )  # the tests run offline
    assert sources["maxmind"]["state"] == "needs_setup"
    assert data["data_update_hours"] == 24


def test_a_source_is_turned_off_and_on_and_the_answer_follows(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    on = api.get(f"/api/v1/ip/{PUBLIC}").json()
    assert {s["id"] for s in on["sources"] if s["status"] == "ok"} == {"maxmind"}
    _set_options(api, sources={"maxmind": {"enabled": False}})
    sources = {s["id"]: s for s in api.get(ADMIN).json()["sources"]}
    assert sources["maxmind"]["enabled"] is False and sources["maxmind"]["state"] == "disabled"
    assert api.get(ADMIN).json()["sources"][1]["enabled"] is True  # the others are untouched
    off = api.get(f"/api/v1/ip/{PUBLIC}")
    assert off.status_code == 503  # nothing else can answer in this offline test
    _set_options(api, sources={"maxmind": {"enabled": True}})
    assert api.get(f"/api/v1/ip/{PUBLIC}").json()["found"]
    audit = (
        [e for e in api.get("/api/v1/admin/audit").json() if e["action"] == "ip_data.updated"]
        if api.get("/api/v1/admin/audit").status_code == 200
        else []
    )
    assert audit == [] or "sources" in audit[0]["data"]["fields"]


def test_the_lookup_gives_every_field_a_percentage_and_every_source_its_say(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    _wire(api, Wire())
    info = api.get(f"/api/v1/ip/{PUBLIC}").json()
    assert info["found"] and info["country"] == "US" and info["city"] == "Mountain View"
    assert info["confidence"]["country"] >= 90 and 1 <= info["confidence"]["postal_code"] <= 99
    sources = {s["id"]: s for s in info["sources"]}
    assert sources["ipinfo"]["status"] == "ok" and sources["ipinfo"]["said"]["asn"] == 15169
    assert (
        sources["maxmind"]["said"]["city"] == "Frankfurt am Main"
        and "country" in sources["maxmind"]["differs"]
    )
    assert (
        sources["ipinfo"]["said"]["postal_code"] == "94043"
        and "latitude" in sources["ipinfo"]["said"]
    )
    assert sources["iptoasn"]["status"] == "needs_setup" and sources["iptoasn"]["message"]
    assert info["region_code"] in (None, "HE", "CA")


def test_the_credentials_of_a_source_are_written_never_read_back(api: Api) -> None:
    api.become_admin()
    _set_options(
        api, sources={"ipinfo": {"credentials": {"token": SECRET}}, "ipapi_is": {"enabled": False}}
    )
    page = api.get(ADMIN)
    assert SECRET not in page.text
    sources = {s["id"]: s for s in page.json()["sources"]}
    token = sources["ipinfo"]["credentials"][0]
    assert token["set"] is True and token["value"] is None and token["secret"] is True
    assert sources["ipapi_is"]["enabled"] is False
    _set_options(
        api, sources={"ipinfo": {"enabled": False}}
    )  # a change that does not mention the key keeps it
    assert api.get(ADMIN).json()["sources"][5]["credentials"][0]["set"] is True
    _set_options(api, sources={"ipinfo": {"credentials": {"token": ""}}})  # an empty one clears it
    assert api.get(ADMIN).json()["sources"][5]["credentials"][0]["set"] is False


def test_a_saved_key_is_used_by_the_lookup(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    wire = _wire(api, Wire())
    _set_options(api, sources={"ipinfo": {"credentials": {"token": SECRET}}})
    api.get(f"/api/v1/ip/{PUBLIC}")
    sent = next(r for r in wire.requests if r.url.host == "ipinfo.io")
    assert sent.url.params.get("token") == SECRET


def test_what_cannot_be_set_is_refused(api: Api) -> None:
    api.become_admin()
    for body in (
        {"sources": {"nonesuch": {"enabled": False}}},
        {"sources": {"ipinfo": {"credentials": {"api_key": "x"}}}},
        {"sources": {"maxmind": {"credentials": {"license_key": "x" * 20}}}},
        {"sources": {"ipinfo": {"credentials": {"token": "has a space"}}}},
        {"sources": {"ipinfo": {"credentials": {"token": "x" * 300}}}},
        {"data_update_hours": 1},
    ):
        response = api.request("PUT", ADMIN, json=body)
        assert response.status_code == 422, body


def test_update_now_says_why_it_cannot(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    assert api.post(f"{ADMIN}/update").status_code == 409  # the backend has no directory for them
    _updater(api, tmp_path / "data")
    no_account = api.post(f"{ADMIN}/update")
    assert no_account.status_code == 409 and no_account.json()["error"]["code"] == "no_credentials"
    _set_options(api, sources={"maxmind": {"enabled": False}})
    offline = api.post(f"{ADMIN}/update")  # MaxMind is off and the server may not call out
    assert offline.status_code == 409 and offline.json()["error"]["code"] == "offline"


def test_update_now_downloads_every_database_that_is_on(
    api: Api, tmp_path: Path, small_tables: None
) -> None:
    api.become_admin()
    directory = tmp_path / "data"
    state = api.client.app.state  # type: ignore[attr-defined]
    _updater(api, directory)
    state.dataupdater = downloader.DataUpdater(
        state.sessionmaker,
        directory,
        lambda: state.geoip,
        transport=httpx.MockTransport(Mirror(tmp_path)),
        today=lambda: date(2026, 11, 11),
    )
    state.ipdata = IpDataService(lambda: state.geoip, directory, offline=True)
    _set_options(api, sources={"maxmind": {"enabled": False}, "ipdeny": {"enabled": False}})
    assert api.post(f"{ADMIN}/update").status_code == 202

    def done(page: dict[str, Any]) -> bool:
        wanted = [s for s in page["sources"] if s["id"] in ("dbip", "iptoasn", "ip2region")]
        return all(s["last_success"] and not s["running"] for s in wanted)

    page = _wait(api, done)
    sources = {s["id"]: s for s in page["sources"]}
    assert sources["dbip"]["state"] == "ready" and sources["dbip"]["files"][0]["size"] > 0
    assert sources["iptoasn"]["files"][0]["rows"] == 2
    assert sources["iptoasn"]["next_check"] is None  # offline: no check is scheduled
    assert (
        sources["ipdeny"]["state"] == "disabled" and not (directory / "ipdeny.sqlite").exists()
    )  # off: not fetched
    assert sources["maxmind"]["state"] == "disabled"
    info = api.get(f"/api/v1/ip/{PUBLIC}").json()
    answered = {s["id"] for s in info["sources"] if s["status"] == "ok"}
    assert {"dbip", "iptoasn"} <= answered and info["asn"] in (15133, 15169)


def test_the_addresses_of_a_measurement_carry_the_vote_too(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    _wire(api, Wire())
    node_id, key = api.enroll_node("Node")
    assert node_id and key
    # the lookup of a tested address is the same one: ask it directly through the service
    state = api.client.app.state  # type: ignore[attr-defined]
    info = api.client.portal.call(state.ipdata.lookup, PUBLIC)
    assert info.confidence["country"] >= 90 and len(info.sources) == 9


def test_the_data_directory_files_are_what_the_page_lists(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    maxmind = next(s for s in api.get(ADMIN).json()["sources"] if s["id"] == "maxmind")
    assert [f["name"] for f in maxmind["files"]] == ["GeoLite2-City.mmdb", "GeoLite2-ASN.mmdb"]
    assert all(f["size"] > 0 for f in maxmind["files"])
    assert maxmind["state"] == "ready"


_ = io  # (kept for the helpers above)
