"""The nine IP data sources: what they are, what they need, and what has to be said about them.

Everything about a source that does not change while the server runs lives here: its name, whether it is a database kept on disk
or a service asked over the Internet, the credentials it can take, and the attribution its licence asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["database", "online"]


@dataclass(frozen=True)
class Credential:
    name: str
    label: str
    secret: bool = True
    required: bool = False


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    kind: Kind
    homepage: str
    summary: str  # what it knows
    cost: str  # what it needs from the administrator, in a sentence
    credentials: tuple[Credential, ...] = ()
    attribution: str | None = None  # what its licence asks to be shown with the data


CATALOG: tuple[SourceInfo, ...] = (
    SourceInfo(
        id="maxmind",
        name="MaxMind GeoLite2",
        kind="database",
        homepage="https://www.maxmind.com/en/geolite2/signup",
        summary="Country, region, city, position, time zone, AS number and organization.",
        cost="Needs a free MaxMind account: its account ID and a license key (set below).",
        credentials=(
            Credential("account_id", "Account ID", secret=False, required=True),
            Credential("license_key", "License key", secret=True, required=True),
        ),
        attribution="This product includes GeoLite2 data created by MaxMind (maxmind.com)",
    ),
    SourceInfo(
        id="dbip",
        name="DB-IP Lite",
        kind="database",
        homepage="https://db-ip.com/db/lite.php",
        summary="Country, region, city, position, AS number and organization.",
        cost="Free, no account. The city and AS files (about 150 MB) are downloaded once a month.",
        attribution="IP geolocation by DB-IP (db-ip.com), licensed CC BY 4.0",
    ),
    SourceInfo(
        id="iptoasn",
        name="IPtoASN",
        kind="database",
        homepage="https://iptoasn.com/",
        summary="AS number, organization and country, from routing data.",
        cost="Free, no account, public domain. One file of about 11 MB, renewed upstream every hour.",
    ),
    SourceInfo(
        id="ipdeny",
        name="IPdeny",
        kind="database",
        homepage="https://www.ipdeny.com/ipblocks/",
        summary="Country, from the regional registries' allocations.",
        cost="Free, no account. Two small files, renewed upstream every day.",
        attribution="IP country data by IPdeny (ipdeny.com)",
    ),
    SourceInfo(
        id="ip2region",
        name="ip2region",
        kind="database",
        homepage="https://github.com/lionsoul2014/ip2region",
        summary="Country, region and city; strongest for addresses in China.",
        cost="Free, no account, Apache-2.0. Two files of about 50 MB together.",
        attribution="ip2region (github.com/lionsoul2014/ip2region), Apache-2.0",
    ),
    SourceInfo(
        id="ipinfo",
        name="IPinfo",
        kind="online",
        homepage="https://ipinfo.io/signup",
        summary="Country, region, city, position, postal code, time zone, AS number and organization.",
        cost="Works without a token at a low daily limit; a free token raises it.",
        credentials=(Credential("token", "Access token"),),
        attribution="IP data from IPinfo (ipinfo.io)",
    ),
    SourceInfo(
        id="iplocate",
        name="IPLocate",
        kind="online",
        homepage="https://iplocate.io/signup",
        summary="Country, region, city, position, postal code, time zone, AS number and organization.",
        cost="Works without a key at a low daily limit; a free key raises it.",
        credentials=(Credential("api_key", "API key"),),
        attribution="IP data from IPLocate (iplocate.io)",
    ),
    SourceInfo(
        id="ipapi_is",
        name="ipapi.is",
        kind="online",
        homepage="https://ipapi.is/",
        summary="Country, region, city, position, time zone, AS number and organization.",
        cost="Works without a key (about 1,000 lookups a day); a key raises it.",
        credentials=(Credential("api_key", "API key"),),
        attribution="IP data from ipapi.is",
    ),
    SourceInfo(
        id="ip2location",
        name="IP2Location",
        kind="online",
        homepage="https://www.ip2location.io/sign-up",
        summary="Country, region, city, position, postal code, AS number and organization.",
        cost="Works without a key (1,000 lookups a day); a free key gives 50,000 a month.",
        credentials=(Credential("api_key", "API key"),),
        attribution="IP data from IP2Location.io (ip2location.io)",
    ),
)

BY_ID: dict[str, SourceInfo] = {info.id: info for info in CATALOG}
IDS: tuple[str, ...] = tuple(info.id for info in CATALOG)
DATABASES: tuple[str, ...] = tuple(info.id for info in CATALOG if info.kind == "database")
ONLINE: tuple[str, ...] = tuple(info.id for info in CATALOG if info.kind == "online")
