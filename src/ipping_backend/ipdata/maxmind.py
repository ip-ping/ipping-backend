"""Downloading a MaxMind GeoLite2 database, checking it, and putting it in place.

The current download service (https://dev.maxmind.com/geoip/updating-databases/): `GET /geoip/databases/<edition>/download
?suffix=tar.gz` with the account ID and the license key as HTTP Basic credentials, answered with a redirect to a signed
address on MaxMind's storage; `suffix=tar.gz.sha256` is the archive's checksum. A GeoLite2 account may download 30 times
a day, and a HEAD request is free, so freshness is asked with a HEAD first.

What is done with the bytes:

* the credentials go only to MaxMind (never along the redirect), and never into a message or a log line;
* the archive's SHA-256 must match the one MaxMind publishes, or nothing is installed;
* only the `<edition>.mmdb` file is taken out of the archive, by name (an archive cannot write anywhere else), and no
  more than `MAX_DATABASE_BYTES` of it;
* the file must open as a MaxMind database of the right kind and answer a lookup before it replaces the old one, and
  the replacement is atomic: a server reading the directory never sees half a file.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
import maxminddb

MAX_ARCHIVE_BYTES = 400 * 1024 * 1024
MAX_DATABASE_BYTES = 800 * 1024 * 1024
_KIND = {"GeoLite2-City": "city", "GeoLite2-Country": "country", "GeoLite2-ASN": "asn"}
_SHA256 = re.compile(r"\b[0-9a-fA-F]{64}\b")


class MaxMindError(Exception):
    """A download that did not work. `message` is safe to show: it never holds the credentials."""

    def __init__(self, code: str, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Installed:
    edition: str
    path: Path
    build_epoch: int
    size: int


def _fail_for(response: httpx.Response, edition: str) -> MaxMindError:
    status = response.status_code
    if status in (401, 403):
        return MaxMindError(
            "refused",
            "MaxMind refused the account ID and license key. Check them in your MaxMind account "
            "(the account ID is a number; a license key is made under Manage license keys).",
        )
    if status == 404:
        return MaxMindError("not_found", f"MaxMind has no {edition} for this account.")
    if status == 429:
        return MaxMindError(
            "rate_limited",
            "MaxMind's daily download limit for this account has been reached (30 a day for GeoLite2).",
            retry_after_s=6 * 3600,
        )
    return MaxMindError("unavailable", f"MaxMind answered {status}. It will be tried again.")


class MaxMind:
    def __init__(
        self,
        account_id: str,
        license_key: str,
        *,
        base_url: str = "https://download.maxmind.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ValueError("MaxMind is only reached over https")
        self._auth = (account_id, license_key)
        self._base = base_url.rstrip("/")
        self._client = client
        self._secret = license_key

    def _url(self, edition: str) -> str:
        return f"{self._base}/geoip/databases/{edition}/download"

    def _scrub(self, text: str) -> str:
        return text.replace(self._secret, "***") if self._secret else text

    async def _request(
        self, client: httpx.AsyncClient, method: str, edition: str, suffix: str
    ) -> httpx.Response:
        try:
            return await client.request(
                method,
                self._url(edition),
                params={"suffix": suffix},
                auth=self._auth,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise MaxMindError(
                "network", f"Could not reach MaxMind: {self._scrub(type(exc).__name__)}."
            ) from None

    async def published(self, edition: str, client: httpx.AsyncClient) -> datetime | None:
        """When MaxMind last published this edition (a free HEAD request), or None if it does not say."""
        try:
            response = await self._request(client, "HEAD", edition, "tar.gz")
        except MaxMindError:
            return None
        if response.status_code in (401, 403, 429):
            raise _fail_for(response, edition)
        stamp = response.headers.get("last-modified")
        if response.status_code != 200 or not stamp:
            return None
        try:
            when = parsedate_to_datetime(stamp)
        except (TypeError, ValueError):
            return None
        return when if when.tzinfo else when.replace(tzinfo=UTC)

    async def install(
        self,
        edition: str,
        directory: Path,
        client: httpx.AsyncClient,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> Installed:
        """Download `edition`, check it, and replace `<directory>/<edition>.mmdb` with it."""
        if edition not in _KIND:
            raise ValueError(f"unknown edition {edition}")
        scratch = await asyncio.to_thread(_scratch, directory)
        try:
            archive = scratch / "archive.tar.gz"
            digest = await self._download(client, edition, archive)
            await self._verify(client, edition, digest)
            if on_progress:
                on_progress("checking")
            return await asyncio.to_thread(_finish, archive, edition, directory, scratch)
        finally:
            await asyncio.to_thread(shutil.rmtree, scratch, True)

    async def _download(self, client: httpx.AsyncClient, edition: str, dest: Path) -> str:
        digest = hashlib.sha256()
        size = 0
        try:
            async with client.stream(
                "GET",
                self._url(edition),
                params={"suffix": "tar.gz"},
                auth=self._auth,
                follow_redirects=True,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise _fail_for(response, edition)
                with dest.open("wb") as out:
                    async for chunk in response.aiter_bytes(1 << 20):
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise MaxMindError(
                                "too_large",
                                "The download is larger than a GeoLite2 database can be.",
                            )
                        digest.update(chunk)
                        out.write(chunk)
        except httpx.HTTPError as exc:
            raise MaxMindError(
                "network",
                f"The download from MaxMind broke off: {self._scrub(type(exc).__name__)}.",
            ) from None
        return digest.hexdigest()

    async def _verify(self, client: httpx.AsyncClient, edition: str, digest: str) -> None:
        response = await self._request(client, "GET", edition, "tar.gz.sha256")
        if response.status_code != 200:
            raise MaxMindError(
                "checksum_unavailable",
                "MaxMind's checksum for the download could not be fetched, so the download was not installed.",
            )
        published = _SHA256.search(response.text)
        if published is None or published.group(0).lower() != digest:
            raise MaxMindError(
                "checksum_mismatch",
                "The download does not match MaxMind's checksum, so it was not installed.",
            )


def _scratch(directory: Path) -> Path:
    """A working directory inside `directory`, so that the final move is a rename on one filesystem."""
    directory.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=directory, prefix=".maxmind-"))


def _finish(archive: Path, edition: str, directory: Path, scratch: Path) -> Installed:
    """Unpack, check and put in place (blocking: runs in a thread)."""
    extracted = scratch / f"{edition}.mmdb"
    _extract(archive, f"{edition}.mmdb", extracted)
    build = _validate(extracted, edition)
    final = directory / f"{edition}.mmdb"
    part = directory / f".{edition}.mmdb.part"
    os.replace(extracted, part)  # one filesystem: atomic
    os.replace(part, final)
    return Installed(edition, final, build, final.stat().st_size)


def _extract(archive: Path, member_name: str, dest: Path) -> None:
    """Take `member_name` (a file anywhere in the archive, matched by its base name) out to `dest`."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            found = [m for m in tar if m.isfile() and Path(m.name).name == member_name]
            if len(found) != 1:
                raise MaxMindError(
                    "bad_archive", f"The archive does not hold exactly one {member_name}."
                )
            source = tar.extractfile(found[0])
            if source is None or found[0].size > MAX_DATABASE_BYTES:
                raise MaxMindError("bad_archive", f"{member_name} in the archive is not usable.")
            with source, dest.open("wb") as out:
                copied = 0
                while chunk := source.read(1 << 20):
                    copied += len(chunk)
                    if copied > MAX_DATABASE_BYTES:
                        raise MaxMindError(
                            "bad_archive", f"{member_name} in the archive is too large."
                        )
                    out.write(chunk)
    except (tarfile.TarError, OSError, EOFError) as exc:
        if isinstance(exc, MaxMindError):
            raise
        raise MaxMindError("bad_archive", "The download is not a readable archive.") from None


def _validate(path: Path, edition: str) -> int:
    """The database's build time, if the file is a MaxMind database of this edition that answers a lookup."""
    kind = _KIND[edition]
    try:
        with maxminddb.open_database(str(path)) as reader:
            meta = reader.metadata()
            if kind not in meta.database_type.lower():
                raise MaxMindError(
                    "bad_database", f"The file is a {meta.database_type} database, not {edition}."
                )
            if reader.get("8.8.8.8") is None:
                raise MaxMindError(
                    "bad_database", f"The {edition} database has no record for 8.8.8.8."
                )
            return int(meta.build_epoch)
    except (OSError, ValueError, maxminddb.InvalidDatabaseError):
        raise MaxMindError("bad_database", "The file is not a MaxMind database.") from None
