"""Where the routers on a traced path are, according to the GeoIP databases (when there are any).

Done on every request, not stored: the databases are replaced every month and a path is not.
Only public addresses are looked up, and only ones the measurement itself found, so this adds no
address to what the result already shows.
"""

from __future__ import annotations

import ipaddress

from ..measurements.schemas import MeasurementOut
from .geoip import GeoIp, valid_ip
from .schemas import HopPlaceOut, PathOut

MAX_ADDRESSES = 1000


def path_for(measurement: MeasurementOut, geoip: GeoIp | None) -> PathOut:
    if geoip is None or not (geoip.has_city or geoip.has_asn):
        return PathOut()
    out = PathOut(
        source=geoip.source,
        attribution=geoip.attribution,
        city_database=geoip.has_city,
        asn_database=geoip.has_asn,
    )
    if measurement.kind != "traceroute":
        return out
    found: dict[str, None] = {}
    for task in measurement.tasks:
        for sample in task.samples:
            if sample.hop is not None and sample.hop.ip:
                found.setdefault(sample.hop.ip)
    for raw in list(found)[:MAX_ADDRESSES]:
        address = valid_ip(raw)
        if address is None or not ipaddress.ip_address(address).is_global:
            continue
        asn, place = geoip.asn(address), geoip.place(address)
        if asn is None and place is None:
            continue
        out.hops[address] = HopPlaceOut(
            asn=asn.number if asn else None,
            as_org=(asn.organization or None) if asn else None,
            country=(place.country or None) if place else None,
            place=place.name if place else None,
            latitude=place.latitude if place else None,
            longitude=place.longitude if place else None,
        )
    return out
