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


def test_every_answer_that_is_given_is_usually_close(unicast: list[dict]) -> None:
    estimated = [r["error_km"] for r in unicast if r["status"] == "estimated"]
    assert statistics.median(estimated) < 450
    assert ev.percentile(estimated, 0.9) < 2000


def test_real_unicast_measurements_are_never_called_contradictory(unicast: list[dict]) -> None:
    assert not [r for r in unicast if r["status"] == "inconclusive"]


def test_far_away_nodes_are_not_enough_to_name_a_place(unicast: list[dict]) -> None:
    withheld = [r for r in unicast if r["status"] == "insufficient_evidence"]
    assert all(r["error_km"] is None for r in withheld)
    assert withheld, "random fleets often have no node near the target"
    answered_far = [r for r in unicast if r["status"] == "estimated" and r["nearest_ms"] > 40]
    assert not answered_far


def test_anycast_servers_are_reported_as_having_no_single_place() -> None:
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
            outcomes.append(E.estimate(observations).status)
    assert outcomes.count("inconclusive") / len(outcomes) >= 0.95
