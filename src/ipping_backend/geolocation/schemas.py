"""API shapes for the estimated network location."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlaceOut(BaseModel):
    name: str
    country: str
    latitude: float
    longitude: float


class AlternativeOut(BaseModel):
    place: PlaceOut
    distance_km: int = Field(description="From the likely place")


class ClosestOut(BaseModel):
    node_id: str
    node_name: str
    rtt_ms: float
    max_distance_km: int = Field(
        description="A physical limit: light cannot cover more in that time"
    )
    typical_distance_km: int = Field(
        description="At the speed of light in fibre; paths are usually longer"
    )


class EvidenceOut(BaseModel):
    node_id: str
    node_name: str
    latitude: float
    longitude: float
    rtt_ms: float
    verified: bool
    used: bool = Field(
        description="False for nodes beyond the twelve nearest, which the estimate ignores"
    )


class RuledOutOut(BaseModel):
    node_id: str
    node_name: str
    rtt_ms: float
    distance_km: int = Field(description="From that node to the place the database names")
    max_distance_km: int = Field(description="The most a round trip that short allows (light)")


class GeoIpOut(BaseModel):
    """What a GeoIP database says about the address, checked against the measurements.

    It never changes the estimate. `ruled_out` means a node measured the address too close for the
    database's place to be right; `consistent` only means no measurement contradicts it.
    """

    source: str
    place: PlaceOut | None = None
    claim: str | None = Field(
        default=None,
        description="geofeed only: what the feed says, in its own words, when no place could be checked",
    )
    prefix: str | None = Field(default=None, description="geofeed only: the prefix the line is for")
    verdict: Literal["consistent", "ruled_out", "not_checked"]
    distance_to_estimate_km: int | None = Field(
        default=None, description="From the place the database names to the likely place"
    )
    ruled_out_by: RuledOutOut | None = None
    attribution: str | None = None


class AsnOut(BaseModel):
    number: int
    organization: str


class AddressGroupOut(BaseModel):
    """One address that some of the nodes measured, when they did not all measure the same one."""

    ip: str
    nodes: int = Field(description="How many nodes measured this address")
    selected: bool = Field(description="Whether the estimate is for this address")


class LocationEstimateOut(BaseModel):
    status: Literal["estimated", "inconclusive", "insufficient_evidence", "unavailable", "pending"]
    summary: str
    model_version: str
    target_ip: str | None = None
    likely: PlaceOut | None = None
    strength: Literal["low", "moderate", "strong"] | None = Field(
        default=None,
        description="A heuristic label, not a probability or a radius; those would need calibration",
    )
    alternatives: list[AlternativeOut] = []
    closest: ClosestOut | None = None
    evidence: list[EvidenceOut] = []
    nodes_total: int = 0
    warnings: list[str] = []
    limitations: list[str] = []
    geoip: GeoIpOut | None = Field(
        default=None, description="A configured GeoIP database's answer, checked against the nodes"
    )
    groups: list[AddressGroupOut] = Field(
        default=[],
        description=(
            "When each node resolved the name itself and they did not all get the same address: the "
            "addresses and how many nodes measured each. The estimate is for the selected one "
            "(`?ip=` chooses). Empty when every node measured one address."
        ),
    )
    geofeed: GeoIpOut | None = Field(
        default=None,
        description="What the network's operator says in a geofeed (RFC 8805) the administrator listed, checked the same way",
    )
    asn: AsnOut | None = Field(
        default=None, description="The network, if an ASN database is set up"
    )


class HopPlaceOut(BaseModel):
    """What a GeoIP database says about one router on a path. Routers are often registered at the
    owner's headquarters, so the place is a hint to be checked against the round-trip time, never a
    fact."""

    asn: int | None = None
    as_org: str | None = None
    country: str | None = None
    place: str | None = Field(default=None, description="City, or the country if no city is known")
    latitude: float | None = None
    longitude: float | None = None


class PathOut(BaseModel):
    """The networks and places of the routers a traceroute measurement found, by address."""

    source: str | None = Field(default=None, description="Database name, when one is loaded")
    attribution: str | None = None
    city_database: bool = False
    asn_database: bool = False
    hops: dict[str, HopPlaceOut] = Field(
        default_factory=dict, description="Keyed by the hop's public address"
    )
