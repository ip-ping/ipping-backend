"""Asking every IP data source and voting (the service), keeping every source's files up to date (the downloader), and the admin API."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import ipaddress
import json
import logging
import socket
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ipping_backend.config import Settings
from ipping_backend.geolocation.geoip import GeoIp
from ipping_backend.ipdata import downloader, local
from ipping_backend.ipdata.catalog import BY_ID, IDS
from ipping_backend.ipdata.options import IpDataOptions
from ipping_backend.ipdata.places import PlaceTable, ip2location_spans, ipapi_spans
from ipping_backend.ipdata.places import build as build_places
from ipping_backend.ipdata.ranges import build, ipdeny_rows, iptoasn_rows
from ipping_backend.ipdata.service import IpDataService
from ipping_backend.logging_config import configure_logging

from .conftest import Api
from .test_geoip import asn_db, city_db, make_db
from .test_ip_sources import _zones, make_xdb
from .test_ipdata import ADMIN, FRANKFURT, PUBLIC, _geoip, _updater, _wait

TOKEN = "SECRET-TOKEN-0123456789"
BUILT = {"built_at": "2026-11-11T00:00:00+00:00"}
PUBLIC_NUMBER = int(ipaddress.ip_address(PUBLIC))
GOOGLE = {"asn": "15169", "country_code": "US", "name": "GOOGLE", "org": "Google LLC"}
MOUNTAIN_VIEW = "Mountain View"
IPAPI_HEADER = "ip_version,start_ip,end_ip,continent,country_code,country,state,city,zip,timezone,latitude,longitude,accuracy,source"
IPAPI_ROWS = [
    IPAPI_HEADER,
    f"4,93.184.216.0,93.184.216.255,NA,US,United States,California,{MOUNTAIN_VIEW},94043,America/Los_Angeles,37.386,-122.0838,5,x",
    "4,8.8.8.0,8.8.8.255,NA,US,United States,California,Mountain View,94035,America/Los_Angeles,37.386,-122.0838,5,x",
    "4,93.184.0.0,93.184.255.255,NA,US,United States,Virginia,Ashburn,20149,America/New_York,39.04,-77.49,5,x",
]  # the first is inside the last
IP2LOCATION_ROWS = [
    f'"{PUBLIC_NUMBER - 34}","{PUBLIC_NUMBER + 221}","US","United States of America","California","{MOUNTAIN_VIEW}","37.38605","-122.08385","94043","-07:00"',
    '"134744064","134744319","US","United States of America","California","Mountain View","37.38605","-122.08385","94039","-07:00"',
]


# ---------------------------------------------------------------------------------------------
# What every source has on disk, written the way the downloader leaves it
# ---------------------------------------------------------------------------------------------


def _places(path: Path, rows: list[str], spans: Any) -> None:
    def read(table: PlaceTable) -> Iterator[Any]:
        yield from spans(rows, table, 4)

    build_places(path, [(4, read)], BUILT)


def write_source(directory: Path, source: str) -> None:
    """The files of one source, all saying PUBLIC is in the US, Google's, Mountain View: except MaxMind and DB-IP, which say Frankfurt."""
    directory.mkdir(parents=True, exist_ok=True)
    asn = {"autonomous_system_number": 15133, "autonomous_system_organization": "Edgecast"}
    match source:
        case "maxmind":
            city_db(directory / "GeoLite2-City.mmdb", FRANKFURT)
            asn_db(directory / "GeoLite2-ASN.mmdb")
        case "dbip":
            city_db(directory / "dbip-city-lite-2026-11.mmdb", FRANKFURT)
            make_db(directory / "dbip-asn-lite-2026-11.mmdb", "ASN", {"93.184.216.0/24": asn})
        case "iplocate":
            make_db(
                directory / local.IPLOCATE_FILES["country"],
                "Country",
                {"93.184.216.0/24": {"country_code": "US"}, "8.8.8.0/24": {"country_code": "US"}},
            )
            make_db(
                directory / local.IPLOCATE_FILES["asn"],
                "ASN",
                {"93.184.216.0/24": {**GOOGLE, "network": "93.184.216.0/24"}, "8.8.8.0/24": GOOGLE},
            )
        case "ipinfo":
            record = {"asn": "AS15169", "as_name": "Google LLC", "country_code": "US"}
            make_db(
                directory / local.IPINFO_FILE,
                "Lite",
                {"93.184.216.0/24": record, "1.1.1.0/24": {**record, "country_code": "AU"}},
            )
        case "ipapi_is":
            _places(directory / local.IPAPI_FILE, IPAPI_ROWS, ipapi_spans)
        case "ip2location":
            _places(directory / local.IP2LOCATION_FILE, IP2LOCATION_ROWS, ip2location_spans)
        case "iptoasn":
            rows = iptoasn_rows(["93.184.216.0\t93.184.216.255\t15169\tUS\tGOOGLE"])
            build(directory / local.IPTOASN_FILE, rows, BUILT)
        case "ipdeny":
            rows = ipdeny_rows(_zones({"us.zone": "93.184.216.0/24\n"}))
            build(directory / local.IPDENY_FILE, rows, BUILT)
        case "ip2region":
            segment = "United States|California|Mountain View|0|US"
            make_xdb(
                directory / local.XDB_FILES[4],
                4,
                [("8.8.0.0", "8.8.255.255", segment), ("93.184.0.0", "93.184.255.255", segment)],
            )
            make_xdb(
                directory / local.XDB_FILES[6],
                6,
                [("2001:4860::", "2001:4860:ffff:ffff:ffff:ffff:ffff:ffff", segment)],
            )
        case _:
            raise AssertionError(source)


def service(
    tmp_path: Path,
    have: tuple[str, ...] = (),
    *,
    offline: bool = False,
    options: IpDataOptions | None = None,
    keys: dict[str, dict[str, str]] | None = None,
) -> IpDataService:
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    for source in have:
        write_source(directory, source)
    geoip = GeoIp(directory=directory)
    built = IpDataService(lambda: geoip, directory, offline=offline)
    built.apply(options or IpDataOptions(), keys or {})
    return built


