"""Estimate where an address is from the round-trip times measured by nodes at known places.

What this is, and is not
------------------------
Latency is not distance. What it gives is a hard physical limit (an echo cannot travel faster than
light) and a noisy hint (real paths are a good deal longer than the straight line). The estimator uses
both, in that order:

1. **Constraints.** A node that measured a round trip of `r` ms heard from the target within
   `150 * r` km, because even at the speed of light in vacuum 1 ms of round trip covers at most 150 km
   of distance. Candidate places outside any node's limit are impossible and are dropped. If two
   nodes' limits cannot overlap, no single place fits at all, and the answer is "inconclusive", not a
   guess: that is what anycast, a rerouted path or a bad measurement looks like.
2. **Ranking.** Among the places that remain, each is scored by how well the measured times match an
   internet path (a path is usually 1.2 to 3 times the fibre distance, plus about a millisecond; the
   factor that fits a place best is used, with a pull towards the typical 1.7), nearest nodes weighted
   most. Anything faster than light in fibre is penalised hard.
3. **Honesty.** The result carries the evidence it was built from, a strength label, the other
   plausible places, and warnings. The label is a rule of thumb, never a percentage or a radius for
   *this* answer; what each label has meant in practice is measured against real targets (below).
   "Insufficient evidence" is a normal outcome, and so is "inconclusive".

Calibration
-----------
The constants marked "fitted" were fitted, and the labels and cut-offs checked, on real measurements:
round trips between RIPE Atlas anchors, which have known positions (`calibration/`,
`scripts/collect_atlas.py`, `scripts/evaluate_location.py`). Targets were split into a calibration
half (constants tuned on it) and a held-out test half (only used to report); the numbers in the
README are the test half's. The unit tests use synthetic paths for the arithmetic, and
`tests/test_real_measurements.py` guards the real-data results. `MODEL_VERSION` changes whenever the
arithmetic does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from .gazetteer import City, cities

MODEL_VERSION = "multilateration-4"

KM_PER_RTT_MS_HARD = (
    150.0  # c in vacuum: 299.8 km per ms one way = 150 km of distance per round-trip ms
)
KM_PER_RTT_MS_FIBRE = 100.0  # two thirds of c, light in glass
# Two nodes whose distances to the target cannot both be right at this speed contradict each other.
# Looser than glass (a few links are faster) but far tighter than light in vacuum: across ~4,000
# pairs of real unicast measurements the worst pair never needed more than 100 km per round-trip ms,
# while 96% of the fleets measuring anycast root servers did.
CONSISTENCY_KM_PER_RTT_MS = 110.0
INFLATION = 1.7  # typical path length over the straight line
# Paths differ by region (a straight submarine cable, a long detour), so each candidate is scored
# at the inflation that fits it best among these, with a mild pull back towards the typical value.
INFLATIONS = (1.2, 1.45, 1.7, 2.0, 2.4, 3.0)
INFLATION_PRIOR = 3.0
BASE_MS = 8.0  # fixed delay on a real path (last mile, switching, queues); fitted on real data
TOLERANCE_KM = 60.0  # node coordinates are usually city-level; do not reject on that alone

MAX_EVIDENCE_NODES = 12
MIN_NODES = 3
MAX_CLOSEST_RTT_MS = 40.0  # beyond this the answer is usually more than 1000 km off
NEAR_EQUAL = 3.0  # score difference within which places count as fitting "about as well"
STRONG_SPREAD_KM = 500.0
MODERATE_SPREAD_KM = 1500.0
ALTERNATIVE_MIN_SEPARATION_KM = 800.0
MAX_ALTERNATIVES = 3

Strength = Literal["low", "moderate", "strong"]

LIMITATIONS = (
    "Anycast addresses are served from many places at once, so one location can be wrong for them.",
    "A CDN edge, VPN or proxy shows where that machine is, not where the site's origin or a user is.",
    "This estimates where the address's network appears to be. It does not locate a person or a business.",
)
NO_SINGLE_LOCATION = (
    "A single location could not be established. Possible explanations include anycast, routing "
    "differences, or unreliable observations."
)


@dataclass(frozen=True, slots=True)
class Observation:
    node_id: str
    name: str
    latitude: float
    longitude: float
    rtt_ms: float  # the minimum valid round trip from that node
    verified: bool = False


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    country: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Alternative:
    place: Place
    distance_km: float  # from the likely place


@dataclass(frozen=True, slots=True)
class Closest:
    observation: Observation
    max_distance_km: float  # physical limit
    typical_distance_km: float  # at fibre speed


@dataclass(slots=True)
class Estimate:
    status: Literal["estimated", "inconclusive", "insufficient_evidence"]
    summary: str
    likely: Place | None = None
    strength: Strength | None = None
    alternatives: list[Alternative] = field(default_factory=list)
    closest: Closest | None = None
    evidence: list[Observation] = field(default_factory=list)
    nodes_total: int = 0
    warnings: list[str] = field(default_factory=list)
    limitations: tuple[str, ...] = LIMITATIONS


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 12742.0 * math.asin(min(1.0, math.sqrt(a)))


def _penalty(rtt_ms: float, distance_km: float, inflation: float) -> float:
    fibre_minimum = distance_km / KM_PER_RTT_MS_FIBRE
    if rtt_ms < fibre_minimum:
        # Faster than light in glass: possible only on unusually direct links. Strongly discouraged.
        return 25.0 + 100.0 * ((fibre_minimum - rtt_ms) / (fibre_minimum + 1.0)) ** 2
    predicted = BASE_MS + inflation * fibre_minimum
    z = (rtt_ms - predicted) / (0.35 * predicted + 2.0)
    # Slower than typical (a detour) is far more plausible than faster, so it is penalised less.
    return (z / 2.0) ** 2 if z > 0 else z * z


def _weight(obs: Observation) -> float:
    return (1.0 / (1.0 + obs.rtt_ms / 40.0)) * (1.0 if obs.verified else 0.6)


def _place(city: City) -> Place:
    return Place(city.name, city.country, city.latitude, city.longitude)


def estimate(
    observations: list[Observation], gazetteer: tuple[City, ...] | None = None
) -> Estimate:
    gazetteer = cities() if gazetteer is None else gazetteer
    usable = sorted((o for o in observations if o.rtt_ms > 0), key=lambda o: o.rtt_ms)
    result = Estimate(status="insufficient_evidence", summary="", nodes_total=len(observations))
    evidence = usable[:MAX_EVIDENCE_NODES]
    result.evidence = list(evidence)

    if len(evidence) < MIN_NODES:
        result.summary = (
            f"Not enough evidence: {len(evidence)} node(s) with a known position measured this "
            f"address, and at least {MIN_NODES} are needed."
        )
        return result

    nearest = evidence[0]
    result.closest = Closest(
        nearest, nearest.rtt_ms * KM_PER_RTT_MS_HARD, nearest.rtt_ms * KM_PER_RTT_MS_FIBRE
    )
    if nearest.rtt_ms > MAX_CLOSEST_RTT_MS:
        result.summary = (
            f"Not enough evidence: the closest node still measured {nearest.rtt_ms:.0f} ms, which "
            "leaves most of the world possible."
        )
        return result

    # Two nodes that cannot both be right about one place.
    for i, a in enumerate(evidence):
        for b in evidence[i + 1 :]:
            apart = haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)
            if apart > CONSISTENCY_KM_PER_RTT_MS * (a.rtt_ms + b.rtt_ms) + 2 * TOLERANCE_KM:
                result.status = "inconclusive"
                result.summary = NO_SINGLE_LOCATION
                result.warnings.append(
                    f"{a.name} ({a.rtt_ms:.1f} ms) and {b.name} ({b.rtt_ms:.1f} ms) are {apart:.0f} km "
                    "apart: no single place can be that close to both."
                )
                return result

    feasible: list[tuple[City, list[float]]] = []
    for city in gazetteer:
        distances: list[float] = []
        for obs in evidence:
            d = haversine_km(obs.latitude, obs.longitude, city.latitude, city.longitude)
            if d > KM_PER_RTT_MS_HARD * obs.rtt_ms + TOLERANCE_KM:
                break
            distances.append(d)
        else:
            feasible.append((city, distances))

    weights = [_weight(o) for o in evidence]
    scored: list[tuple[float, City]] = []
    for city, distances in feasible:
        best_fit = min(
            sum(
                w * _penalty(o.rtt_ms, d, k)
                for w, o, d in zip(weights, evidence, distances, strict=True)
            )
            + INFLATION_PRIOR * math.log(k / INFLATION) ** 2
            for k in INFLATIONS
        )
        scored.append((best_fit, city))
    if not scored:
        result.status = "inconclusive"
        result.summary = NO_SINGLE_LOCATION
        result.warnings.append("No known city is within every node's speed-of-light limit.")
        return result

    scored.sort(key=lambda s: s[0])
    best_score, best = scored[0]
    likely = _place(best)

    picked: list[tuple[float, City]] = [(best_score, best)]
    for score, city in scored[1:]:
        if len(picked) > MAX_ALTERNATIVES:
            break
        if (
            all(
                haversine_km(city.latitude, city.longitude, p.latitude, p.longitude)
                >= ALTERNATIVE_MIN_SEPARATION_KM
                for _, p in picked
            )
            and score - best_score <= 12.0
        ):
            picked.append((score, city))
    alternatives = [
        Alternative(
            _place(c), round(haversine_km(best.latitude, best.longitude, c.latitude, c.longitude))
        )
        for _, c in picked[1:]
    ]
    margin = (picked[1][0] - best_score) if len(picked) > 1 else math.inf
    # How far apart are the places that fit about as well as the best one? A tight cluster means the
    # measurements pin the area down; a long smear means they do not.
    spread_km = max(
        (
            haversine_km(best.latitude, best.longitude, c.latitude, c.longitude)
            for sc, c in scored
            if sc <= best_score + NEAR_EQUAL
        ),
        default=0.0,
    )

    far_apart = max(
        haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)
        for a in evidence
        for b in evidence
    )
    strength: Strength = "low"
    if (
        len(evidence) >= 4
        and nearest.rtt_ms <= 15
        and far_apart >= 1000
        and spread_km <= STRONG_SPREAD_KM
        and margin >= 6
    ):
        strength = "strong"
    elif nearest.rtt_ms <= 10 and spread_km <= MODERATE_SPREAD_KM and margin >= 2:
        strength = "moderate"

    if nearest.rtt_ms > 20:
        result.warnings.append(
            f"The closest node measured {nearest.rtt_ms:.0f} ms, so the area is broad."
        )
    if alternatives:
        result.warnings.append("More than one region fits the measurements; see the alternatives.")
    if sum(1 for o in evidence if not o.verified) > len(evidence) / 2:
        result.warnings.append(
            "Most nodes' coordinates have not been verified by an administrator."
        )
    if far_apart < 500:
        result.warnings.append(
            "The nodes used are all close to each other, which weakens the estimate."
        )

    result.status = "estimated"
    result.likely = likely
    result.strength = strength
    result.alternatives = alternatives
    result.summary = (
        f"Likely near {likely.name}, {likely.country}: the closest node, {nearest.name}, measured "
        f"{nearest.rtt_ms:.1f} ms, so the target is within {nearest.rtt_ms * KM_PER_RTT_MS_HARD:.0f} km of it."
    )
    return result
