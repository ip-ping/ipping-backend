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
