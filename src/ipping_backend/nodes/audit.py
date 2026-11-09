"""Checking where a volunteer's node really is, from the round trips of nodes whose place is known.

A node declares its own location when it enrolls, and a volunteer's declaration is not believed. This asks
at least three *verified official* nodes to ping the address the node connects from, and applies the same
physics the location estimate uses: a node that heard from an address after `r` milliseconds cannot be
further from it than 150 km per millisecond of round trip, whatever the path. So:

* if any verified node measured the address too quickly for the declared place to be right, the place is
  **inconsistent** (and which node ruled it out is named);
* if none did, and at least one verified node is close enough (by its round trip) for that to mean something,
  the place is **consistent**: not contradicted. It does not prove the place (a node in the next city passes);
* otherwise the audit is **inconclusive**: too few verifiers, or none close enough to tell.

An administrator decides what to do with the answer; nothing here verifies a node by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..geolocation.estimate import KM_PER_RTT_MS_HARD, TOLERANCE_KM, haversine_km
from ..geolocation.geoip import GeoPlace, rule_out

MIN_VERIFIERS = 3
# At least one verifier must be this near (by its round trip) for "not contradicted" to mean anything.
TIGHT_KM = 3000.0


@dataclass(frozen=True)
class Verifier:
    node_id: str
    name: str
    latitude: float
    longitude: float
    rtt_ms: float


def judge(
    latitude: float,
    longitude: float,
    address: str,
    verifiers: list[Verifier],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The audit result for a declared place: {status, summary, checked_at, address, evidence}."""
    evidence = [
        {
            "node_id": v.node_id,
            "node_name": v.name,
            "rtt_ms": round(v.rtt_ms, 3),
            "distance_km": round(haversine_km(v.latitude, v.longitude, latitude, longitude)),
            "max_km": round(v.rtt_ms * KM_PER_RTT_MS_HARD + TOLERANCE_KM),
        }
        for v in sorted(verifiers, key=lambda v: v.rtt_ms)
    ]
    result: dict[str, Any] = {
        "status": "inconclusive",
        "checked_at": (now or datetime.now(UTC)).isoformat(),
        "address": address,
        "evidence": evidence,
    }
    if len(verifiers) < MIN_VERIFIERS:
        result["summary"] = (
            f"Only {len(verifiers)} verified node(s) could measure it; at least {MIN_VERIFIERS} are needed."
        )
        return result
    ruled = rule_out(
        GeoPlace("declared place", "", latitude, longitude),
        [(v.node_id, v.name, v.latitude, v.longitude, v.rtt_ms) for v in verifiers],
    )
    if ruled is not None:
        result["status"] = "inconsistent"
        result["summary"] = (
            f"{ruled.node_name} measured the address in {ruled.rtt_ms:g} ms, which allows at most "
            f"{round(ruled.max_distance_km):,} km, but the declared place is {round(ruled.distance_km):,} km "
            "from it. The node is not where it says."
        )
        return result
    tightest = round(min(v.rtt_ms * KM_PER_RTT_MS_HARD + TOLERANCE_KM for v in verifiers))
    if tightest > TIGHT_KM:
        result["summary"] = (
            "No verified node is close enough to tell: the nearest one allows "
            f"{tightest:,} km. Nothing contradicts the declared place, and nothing supports it."
        )
        return result
    result["status"] = "consistent"
    result["summary"] = (
        f"Not contradicted by {len(verifiers)} verified nodes, the nearest allowing {tightest:,} km. "
        "That does not prove the place: a node in the next city would pass too."
    )
    return result
