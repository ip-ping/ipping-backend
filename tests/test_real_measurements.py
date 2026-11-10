"""The location estimator against real measurements: RIPE Atlas anchors, which have known positions.

`calibration/atlas-mesh.json.gz` holds, for 60 anchors around the world, the round trip measured
from ~1,000 other anchors; `calibration/atlas-anycast.json.gz` holds pings to anycast root servers.
This is a regression guard on the numbers quoted in the README (`scripts/evaluate_location.py`
prints the full tables), with room to spare so it fails when the model gets worse, not when it
wobbles. It uses the held-out half of the targets and a small, fixed set of random fleets.
"""

from __future__ import annotations

import gzip
import json
import random
import statistics
import sys
from pathlib import Path

import pytest

from ipping_backend.geolocation import estimate as E

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_location as ev  # noqa: E402  (a script, not a package)

pytestmark = pytest.mark.skipif(
    not ev.DATA.exists() or not ev.ANYCAST.exists(), reason="the calibration data is not present"
)


@pytest.fixture(scope="module")
def unicast() -> list[dict]:
    return ev.run("test", 5, 99)


def test_strong_answers_are_close(unicast: list[dict]) -> None:
    strong = [r["error_km"] for r in unicast if r["strength"] == "strong"]
    assert len(strong) > 40
    assert sum(e <= 300 for e in strong) / len(strong) >= 0.95
    assert statistics.median(strong) < 100


def test_moderate_answers_are_within_a_region(unicast: list[dict]) -> None:
    moderate = [r["error_km"] for r in unicast if r["strength"] == "moderate"]
    assert len(moderate) > 30
    assert sum(e <= 500 for e in moderate) / len(moderate) >= 0.9
    assert sum(e <= 1000 for e in moderate) / len(moderate) >= 0.98


def test_answers_with_a_node_within_40_ms_are_usually_close(unicast: list[dict]) -> None:
    near = [r["error_km"] for r in unicast if r["nearest_ms"] <= 40]
    assert len(near) > 300
    assert statistics.median(near) < 450
    assert ev.percentile(near, 0.9) < 2000


def test_real_unicast_measurements_are_never_called_contradictory(unicast: list[dict]) -> None:
    assert not [r for r in unicast if r["conflicting"]]


def test_every_measurement_gets_an_estimate_and_a_far_one_is_labelled_low(
    unicast: list[dict],
) -> None:
    assert all(r["status"] == "estimated" and r["error_km"] is not None for r in unicast)
    far = [r for r in unicast if r["nearest_ms"] > 40]
    assert len(far) > 100, "random fleets often have no node near the target"
    assert all(r["strength"] == "low" for r in far)


def test_the_stated_accuracy_is_what_the_held_out_half_shows(unicast: list[dict]) -> None:
    """The table is fitted on the calibration half; on targets it never saw, what it says should come true."""
    from ipping_backend.geolocation.accuracy import accuracy_for

    for radius in (300, 500, 1000):
        stated = statistics.fmean(accuracy_for(r["nearest_ms"]).by_radius[radius] for r in unicast)
        actual = 100 * statistics.fmean(r["error_km"] <= radius for r in unicast)
        assert abs(stated - actual) < 7, (radius, stated, actual)
    # and it separates the answers that are close from the ones that are not
    near = [r for r in unicast if r["nearest_ms"] <= 10]
    far = [r for r in unicast if r["nearest_ms"] > 40]
    assert statistics.fmean(r["error_km"] <= 500 for r in near) > 0.9
    assert statistics.fmean(r["error_km"] <= 500 for r in far) < 0.25
    assert accuracy_for(5).headline_pct > 85 and accuracy_for(100).headline_pct < 20


def test_anycast_servers_are_flagged_as_having_no_single_place() -> None:
    with gzip.open(ev.ANYCAST, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    anchors = document["anchors"]
    outcomes = []
    for target in document["targets"]:
        rng = random.Random(target["name"])
        for _ in range(12):
            fleet = rng.sample(target["rtt"], 16)
            observations = [
                E.Observation(str(s), anchors[s][1], anchors[s][2], anchors[s][3], rtt, True)
                for s, rtt in fleet
            ]
            result = E.estimate(observations)
            outcomes.append(result.conflicting)
            assert (
                result.status == "estimated" and result.likely is not None
            )  # an answer, with the disagreement said
    assert sum(outcomes) / len(outcomes) >= 0.95
