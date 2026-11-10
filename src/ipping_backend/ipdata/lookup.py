"""An address looked up in the IP databases."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from typing import Any

from ..geolocation.geoip import GeoIp
from ..security.targets import address_is_public
from .schemas import IpInfoOut


def _name(record: dict[str, Any] | None, *path: str) -> str | None:
    """`record[path...]['names']['en']`, when it is all there."""
    node: Any = record
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    names = node.get("names") if isinstance(node, dict) else None
    value = names.get("en") if isinstance(names, dict) else None
    return str(value) if value else None


def lookup(geoip: GeoIp, address: str) -> IpInfoOut:
    """Raises ValueError for text that is not an IP address."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    text = str(ip)
    base = IpInfoOut(address=text, version=ip.version, public=address_is_public(ip), found=False)
    if not base.public:
        return base
    city, city_prefix = geoip.record("city", text)
    country, country_prefix = geoip.record("country", text) if city is None else (None, None)
    asn, asn_prefix = geoip.record("asn", text)
    geo = city or country or {}
    prefix = (
        city_prefix if city is not None else country_prefix if country is not None else asn_prefix
    )
    if not geo and not asn:
        return base
    where = geo.get("location") or {}
    subdivision = (geo.get("subdivisions") or [{}])[0]
    in_country = geo.get("country") or geo.get("registered_country") or {}
    built = [d["build_epoch"] for d in geoip.databases() if d.get("build_epoch")]
    base.found = True
    base.network = (
        str(ipaddress.ip_network(f"{text}/{prefix}", strict=False)) if prefix is not None else None
    )
    base.country = (
        (str(in_country["iso_code"]).upper() or None) if in_country.get("iso_code") else None
    )
    base.country_name = _name(geo, "country") or _name(geo, "registered_country")
    base.continent = (geo.get("continent") or {}).get("code")
    base.region = _name({"s": subdivision}, "s")
    base.region_code = subdivision.get("iso_code")
    base.city = _name(geo, "city")
    base.postal_code = (geo.get("postal") or {}).get("code")
    base.latitude = where.get("latitude")
    base.longitude = where.get("longitude")
    base.accuracy_radius_km = where.get("accuracy_radius")
    base.time_zone = where.get("time_zone")
    if asn:
        base.asn = (
            int(asn["autonomous_system_number"]) if asn.get("autonomous_system_number") else None
        )
        base.organization = (str(asn.get("autonomous_system_organization") or "")[:120]) or None
    base.source = geoip.source
    base.database_built = datetime.fromtimestamp(max(built), UTC) if built else None
    base.attribution = geoip.attribution
    return base
