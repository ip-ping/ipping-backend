"""The location estimator against synthetic ground truth.

Round-trip times are generated from known target positions with a chosen path inflation and jitter,
so these tests check the arithmetic and, above all, that the estimator says "I don't know" when it
should. They are not a calibration against the real internet: `test_real_measurements.py` is.
"""

from __future__ import annotations

import random
import statistics

import pytest

from ipping_backend.geolocation import estimate as E
from ipping_backend.geolocation.gazetteer import City, cities

BY_NAME = {(c.name, c.country): c for c in cities()}


def city(name: str, country: str) -> City:
    return BY_NAME[(name, country)]


# The fixed delay of a synthetic path, like what real paths show. Kept apart from the model's own
# constant so changing the model cannot quietly change what these tests generate.
SYNTHETIC_BASE_MS = 6.0


def observe(
    target: City, nodes: list[City], inflation: float, rnd: random.Random, *, verified: bool = True
) -> list[E.Observation]:
    result = []
    for n in nodes:
        d = E.haversine_km(target.latitude, target.longitude, n.latitude, n.longitude)
        rtt = SYNTHETIC_BASE_MS + inflation * d / 100 + rnd.uniform(0, 3)
        result.append(E.Observation(n.name, n.name, n.latitude, n.longitude, rtt, verified))
    return result


HUBS = [
    city(*c)
    for c in [
        ("Frankfurt am Main", "DE"),
        ("London", "GB"),
        ("New York City", "US"),
        ("Chicago", "US"),
        ("Los Angeles", "US"),
        ("Sao Paulo", "BR"),
        ("Singapore", "SG"),
        ("Tokyo", "JP"),
        ("Sydney", "AU"),
        ("Johannesburg", "ZA"),
        ("Mumbai", "IN"),
        ("Dubai", "AE"),
    ]
]


def error_km(target: City, result: E.Estimate) -> float:
    assert result.likely is not None
    return E.haversine_km(
        target.latitude, target.longitude, result.likely.latitude, result.likely.longitude
    )


def test_the_gazetteer_loads() -> None:
    assert len(cities()) > 3000
    assert {c.country for c in cities()} >= {"US", "DE", "JP", "BR", "ZA", "AU"}


# The further real paths are from the typical 1.7x, the looser the answer. The ceilings say how much.
@pytest.mark.parametrize(
    ("inflation", "median_ceiling_km", "worst_ceiling_km"),
    [(1.3, 500, 1500), (1.7, 500, 1500), (2.4, 1000, 2500)],
)
def test_finds_cities_near_the_nodes(
    inflation: float, median_ceiling_km: float, worst_ceiling_km: float
) -> None:
    targets = [
        city(*t)
        for t in [
            ("Paris", "FR"),
            ("Amsterdam", "NL"),
            ("Berlin", "DE"),
            ("Madrid", "ES"),
            ("Toronto", "CA"),
            ("Boston", "US"),
            ("Seoul", "KR"),
            ("Melbourne", "AU"),
        ]
    ]
    errors = []
    for i, target in enumerate(targets):
        result = E.estimate(observe(target, HUBS, inflation, random.Random(i)))
        assert result.status == "estimated", result.summary
        errors.append(error_km(target, result))
    assert statistics.median(errors) < median_ceiling_km
    assert max(errors) < worst_ceiling_km


def test_a_target_in_a_node_city_is_placed_there() -> None:
    target = city("Frankfurt am Main", "DE")
    result = E.estimate(observe(target, HUBS, 1.7, random.Random(1)))
    assert result.likely is not None and result.likely.name == "Frankfurt am Main"


