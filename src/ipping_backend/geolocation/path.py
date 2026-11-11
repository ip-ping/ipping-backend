"""Where the routers on a traced path are, according to the IP data sources (when there are any): the vote of every one that is on.

Done on every request, not stored: the databases are replaced regularly and a path is not.
Only public addresses are looked up, and only ones the measurement itself found, so this adds no
address to what the result already shows.
"""

from __future__ import annotations

import ipaddress

from ..ipdata.service import IpDataService, place_of
from ..measurements.schemas import MeasurementOut
from .geoip import valid_ip
from .schemas import HopPlaceOut, PathOut

MAX_ADDRESSES = 1000


def path_for(measurement: MeasurementOut, ipdata: IpDataService | None) -> PathOut:
    if ipdata is None or not (ipdata.can("position") or ipdata.can("asn")):
        return PathOut()
    knowing = ipdata.knowing("position", "asn")
    out = PathOut(
        source=", ".join(i.name for i in knowing),
        attribution="; ".join(i.attribution for i in knowing if i.attribution) or None,
        city_database=ipdata.can("position"),
        asn_database=ipdata.can("asn"),
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
        info = ipdata.lookup(address)
        place = place_of(info)
        if info.asn is None and place is None:
            continue
        out.hops[address] = HopPlaceOut(
            asn=info.asn,
            as_org=(info.organization or "")[:120] or None,
            country=(place.country or None) if place else None,
            place=place.name if place else None,
            latitude=place.latitude if place else None,
            longitude=place.longitude if place else None,
        )
    return out
