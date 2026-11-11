"""The four online sources: one HTTPS request for an address, and what each service's answer means.

IPinfo, IPLocate, ipapi.is and IP2Location.io all answer a lookup without a key (at a low daily limit) and with one (at
a higher one), and the shape of the answer differs between the two for some of them; both are read. Their answers are
parsed into a `Record` with the same cleaning as every other source: a field that is not there, is `0` or is
`unknown` is left empty, never guessed.

A key is sent only to the service it belongs to, in the way that service asks (a header or a query parameter), and it is never
put into an error message or a log line: what is shown for a failure is a short sentence about what went wrong.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import records
from .records import Record

Request = tuple[str, dict[str, str], dict[str, str]]  # url, headers, query parameters


@dataclass(frozen=True)
class OnlineSource:
    id: str
    request: Callable[[str, Mapping[str, str]], Request]
    parse: Callable[[dict[str, Any]], Record]
    # the field of a JSON error body that says what is wrong, for the message
    refused: tuple[int, ...] = (401, 403)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _split_loc(value: Any) -> tuple[Any, Any]:
    """IPinfo's `"37.4056,-122.0775"`."""
    parts = str(value or "").split(",")
    return (parts[0], parts[1]) if len(parts) == 2 else (None, None)


# ---- IPinfo ----------------------------------------------------------------------------------------------------------


def _ipinfo_request(address: str, credentials: Mapping[str, str]) -> Request:
    params = {"token": credentials["token"]} if credentials.get("token") else {}
    return f"https://ipinfo.io/{address}/json", {"Accept": "application/json"}, params


def _ipinfo_parse(data: dict[str, Any]) -> Record:
    if data.get("bogon"):
        return Record()
    number, name = records.asn_and_name(data.get("org"))
    paid = _dict(data.get("asn"))  # the plans with an `asn` object
    number = records.asn(paid.get("asn")) or number
    name = records.text(paid.get("name")) or name
    lat, lon = records.position(*_split_loc(data.get("loc")))
    return Record(
        country=records.country(data.get("country")),
        region=records.text(data.get("region")),
        city=records.text(data.get("city")),
        postal_code=records.text(data.get("postal"), 20),
        latitude=lat,
        longitude=lon,
        time_zone=records.time_zone(data.get("timezone")),
        asn=number,
        organization=name,
        network=records.text(paid.get("route"), 64),
    )


# ---- IPLocate --------------------------------------------------------------------------------------------------------


def _iplocate_request(address: str, credentials: Mapping[str, str]) -> Request:
    headers = {"Accept": "application/json"}
    if credentials.get("api_key"):
        headers["X-API-Key"] = credentials["api_key"]
    return f"https://iplocate.io/api/lookup/{address}", headers, {}


def _iplocate_parse(data: dict[str, Any]) -> Record:
    system = _dict(data.get("asn"))
    company = _dict(data.get("company"))
    lat, lon = records.position(data.get("latitude"), data.get("longitude"))
    return Record(
        country=records.country(data.get("country_code")) or records.country(data.get("country")),
        region=records.text(data.get("subdivision")),
        city=records.text(data.get("city")),
        postal_code=records.text(data.get("postal_code"), 20),
        latitude=lat,
        longitude=lon,
        time_zone=records.time_zone(data.get("time_zone")),
        asn=records.asn(system.get("asn")),
        organization=records.text(system.get("name")) or records.text(company.get("name")),
        network=records.text(system.get("route"), 64),
    )


# ---- ipapi.is --------------------------------------------------------------------------------------------------------


def _ipapi_request(address: str, credentials: Mapping[str, str]) -> Request:
    params = {"q": address}
    if credentials.get("api_key"):
        params["key"] = credentials["api_key"]
    return "https://api.ipapi.is/", {"Accept": "application/json"}, params


def _ipapi_parse(data: dict[str, Any]) -> Record:
    if data.get("is_bogon"):
        return Record()
    place = _dict(data.get("location"))
    system = data.get("asn")
    if isinstance(
        system, dict
    ):  # with a key: {"asn": 15169, "org": "Google LLC", "route": "8.8.8.0/24", ...}
        number = records.asn(system.get("asn"))
        name = records.text(system.get("org")) or records.text(system.get("descr"))
        route = records.text(system.get("route"), 64)
    else:  # without one: "AS15169 Google LLC"
        number, name = records.asn_and_name(system)
        route = None
    company = data.get("company")
    name = name or (
        records.text(_dict(company).get("name"))
        if isinstance(company, dict)
        else records.text(company)
    )
    lat, lon = records.position(
        place.get("latitude", data.get("lat")), place.get("longitude", data.get("lon"))
    )
    return Record(
        country=records.country(place.get("country_code"))
        or records.country(place.get("country"))
        or records.country(data.get("country")),
        region=records.text(place.get("state")) or records.text(data.get("region")),
        city=records.text(place.get("city")) or records.text(data.get("city")),
        postal_code=records.text(place.get("zip"), 20),
        latitude=lat,
        longitude=lon,
        time_zone=records.time_zone(place.get("timezone") or data.get("timezone")),
        asn=number,
        organization=name,
        network=route,
    )


# ---- IP2Location.io --------------------------------------------------------------------------------------------------


def _ip2location_request(address: str, credentials: Mapping[str, str]) -> Request:
    params = {"ip": address, "format": "json"}
    if credentials.get("api_key"):
        params["key"] = credentials["api_key"]
    return "https://api.ip2location.io/", {"Accept": "application/json"}, params


def _ip2location_parse(data: dict[str, Any]) -> Record:
    if "error" in data:
        return Record()
    lat, lon = records.position(data.get("latitude"), data.get("longitude"))
    zone = _dict(data.get("time_zone_info")).get("olson") or data.get(
        "time_zone"
    )  # the plain `time_zone` is an offset
    return Record(
        country=records.country(data.get("country_code"))
        or records.country(data.get("country_name")),
        region=records.text(data.get("region_name")),
        city=records.text(data.get("city_name")),
        postal_code=records.text(data.get("zip_code"), 20),
        latitude=lat,
        longitude=lon,
        time_zone=records.time_zone(zone),
        asn=records.asn(data.get("asn")),
        organization=records.text(data.get("as")),
    )


SOURCES: dict[str, OnlineSource] = {
    "ipinfo": OnlineSource("ipinfo", _ipinfo_request, _ipinfo_parse),
    "iplocate": OnlineSource("iplocate", _iplocate_request, _iplocate_parse),
    "ipapi_is": OnlineSource("ipapi_is", _ipapi_request, _ipapi_parse),
    "ip2location": OnlineSource(
        "ip2location", _ip2location_request, _ip2location_parse, refused=(401, 403)
    ),
}


def valid_address(address: str) -> bool:
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return False
    return True
