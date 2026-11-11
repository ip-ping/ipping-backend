"""Keeping the databases that need no account up to date: DB-IP Lite, IPtoASN, IPdeny and ip2region.

Like the MaxMind updater, every backend process runs this loop and they cooperate through the data directory: a lock (the process
that has it downloads) and a status file (what was fetched when, and how it went), so the admin page shows the same
thing whichever process answers.

A source is asked whether it has something newer with a HEAD request (its `ETag` or `Last-Modified` and size are the
version) and downloaded only when it has. What is downloaded is checked before it is used (it must open, be the right kind
of file, and answer for a well known address) and goes into place atomically, so a process reading the directory never sees half a
file. Nothing is downloaded while `offline` is on, or for a source that is turned off.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import gzip
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import __version__
from ..geolocation.geoip import GeoIp
from ..geolocation.update import check_database, months
from . import local
from .options import IpDataOptions, load_options
from .ranges import RangeDb, build, ipdeny_rows, iptoasn_rows
from .xdb import XdbError, read_header, search

log = logging.getLogger(__name__)

STATUS_FILE = "ipdata-status.json"
LOCK_FILE = ".ipdata.lock"
SOURCES = ("dbip", "iptoasn", "ipdeny", "ip2region")
TICK_S = 300.0
FIRST_TICK_S = 25.0
STALE_RUNNING = timedelta(minutes=30)

DBIP = "https://download.db-ip.com/free"
IPTOASN = "https://iptoasn.com/data/ip2asn-combined.tsv.gz"
IPDENY_V4 = "https://www.ipdeny.com/ipblocks/data/countries/all-zones.tar.gz"
IPDENY_V6 = "https://www.ipdeny.com/ipv6/ipaddresses/blocks/ipv6-all-zones.tar.gz"
IP2REGION = "https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data"
# A table with fewer ranges than this cannot be the real data (the tests lower them)
MIN_ROWS = {"iptoasn": 100_000, "ipdeny": 1000}
MAX_BYTES = {"mmdb": 1_500_000_000, "tsv": 200_000_000, "zones": 60_000_000, "xdb": 200_000_000}


class SourceError(Exception):
    """A download that did not work. `message` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(when: datetime | None) -> str | None:
    return when.isoformat(timespec="seconds") if when else None


def _parse(value: Any) -> datetime | None:
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
        since = _parse(entry.get("running_since")) if isinstance(entry, dict) else None
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


