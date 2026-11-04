"""The location estimator against synthetic ground truth.

Round-trip times are generated from known target positions with a chosen path inflation and jitter,
so these tests check the arithmetic and, above all, that the estimator says "I don't know" when it
should. They are not a calibration against the real internet.
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


def observe(
    target: City, nodes: list[City], inflation: float, rnd: random.Random, *, verified: bool = True
) -> list[E.Observation]:
    result = []
    for n in nodes:
        d = E.haversine_km(target.latitude, target.longitude, n.latitude, n.longitude)
        rtt = E.BASE_MS + inflation * d / 100 + rnd.uniform(0, 3)
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
            ("Dallas", "US"),
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
    assert len(estimated) > 150

    # The label is a promise about how wrong the answer can be.
    for label, ceiling in (("strong", 500), ("moderate", 500)):
        errors = [e for _, s, e in estimated if s == label and e is not None]
        assert all(e <= ceiling for e in errors), (label, sorted(errors)[-3:])
    low = [e for _, s, e in estimated if s == "low" and e is not None]
    assert statistics.median(low) < 800


def test_two_far_apart_nodes_both_very_close_means_no_single_place() -> None:
    """The signature of anycast, a rerouted path or a bad measurement, though it proves none of them."""
    nodes = [city("Frankfurt am Main", "DE"), city("Singapore", "SG"), city("Sydney", "AU")]
    observations = [
        E.Observation(n.name, n.name, n.latitude, n.longitude, 2.0, True) for n in nodes
    ]
    result = E.estimate(observations)
    assert result.status == "inconclusive"
    assert "single location could not be established" in result.summary
    assert "anycast" in result.summary and result.likely is None
    assert any("apart" in w for w in result.warnings)


def test_too_few_nodes_is_insufficient_evidence() -> None:
    result = E.estimate(observe(city("Paris", "FR"), HUBS[:2], 1.7, random.Random(0)))
    assert result.status == "insufficient_evidence" and result.likely is None
    assert "at least 3" in result.summary
    assert E.estimate([]).status == "insufficient_evidence"


def test_when_even_the_closest_node_is_far_away_it_says_so() -> None:
    nodes = HUBS[:4]
    observations = [
        E.Observation(n.name, n.name, n.latitude, n.longitude, 140.0 + i, True)
        for i, n in enumerate(nodes)
    ]
    result = E.estimate(observations)
    assert result.status == "insufficient_evidence"
    assert "leaves most of the world possible" in result.summary
    assert result.closest is not None and result.closest.observation.rtt_ms == 140.0


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
    nodes = [city("Madrid", "ES"), city("Lagos", "NG"), city("Nairobi", "KE")]
    result = E.estimate(observe(city("Cairo", "EG"), nodes, 1.7, random.Random(8)))
    assert result.status == "estimated"
    assert all(a.distance_km >= E.ALTERNATIVE_MIN_SEPARATION_KM - 1 for a in result.alternatives)
    assert len(result.alternatives) <= E.MAX_ALTERNATIVES


def test_the_answer_is_deterministic_and_limitations_always_travel_with_it() -> None:
    observations = observe(city("Seoul", "KR"), HUBS, 1.7, random.Random(9))
    a, b = E.estimate(observations), E.estimate(list(reversed(observations)))
    assert a.likely == b.likely and a.strength == b.strength
    assert len(a.limitations) >= 3 and any("anycast" in t.lower() for t in a.limitations)


def test_no_percentages_or_accuracy_claims_in_the_output() -> None:
    result = E.estimate(observe(city("Paris", "FR"), HUBS, 1.7, random.Random(2)))
    text = result.summary + " ".join(result.warnings)
    assert "%" not in text and "accurate" not in text.lower() and "confidence" not in text.lower()
