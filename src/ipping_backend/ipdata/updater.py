"""Keeping the MaxMind databases up to date, in the background.

Every backend process runs this loop; they cooperate through the GeoIP directory, which holds the databases and two small
files: a lock (the process that has it downloads, the others see its result) and the status (what was downloaded when,
and how the last attempt went), so the admin page shows the same thing whichever process answers.

A new check is due `maxmind_update_hours` after the last attempt (an hour after a failure, six after a refused key or the
daily download limit). An edition is downloaded only when MaxMind has published something newer than what is here (a HEAD
request, which is free); an update that is asked for by hand skips that question.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import __version__
from ..config import Settings
from ..geolocation.geoip import GeoIp
from .maxmind import MaxMind, MaxMindError
from .options import Credentials, IpDataOptions, load_credentials, load_options

log = logging.getLogger(__name__)

STATUS_FILE = "maxmind-status.json"
LOCK_FILE = ".maxmind.lock"
TICK_S = 300.0
FIRST_TICK_S = 20.0
STALE_RUNNING = timedelta(minutes=20)


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
    empty: dict[str, Any] = {
        "running": False,
        "running_since": None,
        "last_attempt": None,
        "last_success": None,
        "error": None,
        "editions": {},
    }
    if directory is None:
        return empty
    try:
        data = json.loads((directory / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    result = {**empty, **{k: v for k, v in data.items() if k in empty}}
    since = _parse(result["running_since"])
    if result["running"] and (since is None or _now() - since > STALE_RUNNING):
        result["running"] = False  # a process that died mid-update
    return result


def _write_status(directory: Path, status: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=directory, prefix=".status-", suffix=".part")
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


def due(options: IpDataOptions, status: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether it is time to ask MaxMind again."""
    now = now or _now()
    last = _parse(status.get("last_attempt"))
    if last is None:
        return True
    error = status.get("error")
    if error:
        wait = (
            timedelta(hours=6)
            if error.get("code") in ("refused", "rate_limited")
            else timedelta(hours=1)
        )
    else:
        wait = timedelta(hours=options.maxmind_update_hours)
    return now - last >= wait


class MaxMindUpdater:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        geoip: GeoIp,
        *,
        on_updated: Callable[[], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._geoip = geoip
        self._on_updated = on_updated
        self._transport = transport
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def directory(self) -> Path | None:
        return Path(self._settings.geoip_dir) if self._settings.geoip_dir else None

    def status(self) -> dict[str, Any]:
        status = read_status(self.directory)
        if self._lock.locked():
            status["running"] = True
        return status

    async def run(self) -> None:
        await asyncio.sleep(FIRST_TICK_S)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("the MaxMind update check failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        if self.directory is None:
            return
        async with self._sessionmaker() as db:
            options = await load_options(db)
            credentials = await load_credentials(db, self._settings)
        if (
            options.maxmind_auto_update
            and credentials.usable
            and due(options, read_status(self.directory))
        ):
            await self.update(options, credentials)

    def start(self, *, force: bool = True) -> bool:
        """Start an update in the background (the admin page's "Update now"); False if one is already going."""
        if self._lock.locked() or (self._task is not None and not self._task.done()):
            return False
        self._task = asyncio.create_task(self._run_once(force), name="maxmind-update")
        return True

    async def _run_once(self, force: bool) -> None:
        try:
            async with self._sessionmaker() as db:
                options = await load_options(db)
                credentials = await load_credentials(db, self._settings)
            if credentials.usable:
                await self.update(options, credentials, force=force)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the MaxMind update failed")

    async def update(
        self, options: IpDataOptions, credentials: Credentials, *, force: bool = False
    ) -> dict[str, Any]:
        directory = self.directory
        if directory is None or not credentials.usable:
            return self.status()
        async with self._lock:
            fd = await asyncio.to_thread(_try_lock, directory)
            if fd is None:
                return (
                    self.status()
                )  # another process is on it: its result lands in the status file
            try:
                return await self._update_locked(directory, options, credentials, force)
            finally:
                await asyncio.to_thread(_unlock, fd)

    async def _update_locked(
        self, directory: Path, options: IpDataOptions, credentials: Credentials, force: bool
    ) -> dict[str, Any]:
        status = await asyncio.to_thread(read_status, directory)
        status.update(
            running=True, running_since=_iso(_now()), last_attempt=_iso(_now()), error=None
        )
        await asyncio.to_thread(_write_status, directory, status)
        installed_any = False
        failure: MaxMindError | None = None
        maxmind = MaxMind(
            credentials.account_id,
            credentials.license_key,
            base_url=self._settings.maxmind_base_url,
        )
        headers = {"User-Agent": f"ipping-backend/{__version__}"}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=180.0), headers=headers, transport=self._transport
        ) as client:
            for edition in options.maxmind_editions:
                entry = dict(status["editions"].get(edition, {}))
                try:
                    if not force and not await self._newer_available(
                        maxmind, edition, entry, directory, client
                    ):
                        entry.update(checked_at=_iso(_now()), error=None)
                    else:
                        installed = await maxmind.install(edition, directory, client)
                        installed_any = True
                        entry = {
                            "downloaded_at": _iso(_now()),
                            "checked_at": _iso(_now()),
                            "build_epoch": installed.build_epoch,
                            "size": installed.size,
                            "error": None,
                        }
                except MaxMindError as exc:
                    entry.update(checked_at=_iso(_now()), error=exc.message)
                    failure = failure or exc
                    log.warning("MaxMind %s: %s", edition, exc.message)
                    if exc.code in ("refused", "rate_limited"):
                        status["editions"][edition] = entry
                        break  # the others would be refused the same way
                status["editions"][edition] = entry
        status.update(running=False, running_since=None)
        if failure is not None:
            status["error"] = {"code": failure.code, "message": failure.message}
        else:
            status["last_success"] = _iso(_now())
        await asyncio.to_thread(_write_status, directory, status)
        if installed_any:
            self._geoip.rescan()
            if self._on_updated is not None:
                try:
                    await self._on_updated()
                except Exception:
                    log.exception("refreshing what is built on the databases failed")
        return status

    @staticmethod
    async def _newer_available(
        maxmind: MaxMind,
        edition: str,
        entry: dict[str, Any],
        directory: Path,
        client: httpx.AsyncClient,
    ) -> bool:
        have = _parse(entry.get("downloaded_at"))
        exists = await asyncio.to_thread((directory / f"{edition}.mmdb").exists)
        if have is None or not exists:
            return True
        published = await maxmind.published(edition, client)
        return published is None or published > have