def due(options: IpDataOptions, entry: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether it is time to ask a source again: never asked, an hour after a failure, else `data_update_hours`."""
    now = now or _now()
    last = _parse(entry.get("last_attempt"))
    if last is None:
        return True
    wait = timedelta(hours=1) if entry.get("error") else timedelta(hours=options.data_update_hours)
    return now - last >= wait


def next_check(options: IpDataOptions, entry: dict[str, Any]) -> datetime | None:
    last = _parse(entry.get("last_attempt"))
    if last is None:
        return None
    return last + (
        timedelta(hours=1) if entry.get("error") else timedelta(hours=options.data_update_hours)
    )


class DataUpdater:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        directory: Path | None,
        geoip_of: Callable[[], GeoIp],
        *,
        offline: bool = False,
        enabled: Callable[[str], bool] = lambda source: True,
        on_updated: Callable[[], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._sessionmaker = sessionmaker
        self.directory = directory
        self._geoip_of = geoip_of
        self.offline = offline
        self._enabled = enabled
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
        async with self._sessionmaker() as db:
            options = await load_options(db)
        status = read_status(self.directory)
        wanted = [
            s for s in SOURCES if options.enabled(s) and due(options, status["sources"].get(s, {}))
        ]
        if wanted:
            await self.update(wanted)

    def start(self, *, force: bool = True) -> bool:
        """Update every source that is on, in the background (the admin page's "Update now"). False if one is going."""
        if self.running():
            return False
        self._task = asyncio.create_task(self._run_once(force), name="ipdata-update")
        return True

    async def _run_once(self, force: bool) -> None:
        try:
            async with self._sessionmaker() as db:
                options = await load_options(db)
            await self.update([s for s in SOURCES if options.enabled(s)], force=force)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the IP data update failed")

    async def update(self, sources: list[str], *, force: bool = False) -> dict[str, Any]:
        directory = self.directory
        if directory is None or self.offline or not sources:
            return self.status()
        async with self._lock:
            fd = await asyncio.to_thread(_try_lock, directory)
            if fd is None:
                return (
                    self.status()
                )  # another process is on it: its result lands in the status file
            try:
                return await self._update_locked(directory, sources, force)
            finally:
                await asyncio.to_thread(_unlock, fd)

    async def _update_locked(
        self, directory: Path, sources: list[str], force: bool
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
                try:
                    changed, stamp = await self._one(
                        source, directory, client, entry.get("stamp"), force
                    )
                    installed_any = installed_any or changed
                    entry.update(error=None, last_success=_iso(_now()), stamp=stamp)
                    if changed:
                        entry["downloaded_at"] = _iso(_now())
                except SourceError as exc:
                    entry["error"] = {"code": exc.code, "message": exc.message}
                    log.warning("IP data %s: %s", source, exc.message)
                except (httpx.HTTPError, OSError, ValueError, XdbError) as exc:
                    entry["error"] = {
                        "code": "failed",
                        "message": f"The update failed ({type(exc).__name__}).",
                    }
                    log.warning("IP data %s: %s", source, type(exc).__name__)
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
    ) -> tuple[bool, str]:
        match source:
            case "dbip":
                return await self._dbip(directory, client, stamp, force)
            case "iptoasn":
                return await self._iptoasn(directory, client, stamp, force)
            case "ipdeny":
                return await self._ipdeny(directory, client, stamp, force)
            case "ip2region":
                return await self._ip2region(directory, client, stamp, force)
        raise SourceError("unknown", "Not a source that is downloaded.")

    async def _dbip(
        self, directory: Path, client: httpx.AsyncClient, stamp: str | None, force: bool
    ) -> tuple[bool, str]:
        changed = False
        stamps: dict[str, str] = {}
        try:
            known = json.loads(stamp) if stamp else {}
        except ValueError:
            known = {}
        for kind in ("city", "asn"):
            found: tuple[str, str] | None = None
            for month in months(self._today()):
                name = f"dbip-{kind}-lite-{month}.mmdb"
                version = await _head(client, f"{DBIP}/{name}.gz")
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
                await _download(client, f"{DBIP}/{name}.gz", part, MAX_BYTES["mmdb"])
                await asyncio.to_thread(_install_dbip, part, directory, name, kind)
            changed = True
        return changed, json.dumps(stamps)

    async def _iptoasn(
        self, directory: Path, client: httpx.AsyncClient, stamp: str | None, force: bool
    ) -> tuple[bool, str]:
        version = await _head(client, IPTOASN) or "unknown"
        if (
            not force
            and stamp == version
            and await asyncio.to_thread((directory / local.IPTOASN_FILE).exists)
        ):
            return False, version
        with _scratch(directory) as part:
            await _download(client, IPTOASN, part, MAX_BYTES["tsv"])

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
        return True, version

    async def _ipdeny(
        self, directory: Path, client: httpx.AsyncClient, stamp: str | None, force: bool
    ) -> tuple[bool, str]:
        versions = [await _head(client, IPDENY_V4) or "", await _head(client, IPDENY_V6) or ""]
        version = "|".join(versions)
        if (
            not force
            and stamp == version
            and await asyncio.to_thread((directory / local.IPDENY_FILE).exists)
        ):
            return False, version
        with _scratch(directory) as first, _scratch(directory) as second:
            await _download(client, IPDENY_V4, first, MAX_BYTES["zones"])
            await _download(client, IPDENY_V6, second, MAX_BYTES["zones"])

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
        return True, version

    async def _ip2region(
        self, directory: Path, client: httpx.AsyncClient, stamp: str | None, force: bool
    ) -> tuple[bool, str]:
        try:
            known = json.loads(stamp) if stamp else {}
        except ValueError:
            known = {}
        stamps: dict[str, str] = {}
        changed = False
        for version, probe in ((4, "8.8.8.8"), (6, "2001:4860:4860::8888")):
            name = local.XDB_FILES[version]
            url = f"{IP2REGION}/{name}"
            head = await _head(client, url) or "unknown"
            stamps[name] = head
            if (
                not force
                and known.get(name) == head
                and await asyncio.to_thread((directory / name).exists)
            ):
                continue
            with _scratch(directory) as part:
                await _download(client, url, part, MAX_BYTES["xdb"])

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
            changed = True
        return changed, json.dumps(stamps)


# ---- helpers -------------------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _scratch(directory: Path):  # type: ignore[no-untyped-def]
    """A temporary file in the data directory (so that it can be renamed into place), gone afterwards."""
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=directory, prefix=".download-", suffix=".part")
    os.close(handle)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


async def _head(client: httpx.AsyncClient, url: str) -> str | None:
    """What identifies the file's current version (its `ETag`, or `Last-Modified`, and size), or `None` if it is not there."""
    response = await client.head(url)
    if response.status_code in (404, 410):
        return None
    if response.status_code >= 400:
        raise SourceError("unavailable", f"The server answered HTTP {response.status_code}.")
    mark = response.headers.get("etag") or response.headers.get("last-modified") or ""
    return f"{mark}:{response.headers.get('content-length', '')}"


async def _download(client: httpx.AsyncClient, url: str, target: Path, limit: int) -> None:
    async with client.stream("GET", url) as response:
        if response.status_code >= 400:
            raise SourceError("unavailable", f"The server answered HTTP {response.status_code}.")
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
                out.write(chunk)


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
