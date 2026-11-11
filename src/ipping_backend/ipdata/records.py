"""What one IP data source says about an address, in one shape, and the cleaning that gets it there.

Every source (a MaxMind-format file, a range table, an online service) answers in its own words: a country as a code, a
name or a Chinese name, an AS number as `15169`, `AS15169` or `AS15169 Google LLC`, a place that is `0` when unknown. `Record` is
what is left after that: only what the source really said, and nothing made up.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from .countries import ALIASES, NAMES

# What sources write when they do not know
_UNKNOWN = {
    "",
    "0",
    "-",
    "--",
    "none",
    "null",
    "n/a",
    "na",
    "unknown",
    "reserved",
    "undefined",
    "not available",
}
_AS_PREFIX = re.compile(r"^\s*AS\s*(\d{1,10})\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
_NUMBER = re.compile(r"^\s*(\d{1,10})\s*$")


@dataclass(slots=True)
class Record:
    """Everything a source said about one address. A field is `None` when the source said nothing about it."""

    country: str | None = None  # ISO 3166-1 alpha-2, upper case
    region: str | None = None
    region_code: str | None = (
        None  # a code for the region (`HE`), carried along with the region and never voted on
    )
    city: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy_radius_km: int | None = None
    time_zone: str | None = None  # an IANA name, e.g. Europe/Berlin
    asn: int | None = None
    organization: str | None = None
    network: str | None = None  # the CIDR the source's record covers, when it says

    def empty(self) -> bool:
        """True when it says nothing that can be voted on."""
        return not any(
            (
                self.country,
                self.region,
                self.city,
                self.postal_code,
                self.time_zone,
                self.asn,
                self.organization,
                self.latitude is not None and self.longitude is not None,
            )
        )

    def fields(self) -> dict[str, Any]:
        """The fields it has, for showing what a source said."""
        data = {k: v for k, v in asdict(self).items() if v is not None}
        if ("latitude" in data) != ("longitude" in data):  # half a position is no position
            data.pop("latitude", None)
            data.pop("longitude", None)
        data.pop("network", None)
        data.pop("region_code", None)
        return data


def text(value: Any, limit: int = 120) -> str | None:
    """A cleaned string, or `None` for the words sources use for "not known"."""
    if value is None or isinstance(value, bool):
        return None
    cleaned = " ".join(str(value).split())
    if cleaned.casefold() in _UNKNOWN:
        return None
    return cleaned[:limit]


def country(value: Any) -> str | None:
    """An ISO alpha-2 code from a code or a name (English; the usual forms of it)."""
    cleaned = text(value)
    if cleaned is None:
        return None
    if len(cleaned) == 2 and cleaned.isalpha():
        code = cleaned.upper()
        return code if code in NAMES or code == "XK" else None
    return ALIASES.get(cleaned.casefold())


def asn(value: Any) -> int | None:
    """An AS number from `15169`, `"15169"`, `"AS15169"` or `"AS15169 Google LLC"`. 0 and out-of-range are unknown."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    else:
        match = _AS_PREFIX.match(str(value)) or _NUMBER.match(str(value))
        if match is None:
            return None
        number = int(match.group(1))
    return number if 0 < number < 2**32 else None


def asn_and_name(value: Any) -> tuple[int | None, str | None]:
    """`"AS15169 Google LLC"` -> (15169, "Google LLC"); a string without a number is just a name."""
    if value is None:
        return None, None
    match = _AS_PREFIX.match(str(value))
    if match is None:
        return None, text(value)
    return asn(match.group(1)), text(match.group(2))


def number(value: Any, low: float, high: float) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if low <= parsed <= high else None


def position(latitude: Any, longitude: Any) -> tuple[float | None, float | None]:
    """Both or neither: a place with half a coordinate is none, and (0, 0) is the "null island" databases use for unknown."""
    lat, lon = number(latitude, -90, 90), number(longitude, -180, 180)
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None, None
    return round(lat, 4), round(lon, 4)


def time_zone(value: Any) -> str | None:
    """An IANA name (it has a slash: `Europe/Berlin`). Offsets such as `-08:00` cannot be compared, so they are dropped."""
    cleaned = text(value, 64)
    if cleaned is None or "/" not in cleaned or cleaned.startswith(("+", "-", "UTC")):
        return None
    return cleaned


# ---- Comparing -------------------------------------------------------------------------------------------------------

_ORGANIZATION_NOISE = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "ag", "sa", "sas", "srl", "bv", "nv", "plc",
    "oy", "ab", "as", "kk", "pty", "pte", "lp", "llp", "the", "of", "and", "ooo", "zao", "jsc", "ltda", "sl", "spa", "kg",
}  # fmt: skip


def fold(value: str) -> str:
    """Letters and digits only, lower case, accents and case removed: `São Paulo` and `Sao Paulo` are one place."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in stripped.casefold() if c.isalnum())


def organization_key(value: str) -> str:
    """The name of a network with its corporate suffixes gone: `Google LLC` and `GOOGLE` are one organization."""
    words = [w for w in re.split(r"[^\w]+", unicodedata.normalize("NFKD", value).casefold()) if w]
    kept = [w for w in words if w not in _ORGANIZATION_NOISE] or words
    return fold(" ".join(kept))
