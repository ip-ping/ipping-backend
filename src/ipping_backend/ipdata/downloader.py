"""Keeping every IP data source's files on disk and up to date: all nine are treated the same.

Every backend process runs this loop and they cooperate through the data directory: a lock (the process that has it
downloads) and a status file (what was fetched when, and how it went), so the admin page shows the same thing whichever
process answers.

A source is asked whether it has something newer (a free HEAD request, a checksum, or the month: whatever the source offers) and
downloaded only when it has. What is downloaded is checked before it is used (it must open, be the right kind of file, and answer
for a well known address) and goes into place atomically, so a process reading the directory never sees half a file. Nothing is
downloaded while `offline` is on, or for a source that is turned off or that needs a key it has not been given.

A key or token is sent only to the service it belongs to, and never put into a message, the status file or a log line.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import gzip
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Any

import httpx
import maxminddb
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import __version__
from ..config import Settings
from ..geolocation.geoip import GeoIp
from ..geolocation.update import check_database, months
from . import local
from .catalog import BY_ID, IDS, fetchable
from .maxmind import MaxMind, MaxMindError
from .options import IpDataOptions, load_keys, load_options
from .places import PlaceDb, PlaceTable, Span, csv_lines, ip2location_spans, ipapi_spans
from .places import build as build_places
from .ranges import RangeDb, RangeError, build, ipdeny_rows, iptoasn_rows
from .xdb import XdbError, read_header, search

log = logging.getLogger(__name__)

STATUS_FILE = "ipdata-status.json"
LOCK_FILE = ".ipdata.lock"
TICK_S = 300.0
FIRST_TICK_S = 25.0
STALE_RUNNING = timedelta(minutes=30)

DBIP = "https://download.db-ip.com/free"
IPTOASN = "https://iptoasn.com/data/ip2asn-combined.tsv.gz"
IPDENY_V4 = "https://www.ipdeny.com/ipblocks/data/countries/all-zones.tar.gz"
IPDENY_V6 = "https://www.ipdeny.com/ipv6/ipaddresses/blocks/ipv6-all-zones.tar.gz"
IP2REGION = "https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data"
IPLOCATE = "https://github.com/iplocate/ip-address-databases/raw/main"
IPLOCATE_URLS = {
    local.IPLOCATE_FILES["country"]: f"{IPLOCATE}/ip-to-country/ip-to-country.mmdb",
    local.IPLOCATE_FILES["asn"]: f"{IPLOCATE}/ip-to-asn/ip-to-asn.mmdb",
}
IPINFO = "https://ipinfo.io/data/ipinfo_lite.mmdb"
IPAPI_V4 = "https://ipapi.is/data/geolocationDatabaseIPv4.csv.zip"
IPAPI_V6 = "https://ipapi.is/data/geolocationDatabaseIPv6.csv.zip"
IP2LOCATION = "https://www.ip2location.com/download/"
IP2LOCATION_V4, IP2LOCATION_V6 = "DB11LITE", "DB11LITEIPV6"
MAXMIND_EDITIONS = ("GeoLite2-City", "GeoLite2-ASN")
# A table with fewer ranges than this cannot be the real data (the tests lower them)
MIN_ROWS = {"iptoasn": 100_000, "ipdeny": 1000, "ipapi_is": 1_000_000, "ip2location": 1_000_000}
MAX_BYTES = {
    "mmdb": 1_500_000_000,
    "tsv": 200_000_000,
    "zones": 60_000_000,
    "xdb": 200_000_000,
    "zip": 600_000_000,
}


class SourceError(Exception):
    """A download that did not work. `message` is safe to show: it never holds a key."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Outcome:
    """What a source's update did, filled in as it goes so that what was done before a failure is not lost."""

    changed: bool = False  # a file was replaced
    stamp: str | None = None  # what says which version is here


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(when: datetime | None) -> str | None:
    return when.isoformat(timespec="seconds") if when else None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_status(directory: Path | None) -> dict[str, Any]:
    empty: dict[str, Any] = {"sources": {}}
    if directory is None:
        return empty
    try:
        data = json.loads((directory / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return empty
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, dict):
        return empty
    for entry in sources.values():
        since = parse_time(entry.get("running_since")) if isinstance(entry, dict) else None
        if (
            isinstance(entry, dict)
            and entry.get("running")
            and (since is None or _now() - since > STALE_RUNNING)
        ):
            entry["running"] = False  # a process that died mid-download
    return {"sources": {k: v for k, v in sources.items() if isinstance(v, dict)}}


def _write_status(directory: Path, status: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=directory, prefix=".ipdata-status-", suffix=".part")
    try:
        with os.fdopen(handle, "w") as out:
            json.dump(status, out)
        os.replace(name, directory / STATUS_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def _try_lock(directory: Path) -> int | None:
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _wait(source: str, options: IpDataOptions, entry: dict[str, Any]) -> timedelta:
    """How long after the last attempt a source is asked again: the administrator's interval (or the source's own, if longer);
    an hour after a failure, six after a key that was refused or a limit that was reached."""
    error = entry.get("error")
    if isinstance(error, dict):
        if error.get("code") in ("refused", "rate_limited"):
            return timedelta(hours=6)
        return timedelta(hours=1)
    return timedelta(hours=max(options.data_update_hours, BY_ID[source].min_hours))


def due(
    source: str, options: IpDataOptions, entry: dict[str, Any], now: datetime | None = None
) -> bool:
    now = now or _now()
    last = parse_time(entry.get("last_attempt"))
    return last is None or now - last >= _wait(source, options, entry)


def next_check(source: str, options: IpDataOptions, entry: dict[str, Any]) -> datetime | None:
    last = parse_time(entry.get("last_attempt"))
    return None if last is None else last + _wait(source, options, entry)


class DataUpdater:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        directory: Path | None,
        geoip_of: Callable[[], GeoIp],
        *,
        offline: bool = False,
        on_updated: Callable[[], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self.directory = directory
        self._geoip_of = geoip_of
        self.offline = offline
        self._on_updated = on_updated
        self._transport = transport
        self._today = today
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def status(self) -> dict[str, Any]:
        status = read_status(self.directory)
        if self._lock.locked():
            for entry in status["sources"].values():
                entry.setdefault("running", False)
        return status

    def running(self) -> bool:
        return self._lock.locked() or (self._task is not None and not self._task.done())

    async def _settings_now(self) -> tuple[IpDataOptions, dict[str, dict[str, str]]]:
        async with self._sessionmaker() as db:
            return await load_options(db), await load_keys(db, self._settings)

    def wanted(self, options: IpDataOptions, keys: dict[str, dict[str, str]]) -> list[str]:
        """The sources to download: on, and given what they need."""
        return [s for s in IDS if options.enabled(s) and fetchable(s, keys)]

    async def run(self) -> None:
        await asyncio.sleep(FIRST_TICK_S)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("the IP data update check failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        if self.directory is None or self.offline:
            return
        options, keys = await self._settings_now()
        status = read_status(self.directory)
        wanted = [
            s for s in self.wanted(options, keys) if due(s, options, status["sources"].get(s, {}))
        ]
        if wanted:
            await self.update(wanted, keys=keys)

    def start(self, *, force: bool = False) -> bool:
        """Check every source that is on, in the background (the admin page's "Check now"). False if a check is going.

        A source is downloaded only if it has something newer, unless `force` says to download everything again."""
        if self.running():
            return False
        self._task = asyncio.create_task(self._run_once(force), name="ipdata-update")
        return True

    async def _run_once(self, force: bool) -> None:
        try:
            options, keys = await self._settings_now()
            await self.update(self.wanted(options, keys), force=force, keys=keys)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the IP data update failed")

    async def update(
        self,
        sources: list[str],
        *,
        force: bool = False,
        keys: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        directory = self.directory
        if directory is None or self.offline or not sources:
            return self.status()
        if keys is None:
            keys = (await self._settings_now())[1]
        async with self._lock:
            fd = await asyncio.to_thread(_try_lock, directory)
            if fd is None:
                return (
                    self.status()
                )  # another process is on it: its result lands in the status file
            try:
                return await self._update_locked(directory, sources, force, keys)
            finally:
                await asyncio.to_thread(_unlock, fd)

    async def _update_locked(
        self, directory: Path, sources: list[str], force: bool, keys: dict[str, dict[str, str]]
    ) -> dict[str, Any]:
        status = await asyncio.to_thread(read_status, directory)
        installed_any = False
        headers = {"User-Agent": f"ipping-backend/{__version__}"}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=180.0),
            headers=headers,
            transport=self._transport,
            follow_redirects=True,
        ) as client:
            for source in sources:
                entry = dict(status["sources"].get(source, {}))
                entry.update(running=True, running_since=_iso(_now()), last_attempt=_iso(_now()))
                status["sources"][source] = entry
                await asyncio.to_thread(_write_status, directory, status)
                outcome = Outcome()
                try:
                    await self._one(
                        source,
                        directory,
                        client,
                        entry.get("stamp"),
                        force,
                        keys.get(source, {}),
                        outcome,
                    )
                    entry.update(error=None, last_success=_iso(_now()))
                except SourceError as exc:
                    entry["error"] = {"code": exc.code, "message": exc.message}
                    log.warning("IP data %s: %s", source, exc.message)
                except RangeError as exc:
                    entry["error"] = {"code": "bad_data", "message": str(exc)}
                    log.warning("IP data %s: %s", source, exc)
                except (httpx.HTTPError, OSError, ValueError, XdbError, sqlite3.Error) as exc:
                    # the text of these can hold a URL, and a URL can hold a token: only what kind of failure it was is kept
                    entry["error"] = {
                        "code": "failed",
                        "message": f"The update failed ({type(exc).__name__}).",
                    }
                    log.warning("IP data %s: %s", source, type(exc).__name__)
                if outcome.stamp is not None:
                    entry["stamp"] = outcome.stamp
                if outcome.changed:
                    entry["downloaded_at"] = _iso(_now())
                    installed_any = True
                entry.update(running=False, running_since=None)
                status["sources"][source] = entry
                await asyncio.to_thread(_write_status, directory, status)
        if installed_any:
            self._geoip_of().rescan()
            if self._on_updated is not None:
                try:
                    await self._on_updated()
                except Exception:
                    log.exception("refreshing what is built on the databases failed")
        return status

    # ---- each source ------------------------------------------------------------------------------------------------
    async def _one(
        self,
        source: str,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        credentials: dict[str, str],
        out: Outcome,
    ) -> None:
        match source:
            case "maxmind":
                await self._maxmind(directory, client, stamp, force, credentials, out)
            case "dbip":
                await self._dbip(directory, client, stamp, force, out)
            case "ipinfo":
                await self._ipinfo(directory, client, stamp, force, credentials, out)
            case "iplocate":
                await self._iplocate(directory, client, stamp, force, out)
            case "ipapi_is":
                await self._ipapi(directory, client, stamp, force, out)
            case "ip2location":
                await self._ip2location(directory, client, stamp, force, credentials, out)
            case "iptoasn":
                await self._iptoasn(directory, client, stamp, force, out)
            case "ipdeny":
                await self._ipdeny(directory, client, stamp, force, out)
            case "ip2region":
                await self._ip2region(directory, client, stamp, force, out)
            case _:
                raise SourceError("unknown", "Not a source that is downloaded.")

    async def _maxmind(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        credentials: dict[str, str],
        out: Outcome,
    ) -> None:
        """The City and ASN databases. MaxMind says when it published each (a HEAD request, which is free); one is downloaded
        when that is later than what is here, and its checksum must match the one MaxMind publishes."""
        maxmind = MaxMind(
            credentials["account_id"],
            credentials["license_key"],
            base_url=self._settings.maxmind_base_url,
        )
        known = _stamps(stamp)
        stamps = dict(known)
        failure: SourceError | None = None
        for edition in MAXMIND_EDITIONS:
            final = directory / f"{edition}.mmdb"
            try:
                published = await maxmind.published(edition, client)
                have = parse_time(known.get(edition)) or await asyncio.to_thread(_modified, final)
                if (
                    not force
                    and have is not None
                    and published is not None
                    and published <= have
                    and await asyncio.to_thread(final.exists)
                ):
                    continue
                await maxmind.install(edition, directory, client)
                stamps[edition] = _iso(published or _now()) or ""
                out.changed = True
            except MaxMindError as exc:
                failure = failure or SourceError(exc.code, exc.message)
                log.warning("MaxMind %s: %s", edition, exc.message)
                if exc.code in ("refused", "rate_limited"):
                    break  # the other would be refused the same way
            out.stamp = json.dumps(stamps)
        out.stamp = json.dumps(stamps)
        if failure is not None:
            raise failure

    async def _dbip(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        out: Outcome,
    ) -> None:
        known = _stamps(stamp)
        stamps: dict[str, str] = {}
        for kind in ("city", "asn"):
            found: tuple[str, str] | None = None
            for month in months(self._today()):
                name = f"dbip-{kind}-lite-{month}.mmdb"
                version = await _head(client, f"{DBIP}/{name}.gz", "DB-IP")
                if version is not None:
                    found = (name, version)
                    break
            if found is None:
                raise SourceError(
                    "not_found", f"DB-IP has not published the {kind} database for this month yet."
                )
            name, version = found
            stamps[kind] = f"{name}:{version}"
            if (
                not force
                and known.get(kind) == stamps[kind]
                and await asyncio.to_thread(_has_dbip, directory, kind)
            ):
                continue
            with _scratch(directory) as part:
                await _download(client, f"{DBIP}/{name}.gz", part, MAX_BYTES["mmdb"], name="DB-IP")
                await asyncio.to_thread(_install_dbip, part, directory, name, kind)
            out.changed = True
            out.stamp = json.dumps({**known, **stamps})
        out.stamp = json.dumps(stamps)

    async def _ipinfo(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        credentials: dict[str, str],
        out: Outcome,
    ) -> None:
        """IPinfo Lite. The checksum IPinfo publishes for the file (it does not count against the ten downloads a day) says
        whether there is something newer, and the download must match it."""
        token = credentials["token"]
        final = directory / local.IPINFO_FILE
        checksum = await _ipinfo_checksum(client, token)
        if (
            not force
            and checksum is not None
            and stamp == checksum
            and await asyncio.to_thread(final.exists)
        ):
            out.stamp = checksum
            return
        with _scratch(directory) as part:
            digest = await _download(
                client, IPINFO, part, MAX_BYTES["mmdb"], name="IPinfo", params={"token": token}
            )
            if checksum is not None and digest != checksum:
                raise SourceError(
                    "checksum_mismatch",
                    "The download does not match IPinfo's checksum, so it was not installed.",
                )
            await asyncio.to_thread(_install_mmdb, part, final, "1.1.1.1", "asn")
        out.changed = True
        out.stamp = checksum or digest

    async def _iplocate(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        out: Outcome,
    ) -> None:
        known = _stamps(stamp)
        stamps = dict(known)
        for name, url in IPLOCATE_URLS.items():
            version = await _head(client, url, "IPLocate")
            if version is None:
                raise SourceError("not_found", f"IPLocate has no {name} to download.")
            if (
                not force
                and known.get(name) == version
                and await asyncio.to_thread((directory / name).exists)
            ):
                continue
            with _scratch(directory) as part:
                await _download(client, url, part, MAX_BYTES["mmdb"], name="IPLocate")
                must_have = "country_code" if "country" in name else "asn"
                await asyncio.to_thread(_install_mmdb, part, directory / name, "8.8.8.8", must_have)
            stamps[name] = version
            out.changed = True
            out.stamp = json.dumps(stamps)
        out.stamp = json.dumps(stamps)

    async def _ipapi(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        out: Outcome,
    ) -> None:
        versions = [
            await _head(client, IPAPI_V4, "ipapi.is"),
            await _head(client, IPAPI_V6, "ipapi.is"),
        ]
        if None in versions:
            raise SourceError("not_found", "ipapi.is has no geolocation file to download.")
        version = "|".join(v or "" for v in versions)
        if (
            not force
            and stamp == version
            and await asyncio.to_thread((directory / local.IPAPI_FILE).exists)
        ):
            out.stamp = version
            return
        with _scratch(directory) as first, _scratch(directory) as second:
            await _download(client, IPAPI_V4, first, MAX_BYTES["zip"], name="ipapi.is")
            await _download(client, IPAPI_V6, second, MAX_BYTES["zip"], name="ipapi.is")
            await asyncio.to_thread(
                _build_places,
                directory,
                local.IPAPI_FILE,
                [
                    (4, _csv_pass(first, 4, ipapi_spans)),
                    (6, _csv_pass(second, 6, ipapi_spans)),
                ],
                "ipapi.is",
                "ipapi_is",
            )
        out.changed = True
        out.stamp = version

    async def _ip2location(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        credentials: dict[str, str],
        out: Outcome,
    ) -> None:
        """IP2Location LITE: renewed on the first of the month, and its owners ask for one download a month, so the month is the version."""
        token = credentials["download_token"]
        month = self._today().strftime("%Y-%m")
        if (
            not force
            and stamp == month
            and await asyncio.to_thread((directory / local.IP2LOCATION_FILE).exists)
        ):
            out.stamp = month
            return
        with _scratch(directory) as first, _scratch(directory) as second:
            for package, part in ((IP2LOCATION_V4, first), (IP2LOCATION_V6, second)):
                await _download(
                    client,
                    IP2LOCATION,
                    part,
                    MAX_BYTES["zip"],
                    name="IP2Location",
                    params={"token": token, "file": package},
                )
                await asyncio.to_thread(_expect_zip, part, "IP2Location", token)
            await asyncio.to_thread(
                _build_places,
                directory,
                local.IP2LOCATION_FILE,
                [
                    (4, _csv_pass(first, 4, ip2location_spans)),
                    (6, _csv_pass(second, 6, ip2location_spans)),
                ],
                "lite.ip2location.com",
                "ip2location",
            )
        out.changed = True
        out.stamp = month

    async def _iptoasn(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        out: Outcome,
    ) -> None:
        version = await _head(client, IPTOASN, "IPtoASN") or "unknown"
        if (
            not force
            and stamp == version
            and await asyncio.to_thread((directory / local.IPTOASN_FILE).exists)
        ):
            out.stamp = version
            return
        with _scratch(directory) as part:
            await _download(client, IPTOASN, part, MAX_BYTES["tsv"], name="IPtoASN")

            def build_table() -> None:
                table = directory / f".{local.IPTOASN_FILE}.part"
                try:
                    with gzip.open(part, "rt", encoding="utf-8", errors="replace") as lines:
                        count = build(
                            table,
                            iptoasn_rows(lines),
                            {"built_at": _iso(_now()) or "", "source": "iptoasn.com"},
                        )
                    found = RangeDb(table).find("8.8.8.8")
                    if count < MIN_ROWS["iptoasn"] or found is None or found.asn != 15169:
                        raise SourceError(
                            "bad_data", "The IPtoASN file did not look like IPtoASN data."
                        )
                    os.replace(table, directory / local.IPTOASN_FILE)
                finally:
                    table.unlink(missing_ok=True)

            await asyncio.to_thread(build_table)
        out.changed = True
        out.stamp = version

    async def _ipdeny(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        out: Outcome,
    ) -> None:
        versions = [
            await _head(client, IPDENY_V4, "IPdeny") or "",
            await _head(client, IPDENY_V6, "IPdeny") or "",
        ]
        version = "|".join(versions)
        if (
            not force
            and stamp == version
            and await asyncio.to_thread((directory / local.IPDENY_FILE).exists)
        ):
            out.stamp = version
            return
        with _scratch(directory) as first, _scratch(directory) as second:
            await _download(client, IPDENY_V4, first, MAX_BYTES["zones"], name="IPdeny")
            await _download(client, IPDENY_V6, second, MAX_BYTES["zones"], name="IPdeny")

            def build_table() -> None:
                table = directory / f".{local.IPDENY_FILE}.part"
                try:
                    rows = chain(ipdeny_rows(first.read_bytes()), ipdeny_rows(second.read_bytes()))
                    count = build(
                        table, rows, {"built_at": _iso(_now()) or "", "source": "ipdeny.com"}
                    )
                    found = RangeDb(table).find("8.8.8.8")
                    if count < MIN_ROWS["ipdeny"] or found is None or found.country != "US":
                        raise SourceError(
                            "bad_data", "The IPdeny files did not look like IPdeny data."
                        )
                    os.replace(table, directory / local.IPDENY_FILE)
                finally:
                    table.unlink(missing_ok=True)

            await asyncio.to_thread(build_table)
        out.changed = True
        out.stamp = version

    async def _ip2region(
        self,
        directory: Path,
        client: httpx.AsyncClient,
        stamp: str | None,
        force: bool,
        out: Outcome,
    ) -> None:
        known = _stamps(stamp)
        stamps: dict[str, str] = {}
        for version, probe in ((4, "8.8.8.8"), (6, "2001:4860:4860::8888")):
            name = local.XDB_FILES[version]
            url = f"{IP2REGION}/{name}"
            head = await _head(client, url, "ip2region") or "unknown"
            stamps[name] = head
            if (
                not force
                and known.get(name) == head
                and await asyncio.to_thread((directory / name).exists)
            ):
                continue
            with _scratch(directory) as part:
                await _download(client, url, part, MAX_BYTES["xdb"], name="ip2region")

                def check(
                    part: Path = part, version: int = version, probe: str = probe, name: str = name
                ) -> None:
                    with part.open("rb") as handle:
                        data = handle.read()
                    header = read_header(data)
                    if header.version != version or search(data, header, probe) is None:
                        raise SourceError(
                            "bad_data", f"{name} is not an ip2region IPv{version} file."
                        )

                await asyncio.to_thread(check)
                os.replace(part, directory / name)
            out.changed = True
            out.stamp = json.dumps({**known, **stamps})
        out.stamp = json.dumps(stamps)


# ---- helpers -------------------------------------------------------------------------------------------------------------


def _stamps(stamp: str | None) -> dict[str, str]:
    """A source with more than one file keeps one stamp per file, as JSON."""
    try:
        known = json.loads(stamp) if stamp else {}
    except ValueError:
        return {}
    return {k: v for k, v in known.items() if isinstance(v, str)} if isinstance(known, dict) else {}


def _modified(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


@contextlib.contextmanager
def _scratch(directory: Path) -> Iterator[Path]:
    """A temporary file in the data directory (so that it can be renamed into place), gone afterwards."""
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=directory, prefix=".download-", suffix=".part")
    os.close(handle)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _fail_for(status: int, name: str) -> SourceError:
    if status in (401, 403):
        return SourceError(
            "refused", f"{name} did not accept the key or token. Check it in your account there."
        )
    if status == 429:
        return SourceError(
            "rate_limited",
            f"{name}'s download limit has been reached for now. It will be tried again later.",
        )
    return SourceError("unavailable", f"{name} answered HTTP {status}.")


async def _head(client: httpx.AsyncClient, url: str, name: str) -> str | None:
    """What identifies the file's current version (its `ETag`, or `Last-Modified`, and size), or `None` if it is not there."""
    response = await client.head(url)
    if response.status_code in (404, 410):
        return None
    if response.status_code >= 400:
        raise _fail_for(response.status_code, name)
    mark = response.headers.get("etag") or response.headers.get("last-modified") or ""
    return f"{mark}:{response.headers.get('content-length', '')}"


async def _download(
    client: httpx.AsyncClient,
    url: str,
    target: Path,
    limit: int,
    *,
    name: str,
    params: dict[str, str] | None = None,
) -> str:
    """Download to `target`, never more than `limit` bytes, only over https. Returns the file's SHA-256."""
    digest = hashlib.sha256()
    async with client.stream("GET", url, params=params) as response:
        if response.status_code >= 400:
            raise _fail_for(response.status_code, name)
        if response.url.scheme != "https":
            raise SourceError("insecure", "The download was sent to a plain http address.")
        total = 0
        with target.open("wb") as out:
            async for chunk in response.aiter_bytes(1 << 20):
                total += len(chunk)
                if total > limit:
                    raise SourceError(
                        "too_large", "The file is larger than any file of this kind should be."
                    )
                digest.update(chunk)
                out.write(chunk)
    return digest.hexdigest()


async def _ipinfo_checksum(client: httpx.AsyncClient, token: str) -> str | None:
    """The SHA-256 IPinfo publishes for its Lite database, or `None` if it does not say. Raises if the token is refused."""
    response = await client.get(f"{IPINFO}/checksums", params={"token": token})
    if response.status_code in (401, 403, 429):
        raise _fail_for(response.status_code, "IPinfo")
    if response.status_code != 200:
        return None
    try:
        value = response.json()["checksums"]["sha256"]
    except (ValueError, KeyError, TypeError):
        return None
    return value.lower() if isinstance(value, str) and len(value) == 64 else None


def _expect_zip(path: Path, name: str, token: str) -> None:
    """IP2Location answers a refused download with a text (and status 200): say what it said, without the token."""
    with path.open("rb") as handle:
        head = handle.read(200)
    if head[:4] == b"PK\x03\x04":
        return
    said = (
        "".join(c for c in head.decode("ascii", "replace") if c.isprintable())
        .replace(token, "***")
        .strip()[:80]
    )
    raise SourceError(
        "refused",
        f"{name} did not give the file{f' ({said})' if said else ''}. Check the download token on its Download page.",
    )


def _check_mmdb(path: Path, probe: str, must_have: str) -> None:
    """The file must open as an `.mmdb` database and have, for a well known address, a record with the key a database of this kind has."""
    try:
        with maxminddb.open_database(str(path)) as reader:
            record = reader.get(probe)
    except (OSError, ValueError, maxminddb.InvalidDatabaseError):
        raise SourceError("bad_data", "The file is not a readable database.") from None
    if not isinstance(record, dict) or must_have not in record:
        raise SourceError("bad_data", "The file does not look like the database it should be.")


def _install_mmdb(part: Path, final: Path, probe: str, must_have: str) -> None:
    _check_mmdb(part, probe, must_have)
    os.replace(part, final)


def _csv_pass(archive: Path, family: int, spans: Callable[..., Iterator[Span]]) -> Any:
    def read(table: PlaceTable) -> Iterator[Span]:
        with csv_lines(archive) as lines:
            yield from spans(lines, table, family)

    return read


def _build_places(directory: Path, name: str, passes: Any, source: str, kind: str) -> None:
    """Build a place table from downloaded CSV files, check it, and put it in place (blocking: runs in a thread)."""
    table = directory / f".{name}.part"
    try:
        count = build_places(table, passes, {"built_at": _iso(_now()) or "", "source": source})
        reader = PlaceDb(table)
        try:
            found = reader.find("8.8.8.8")
        finally:
            reader.close()
        if count < MIN_ROWS[kind] or found is None or found.country != "US":
            raise SourceError(
                "bad_data", f"The {source} files did not look like its geolocation data."
            )
        os.replace(table, directory / name)
    finally:
        table.unlink(missing_ok=True)


def _has_dbip(directory: Path, kind: str) -> bool:
    return any(directory.glob(f"dbip-{kind}-lite-*.mmdb"))


def _install_dbip(part: Path, directory: Path, name: str, kind: str) -> None:
    """Unpack a downloaded DB-IP file, check it, put it in place and remove the older months' files of that kind."""
    unpacked = part.with_suffix(".mmdb")
    try:
        _gunzip(part, unpacked, MAX_BYTES["mmdb"])
        check_database(unpacked, kind)
        os.replace(unpacked, directory / name)
    finally:
        unpacked.unlink(missing_ok=True)
    for old in directory.glob(f"dbip-{kind}-lite-*.mmdb"):
        if old.name != name:
            old.unlink(missing_ok=True)


def _gunzip(source: Path, target: Path, limit: int) -> None:
    written = 0
    with gzip.open(source, "rb") as src, target.open("wb") as out:
        while chunk := src.read(1 << 20):
            written += len(chunk)
            if written > limit:
                raise SourceError(
                    "too_large", "The file is larger than any file of this kind should be."
                )
            out.write(chunk)
