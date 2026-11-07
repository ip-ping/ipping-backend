"""Download the free DB-IP Lite city and ASN databases into a directory.

DB-IP publishes a new file each month at a predictable address (CC BY 4.0: the result shows the
attribution it asks for). A file is checked before it replaces anything: it must unpack, open as a
MaxMind database of the right type and answer a lookup, and the swap is atomic, so a server reading the
directory never sees a half-written file.
"""

from __future__ import annotations

import gzip
import shutil
import tempfile
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import IO

import maxminddb

BASE = "https://download.db-ip.com/free"
KINDS = {"city": "City", "asn": "ASN"}

Opener = Callable[[str], IO[bytes]]


def _open(url: str) -> IO[bytes]:
    if not url.startswith("https://"):
        raise ValueError("only https addresses are fetched")
    request = urllib.request.Request(  # noqa: S310 - checked above
        url, headers={"User-Agent": "ipping-backend geoip-update"}
    )
    response: IO[bytes] = urllib.request.urlopen(request, timeout=60)  # noqa: S310
    return response


def months(today: date) -> list[str]:
    """This month, then last month (a new file appears a little into the month)."""
    previous = (
        date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)
    )
    return [f"{today.year}-{today.month:02d}", f"{previous.year}-{previous.month:02d}"]


def check_database(path: Path, kind: str) -> None:
    """Raise if `path` is not a usable database of this kind."""
    with maxminddb.open_database(str(path)) as reader:
        kind_name = reader.metadata().database_type
        if KINDS[kind].lower() not in kind_name.lower():
            raise ValueError(f"{path.name} is a {kind_name} database, not {KINDS[kind]}")
        # a database that cannot answer for a well known address is broken
        if reader.get("8.8.8.8") is None:
            raise ValueError(f"{path.name} has no record for 8.8.8.8")


def fetch(directory: Path, kind: str, today: date, opener: Opener = _open) -> Path:
    last_error: Exception | None = None
    for month in months(today):
        name = f"dbip-{kind}-lite-{month}.mmdb"
        try:
            source = opener(f"{BASE}/{name}.gz")
        except OSError as exc:  # not published yet, or no network
            last_error = exc
            continue
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as out:
            temporary = Path(out.name)
        try:
            with source, gzip.GzipFile(fileobj=source) as unpacked, temporary.open("wb") as target:
                shutil.copyfileobj(unpacked, target)
            check_database(temporary, kind)
            final = directory / name
            temporary.replace(final)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        for old in directory.glob(f"dbip-{kind}-lite-*.mmdb"):
            if old != final:
                old.unlink(missing_ok=True)
        return final
    raise RuntimeError(f"could not download the DB-IP {kind} database: {last_error}")


def update(directory: Path, today: date | None = None, opener: Opener = _open) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    today = today or date.today()
    return [fetch(directory, kind, today, opener) for kind in KINDS]
