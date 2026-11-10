"""Estimate where an address is from the round-trip times measured by nodes at known places.

What this is, and is not
------------------------
Latency is not distance. What it gives is a hard physical limit (an echo cannot travel faster than
light) and a noisy hint (real paths are a good deal longer than the straight line). The estimator uses
both, in that order:

1. **Constraints.** A node that measured a round trip of `r` ms heard from the target within
   `150 * r` km, because even at the speed of light in vacuum 1 ms of round trip covers at most 150 km
   of distance. Candidate places outside any node's limit are impossible and are dropped. If two
   nodes' limits cannot overlap, no single place fits all of them: that is what anycast, a rerouted
   path or a bad measurement looks like. The nodes are then said to disagree (`conflicting`) and the
   estimate is made from the nearest node and the nodes that agree with it.
2. **Ranking.** Among the places that remain, each is scored by how well the measured times match an
   internet path (a path is usually 1.2 to 3 times the fibre distance, plus about a millisecond; the
   factor that fits a place best is used, with a pull towards the typical 1.7), nearest nodes weighted
   most. Anything faster than light in fibre is penalised hard.
3. **Always an answer, with its odds.** Whenever one node with a known position measured the address
   there is an estimate, however thin the evidence; only a measurement with no such node has none.
   Nodes that cannot all be right (anycast, a rerouted path, a bad measurement) are not a reason to
   refuse: the estimate is made from the nearest node and the nodes that agree with it, and the
   result says that the nodes disagree. The result carries the evidence, a strength label, the other
   plausible places, warnings and an **accuracy**: how often, on real targets of known position, an
   answer from a closest node this far away was within 100, 300, 500, 1,000 and 2,000 km
   (`accuracy.py`; the table is fitted on the calibration half and checked on the test half). It is
   a measured rate for answers like this one, not a probability for this particular address.

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

from .accuracy import Accuracy, accuracy_for
from .gazetteer import City, cities

MODEL_VERSION = "multilateration-5"

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
ENOUGH_NODES = 3  # fewer is still answered, with a warning
FAR_CLOSEST_RTT_MS = (
    40.0  # beyond this the answer is usually more than 1000 km off: said, not withheld
)
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
    "The nodes disagree about where this address is, so no single place fits all of them. Possible "
    "explanations include anycast, routing differences, or unreliable observations. The estimate "
    "is made from the nearest node and the nodes that agree with it."
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
    # "estimated" whenever a node with a known position measured the address; "insufficient_evidence" only when none did.
    # ("inconclusive" is no longer given: nodes that disagree set `conflicting` and the estimate is made from those that agree.)
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
    conflicting: bool = False
    accuracy: Accuracy | None = None


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


def _agreeing(evidence: list[Observation]) -> tuple[list[Observation], str | None]:
    """The nearest node and, in order of round trip, every node that does not contradict one already taken.

    Two nodes contradict each other when they are further apart than any real path allows given how close
    both say the target is. The second value says so for the first pair found (None: nobody disagreed).
    """
    taken: list[Observation] = []
    first_clash: str | None = None
    for obs in evidence:
        clash = next(
            (
                (o, apart)
                for o in taken
                if (apart := haversine_km(o.latitude, o.longitude, obs.latitude, obs.longitude))
                > CONSISTENCY_KM_PER_RTT_MS * (o.rtt_ms + obs.rtt_ms) + 2 * TOLERANCE_KM
            ),
            None,
        )
        if clash is None:
            taken.append(obs)
        elif first_clash is None:
            o, apart = clash
            first_clash = (
                f"{o.name} ({o.rtt_ms:.1f} ms) and {obs.name} ({obs.rtt_ms:.1f} ms) are {apart:.0f} km "
                "apart: no single place can be that close to both."
            )
    return taken, first_clash


def estimate(
    observations: list[Observation], gazetteer: tuple[City, ...] | None = None
) -> Estimate:
    gazetteer = cities() if gazetteer is None else gazetteer
    usable = sorted((o for o in observations if o.rtt_ms > 0), key=lambda o: o.rtt_ms)
    result = Estimate(status="insufficient_evidence", summary="", nodes_total=len(observations))
    evidence = usable[:MAX_EVIDENCE_NODES]
    result.evidence = list(evidence)

    if not evidence:
        result.summary = (
            "No estimate: no node with a known position measured this address. Give the nodes a "
            "position (the admin page) and measure again."
        )
        return result

    nearest = evidence[0]
    result.closest = Closest(
        nearest, nearest.rtt_ms * KM_PER_RTT_MS_HARD, nearest.rtt_ms * KM_PER_RTT_MS_FIBRE
    )
    # Nodes that cannot all be right: go on with the ones that agree with the nearest, and say so.
    evidence, clash = _agreeing(evidence)
    if clash is not None:
        result.conflicting = True
        result.warnings.append(NO_SINGLE_LOCATION)
        result.warnings.append(clash)

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
    scored: list[tuple[float, float, City]] = []
    for city, distances in feasible:
        best_fit = min(
            sum(
                w * _penalty(o.rtt_ms, d, k)
                for w, o, d in zip(weights, evidence, distances, strict=True)
            )
            + INFLATION_PRIOR * math.log(k / INFLATION) ** 2
            for k in INFLATIONS
        )
        # a tie (a ring of equally good places around one node) goes to the one nearest the nearest node
        scored.append((best_fit, distances[0], city))
    if not scored:
        # no city is within every limit (a node's neighbourhood has no city of 100,000): the nearest node's nearest city
        near_city = min(
            gazetteer,
            key=lambda c: haversine_km(
                nearest.latitude, nearest.longitude, c.latitude, c.longitude
            ),
        )
        scored = [(0.0, 0.0, near_city)]
        result.warnings.append(
            "No known city is within every node's speed-of-light limit; the city nearest the closest node is used."
        )

    scored.sort(key=lambda s: (s[0], s[1]))
    best_score, _, best = scored[0]
    likely = _place(best)
    scored2 = [(score, city) for score, _, city in scored]

    picked: list[tuple[float, City]] = [(best_score, best)]
    for score, city in scored2[1:]:
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
            for sc, c in scored2
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
    if result.conflicting:
        pass  # nodes that disagree are never strong evidence of one place
    elif (
        len(evidence) >= 4
        and nearest.rtt_ms <= 15
        and far_apart >= 1000
        and spread_km <= STRONG_SPREAD_KM
        and margin >= 6
    ):
        strength = "strong"
    elif nearest.rtt_ms <= 10 and spread_km <= MODERATE_SPREAD_KM and margin >= 2:
        strength = "moderate"

    if len(evidence) < ENOUGH_NODES:
        result.warnings.append(
            f"Only {len(evidence)} node(s) with a known position {'agree' if result.conflicting else 'measured this address'}; "
            f"{ENOUGH_NODES} or more narrow it down."
        )
    if nearest.rtt_ms > FAR_CLOSEST_RTT_MS:
        result.warnings.append(
            f"The closest node still measured {nearest.rtt_ms:.0f} ms, which leaves most of the world possible: "
            "treat this as a guess."
        )
    elif nearest.rtt_ms > 20:
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
    result.accuracy = accuracy_for(nearest.rtt_ms)
    result.alternatives = alternatives
    result.summary = (
        f"Likely near {likely.name}, {likely.country}: the closest node, {nearest.name}, measured "
        f"{nearest.rtt_ms:.1f} ms, so the target is within {nearest.rtt_ms * KM_PER_RTT_MS_HARD:.0f} km of it."
    )
    return result