def status(info: Any) -> dict[str, str]:
    return {s.id: s.status for s in info.sources}


# ---------------------------------------------------------------------------------------------
# Asking, and voting
# ---------------------------------------------------------------------------------------------


def test_every_source_that_is_on_is_asked_and_the_vote_is_taken(tmp_path: Path) -> None:
    info = service(tmp_path, IDS).lookup(PUBLIC)
    assert info.found and info.public
    # Frankfurt from MaxMind and DB-IP against the United States from the seven others: a majority
    assert (info.country, info.city) == ("US", MOUNTAIN_VIEW)
    assert info.asn == 15169 and (info.organization or "").lower().startswith("google")
    assert set(info.confidence) >= {"country", "region", "city", "location", "asn", "organization"}
    assert all(1 <= p <= 99 for p in info.confidence.values())
    assert len(info.sources) == len(IDS) == 9 and set(status(info).values()) == {"ok"}
    by_id = {s.id: s for s in info.sources}
    # MaxMind said Frankfurt: it is named as differing on the country, and then has no say about the city of another country
    assert "country" in by_id["maxmind"].differs and "city" not in by_id["maxmind"].differs
    assert "country" in by_id["ipinfo"].agrees and by_id["ipapi_is"].said.city == MOUNTAIN_VIEW
    assert "asn" in by_id["maxmind"].differs and "asn" in by_id["iptoasn"].agrees
    assert by_id["ip2location"].said.postal_code == "94043"
    assert by_id["ipapi_is"].said.latitude == 37.386
    for name in ("MaxMind", "IPinfo", "IPLocate", "ipapi.is", "IP2Location", "IPtoASN"):
        assert name in (info.source or "")
    for notice in ("ipinfo.io", "iplocate.io", "ipapi.is", "lite.ip2location.com", "GeoLite2"):
        assert notice in (info.attribution or "")
    assert info.database_built is not None


def test_one_source_with_data_is_enough(tmp_path: Path) -> None:
    """Eight sources have nothing; the one that has something is the answer, at its own reliability."""
    svc = service(tmp_path, ("iplocate",))
    info = svc.lookup(PUBLIC)
    assert info.found and (info.country, info.asn) == ("US", 15169) and info.source == "IPLocate"
    assert info.confidence == {
        "country": 96,
        "asn": 98,
        "organization": 93,
    }  # IPLocate's own reliability
    got = status(info)
    assert got["iplocate"] == "ok"
    assert (
        got["maxmind"] == got["ipinfo"] == got["ip2location"] == "needs_setup"
    )  # they need an account first
    assert got["dbip"] == got["ipapi_is"] == got["iptoasn"] == "waiting"  # not downloaded yet
    messages = {s.id: s.message for s in info.sources}
    assert "account ID and license key" in (messages["maxmind"] or "")
    assert "access token" in (messages["ipinfo"] or "")
    assert "download token" in (messages["ip2location"] or "")
    assert messages["dbip"] == "Not downloaded yet."


def test_when_nobody_knows_nothing_is_found_and_everyone_is_listed(tmp_path: Path) -> None:
    info = service(tmp_path, IDS).lookup("1.2.3.4")
    assert info.public and not info.found and info.country is None and info.confidence == {}
    assert set(status(info).values()) == {"empty"} and len(info.sources) == 9


def test_a_private_address_asks_nobody(tmp_path: Path) -> None:
    info = service(tmp_path, IDS).lookup("10.1.2.3")
    assert not info.public and not info.found and info.sources == []


def test_what_is_not_an_address_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        service(tmp_path, IDS).lookup("not an address")


def test_a_lookup_never_touches_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service(tmp_path, IDS)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a lookup reached for the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    assert svc.lookup(PUBLIC).found


def test_a_source_that_is_off_is_not_asked(tmp_path: Path) -> None:
    options = IpDataOptions(source_enabled={"ipinfo": False, "dbip": False})
    info = service(tmp_path, IDS, options=options).lookup(PUBLIC)
    assert status(info)["ipinfo"] == "disabled" and status(info)["dbip"] == "disabled"
    assert "IPinfo" not in (info.source or "") and "DB-IP" not in (info.source or "")
    assert info.city == MOUNTAIN_VIEW  # the sources that are on still answer
    assert {s.message for s in info.sources if s.status == "disabled"} == {"Turned off."}


def test_everything_off_answers_nothing_and_says_so(tmp_path: Path) -> None:
    options = IpDataOptions(source_enabled={i: False for i in IDS})
    svc = service(tmp_path, IDS, options=options)
    info = svc.lookup(PUBLIC)
    assert not info.found and set(status(info).values()) == {"disabled"}
    assert not svc.available() and not svc.can("position") and svc.knowing("asn") == []


def test_offline_changes_nothing_about_what_is_on_disk(tmp_path: Path) -> None:
    keys = {"ipinfo": {"token": TOKEN}}
    svc = service(tmp_path, ("dbip",), offline=True, keys=keys)
    info = svc.lookup(PUBLIC)
    assert info.found and (info.country, info.city) == ("DE", "Frankfurt am Main")
    assert status(info)["dbip"] == "ok" and status(info)["ipinfo"] == "offline"
    assert svc.available()  # the database is enough
    assert "Offline mode" in (svc.state("ipinfo")[1] or "")


def test_maxmind_and_dbip_are_two_voters_read_from_their_own_files(tmp_path: Path) -> None:
    both = service(tmp_path, ("maxmind", "dbip"), offline=True).lookup(PUBLIC)
    one = service(tmp_path / "one", ("dbip",), offline=True).lookup(PUBLIC)
    assert both.confidence["city"] > one.confidence["city"]
    assert {s.id for s in both.sources if s.status == "ok"} == {"maxmind", "dbip"}
    off = service(
        tmp_path / "off",
        ("maxmind", "dbip"),
        offline=True,
        options=IpDataOptions(source_enabled={"maxmind": False}),
    ).lookup(PUBLIC)
    assert status(off)["maxmind"] == "disabled" and status(off)["dbip"] == "ok"