def test_a_random_population_of_scenarios_and_what_each_label_promises() -> None:
    rnd = random.Random(2026)
    pool = cities()
    outcomes: list[tuple[str, str | None, float | None]] = []
    for _ in range(250):
        nodes = rnd.sample(pool[:600], rnd.choice([4, 6, 8, 12, 16, 24, 32]))
        target = rnd.choice(pool)
        if any(n is target for n in nodes):
            continue
        observations = observe(
            target, nodes, rnd.uniform(1.2, 2.8), rnd, verified=rnd.random() < 0.7
        )
        result = E.estimate(observations)
        outcomes.append(
            (result.status, result.strength, error_km(target, result) if result.likely else None)
        )

    # Consistent data is never called contradictory: the physical limits only exclude what is impossible.
    assert all(status != "inconclusive" for status, _, _ in outcomes)
    estimated = [o for o in outcomes if o[0] == "estimated"]
    assert len(estimated) > 100

    # The label is a promise about how wrong the answer can be.
    for label, ceiling in (("strong", 500), ("moderate", 500)):
        errors = [e for _, s, e in estimated if s == label and e is not None]
        assert all(e <= ceiling for e in errors), (label, sorted(errors)[-3:])
    low = [e for _, s, e in estimated if s == "low" and e is not None]
    assert statistics.median(low) < 800


def test_two_far_apart_nodes_both_very_close_still_give_an_estimate_and_say_they_disagree() -> None:
    """The signature of anycast, a rerouted path or a bad measurement, though it proves none of them."""
    nodes = [city("Frankfurt am Main", "DE"), city("Singapore", "SG"), city("Sydney", "AU")]
    observations = [
        E.Observation(n.name, n.name, n.latitude, n.longitude, 2.0 + i * 0.1, True)
        for i, n in enumerate(nodes)
    ]
    result = E.estimate(observations)
    # an answer all the same: the nearest node and the nodes that agree with it, with the disagreement said
    assert result.status == "estimated" and result.likely is not None and result.conflicting
    assert result.strength == "low" and result.accuracy is not None
    assert result.likely.name == "Frankfurt am Main"
    assert any("anycast" in w for w in result.warnings) and any(
        "apart" in w for w in result.warnings
    )
    # the nodes are 10,000 km apart, so one place cannot be near all of them, however close the nearest was
    assert result.disagreement_km > 9000
    agreeing = E.estimate(observations[:1])
    assert agreeing.accuracy is not None and result.accuracy is not None
    assert result.accuracy.headline_pct < agreeing.accuracy.headline_pct <= 99
    assert result.accuracy.headline_pct <= 20


def test_too_few_nodes_still_give_an_estimate_with_a_warning() -> None:
    result = E.estimate(observe(city("Paris", "FR"), HUBS[:2], 1.7, random.Random(0)))
    assert result.status == "estimated" and result.likely is not None
    assert any("Only 2 node" in w for w in result.warnings)
    one = E.estimate(observe(city("Paris", "FR"), HUBS[:1], 1.7, random.Random(0)))
    assert one.status == "estimated" and one.likely is not None and one.accuracy is not None
    assert any("Only 1 node" in w for w in one.warnings)


def test_only_no_node_at_all_means_no_estimate() -> None:
    result = E.estimate([])
    assert (
        result.status == "insufficient_evidence"
        and result.likely is None
        and result.accuracy is None
    )
    assert (
        "No node with a known position" in result.summary
        or "no node with a known position" in result.summary
    )


def test_when_even_the_closest_node_is_far_away_there_is_a_guess_and_it_says_so() -> None:
    nodes = HUBS[:4]
    observations = [
        E.Observation(n.name, n.name, n.latitude, n.longitude, 140.0 + i, True)
        for i, n in enumerate(nodes)
    ]
    result = E.estimate(observations)
    assert result.status == "estimated" and result.likely is not None and result.strength == "low"
    assert any("treat this as a guess" in w for w in result.warnings)
    assert result.closest is not None and result.closest.observation.rtt_ms == 140.0
    assert result.accuracy is not None and result.accuracy.headline_pct <= 10


def test_a_far_closest_node_only_adds_a_warning() -> None:
    nodes = HUBS[:4]
    inside = [
        E.Observation(n.name, n.name, n.latitude, n.longitude, E.FAR_CLOSEST_RTT_MS - 1 + i, True)
        for i, n in enumerate(nodes)
    ]
    outside = [
        E.Observation(n.name, n.name, n.latitude, n.longitude, E.FAR_CLOSEST_RTT_MS + 1 + i, True)
        for i, n in enumerate(nodes)
    ]
    assert not any("guess" in w for w in E.estimate(inside).warnings)
    far = E.estimate(outside)
    assert far.status == "estimated" and any("guess" in w for w in far.warnings)


