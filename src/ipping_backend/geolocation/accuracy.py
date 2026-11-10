"""How accurate an estimate usually is, measured on real targets whose position is known.

The one thing that decides it most is how close the nearest node was: a node 3 ms away pins the target to its
neighbourhood, one 90 ms away says little. So the table is indexed by the round trip of the closest node, and
each row says what share of the answers made with a closest node that far away were within 100, 300, 500, 1,000 and 2,000 km of the truth.

The table is **generated**, not written by hand: `uv run python scripts/evaluate_location.py --fit-accuracy` fits it on the
calibration half of the RIPE Atlas data (`calibration/atlas-mesh.json.gz`), makes it never rise with the round trip, and prints
this file's `TABLE` and how it does on the test half, which is what the README quotes. Re-fit it whenever `MODEL_VERSION` changes.

It is a rate for answers like this one, not a probability for this address: it knows nothing about this address that the
closest node's round trip does not say. Nodes whose positions are wrong, an address that is anycast, or a path that is far longer than
usual make the real figure lower, and the estimate says so in its warnings.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

RADII_KM = (100, 300, 500, 1000, 2000)
HEADLINE_KM = 500  # "within about the size of a region": the figure shown first

# (upper edge of the closest node's round trip in ms, percent within each of RADII_KM): generated, see above
EDGES_MS: tuple[float, ...] = (
    2.0,
    5.0,
    10.0,
    15.0,
    20.0,
    30.0,
    40.0,
    60.0,
    80.0,
    120.0,
    float("inf"),
)
TABLE: tuple[tuple[int, ...], ...] = (
    (97, 99, 99, 99, 99),  # closest node up to 2 ms (172 calibration answers)
    (25, 99, 99, 99, 99),  # up to 5 ms (158)
    (9, 61, 96, 99, 99),  # up to 10 ms (393)
    (9, 42, 72, 97, 99),  # up to 15 ms (412)
    (6, 27, 49, 81, 99),  # up to 20 ms (383)
    (3, 12, 28, 54, 91),  # up to 30 ms (648)
    (3, 12, 19, 47, 74),  # up to 40 ms (334)
    (3, 10, 17, 31, 56),  # up to 60 ms (506)
    (3, 10, 15, 25, 49),  # up to 80 ms (277)
    (2, 5, 9, 18, 46),  # up to 120 ms (488)
    (2, 4, 7, 13, 25),  # slower than that (1269)
)
MIN_PCT, MAX_PCT = 1, 99  # never claim certainty either way


@dataclass(frozen=True, slots=True)
class Accuracy:
    """How often answers like this one were right, on real targets: the share within each radius."""

    headline_km: int
    headline_pct: int
    by_radius: dict[int, int]


def accuracy_for(nearest_ms: float) -> Accuracy:
    row = TABLE[min(bisect.bisect_left(EDGES_MS, nearest_ms), len(TABLE) - 1)]
    by_radius = {r: max(MIN_PCT, min(MAX_PCT, p)) for r, p in zip(RADII_KM, row, strict=True)}
    return Accuracy(HEADLINE_KM, by_radius[HEADLINE_KM], by_radius)