def test_what_a_source_needs_is_said_the_same_way_for_each(tmp_path: Path) -> None:
    svc = service(tmp_path)
    assert svc.state("maxmind") == (
        "needs_setup",
        "Needs its account ID and license key to be downloaded.",
    )
    assert svc.state("ipinfo") == ("needs_setup", "Needs its access token to be downloaded.")
    assert svc.state("ip2location") == ("needs_setup", "Needs its download token to be downloaded.")
    assert svc.state("dbip") == ("waiting", "Not downloaded yet.")
    # with its account given a source waits for its first download, like the others
    given = service(
        tmp_path / "given",
        keys={
            "maxmind": {"account_id": "1", "license_key": "k" * 20},
            "ipinfo": {"token": TOKEN},
            "ip2location": {"download_token": TOKEN},
        },
    )
    assert {given.state(s)[0] for s in ("maxmind", "ipinfo", "ip2location", "dbip")} == {"waiting"}
    # half an account is not one
    half = service(tmp_path / "half", keys={"maxmind": {"account_id": "1"}})
    assert half.state("maxmind")[0] == "needs_setup"
    nowhere = IpDataService(lambda: GeoIp(), None, offline=True)
    assert (
        nowhere.state("dbip")[0] == "no_directory" and nowhere.state("maxmind")[0] == "needs_setup"
    )


def test_a_table_that_cannot_be_read_does_not_spoil_the_others(tmp_path: Path) -> None:
    svc = service(tmp_path, ("dbip",))
    (tmp_path / "data" / local.IPAPI_FILE).write_bytes(b"not sqlite" * 100)
    info = svc.lookup(PUBLIC)
    assert info.found and info.city == "Frankfurt am Main"
    assert status(info)["ipapi_is"] == "empty" and status(info)["dbip"] == "ok"


def test_a_source_that_fails_is_listed_as_failed_and_the_others_still_vote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service(tmp_path, ("dbip", "iplocate"))
    real = svc.local.lookup

    def lookup(source: str, address: str) -> Any:
        if source == "iplocate":
            raise RuntimeError("broken")
        return real(source, address)

    monkeypatch.setattr(svc.local, "lookup", lookup)
    info = svc.lookup(PUBLIC)
    assert info.found and status(info)["iplocate"] == "error" and status(info)["dbip"] == "ok"
    assert next(s.message for s in info.sources if s.id == "iplocate") == "Could not read its data."


def test_what_a_source_knows_is_what_its_files_have(tmp_path: Path) -> None:
    svc = service(tmp_path, ("iptoasn", "ipdeny"))
    assert svc.can("asn") and not svc.can("position")
    assert [i.id for i in svc.knowing("asn")] == ["iptoasn"]
    assert [i.id for i in svc.knowing("country")] == ["iptoasn", "ipdeny"]
    assert svc.local.knows("ipdeny") == {"country"}


# ---------------------------------------------------------------------------------------------
# Keeping every source's files up to date
# ---------------------------------------------------------------------------------------------

THIS, LAST = "2026-11", "2026-10"
SOURCES = [s for s in IDS if s != "maxmind"]  # MaxMind has its own pretend server (test_maxmind)
KEYS = {"ipinfo": {"token": TOKEN}, "ip2location": {"download_token": TOKEN}}
CDN = "cdn.ipinfo.example"
MEDIA = "media.githubusercontent.com"


def _gz(path: Path) -> bytes:
    return gzip.compress(path.read_bytes())


