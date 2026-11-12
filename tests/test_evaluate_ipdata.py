"""The evaluation of the IP data sources (`scripts/evaluate_ipdata.py`): the anchors it uses and how it scores an answer.

The numbers it produces are in `ipdata/measured.py`; running it needs the databases, so this checks the parts that do not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ipping_backend.ipdata.records import Record

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_ipdata as ev  # noqa: E402  (a script, not a package)

pytestmark = pytest.mark.skipif(not ev.DATA.exists(), reason="the anchor snapshot is not present")

HERE = ev.Target("203.0.113.9", 4, "NO", 59.91, 10.75, 2116, "test")  # Oslo


def test_the_anchor_snapshot_is_whole() -> None:
    targets = ev.load()
    assert len(targets) > 1500
    assert {t.half for t in targets} == {"calibration", "test"}
    halves = [sum(t.half == h for t in targets) for h in ("calibration", "test")]
    assert abs(halves[0] - halves[1]) < 0.1 * len(targets)  # the halves are of a size
    assert any(t.version == 6 for t in targets) and any(t.version == 4 for t in targets)
    assert len({t.address for t in targets}) == len(targets)  # an address is asked about once
    for t in targets:
        assert len(t.country) == 2 and -90 <= t.lat <= 90 and -180 <= t.lon <= 180, t


def test_both_addresses_of_an_anchor_are_in_the_same_half() -> None:
    """A source must not be fitted on one address of a host and tested on the other."""
    halves: dict[int, set[str]] = {}
    for t in ev.load():
        halves.setdefault(t.anchor, set()).add(t.half)
    assert len(halves) > 1000 and all(len(h) == 1 for h in halves.values())
    assert any(
        sum(t.anchor == a for t in ev.load()) == 2 for a in list(halves)[:50]
    )  # some anchors have both an IPv4 and an IPv6


def test_a_country_is_right_or_wrong_or_not_said() -> None:
    assert ev.right("country", Record(country="NO"), HERE) is True
    assert (
        ev.right("country", Record(country="no"), HERE) is True
    )  # the case of a code does not matter
    assert ev.right("country", Record(country="SE"), HERE) is False
    assert (
        ev.right("country", Record(asn=2116), HERE) is None
    )  # a table of AS numbers says nothing about it


def test_a_location_is_right_within_the_distance_the_vote_counts_as_one_place() -> None:
    from ipping_backend.ipdata.consensus import SAME_PLACE_KM

    assert ev.right("location", Record(latitude=59.95, longitude=10.76), HERE) is True  # 4 km away
    assert (
        ev.right("location", Record(latitude=60.39, longitude=5.32), HERE) is False
    )  # Bergen, 300 km away
    assert (
        SAME_PLACE_KM == 60.0
    )  # the evaluation and the vote use one distance: change both together
    assert ev.right("location", Record(country="NO"), HERE) is None


def test_an_as_number_is_compared_with_the_one_the_anchor_registered() -> None:
    assert ev.right("asn", Record(asn=2116), HERE) is True
    assert ev.right("asn", Record(asn=3), HERE) is False
    assert (
        ev.right("asn", Record(asn=2116), ev.Target("::1", 6, "NO", 0, 0, None, "test")) is None
    )  # no AS registered


def test_a_source_is_never_scored_zero_or_perfect_on_little_data() -> None:
    assert ev.rate(0, 0) == 0.5
    assert 0.5 < ev.rate(10, 10) < 1 and 0 < ev.rate(10, 0) < 0.5
    assert ev.rate(1000, 900) == pytest.approx(901 / 1002)


def test_a_tally_counts_answers_and_right_answers_by_half() -> None:
    other = ev.Target("198.51.100.7", 4, "SE", 59.33, 18.07, 1, "calibration")
    records = {
        HERE.address: {"dbip": Record(country="NO"), "ipdeny": Record(country="SE")},
        other.address: {"dbip": Record(country="DK")},
    }
    counts, asked = ev.tally(["dbip", "ipdeny"], [HERE, other], records, "test")
    assert asked == 1
    assert counts[("dbip", "country")] == [1, 1] and counts[("ipdeny", "country")] == [1, 0]
    counts, asked = ev.tally(["dbip", "ipdeny"], [HERE, other], records, "calibration")
    assert (
        asked == 1 and counts[("dbip", "country")] == [1, 0] and ("ipdeny", "country") not in counts
    )
