"""Many sources, one answer: a majority vote for each field, and how sure the answer is.

Every source that says something about a field votes for what it said. The value with the most **evidence** wins (the
evidence is what the percentage below is made of, so the answer is always the one with the highest percentage; with sources
that are equally reliable it is the plain majority, and a tie goes to the value with more sources, then to the source listed
first), and a field is answered **whenever at least one source has something**: one source of nine with data, the other
eight empty or failed, still gives that one answer.

Hierarchy keeps the answer in one piece: only sources that agree with the winning country vote on the region, the city and
the position; only those that agree with the winning AS number vote on the organization. A source that has no country
(a table of AS numbers) votes only on what it has.

**The percentage** is the odds that the winning value is right, worked out like this. Each source has a *reliability*
for a kind of data (`RELIABILITY`: how often such a source is right about a country, a city, an AS number: priors,
not measurements of this deployment). A source that is right with probability `a` speaks with odds `a / (1 - a)`. Sources
that are right and wrong independently, and that are wrong in different ways, agree on a wrong value only by accident, so the
odds of a value are the product of the odds of the sources that gave it (sources that are known to share their mistakes are one
witness, see `FAMILIES`), and

    percent = odds(value) / (1 + the odds of every value that was given)

where the 1 is "none of them is right". One source with `a = 0.8` gives 80%; two that agree give 94%; two that disagree give
44% each; three against one give 93%. The percentage is never shown as 0 or 100: 1 to 99.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .measured import MEASURED
from .records import Record, fold, organization_key, organization_words

# Where a source is listed first, it wins a tie that reliability does not settle.
ORDER = (
    "maxmind",
    "ipinfo",
    "dbip",
    "iplocate",
    "ipapi_is",
    "ip2location",
    "iptoasn",
    "ipdeny",
    "ip2region",
)

# Sources that are wrong the same way are one witness, not several: of the addresses of RIPE Atlas anchors on which both were
# wrong, IPtoASN and IPdeny gave the same wrong country 81 % of the time. Both only say where an address block is *registered*, which
# is not where the address is (a German data centre on an American block is "US" to both). Counted once, with the more reliable
# of them, the country was right more often than with each counted (93.2 % against 91.7 % on the test half: README, IP data sources).
FAMILIES: tuple[frozenset[str], ...] = (frozenset({"iptoasn", "ipdeny"}),)

# How often a source is right about this kind of data, as a fraction: priors, from what the vendors publish about their
# databases and from how the kinds of data differ (a country is far easier than a city; an AS number comes from routing
# data and is the easiest). Where `measured.py` (made by `scripts/evaluate_ipdata.py`) has a number it is used instead;
# a source or a field with no number there (a source that needs an account nobody had when it was made, a field the anchors
# say nothing about: region, city, postal code, time zone, organization) keeps its prior, and the README says so.
PRIORS: dict[str, dict[str, float]] = {
    "maxmind": {"country": 0.98, "region": 0.80, "city": 0.68, "location": 0.66, "postal_code": 0.45, "time_zone": 0.88, "asn": 0.97, "organization": 0.93},
    "ipinfo": {"country": 0.98, "region": 0.82, "city": 0.72, "location": 0.70, "postal_code": 0.55, "time_zone": 0.90, "asn": 0.98, "organization": 0.93},
    "dbip": {"country": 0.96, "region": 0.75, "city": 0.65, "location": 0.63, "postal_code": 0.40, "time_zone": 0.85, "asn": 0.96, "organization": 0.92},
    "iplocate": {"country": 0.96, "region": 0.78, "city": 0.68, "location": 0.66, "postal_code": 0.45, "time_zone": 0.86, "asn": 0.98, "organization": 0.93},
    "ipapi_is": {"country": 0.95, "region": 0.75, "city": 0.65, "location": 0.63, "postal_code": 0.40, "time_zone": 0.85, "asn": 0.98, "organization": 0.92},
    "ip2location": {"country": 0.95, "region": 0.76, "city": 0.66, "location": 0.64, "postal_code": 0.45, "time_zone": 0.80, "asn": 0.96, "organization": 0.90},
    "iptoasn": {"country": 0.93, "asn": 0.99, "organization": 0.95},
    "ipdeny": {"country": 0.93},
    "ip2region": {"country": 0.94, "region": 0.72, "city": 0.62, "location": 0.60},
}  # fmt: skip
DEFAULT_RELIABILITY = 0.6
FLOOR, CEILING = 0.55, 0.99


def merged() -> dict[str, dict[str, float]]:
    """The priors with the measured numbers laid over them.

    A source that could not be measured (one that needs an account nobody had) is trusted no more than the best source that
    was, for a field that was measured at all: what a vendor says about itself cannot be set beside numbers found the same way
    for every other source, and a claim of 98 % over a measured 94 % would make it out-weigh sources nobody has doubted.
    """
    out = {source: dict(kinds) for source, kinds in PRIORS.items()}
    best: dict[str, float] = {}
    for measured in MEASURED.values():
        for kind, value in measured.items():
            best[kind] = max(best.get(kind, 0.0), value)
    for source, kinds in out.items():
        for kind in kinds:
            if kind in best and kind not in MEASURED.get(source, {}):
                kinds[kind] = min(kinds[kind], best[kind])
    for source, measured in MEASURED.items():
        out.setdefault(source, {}).update(measured)
    return out


RELIABILITY: dict[str, dict[str, float]] = merged()

# Positions closer than this are the same place for voting (databases put one address on different spots of one city)
SAME_PLACE_KM = 60.0


def reliability(source: str, kind: str) -> float:
    return min(CEILING, max(FLOOR, RELIABILITY.get(source, {}).get(kind, DEFAULT_RELIABILITY)))


def _odds(source: str, kind: str) -> float:
    a = reliability(source, kind)
    return a / (1 - a)


def _evidence(sources: Sequence[str], kind: str) -> float:
    """The odds that a value is right when these sources gave it: each witness speaks once. Sources of a family are one witness,
    the most reliable of them."""
    witnesses: dict[object, float] = {}
    for source in sources:
        family = next((f for f in FAMILIES if source in f), source)
        witnesses[family] = max(witnesses.get(family, 0.0), _odds(source, kind))
    return math.prod(witnesses.values())


def _rank(source: str) -> int:
    return ORDER.index(source) if source in ORDER else len(ORDER)


@dataclass
class Decision:
    """The winner of one vote."""

    value: Any  # what to show (the winner's own spelling)
    percent: int  # 1..99
    agree: list[str]  # sources that voted for it
    differ: list[str]  # sources that voted for something else


def _vote(
    ballots: Sequence[tuple[str, Hashable, Any]],
    kind: str,
    *,
    weigh: Callable[[Any], Any] | None = None,
) -> Decision | None:
    """`ballots` are (source, what it is compared as, what it says). Returns the winner, or `None` when nobody voted."""
    if not ballots:
        return None
    groups: dict[Hashable, list[tuple[str, Any]]] = defaultdict(list)
    for source, key, shown in ballots:
        groups[key].append((source, shown))
    score = {key: _evidence([s for s, _ in members], kind) for key, members in groups.items()}

    def standing(key: Hashable) -> tuple[float, int, int]:
        members = groups[key]
        return (-round(score[key], 9), -len(members), min(_rank(s) for s, _ in members))

    winner = min(groups, key=standing)
    percent = round(100 * score[winner] / (1 + sum(score.values())))
    members = groups[winner]
    # the spelling most sources used, the first listed source's among equals
    spellings = Counter(str(shown) for _, shown in members)
    best = max(spellings.values())
    ordered = sorted(members, key=lambda m: _rank(m[0]))
    shown = next(shown for _, shown in ordered if spellings[str(shown)] == best)
    chosen = weigh(shown) if weigh else shown
    return Decision(
        value=chosen,
        percent=min(99, max(1, percent)),
        agree=sorted((s for s, _ in members), key=_rank),
        differ=sorted((s for k, ms in groups.items() if k != winner for s, _ in ms), key=_rank),
    )


# ---- Positions -------------------------------------------------------------------------------------------------------


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 12742 * math.asin(min(1.0, math.sqrt(h)))


def _clusters(
    points: Sequence[tuple[str, tuple[float, float]]],
) -> list[list[tuple[str, tuple[float, float]]]]:
    """Group positions that are within `SAME_PLACE_KM` of a first member (single pass, in listed order)."""
    groups: list[list[tuple[str, tuple[float, float]]]] = []
    for item in points:
        for group in groups:
            if _km(group[0][1], item[1]) <= SAME_PLACE_KM:
                group.append(item)
                break
        else:
            groups.append([item])
    return groups


def _center(group: Sequence[tuple[str, tuple[float, float]]]) -> tuple[float, float]:
    """The middle of a group: the median of each coordinate (one outlier cannot move it)."""
    lats = sorted(p[1][0] for p in group)
    lons = sorted(p[1][1] for p in group)
    mid = len(group) // 2
    if len(group) % 2:
        return round(lats[mid], 4), round(lons[mid], 4)
    return round((lats[mid - 1] + lats[mid]) / 2, 4), round((lons[mid - 1] + lons[mid]) / 2, 4)


def _same_organization(items: Sequence[tuple[str, str]]) -> list[tuple[str, int, str]]:
    """Ballots (source, group, name) for names of networks: a name belongs to a group when its words are the group's, or
    contain them, or are contained in them (`GOOGLE - Google LLC`, `Google LLC` and `Google Cloud` are one organization;
    `Hetzner Online GmbH` and `HETZNER-AS` are one). Names are taken in the order given and join the first group they fit."""
    groups: list[frozenset[str]] = []
    ballots: list[tuple[str, int, str]] = []
    for source, name in items:
        words = organization_words(name)
        for index, rep in enumerate(groups):
            if words <= rep or rep <= words:
                groups[index] = rep & words if rep & words else rep
                break
        else:
            groups.append(words)
            index = len(groups) - 1
        ballots.append((source, index, name))
    return ballots


# ---- The whole answer ------------------------------------------------------------------------------------------------


@dataclass
class Consensus:
    """The voted answer: a `Record`, how sure each field is, and who agreed or differed on each."""

    record: Record = field(default_factory=Record)
    percent: dict[str, int] = field(default_factory=dict)
    agree: dict[str, list[str]] = field(default_factory=dict)
    differ: dict[str, list[str]] = field(default_factory=dict)
    contributors: list[str] = field(default_factory=list)


def decide(records: dict[str, Record]) -> Consensus:
    """Vote on every field over the records of the sources that answered (`{source id: record}`)."""
    result = Consensus()
    answered = {s: r for s, r in records.items() if not r.empty()}
    if not answered:
        return result
    out = result.record

    def settle(name: str, decision: Decision | None) -> Any:
        if decision is None:
            return None
        result.percent[name] = decision.percent
        result.agree[name] = decision.agree
        result.differ[name] = decision.differ
        return decision.value

    # country
    country = _vote(
        [(s, r.country, r.country) for s, r in answered.items() if r.country], "country"
    )
    out.country = settle("country", country)

    def in_country(r: Record) -> bool:
        return out.country is None or r.country in (None, out.country)

    local = {s: r for s, r in answered.items() if in_country(r)}

    # region and city
    region = _vote(
        [(s, fold(r.region), r.region) for s, r in local.items() if r.region and fold(r.region)],
        "region",
    )
    out.region = settle("region", region)
    if (
        out.region is not None
    ):  # the code of the region, from the first source that agrees on the region and has one
        out.region_code = next(
            (
                r.region_code
                for s, r in sorted(local.items(), key=lambda item: _rank(item[0]))
                if r.region_code and r.region and fold(r.region) == fold(out.region)
            ),
            None,
        )
    city_pool = {
        s: r
        for s, r in local.items()
        if out.region is None or not r.region or fold(r.region) == fold(out.region)
    }
    city = _vote(
        [(s, fold(r.city), r.city) for s, r in city_pool.items() if r.city and fold(r.city)], "city"
    )
    out.city = settle("city", city)

    # position: the biggest group of positions within one place; the median of it
    points = [
        (s, (r.latitude, r.longitude))
        for s, r in local.items()
        if r.latitude is not None and r.longitude is not None
    ]
    points.sort(key=lambda item: _rank(item[0]))
    if points:
        groups = _clusters(points)
        decision = _vote(
            [(s, index, index) for index, group in enumerate(groups) for s, _ in group], "location"
        )
        if decision is not None:
            winning = groups[decision.value]
            out.latitude, out.longitude = _center(winning)
            result.percent["location"] = decision.percent
            result.agree["location"] = decision.agree
            result.differ["location"] = decision.differ
            radii = sorted(
                radius for s, _ in winning if (radius := records[s].accuracy_radius_km) is not None
            )
            out.accuracy_radius_km = radii[len(radii) // 2] if radii else None

    # postal code and time zone
    postal = _vote(
        [
            (s, fold(r.postal_code), r.postal_code)
            for s, r in city_pool.items()
            if r.postal_code and fold(r.postal_code)
        ],
        "postal_code",
    )
    out.postal_code = settle("postal_code", postal)
    zone = _vote(
        [(s, r.time_zone, r.time_zone) for s, r in local.items() if r.time_zone], "time_zone"
    )
    out.time_zone = settle("time_zone", zone)

    # the network: the AS number, then who it belongs to among the sources that gave that number
    asn = _vote([(s, r.asn, r.asn) for s, r in answered.items() if r.asn], "asn")
    out.asn = settle("asn", asn)
    owners = {s: r for s, r in answered.items() if out.asn is None or r.asn in (None, out.asn)}
    names = [
        (s, r.organization)
        for s, r in sorted(owners.items(), key=lambda item: _rank(item[0]))
        if r.organization and organization_key(r.organization)
    ]
    organization = _vote(_same_organization(names), "organization")
    out.organization = settle("organization", organization)

    # the network the winner's own record covers: from the first source that agrees on the AS number and has one
    for source in sorted(answered, key=_rank):
        r = answered[source]
        if r.network and (out.asn is None or r.asn in (None, out.asn)) and in_country(r):
            out.network = r.network
            break
    result.contributors = sorted(answered, key=_rank)
    return result