def _zip_of(name: str, rows: list[str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(name, "\n".join(rows) + "\n")
        bundle.writestr("LICENSE.txt", "CC BY-SA 4.0")
    return out.getvalue()


class Mirror:
    """Pretend servers of every source but MaxMind; counts what was downloaded and how it was asked."""

    def __init__(
        self, tmp_path: Path, months: tuple[str, ...] = (THIS,), version: str = "v1"
    ) -> None:
        work = tmp_path / "mirror"
        work.mkdir(exist_ok=True)
        self.version = version
        self.token = TOKEN
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
        write_source(work / "own", "ip2region")
        self.files[
            "raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v4.xdb"
        ] = (work / "own" / local.XDB_FILES[4]).read_bytes()
        self.files[
            "raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v6.xdb"
        ] = (work / "own" / local.XDB_FILES[6]).read_bytes()
        write_source(work / "own", "iplocate")
        write_source(work / "own", "ipinfo")
        self.files[
            f"{MEDIA}/media/iplocate/ip-address-databases/main/ip-to-country/ip-to-country.mmdb"
        ] = (work / "own" / local.IPLOCATE_FILES["country"]).read_bytes()
        self.files[f"{MEDIA}/media/iplocate/ip-address-databases/main/ip-to-asn/ip-to-asn.mmdb"] = (
            work / "own" / local.IPLOCATE_FILES["asn"]
        ).read_bytes()
        self.ipinfo = (work / "own" / local.IPINFO_FILE).read_bytes()
        self.ipinfo_checksum: str | None = None  # what to say instead of the real one
        self.ipinfo_checksums_status = 200
        self.files["ipapi.is/data/geolocationDatabaseIPv4.csv.zip"] = _zip_of(
            "geolocationDatabaseIPv4.csv", IPAPI_ROWS
        )
        self.files["ipapi.is/data/geolocationDatabaseIPv6.csv.zip"] = _zip_of(
            "geolocationDatabaseIPv6.csv", [IPAPI_HEADER]
        )
        self.ip2location = {
            "DB11LITE": _zip_of("IP2LOCATION-LITE-DB11.CSV", IP2LOCATION_ROWS),
            "DB11LITEIPV6": _zip_of("IP2LOCATION-LITE-DB11.IPV6.CSV", []),
        }
        self.gets: list[str] = []
        self.heads: list[str] = []
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def asked(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "ipinfo.io":
            return self._ipinfo(request)
        if host == CDN:
            return httpx.Response(200, content=self.ipinfo)
        if host == "www.ip2location.com":
            return self._ip2location(request)
        if (
            host == "github.com"
        ):  # IPLocate's databases are on Git LFS: a redirect to the media host
            _, _, rest = path.partition("/iplocate/ip-address-databases/raw/main/")
            target = f"https://{MEDIA}/media/iplocate/ip-address-databases/main/{rest}"
            return httpx.Response(302, headers={"location": target})
        key = f"{host}{path}"
        body = self.files.get(key)
        if body is None:
            return httpx.Response(404)
        headers = {"ETag": f'"{self.version}-{len(body)}"', "Content-Length": str(len(body))}
        if request.method == "HEAD":
            self.heads.append(key)
            return httpx.Response(200, headers=headers)
        self.gets.append(key)
        return httpx.Response(200, content=body, headers=headers)

    def _ipinfo(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("token") != self.token:
            return httpx.Response(401, text="Unauthorized")
        if request.url.path.endswith("/checksums"):
            if self.ipinfo_checksums_status != 200:
                return httpx.Response(self.ipinfo_checksums_status)
            digest = self.ipinfo_checksum or hashlib.sha256(self.ipinfo).hexdigest()
            return httpx.Response(200, json={"checksums": {"sha256": digest}})
        self.gets.append("ipinfo.io/data/ipinfo_lite.mmdb")
        return httpx.Response(
            302, headers={"location": f"https://{CDN}/signed/ipinfo_lite.mmdb?sig=1"}
        )

    def _ip2location(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("token") != self.token:
            return httpx.Response(200, text="NO PERMISSION", headers={"content-type": "text/html"})
        package = request.url.params.get("file", "")
        self.gets.append(f"ip2location/{package}")
        if package not in self.ip2location:
            return httpx.Response(200, text="INVALID FILE CODE")
        return httpx.Response(200, content=self.ip2location[package])


def updater(
    tmp_path: Path,
    mirror: Mirror,
    *,
    offline: bool = False,
    today: date = date(2026, 11, 11),
    directory: Path | None = None,
) -> tuple[downloader.DataUpdater, Path]:
    directory = directory or tmp_path / "data"
    geoip = GeoIp(directory=directory)
    settings = Settings(geoip_dir=str(directory), database_url="sqlite+aiosqlite://")
    return (
        downloader.DataUpdater(
            None,  # type: ignore[arg-type]
            settings,
            directory,
            lambda: geoip,
            offline=offline,
            transport=mirror.transport(),
            today=lambda: today,
        ),
        directory,
    )


def update(
    up: downloader.DataUpdater,
    sources: list[str] | None = None,
    *,
    force: bool = False,
    keys: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        up.update(sources or SOURCES, force=force, keys=KEYS if keys is None else keys)
    )["sources"]  # type: ignore[no-any-return]


@pytest.fixture
def small_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    for source in ("iptoasn", "ipdeny", "ipapi_is", "ip2location"):
        monkeypatch.setitem(downloader.MIN_ROWS, source, 1)


def test_every_source_is_downloaded_checked_and_readable(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    entries = update(up)
    assert all(not e["error"] and e["last_success"] for e in entries.values()), entries
    assert sorted(entries) == sorted(SOURCES)
    names = sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))
    assert names == [
        f"dbip-asn-lite-{THIS}.mmdb", f"dbip-city-lite-{THIS}.mmdb", "ip2location-lite.sqlite",
        "ip2region_v4.xdb", "ip2region_v6.xdb", "ipapi-is.sqlite", "ipdata-status.json",
        "ipdeny.sqlite", "ipinfo-lite.mmdb", "iplocate-asn.mmdb", "iplocate-country.mmdb",
        "iptoasn.sqlite",
    ]  # fmt: skip
    reading = local.LocalSources(lambda: GeoIp(directory=directory), directory)
    dbip = reading.lookup("dbip", PUBLIC)
    assert dbip is not None and dbip.city == "Frankfurt am Main" and dbip.asn == 15133
    asn = reading.lookup("iptoasn", "8.8.8.8")
    assert asn is not None and (asn.asn, asn.organization, asn.country) == (15169, "GOOGLE", "US")
    assert reading.lookup("ipdeny", PUBLIC) == local.Record(country="DE")
    region = reading.lookup("ip2region", PUBLIC)
    assert region is not None and (region.country, region.city) == ("US", MOUNTAIN_VIEW)
    assert reading.lookup("ip2region", "2001:4860:4860::8888") is not None
    ipinfo = reading.lookup("ipinfo", "1.1.1.1")
    assert ipinfo is not None and (ipinfo.country, ipinfo.asn) == ("AU", 15169)
    iplocate = reading.lookup("iplocate", PUBLIC)
    assert iplocate is not None and (iplocate.country, iplocate.asn) == ("US", 15169)
    ipapi = reading.lookup("ipapi_is", PUBLIC)
    assert ipapi is not None and ipapi.city == MOUNTAIN_VIEW  # the narrower range inside Ashburn's
    assert reading.lookup("ipapi_is", "93.184.1.1").city == "Ashburn"  # type: ignore[union-attr]
    ip2location = reading.lookup("ip2location", PUBLIC)
    assert ip2location is not None and ip2location.postal_code == "94043"
    assert all(reading.present(s) for s in SOURCES)
    assert reading.files("iptoasn")[0]["rows"] == 2


def test_nothing_is_downloaded_again_while_the_servers_have_nothing_newer(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    up, _ = updater(tmp_path, mirror)
    update(up)
    first = len(mirror.gets)
    # dbip 2, iptoasn 1, ipdeny 2, ip2region 2, iplocate 2, ipinfo 1, ipapi 2, ip2location 2
    assert first == 2 + 1 + 2 + 2 + 2 + 1 + 2 + 2
    asked, ip2location_asked = len(mirror.requests), len(mirror.asked("www.ip2location.com"))
    update(up)
    assert len(mirror.gets) == first and len(mirror.requests) > asked  # asked, not downloaded
    assert (
        len(mirror.asked("www.ip2location.com")) == ip2location_asked == 2
    )  # not asked within the month
    mirror.version = "v2"  # a new version upstream (same bytes, new ETag: still newer)
    update(up)
    # everything that goes by ETag is downloaded again: not IPinfo (its checksum is the same), not IP2Location (the same month)
    assert len(mirror.gets) == first + (2 + 1 + 2 + 2 + 2 + 2)
    newer = {"asn": "AS64500", "country_code": "AU"}
    mirror.ipinfo = make_db(
        tmp_path / "ipinfo-v2.mmdb", "Lite", {"1.1.1.0/24": newer, "93.184.216.0/24": newer}
    ).read_bytes()  # IPinfo's checksum changed
    update(up, ["ipinfo"])
    assert mirror.gets.count("ipinfo.io/data/ipinfo_lite.mmdb") == 1 + 1
    before = len(mirror.gets)
    update(up, force=True)
    assert len(mirror.gets) == before + first


def test_ip2location_is_downloaded_once_a_month(tmp_path: Path, small_tables: None) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror, today=date(2026, 11, 11))
    update(up, ["ip2location"])
    assert mirror.gets == ["ip2location/DB11LITE", "ip2location/DB11LITEIPV6"]
    for request in mirror.asked("www.ip2location.com"):
        assert request.url.params["token"] == TOKEN and request.url.host == "www.ip2location.com"
    asked = len(mirror.requests)
    update(up, ["ip2location"])  # the same month: not even asked
    assert len(mirror.requests) == asked
    later, _ = updater(tmp_path, mirror, today=date(2026, 12, 2))
    update(later, ["ip2location"])  # a new month
    assert len(mirror.gets) == 4
    entry = downloader.read_status(directory)["sources"]["ip2location"]
    assert entry["stamp"] == "2026-12" and not entry["error"]


def test_ip2location_says_so_when_it_does_not_give_the_file(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    wrong = {"ip2location": {"download_token": "not-the-token-0123456789"}}
    entry = update(up, ["ip2location"], keys=wrong)["ip2location"]
    assert entry["error"]["code"] == "refused"
    assert "NO PERMISSION" in entry["error"]["message"] and "not-the-token" not in str(entry)
    assert not (directory / local.IP2LOCATION_FILE).exists()
    # a refusal is not asked again for hours
    now = datetime.now(UTC)
    assert not downloader.due("ip2location", IpDataOptions(), entry, now + timedelta(hours=5))
    assert downloader.due("ip2location", IpDataOptions(), entry, now + timedelta(hours=7))
    # a package that is not a zip file
    mirror.ip2location["DB11LITE"] = b"<html>sorry</html>"
    broken = update(up, ["ip2location"])["ip2location"]
    assert broken["error"]["code"] == "refused" and "sorry" in broken["error"]["message"]


def test_the_ipinfo_token_goes_to_ipinfo_and_not_along_the_redirect(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    entry = update(up, ["ipinfo"])["ipinfo"]
    assert not entry["error"] and (directory / local.IPINFO_FILE).exists()
    assert entry["stamp"] == hashlib.sha256(mirror.ipinfo).hexdigest()
    to_ipinfo = mirror.asked("ipinfo.io")
    assert [r.url.path for r in to_ipinfo] == [
        "/data/ipinfo_lite.mmdb/checksums",  # the free question first
        "/data/ipinfo_lite.mmdb",
    ]
    assert all(r.url.params["token"] == TOKEN for r in to_ipinfo)
    assert all("token" not in str(r.url) for r in mirror.asked(CDN))
    assert TOKEN not in json.dumps(downloader.read_status(directory))


def test_a_refused_ipinfo_token_is_explained_without_the_token(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    entry = update(up, ["ipinfo"], keys={"ipinfo": {"token": "wrong-token-0123456789"}})["ipinfo"]
    assert entry["error"]["code"] == "refused" and "IPinfo" in entry["error"]["message"]
    assert "wrong-token" not in str(entry) and not (directory / local.IPINFO_FILE).exists()
    assert [r.url.path for r in mirror.asked("ipinfo.io")] == ["/data/ipinfo_lite.mmdb/checksums"]


def test_an_ipinfo_download_that_does_not_match_its_checksum_replaces_nothing(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    update(up, ["ipinfo"])
    good = (directory / local.IPINFO_FILE).read_bytes()
    mirror.ipinfo = mirror.ipinfo + b"tampered"
    mirror.ipinfo_checksum = hashlib.sha256(b"something else").hexdigest()
    entry = update(up, ["ipinfo"])["ipinfo"]
    assert entry["error"]["code"] == "checksum_mismatch"
    assert (directory / local.IPINFO_FILE).read_bytes() == good


def test_ipinfo_without_its_checksum_is_still_downloaded(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path)
    mirror.ipinfo_checksums_status = 500
    up, directory = updater(tmp_path, mirror)
    entry = update(up, ["ipinfo"])["ipinfo"]
    assert not entry["error"] and (directory / local.IPINFO_FILE).exists()
    assert entry["stamp"] == hashlib.sha256(mirror.ipinfo).hexdigest()  # the file's own


def test_last_months_dbip_file_is_used_until_this_months_is_published(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path, months=(LAST,))
    up, directory = updater(tmp_path, mirror)
    result = update(up, ["dbip"])["dbip"]
    assert not result["error"] and (directory / f"dbip-city-lite-{LAST}.mmdb").exists()
    mirror2 = Mirror(tmp_path, months=(THIS, LAST))
    up2, _ = updater(tmp_path, mirror2)
    update(up2, ["dbip"])
    assert (directory / f"dbip-city-lite-{THIS}.mmdb").exists()
    assert not (directory / f"dbip-city-lite-{LAST}.mmdb").exists()  # the older file is gone


def test_a_source_that_has_nothing_published_is_an_error_and_the_others_go_on(
    tmp_path: Path, small_tables: None
) -> None:
    mirror = Mirror(tmp_path, months=())
    up, directory = updater(tmp_path, mirror)
    result = update(up, ["dbip", "iptoasn"])
    assert (
        result["dbip"]["error"]["code"] == "not_found"
        and "DB-IP" in result["dbip"]["error"]["message"]
    )
    assert not result["iptoasn"]["error"] and (directory / "iptoasn.sqlite").exists()
    del mirror.files["ipapi.is/data/geolocationDatabaseIPv6.csv.zip"]
    assert update(up, ["ipapi_is"])["ipapi_is"]["error"]["code"] == "not_found"


def test_data_that_is_not_what_it_says_replaces_nothing(tmp_path: Path, small_tables: None) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    update(up, ["iptoasn", "iplocate", "ipapi_is", "ip2location"])
    kept = {
        name: (directory / name).read_bytes()
        for name in (
            "iptoasn.sqlite",
            local.IPLOCATE_FILES["country"],
            local.IPAPI_FILE,
            local.IP2LOCATION_FILE,
        )
    }
    mirror.version = "v2"
    mirror.files["iptoasn.com/data/ip2asn-combined.tsv.gz"] = gzip.compress(
        b"1.0.0.0\t1.0.0.255\t13335\tAU\tCLOUDFLARENET\n"
    )
    # an mmdb of the wrong kind (it has no country_code), a CSV that is not ipapi.is's, a zip that is not a zip
    wrong = make_db(tmp_path / "wrong.mmdb", "City", {"8.8.8.0/24": {"hello": "world"}})
    country = f"{MEDIA}/media/iplocate/ip-address-databases/main/ip-to-country/ip-to-country.mmdb"
    mirror.files[country] = wrong.read_bytes()
    mirror.files["ipapi.is/data/geolocationDatabaseIPv4.csv.zip"] = _zip_of("x.csv", ["a,b", "1,2"])
    mirror.ip2location["DB11LITE"] = b"PK\x03\x04 this is not really a zip"
    result = update(up, ["iptoasn", "iplocate", "ipapi_is", "ip2location"], force=True)
    for source in ("iptoasn", "iplocate", "ipapi_is", "ip2location"):
        assert result[source]["error"], source
    assert result["iptoasn"]["error"]["code"] == "bad_data"
    assert result["iplocate"]["error"]["code"] == "bad_data"
    assert "ipapi.is" in result["ipapi_is"]["error"]["message"]
    for name, data in kept.items():
        assert (directory / name).read_bytes() == data, name
    mirror.files[
        "raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v4.xdb"
    ] = b"not an xdb" * 70000
    broken = update(up, ["ip2region"])["ip2region"]
    assert broken["error"] and not (directory / "ip2region_v4.xdb").exists()
    assert not list(directory.glob(".*.part")) and not list(
        directory.glob(".download-*")
    )  # nothing left behind


def test_a_partial_failure_keeps_what_was_done(tmp_path: Path, small_tables: None) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror)
    key = f"{MEDIA}/media/iplocate/ip-address-databases/main/ip-to-asn/ip-to-asn.mmdb"
    good = mirror.files[key]
    mirror.files[key] = b"garbage"  # the second of IPLocate's two databases is bad
    entry = update(up, ["iplocate"])["iplocate"]
    assert entry["error"]["code"] == "bad_data"
    assert (directory / local.IPLOCATE_FILES["country"]).exists()
    assert list(json.loads(entry["stamp"])) == [local.IPLOCATE_FILES["country"]]
    mirror.files[key] = good
    before = len(mirror.gets)
    assert not update(up, ["iplocate"])["iplocate"]["error"]
    assert len(mirror.gets) == before + 1  # only the one that was missing


def test_nothing_is_downloaded_offline_or_without_a_directory(tmp_path: Path) -> None:
    mirror = Mirror(tmp_path)
    up, directory = updater(tmp_path, mirror, offline=True)
    update(up)
    assert mirror.requests == [] and not directory.exists()
    nowhere = downloader.DataUpdater(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,
        lambda: GeoIp(),
        transport=mirror.transport(),
    )
    asyncio.run(nowhere.update(["dbip"], keys={}))
    assert mirror.requests == []


def test_a_source_that_needs_a_key_is_not_downloaded_without_it() -> None:
    options = IpDataOptions(source_enabled={"dbip": False})
    wanted = downloader.DataUpdater(None, None, None, lambda: GeoIp()).wanted(options, {})  # type: ignore[arg-type]
    assert "dbip" not in wanted  # off
    assert not {"maxmind", "ipinfo", "ip2location"} & set(wanted)  # no account
    assert {"iplocate", "ipapi_is", "iptoasn", "ipdeny", "ip2region"} <= set(wanted)
    given = downloader.DataUpdater(None, None, None, lambda: GeoIp()).wanted(  # type: ignore[arg-type]
        options,
        {
            "ipinfo": {"token": "x"},
            "maxmind": {"account_id": "1"},
            "ip2location": {"download_token": "y"},
        },
    )
    assert (
        "ipinfo" in given and "ip2location" in given and "maxmind" not in given
    )  # half an account


def _logged(tmp_path: Path, *, configured: bool) -> str:
    """Everything logged at INFO or above while the token-carrying sources are downloaded, with the server's logging set up or not."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    records: list[logging.LogRecord] = []

    class Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    saved = (list(root.handlers), root.level, httpx_logger.level)
    try:
        if configured:
            configure_logging("INFO")
        else:
            root.setLevel(logging.INFO)
            httpx_logger.setLevel(logging.NOTSET)  # as it is when nobody has configured it
        root.addHandler(Catch(level=logging.DEBUG))
        mirror = Mirror(tmp_path)
        up, _ = updater(tmp_path, mirror)
        update(up, ["ipinfo", "ip2location"])
        wrong = {"ipinfo": {"token": "wrong-token-0123456789"}}
        assert update(up, ["ipinfo"], keys=wrong, force=True)["ipinfo"]["error"]
        assert mirror.asked("ipinfo.io"), "the downloads did happen"
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        httpx_logger.setLevel(saved[2])
    return " ".join(r.getMessage() for r in records)


def test_a_token_never_reaches_a_log(tmp_path: Path, small_tables: None) -> None:
    """httpx logs the whole URL of a request at INFO and the token is in the URL: the server's logging must not let it through."""
    unconfigured = _logged(tmp_path / "a", configured=False)
    assert TOKEN in unconfigured, "without the server's logging set up the token would be logged"
    text = _logged(tmp_path / "b", configured=True)
    assert TOKEN not in text and "wrong-token" not in text and "token=" not in text


def test_when_a_check_is_due() -> None:
    options = IpDataOptions(data_update_hours=24)
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    assert downloader.due("dbip", options, {}, now)
    fresh = {"last_attempt": (now - timedelta(hours=3)).isoformat()}
    assert not downloader.due("dbip", options, fresh, now)
    assert downloader.due(
        "dbip", options, {"last_attempt": (now - timedelta(hours=25)).isoformat()}, now
    )
    failed = {
        "last_attempt": (now - timedelta(minutes=70)).isoformat(),
        "error": {"code": "x", "message": "y"},
    }
    assert downloader.due("dbip", options, failed, now)  # an hour after a failure
    recent = {
        "last_attempt": (now - timedelta(minutes=30)).isoformat(),
        "error": {"code": "x", "message": "y"},
    }
    assert not downloader.due("dbip", options, recent, now)
    # a source renewed less often than that is asked less often
    assert BY_ID["ipapi_is"].min_hours == 168 and BY_ID["ip2location"].min_hours == 168
    old = {"last_attempt": (now - timedelta(hours=100)).isoformat()}
    assert downloader.due("dbip", options, old, now)
    assert not downloader.due("ipapi_is", options, old, now)
    assert downloader.next_check("ipapi_is", options, old) == now - timedelta(
        hours=100
    ) + timedelta(hours=168)
    assert downloader.next_check("dbip", options, {}) is None


def test_a_status_file_of_a_process_that_died_is_not_running(tmp_path: Path) -> None:
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    downloader._write_status(  # noqa: SLF001
        tmp_path, {"sources": {"dbip": {"running": True, "running_since": old}}}
    )
    assert downloader.read_status(tmp_path)["sources"]["dbip"]["running"] is False
    assert downloader.read_status(None) == {"sources": {}}
    (tmp_path / downloader.STATUS_FILE).write_text("{not json")
    assert downloader.read_status(tmp_path) == {"sources": {}}


# ---------------------------------------------------------------------------------------------
# The admin API and the lookup API
# ---------------------------------------------------------------------------------------------


def _all_sources(api: Api, directory: Path) -> None:
    """The app with every source's files in its data directory (MaxMind's are what `_geoip` writes)."""
    _geoip(api, directory)
    for source in IDS:
        if source != "maxmind":
            write_source(directory, source)
    state = api.client.app.state  # type: ignore[attr-defined]
    state.ipdata.local.directory = directory


def test_a_source_is_turned_off_and_on_and_the_answer_follows(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _geoip(api, tmp_path / "geoip")
    on = api.get(f"/api/v1/ip/{PUBLIC}").json()
    assert {s["id"] for s in on["sources"] if s["status"] == "ok"} == {"maxmind"}
    api.request("PUT", ADMIN, json={"sources": {"maxmind": {"enabled": False}}})
    sources = {s["id"]: s for s in api.get(ADMIN).json()["sources"]}
    assert sources["maxmind"]["enabled"] is False and sources["maxmind"]["state"] == "disabled"
    assert sources["dbip"]["enabled"] is True  # the others are untouched
    assert api.get(f"/api/v1/ip/{PUBLIC}").status_code == 503  # nothing else can answer
    api.request("PUT", ADMIN, json={"sources": {"maxmind": {"enabled": True}}})
    assert api.get(f"/api/v1/ip/{PUBLIC}").json()["found"]
    changed = [e for e in api.get("/api/v1/admin/audit").json() if e["action"] == "ip_data.updated"]
    assert changed and all("sources" in e["data"]["fields"] for e in changed)


def test_the_lookup_gives_every_field_a_percentage_and_every_source_its_say(
    api: Api, tmp_path: Path
) -> None:
    api.become_admin()
    _all_sources(api, tmp_path / "geoip")
    info = api.get(f"/api/v1/ip/{PUBLIC}").json()
    assert info["found"] and info["country"] == "US" and info["city"] == MOUNTAIN_VIEW
    assert info["confidence"]["country"] >= 90 and 1 <= info["confidence"]["postal_code"] <= 99
    assert len(info["sources"]) == 9 and all(s["status"] == "ok" for s in info["sources"])
    assert all("kind" not in s for s in info["sources"])
    sources = {s["id"]: s for s in info["sources"]}
    assert (
        sources["ipinfo"]["said"]["asn"] == 15169 and sources["ipinfo"]["said"]["country"] == "US"
    )
    assert (
        sources["maxmind"]["said"]["city"] == "Frankfurt am Main"
        and "country" in sources["maxmind"]["differs"]
    )
    assert sources["ipapi_is"]["said"]["postal_code"] == "94043"
    assert sources["ip2location"]["said"]["latitude"] == 37.386
    assert sources["iptoasn"]["said"]["organization"] == "GOOGLE"
    assert info["region_code"] in (None, "HE", "CA")


def test_what_cannot_be_set_is_refused(api: Api) -> None:
    api.become_admin()
    for body in (
        {"sources": {"nonesuch": {"enabled": False}}},
        {"sources": {"ipinfo": {"credentials": {"api_key": "x"}}}},
        {"sources": {"ip2location": {"credentials": {"token": "x"}}}},
        {"sources": {"maxmind": {"credentials": {"license_key": "x" * 20, "token": "x"}}}},
        {"sources": {"ipinfo": {"credentials": {"token": "has a space"}}}},
        {"sources": {"ipinfo": {"credentials": {"token": "x" * 300}}}},
        {"data_update_hours": 1},
    ):
        response = api.request("PUT", ADMIN, json=body)
        assert response.status_code == 422, body


def test_update_now_says_why_it_cannot(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    assert api.post(f"{ADMIN}/update").status_code == 409  # the backend has no directory for them
    updater_ = _updater(api, tmp_path / "data")
    off = {source: {"enabled": False} for source in IDS}
    api.request("PUT", ADMIN, json={"sources": off})
    nothing = api.post(f"{ADMIN}/update")
    assert nothing.status_code == 409 and nothing.json()["error"]["code"] == "nothing_to_update"
    api.request("PUT", ADMIN, json={"sources": {"maxmind": {"enabled": True}}})
    no_account = api.post(
        f"{ADMIN}/update"
    )  # MaxMind is on, and has no account to be downloaded with
    assert (
        no_account.status_code == 409 and no_account.json()["error"]["code"] == "nothing_to_update"
    )
    updater_.offline = True
    api.request("PUT", ADMIN, json={"sources": {"dbip": {"enabled": True}}})
    offline = api.post(f"{ADMIN}/update")  # the server may not call out
    assert offline.status_code == 409 and offline.json()["error"]["code"] == "offline"


def test_update_now_downloads_every_source_that_is_on(
    api: Api, tmp_path: Path, small_tables: None
) -> None:
    api.become_admin()
    directory = tmp_path / "data"
    mirror = Mirror(tmp_path)
    _updater(api, directory, mirror)  # type: ignore[arg-type]
    state = api.client.app.state  # type: ignore[attr-defined]
    state.dataupdater._today = lambda: date(2026, 11, 11)  # noqa: SLF001
    api.request(
        "PUT",
        ADMIN,
        json={"sources": {"maxmind": {"enabled": False}, "ipdeny": {"enabled": False}}},
    )
    given = {
        "ipinfo": {"credentials": {"token": TOKEN}},
        "ip2location": {"credentials": {"download_token": TOKEN}},
    }
    assert api.request("PUT", ADMIN, json={"sources": given}).status_code == 200
    assert api.post(f"{ADMIN}/update").status_code == 202
    wanted = [s for s in IDS if s not in ("maxmind", "ipdeny")]

    def done(page: dict[str, Any]) -> bool:
        rows = [s for s in page["sources"] if s["id"] in wanted]
        return all(s["last_success"] and not s["running"] for s in rows)

    page = _wait(api, done)
    sources = {s["id"]: s for s in page["sources"]}
    assert all(sources[s]["state"] == "ready" and not sources[s]["error"] for s in wanted)
    assert sources["dbip"]["files"][0]["size"] > 0 and sources["iptoasn"]["files"][0]["rows"] == 2
    assert sources["ipapi_is"]["files"][0]["name"] == local.IPAPI_FILE
    assert sources["ip2location"]["files"][0]["name"] == local.IP2LOCATION_FILE
    assert sources["ipdeny"]["state"] == "disabled" and not (directory / "ipdeny.sqlite").exists()
    assert sources["maxmind"]["state"] == "disabled"
    assert sources["ipinfo"]["next_check"] and sources["ip2location"]["next_check"]
    assert TOKEN not in json.dumps(page) and TOKEN not in api.get("/api/v1/admin/audit").text
    info = api.get(f"/api/v1/ip/{PUBLIC}").json()
    answered = {s["id"] for s in info["sources"] if s["status"] == "ok"}
    assert {"dbip", "iptoasn", "iplocate", "ipinfo", "ipapi_is", "ip2location"} <= answered
    assert info["asn"] in (15133, 15169)


def test_the_addresses_of_a_measurement_carry_the_vote_too(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _all_sources(api, tmp_path / "geoip")
    state = api.client.app.state  # type: ignore[attr-defined]
    info = state.ipdata.lookup(PUBLIC)
    assert info.confidence["country"] >= 90 and len(info.sources) == 9


def test_the_data_directory_files_are_what_the_page_lists(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    _all_sources(api, tmp_path / "geoip")
    sources = {s["id"]: s for s in api.get(ADMIN).json()["sources"]}
    assert [f["name"] for f in sources["maxmind"]["files"]] == [
        "GeoLite2-City.mmdb",
        "GeoLite2-ASN.mmdb",
    ]
    assert [f["name"] for f in sources["iplocate"]["files"]] == list(local.IPLOCATE_FILES.values())
    assert [f["name"] for f in sources["ipinfo"]["files"]] == [local.IPINFO_FILE]
    assert [f["name"] for f in sources["ipapi_is"]["files"]] == [local.IPAPI_FILE]
    assert sources["ipapi_is"]["files"][0]["rows"] and sources["ipapi_is"]["files"][0]["built"]
    assert [f["name"] for f in sources["ip2region"]["files"]] == list(local.XDB_FILES.values())
    assert all(f["size"] > 0 for s in sources.values() for f in s["files"])
    assert all(s["state"] == "ready" for s in sources.values())
    # the dashboard says how many sources know places or networks, not nine names in a line
    geoip = api.get("/api/v1/admin/status").json()["geoip"]
    assert geoip["city"] and geoip["asn"] and geoip["source"] == "7 IP data sources"