def test_nodes_that_contradict_each_other_at_any_real_speed_are_flagged_not_refused() -> None:
    """3,600 km apart and both 14 ms away: light in vacuum would allow it (4,200 km), no path does."""
    a = E.Observation("a", "Madrid", 40.42, -3.70, 14.0, True)
    b = E.Observation("b", "Cairo", 30.04, 31.24, 14.0, True)
    c = E.Observation("c", "Rome", 41.9, 12.5, 14.0, True)
    assert E.haversine_km(a.latitude, a.longitude, b.latitude, b.longitude) > 3300
    result = E.estimate([a, b, c])
    assert result.status == "estimated" and result.conflicting and result.likely is not None


def test_nodes_that_agree_are_not_flagged() -> None:
    result = E.estimate(observe(city("Paris", "FR"), HUBS, 1.7, random.Random(2)))
    assert not result.conflicting


def test_the_closest_node_gives_a_physical_bound_not_a_guess() -> None:
    result = E.estimate(observe(city("Amsterdam", "NL"), HUBS, 1.7, random.Random(3)))
    assert result.closest is not None
    c = result.closest
    assert c.max_distance_km == pytest.approx(150 * c.observation.rtt_ms)
    assert c.typical_distance_km == pytest.approx(100 * c.observation.rtt_ms)
    # the target really is inside that physical limit
    d = E.haversine_km(52.37, 4.9, c.observation.latitude, c.observation.longitude)
    assert d <= c.max_distance_km


def test_unverified_coordinates_are_called_out_and_weigh_less() -> None:
    target = city("Paris", "FR")
    result = E.estimate(observe(target, HUBS, 1.7, random.Random(5), verified=False))
    assert any("not been verified" in w for w in result.warnings)
    assert result.strength != "strong"


def test_alternatives_are_distinct_regions() -> None:
    nodes = [city("Athens", "GR"), city("Riyadh", "SA"), city("Nairobi", "KE")]
    result = E.estimate(observe(city("Cairo", "EG"), nodes, 1.7, random.Random(8)))
    assert result.status == "estimated"
    assert all(a.distance_km >= E.ALTERNATIVE_MIN_SEPARATION_KM - 1 for a in result.alternatives)
    assert len(result.alternatives) <= E.MAX_ALTERNATIVES


def test_the_answer_is_deterministic_and_limitations_always_travel_with_it() -> None:
    observations = observe(city("Seoul", "KR"), HUBS, 1.7, random.Random(9))
    a, b = E.estimate(observations), E.estimate(list(reversed(observations)))
    assert a.likely == b.likely and a.strength == b.strength
    assert len(a.limitations) >= 3 and any("anycast" in t.lower() for t in a.limitations)


def test_every_estimate_carries_how_accurate_answers_like_it_have_been() -> None:
    result = E.estimate(observe(city("Paris", "FR"), HUBS, 1.7, random.Random(2)))
    assert result.accuracy is not None
    acc = result.accuracy
    assert acc.headline_km == 500 and acc.by_radius[500] == acc.headline_pct
    assert sorted(acc.by_radius) == [100, 300, 500, 1000, 2000]
    assert all(1 <= pct <= 99 for pct in acc.by_radius.values())
    # a wider radius is never less likely to hold the truth
    values = [acc.by_radius[r] for r in sorted(acc.by_radius)]
    assert values == sorted(values)


def test_accuracy_never_rises_as_the_closest_node_gets_further_away() -> None:
    from ipping_backend.geolocation.accuracy import RADII_KM, accuracy_for

    previous = {r: 100 for r in RADII_KM}
    for ms in (0.5, 1, 3, 7, 12, 18, 25, 35, 50, 70, 100, 200, 5000):
        now = accuracy_for(ms).by_radius
        assert all(now[r] <= previous[r] for r in RADII_KM), (ms, now, previous)
        previous = now
    # a node next door pins it down, one on the other side of the world says little
    assert accuracy_for(1).headline_pct >= 95 and accuracy_for(300).headline_pct <= 10
