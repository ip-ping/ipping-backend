"""The nine IP data sources: what they are, what they need, and what has to be said about them.

Every source is a database that is downloaded and kept on disk, and answered from there: nothing is asked of a service when an
address is looked up. Everything about a source that does not change while the server runs lives here: its name, the
credentials it takes (an account's key, where its free download needs one), what it knows, and the attribution its licence asks
for.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Credential:
    name: str
    label: str
    secret: bool = True
    required: bool = True


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    homepage: str
    summary: str  # what it knows
    cost: str  # what it needs from the administrator and how big it is, in a sentence
    provides: frozenset[
        str
    ]  # of: country region city position postal_code time_zone asn organization
    credentials: tuple[Credential, ...] = ()
    attribution: str | None = None  # what its licence asks to be shown with the data
    min_hours: int = 0  # the least time between two downloads, where the source asks for less often than the administrator's setting


GEO = frozenset({"country", "region", "city", "position", "postal_code", "time_zone"})
NETWORK = frozenset({"asn", "organization"})

CATALOG: tuple[SourceInfo, ...] = (
    SourceInfo(
        id="maxmind",
        name="MaxMind GeoLite2",
        homepage="https://www.maxmind.com/en/geolite2/signup",
        summary="Country, region, city, position, postal code, time zone, AS number and organization.",
        cost="Free with a MaxMind account: its account ID and a license key. The City and ASN databases, renewed on Tuesdays and Fridays.",
        provides=GEO | NETWORK,
        credentials=(
            Credential("account_id", "Account ID", secret=False),
            Credential("license_key", "License key"),
        ),
        attribution="This product includes GeoLite2 data created by MaxMind (maxmind.com)",
    ),
    SourceInfo(
        id="dbip",
        name="DB-IP Lite",
        homepage="https://db-ip.com/db/lite.php",
        summary="Country, region, city, position, AS number and organization.",
        cost="Free, no account. The city and AS databases, about 130 MB, renewed once a month.",
        provides=frozenset({"country", "region", "city", "position"}) | NETWORK,
        attribution="IP geolocation by DB-IP (db-ip.com), licensed CC BY 4.0",
    ),
    SourceInfo(
        id="ipinfo",
        name="IPinfo Lite",
        homepage="https://ipinfo.io/signup",
        summary="Country, AS number and organization.",
        cost="Free with an IPinfo account: its access token. One database of about 26 MB, renewed every day (10 downloads a day are allowed).",
        provides=frozenset({"country"}) | NETWORK,
        credentials=(Credential("token", "Access token"),),
        attribution="IP address data powered by IPinfo (ipinfo.io)",
    ),
    SourceInfo(
        id="iplocate",
        name="IPLocate",
        homepage="https://www.iplocate.io/free-databases",
        summary="Country, AS number and organization.",
        cost="Free, no account. The country and ASN databases, about 90 MB, renewed every day.",
        provides=frozenset({"country"}) | NETWORK,
        attribution="IP address data powered by IPLocate.io (iplocate.io), licensed CC BY-SA 4.0",
    ),
    SourceInfo(
        id="ipapi_is",
        name="ipapi.is",
        homepage="https://ipapi.is/geolocation.html",
        summary="Country, region, city, position, postal code and time zone.",
        cost="Free, no account. Two files of 75 MB, built into a table of about 110 MB. Renewed every day, asked for once a week.",
        provides=GEO,
        attribution="IP geolocation data by ipapi.is (ipapi.is), licensed CC BY-SA 4.0",
        min_hours=168,
    ),
    SourceInfo(
        id="ip2location",
        name="IP2Location LITE",
        homepage="https://lite.ip2location.com/sign-up",
        summary="Country, region, city, position and postal code.",
        cost="Free with an IP2Location LITE account: its download token (on its Download page). Two zip files (the IPv4 one is 330 MB unpacked), built into one table. Renewed once a month, and downloaded once a month, as IP2Location asks.",
        provides=frozenset({"country", "region", "city", "position", "postal_code"}),
        credentials=(Credential("download_token", "Download token"),),
        attribution="This site or product includes IP2Location LITE data available from https://lite.ip2location.com.",
        min_hours=168,
    ),
    SourceInfo(
        id="iptoasn",
        name="IPtoASN",
        homepage="https://iptoasn.com/",
        summary="AS number, organization and country, from routing data.",
        cost="Free, no account, public domain. One file of about 11 MB, renewed every hour.",
        provides=frozenset({"country"}) | NETWORK,
    ),
    SourceInfo(
        id="ipdeny",
        name="IPdeny",
        homepage="https://www.ipdeny.com/ipblocks/",
        summary="Country, from the regional registries' allocations.",
        cost="Free, no account. Two small files, renewed every day.",
        provides=frozenset({"country"}),
        attribution="IP country data by IPdeny (ipdeny.com)",
    ),
    SourceInfo(
        id="ip2region",
        name="ip2region",
        homepage="https://github.com/lionsoul2014/ip2region",
        summary="Country, region and city; strongest for addresses in China.",
        cost="Free, no account, Apache-2.0. Two files of about 50 MB together.",
        provides=frozenset({"country", "region", "city"}),
        attribution="ip2region (github.com/lionsoul2014/ip2region), Apache-2.0",
    ),
)

BY_ID: dict[str, SourceInfo] = {info.id: info for info in CATALOG}
IDS: tuple[str, ...] = tuple(info.id for info in CATALOG)


def fetchable(source: str, keys: dict[str, dict[str, str]]) -> bool:
    """Whether a source has what it needs to be downloaded: every credential it requires."""
    have = keys.get(source, {})
    return all(have.get(c.name) for c in BY_ID[source].credentials if c.required)
