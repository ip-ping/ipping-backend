"""Downloading and keeping up the MaxMind databases, against a pretend MaxMind (no network)."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import io
import os
import tarfile
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ipping_backend.config import Settings
from ipping_backend.geolocation.geoip import GeoIp
from ipping_backend.ipdata import updater
from ipping_backend.ipdata.maxmind import MaxMind, MaxMindError
from ipping_backend.ipdata.options import Credentials, IpDataOptions

from .test_geoip import AMSTERDAM, TOKYO, asn_db, city_db, make_db

ACCOUNT, KEY = "123456", "abcDEF0123456789_abcDEF0123456789"
DEFAULT_PUBLISHED = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)


def _archive(
    edition: str, mmdb: bytes, *, name: str | None = None, extra: dict[str, bytes] | None = None
) -> bytes:
    """What MaxMind serves: a folder named for the edition and the date, with the database and some notices."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        files = {
            f"{edition}_20261110/COPYRIGHT.txt": b"copyright",
            f"{edition}_20261110/{name or edition + '.mmdb'}": mmdb,
        }
        files.update(extra or {})
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class FakeMaxMind:
    """MaxMind's download service: Basic credentials, a redirect to a storage host, a checksum, and Last-Modified."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.archives: dict[str, bytes] = {}
        self.published = DEFAULT_PUBLISHED
        self.requests: list[
            tuple[str, str, str, str | None]
        ] = []  # method, host, path+query, Authorization
        self.status_override: dict[str, int] = {}
        self.checksum_override: dict[str, str] = {}
        self.add("GeoLite2-City", city_db(tmp / "city-src.mmdb", AMSTERDAM).read_bytes())
        self.add("GeoLite2-ASN", asn_db(tmp / "asn-src.mmdb").read_bytes())

    def add(self, edition: str, mmdb: bytes, **kw: Any) -> None:
        self.archives[edition] = _archive(edition, mmdb, **kw)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        self.requests.append(
            (
                request.method,
                request.url.host,
                request.url.path + "?" + request.url.query.decode(),
                auth,
            )
        )
        if request.url.host == "storage.example.net":  # the signed address MaxMind redirects to
            edition = request.url.path.split("/")[-1].removesuffix(".tar.gz")
            return httpx.Response(
                200,
                content=self.archives[edition],
                headers={"last-modified": format_datetime(self.published, usegmt=True)},
            )
        assert request.url.host == "download.maxmind.com", request.url
        edition = request.url.path.split("/")[-2]
        if self.status_override.get(edition):
            return httpx.Response(self.status_override[edition])
        import base64

        if auth != "Basic " + base64.b64encode(f"{ACCOUNT}:{KEY}".encode()).decode():
            return httpx.Response(401, text="no")
        if edition not in self.archives:
            return httpx.Response(404)
        suffix = request.url.params["suffix"]
        stamp = {"last-modified": format_datetime(self.published, usegmt=True)}
        if suffix == "tar.gz.sha256":
            digest = (
                self.checksum_override.get(edition)
                or hashlib.sha256(self.archives[edition]).hexdigest()
            )
            return httpx.Response(200, text=f"{digest}  {edition}_20261110.tar.gz\n")
        if request.method == "HEAD":
            return httpx.Response(200, headers=stamp)
        return httpx.Response(
            302,
            headers={"location": f"https://storage.example.net/signed/{edition}.tar.gz?sig=abc"},
        )

    def gets(self, edition: str) -> int:
        """How many times the archive itself was downloaded (from the storage host MaxMind redirects to)."""
        return sum(
            1
            for _, host, path, _ in self.requests
            if host == "storage.example.net" and edition in path
        )


@pytest.fixture
def fake(tmp_path: Path) -> FakeMaxMind:
    return FakeMaxMind(tmp_path)


def _install(
    fake: FakeMaxMind, directory: Path, edition: str = "GeoLite2-City", key: str = KEY
) -> Any:
    async def go() -> Any:
        async with httpx.AsyncClient(transport=fake.transport()) as client:
            return await MaxMind(ACCOUNT, key).install(edition, directory, client)

    return asyncio.run(go())


def _error(
    fake: FakeMaxMind, directory: Path, edition: str = "GeoLite2-City", key: str = KEY
) -> MaxMindError:
    with pytest.raises(MaxMindError) as caught:
        _install(fake, directory, edition, key)
    return caught.value


# ---------------------------------------------------------------------------------------------
# One database
# ---------------------------------------------------------------------------------------------


def test_a_database_is_downloaded_checked_and_put_in_place(
    fake: FakeMaxMind, tmp_path: Path
) -> None:
    directory = tmp_path / "geoip"
    installed = _install(fake, directory)
    assert (
        installed.path == directory / "GeoLite2-City.mmdb"
        and installed.size > 0
        and installed.build_epoch > 0
    )
    assert sorted(p.name for p in directory.iterdir()) == [
        "GeoLite2-City.mmdb"
    ]  # nothing left over
    # MaxMind's file is used in preference to another that is newer: it is the one the administrator chose
    other = city_db(directory / "dbip-city-lite-2026-11.mmdb", TOKYO)
    os.utime(other, (time.time() + 600, time.time() + 600))
    geoip = GeoIp(directory=directory)
    place = geoip.place("93.184.216.34")
    assert place is not None and place.name == "Amsterdam"
    assert geoip.source == "MaxMind GeoLite2" and "MaxMind" in (geoip.attribution or "")


def test_the_credentials_go_to_maxmind_and_not_along_the_redirect(
    fake: FakeMaxMind, tmp_path: Path
) -> None:
    _install(fake, tmp_path / "geoip")
    to_maxmind = [r for r in fake.requests if r[1] == "download.maxmind.com"]
    to_storage = [r for r in fake.requests if r[1] == "storage.example.net"]
    assert to_maxmind and all(r[3] and r[3].startswith("Basic ") for r in to_maxmind)
    assert to_storage and all(r[3] is None for r in to_storage)


def test_a_download_that_does_not_match_the_published_checksum_is_not_installed(
    fake: FakeMaxMind, tmp_path: Path
) -> None:
    directory = tmp_path / "geoip"
    _install(fake, directory)
    before = (directory / "GeoLite2-City.mmdb").read_bytes()
    fake.add("GeoLite2-City", city_db(tmp_path / "other.mmdb", TOKYO).read_bytes())
    fake.checksum_override["GeoLite2-City"] = "0" * 64
    assert _error(fake, directory).code == "checksum_mismatch"
    assert (directory / "GeoLite2-City.mmdb").read_bytes() == before  # the old one is still there
    fake.checksum_override["GeoLite2-City"] = "not a checksum"
    assert _error(fake, directory).code == "checksum_mismatch"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "refused"),
        (403, "refused"),
        (404, "not_found"),
        (429, "rate_limited"),
        (503, "unavailable"),
    ],
)
def test_what_maxmind_says_is_explained_without_the_key(
    fake: FakeMaxMind, tmp_path: Path, status: int, code: str
) -> None:
    fake.status_override["GeoLite2-City"] = status
    error = _error(fake, tmp_path / "geoip")
    assert error.code == code and KEY not in error.message and ACCOUNT not in error.message
    if code == "rate_limited":
        assert error.retry_after_s and error.retry_after_s >= 3600


def test_wrong_credentials_are_refused(fake: FakeMaxMind, tmp_path: Path) -> None:
    assert _error(fake, tmp_path / "geoip", key="wrongwrongwrongwrong1234").code == "refused"


def test_a_failed_connection_does_not_leak_the_key(tmp_path: Path) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect with {KEY}", request=request)

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            await MaxMind(ACCOUNT, KEY).install("GeoLite2-City", tmp_path / "geoip", client)

    with pytest.raises(MaxMindError) as caught:
        asyncio.run(go())
    assert (
        caught.value.code == "network"
        and KEY not in caught.value.message
        and KEY not in str(caught.value)
    )


def test_a_database_of_the_wrong_kind_or_a_broken_archive_is_not_installed(
    fake: FakeMaxMind, tmp_path: Path
) -> None:
    directory = tmp_path / "geoip"
    fake.add(
        "GeoLite2-City", asn_db(tmp_path / "x.mmdb").read_bytes()
    )  # an ASN database served as City
    assert _error(fake, directory).code == "bad_database"
    fake.add("GeoLite2-City", b"this is not a database")
    assert _error(fake, directory).code == "bad_database"
    fake.archives["GeoLite2-City"] = b"not an archive at all"
    assert _error(fake, directory).code == "bad_archive"
    fake.add("GeoLite2-City", b"x", name="something-else.mmdb")  # no GeoLite2-City.mmdb in it
    assert _error(fake, directory).code == "bad_archive"
    both = city_db(tmp_path / "y.mmdb").read_bytes()
    fake.add(
        "GeoLite2-City", both, extra={"GeoLite2-City_20261110/sub/GeoLite2-City.mmdb": both}
    )  # two of them
    assert _error(fake, directory).code == "bad_archive"
    assert not (directory / "GeoLite2-City.mmdb").exists()
    assert [
        p.name for p in directory.iterdir()
    ] == []  # the scratch space is cleaned up after each failure


def test_a_database_without_an_answer_for_a_known_address_is_not_installed(
    fake: FakeMaxMind, tmp_path: Path
) -> None:
    empty = make_db(tmp_path / "empty.mmdb", "City", {"192.0.2.0/24": TOKYO})
    fake.add("GeoLite2-City", empty.read_bytes())
    assert _error(fake, tmp_path / "geoip").code == "bad_database"


def test_only_https_is_used() -> None:
    with pytest.raises(ValueError, match="https"):
        MaxMind(ACCOUNT, KEY, base_url="http://download.maxmind.com")


def test_publication_is_asked_with_a_head_request(fake: FakeMaxMind) -> None:
    async def go() -> list[datetime | None]:
        async with httpx.AsyncClient(transport=fake.transport()) as client:
            mm = MaxMind(ACCOUNT, KEY)
            first = await mm.published("GeoLite2-City", client)
            fake.status_override["GeoLite2-ASN"] = 200  # answers, but says nothing about when
            second = await mm.published("GeoLite2-ASN", client)
            return [first, second]

    first, second = asyncio.run(go())
    assert first == DEFAULT_PUBLISHED and second is None
    assert [r[0] for r in fake.requests if "GeoLite2-City" in r[2]] == [
        "HEAD"
    ]  # no download for a question

    async def refused() -> None:
        async with httpx.AsyncClient(transport=fake.transport()) as client:
            await MaxMind(ACCOUNT, "wrongwrongwrongwrong1234").published("GeoLite2-City", client)

    with pytest.raises(MaxMindError):
        asyncio.run(refused())


# ---------------------------------------------------------------------------------------------
# Keeping them up to date
# ---------------------------------------------------------------------------------------------


def _updater(tmp_path: Path, fake: FakeMaxMind) -> tuple[updater.MaxMindUpdater, GeoIp, Path]:
    directory = tmp_path / "geoip"
    geoip = GeoIp(directory=directory)
    settings = Settings(geoip_dir=str(directory), database_url="sqlite+aiosqlite://")
    return (
        updater.MaxMindUpdater(None, settings, geoip, transport=fake.transport()),
        geoip,
        directory,
    )  # type: ignore[arg-type]


OPTIONS = IpDataOptions(maxmind_editions=["GeoLite2-City", "GeoLite2-ASN"])
CREDENTIALS = Credentials(account_id=ACCOUNT, license_key=KEY, source="admin")


def test_an_update_downloads_each_edition_and_records_it(tmp_path: Path, fake: FakeMaxMind) -> None:
    up, geoip, directory = _updater(tmp_path, fake)
    assert geoip.place("93.184.216.34") is None  # nothing yet
    status = asyncio.run(up.update(OPTIONS, CREDENTIALS))
    assert status["error"] is None and status["last_success"] and not status["running"]
    assert sorted(status["editions"]) == ["GeoLite2-ASN", "GeoLite2-City"]
    assert all(
        e["build_epoch"] > 0 and e["size"] > 0 and e["error"] is None
        for e in status["editions"].values()
    )
    place, asn = geoip.place("93.184.216.34"), geoip.asn("93.184.216.34")
    assert (
        place is not None and place.name == "Amsterdam" and asn is not None and asn.number == 15133
    )  # without a restart
    assert updater.read_status(directory) == status  # kept for whichever process is asked


def test_an_edition_that_is_not_newer_is_not_downloaded_again(
    tmp_path: Path, fake: FakeMaxMind
) -> None:
    up, _, _ = _updater(tmp_path, fake)
    asyncio.run(up.update(OPTIONS, CREDENTIALS))
    assert fake.gets("GeoLite2-City") == 1
    asyncio.run(
        up.update(OPTIONS, CREDENTIALS)
    )  # MaxMind published before we downloaded: nothing to do
    assert fake.gets("GeoLite2-City") == 1 and fake.gets("GeoLite2-ASN") == 1
    fake.published = datetime.now(UTC) + timedelta(hours=1)  # something newer
    asyncio.run(up.update(OPTIONS, CREDENTIALS))
    assert fake.gets("GeoLite2-City") == 2
    asyncio.run(up.update(OPTIONS, CREDENTIALS, force=True))  # asked for by hand: no question first
    assert fake.gets("GeoLite2-City") == 3


def test_a_refused_key_is_recorded_and_stops_the_rest(tmp_path: Path, fake: FakeMaxMind) -> None:
    up, _, directory = _updater(tmp_path, fake)
    bad = Credentials(account_id=ACCOUNT, license_key="wrongwrongwrongwrong1234", source="admin")
    status = asyncio.run(up.update(OPTIONS, bad))
    assert status["error"]["code"] == "refused" and KEY not in str(status)
    assert status["editions"]["GeoLite2-City"]["error"] and "GeoLite2-ASN" not in status["editions"]
    assert status["last_success"] is None and not (directory / "GeoLite2-City.mmdb").exists()
    assert not updater.due(
        OPTIONS, status, datetime.now(UTC) + timedelta(hours=1)
    )  # not tried again at once
    assert updater.due(OPTIONS, status, datetime.now(UTC) + timedelta(hours=7))


def test_when_the_next_check_is_due() -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    never: dict[str, Any] = {"last_attempt": None, "error": None}
    assert updater.due(OPTIONS, never, now)
    fine = {"last_attempt": (now - timedelta(hours=23)).isoformat(), "error": None}
    assert not updater.due(OPTIONS, fine, now)
    assert updater.due(
        OPTIONS, {**fine, "last_attempt": (now - timedelta(hours=25)).isoformat()}, now
    )
    assert updater.due(
        IpDataOptions(maxmind_update_hours=6),
        {**fine, "last_attempt": (now - timedelta(hours=7)).isoformat()},
        now,
    )
    broken = {
        "last_attempt": (now - timedelta(minutes=30)).isoformat(),
        "error": {"code": "network"},
    }
    assert not updater.due(OPTIONS, broken, now)
    assert updater.due(
        OPTIONS, {**broken, "last_attempt": (now - timedelta(minutes=70)).isoformat()}, now
    )


def test_one_process_per_directory_downloads(tmp_path: Path, fake: FakeMaxMind) -> None:
    up, _, directory = _updater(tmp_path, fake)
    directory.mkdir()
    other = os.open(directory / updater.LOCK_FILE, os.O_CREAT | os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)  # another backend process is downloading
    try:
        asyncio.run(up.update(OPTIONS, CREDENTIALS))
        assert fake.requests == []
    finally:
        os.close(other)
    asyncio.run(up.update(OPTIONS, CREDENTIALS))
    assert fake.gets("GeoLite2-City") == 1


def test_a_process_that_died_mid_update_does_not_leave_the_page_running(tmp_path: Path) -> None:
    directory = tmp_path / "geoip"
    stale = {"running": True, "running_since": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    directory.mkdir()
    (directory / updater.STATUS_FILE).write_text(__import__("json").dumps(stale))
    assert updater.read_status(directory)["running"] is False
    (directory / updater.STATUS_FILE).write_text("not json")
    assert (
        updater.read_status(directory)["last_attempt"] is None
    )  # an unreadable file is an empty one
    assert updater.read_status(None)["editions"] == {}
