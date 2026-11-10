"""Check the location estimator against real measurements of targets whose position is known.

    uv run python scripts/evaluate_location.py                    # every target, a quick run
    uv run python scripts/evaluate_location.py --fleets 30 --set test --json report.json

The data is `calibration/atlas-mesh.json.gz` (see `scripts/collect_atlas.py`): for each of ~60
RIPE Atlas anchors around the world, the round trip measured from ~1000 other anchors. A "fleet" is a
random handful of those sources, standing in for the nodes of an ipping deployment. For every fleet
the estimator is run and its answer is compared with the target's true position.

Targets are split in two, by a fixed rule, into a **calibration** half (constants may be tuned on
it) and a **test** half (only looked at to report). Quote test-half numbers.

The "floor" is the distance from the truth to the nearest city in the gazetteer: the estimator can
only name cities of 100,000 people or more, so even a perfect answer is that far off for a target in
a small town.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

from ipping_backend.geolocation import accuracy as A
from ipping_backend.geolocation import estimate as E
from ipping_backend.geolocation.gazetteer import cities

DATA = Path(__file__).resolve().parent.parent / "calibration" / "atlas-mesh.json.gz"
ANYCAST = Path(__file__).resolve().parent.parent / "calibration" / "atlas-anycast.json.gz"
FLEET_SIZES = (1, 2, 4, 8, 16, 32)
THRESHOLDS = (100, 300, 500, 1000)
MIN_SOURCES = 100


def load() -> tuple[list[dict], list[list]]:
    with gzip.open(DATA, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    return document["targets"], document["anchors"]


def split_of(index: int) -> str:
    """Alternate targets between the halves, in anchor order, so both span the world."""
    return "calibration" if index % 2 == 0 else "test"


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def run(which: str, fleets: int, seed: int) -> list[dict]:
    targets, anchors = load()
    gazetteer = cities()
    rows: list[dict] = []
    usable = [t for t in targets if len(t["rtt"]) >= MIN_SOURCES]
    for position, target in enumerate(sorted(usable, key=lambda t: t["anchor"])):
        half = split_of(position)
        if which not in ("all", half):
            continue
        country, city, lat, lon = anchors[target["anchor"]]
        floor = min(E.haversine_km(lat, lon, c.latitude, c.longitude) for c in gazetteer)
        rng = random.Random(f"{seed}-{target['anchor']}")  # noqa: S311 - sampling, not security
        for size in FLEET_SIZES:
            for _ in range(fleets):
                picked = rng.sample(target["rtt"], size)
                observations = [
                    E.Observation(
                        node_id=str(source),
                        name=anchors[source][1],
                        latitude=anchors[source][2],
                        longitude=anchors[source][3],
                        rtt_ms=rtt,
                        verified=True,
                    )
                    for source, rtt in picked
                ]
                result = E.estimate(observations, gazetteer)
                error = (
                    E.haversine_km(lat, lon, result.likely.latitude, result.likely.longitude)
                    if result.likely
                    else None
                )
                rows.append(
                    {
                        "half": half,
                        "target": f"{city}, {country}",
                        "size": size,
                        "status": result.status,
                        "strength": result.strength,
                        "conflicting": result.conflicting,
                        "error_km": error,
                        "floor_km": floor,
                        "nearest_ms": min(r for _, r in picked),
                    }
                )
    return rows


def share(values: list[float], limit: float) -> str:
    return f"{100 * sum(v <= limit for v in values) / len(values):4.0f}%"


NEAREST_BUCKETS = (5, 10, 20, 40, 80, 1e9)


def bucket(ms: float) -> str:
    low = 0
    for high in NEAREST_BUCKETS:
        if ms <= high:
            return f"{low:>3}-{'∞' if high > 1e6 else int(high):>3} ms"
        low = int(high)
    return "?"


def table(rows: list[dict], key: str) -> str:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if key == "nearest":
            label = bucket(row["nearest_ms"]) if row["status"] == "estimated" else "no estimate"
        else:
            label = row[key] if row["status"] == "estimated" or key != "strength" else row["status"]
        groups[str(label)].append(row)
    lines = [
        f"{key:<22} {'runs':>6}  "
        + "  ".join(f"≤{t:>4} km" for t in THRESHOLDS)
        + f"  {'median':>8} {'p90':>8} {'floor':>7}"
    ]
    order = ["strong", "moderate", "low", "inconclusive", "insufficient_evidence"]
    for label in sorted(groups, key=lambda k: (order.index(k) if k in order else -1, k)):
        group = groups[label]
        errors = [r["error_km"] for r in group if r["error_km"] is not None]
        if not errors:
            lines.append(f"{label:<22} {len(group):>6}  (no location given)")
            continue
        lines.append(
            f"{label:<22} {len(group):>6}  "
            + "  ".join(f"{share(errors, t):>8}" for t in THRESHOLDS)
            + f"  {statistics.median(errors):>6.0f}km {percentile(errors, 0.9):>6.0f}km "
            + f"{statistics.median(r['floor_km'] for r in group if r['error_km'] is not None):>5.0f}km"
        )
    return "\n".join(lines)


def anycast(fleets: int, seed: int) -> None:
    """Anycast targets have no single place. A good estimator says so (`conflicting`) while still answering."""
    with gzip.open(ANYCAST, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    anchors = document["anchors"]
    gazetteer = cities()
    print(
        f"model {E.MODEL_VERSION}: anycast root servers, {fleets} random fleets per size and server\n"
    )
    print(
        f"{'target':<22} {'fleet':>5}  {'disagree':>12} {'no estimate':>12} {'agree':>10}   answered as (strength)"
    )
    totals: dict[str, int] = defaultdict(int)
    for target in document["targets"]:
        rng = random.Random(f"{seed}-{target['name']}")  # noqa: S311 - sampling, not security
        for size in FLEET_SIZES:
            outcomes: dict[str, int] = defaultdict(int)
            for _ in range(fleets):
                observations = [
                    E.Observation(str(s), anchors[s][1], anchors[s][2], anchors[s][3], rtt, True)
                    for s, rtt in rng.sample(target["rtt"], size)
                ]
                result = E.estimate(observations, gazetteer)
                kind = "disagree" if result.conflicting else result.status
                outcomes[kind] += 1
                totals[kind] += 1
                if result.likely is not None:
                    outcomes[f"strength:{result.strength}"] += 1
                    totals[f"strength:{result.strength}"] += 1
            strengths = ", ".join(
                f"{k.split(':')[1]} {v}"
                for k, v in sorted(outcomes.items())
                if k.startswith("strength:")
            )
            print(
                f"{target['name']:<22} {size:>5}  {outcomes['disagree']:>12} {outcomes['insufficient_evidence']:>12} "
                f"{outcomes['estimated']:>10}   {strengths}"
            )
    runs = sum(v for k, v in totals.items() if not k.startswith("strength:"))
    print(f"\nall {runs} runs: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items())))


def _pav(values: list[float], weights: list[float]) -> list[float]:
    """The closest non-increasing fit (pool adjacent violators): accuracy never rises as the closest node gets further."""
    blocks: list[list[float]] = []  # [value, weight, count]
    for v, w in zip(values, weights, strict=True):
        blocks.append([v, w, 1])
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            (v2, w2, c2), (v1, w1, c1) = blocks[-1], blocks[-2]
            blocks[-2:] = [[(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2]]
    out: list[float] = []
    for v, _, c in blocks:
        out.extend([v] * int(c))
    return out


def _bucket_of(ms: float) -> int:
    return min(next(i for i, edge in enumerate(A.EDGES_MS) if ms <= edge), len(A.EDGES_MS) - 1)


def fit_accuracy(fleets: int, seed: int) -> None:
    """Fit `accuracy.TABLE` on the calibration half, print it, and report how it does on the test half."""
    fitted = run("calibration", fleets, seed)
    held_out = run("test", fleets, seed)
    n_buckets = len(A.EDGES_MS)
    columns: list[list[float]] = []
    for radius in A.RADII_KM:
        hits = [0] * n_buckets
        total = [0] * n_buckets
        for row in fitted:
            if row["error_km"] is None:
                continue
            i = _bucket_of(row["nearest_ms"])
            total[i] += 1
            hits[i] += row["error_km"] <= radius
        # (hits + 1) / (total + 2): a bucket with few answers is pulled towards "no idea", not to 0 or 100
        rate = [100 * (hits[i] + 1) / (total[i] + 2) for i in range(n_buckets)]
        columns.append(_pav(rate, [total[i] + 2 for i in range(n_buckets)]))
    table = [
        tuple(max(A.MIN_PCT, min(A.MAX_PCT, round(columns[c][i]))) for c in range(len(A.RADII_KM)))
        for i in range(n_buckets)
    ]
    counts = [
        sum(1 for r in fitted if r["error_km"] is not None and _bucket_of(r["nearest_ms"]) == i)
        for i in range(n_buckets)
    ]
    print(
        f"model {E.MODEL_VERSION}: fitted on {len(fitted)} calibration answers, checked on {len(held_out)} test answers\n"
    )
    print("EDGES_MS = " + repr(A.EDGES_MS).replace("inf", 'float("inf")'))
    print("TABLE = (")
    for i, row in enumerate(table):
        print(
            f"    {row!r},  # closest node up to {A.EDGES_MS[i]} ms ({counts[i]} calibration answers)"
        )
    print(")\n")
    print(
        f"{'closest node':<14} {'test runs':>9}   "
        + "   ".join(f"≤{r:>4} km (said/was)" for r in A.RADII_KM)
    )
    worst = 0.0
    for i in range(n_buckets):
        rows = [
            r for r in held_out if r["error_km"] is not None and _bucket_of(r["nearest_ms"]) == i
        ]
        if not rows:
            continue
        low = 0 if i == 0 else A.EDGES_MS[i - 1]
        cells = []
        for c, radius in enumerate(A.RADII_KM):
            actual = 100 * sum(r["error_km"] <= radius for r in rows) / len(rows)
            worst = max(worst, abs(actual - table[i][c]) * len(rows))
            cells.append(f"{table[i][c]:>5.0f}% / {actual:>4.0f}%     ")
        label = f"{low:g}-{'∞' if A.EDGES_MS[i] == float('inf') else int(A.EDGES_MS[i])} ms"
        print(f"{label:<14} {len(rows):>9}   " + "   ".join(cells))
    errors = [
        (c, abs(table[_bucket_of(r["nearest_ms"])][c] / 100 - (r["error_km"] <= radius)))
        for r in held_out
        if r["error_km"] is not None
        for c, radius in enumerate(A.RADII_KM)
    ]
    brier = sum(
        (table[_bucket_of(r["nearest_ms"])][c] / 100 - (r["error_km"] <= radius)) ** 2
        for r in held_out
        if r["error_km"] is not None
        for c, radius in enumerate(A.RADII_KM)
    ) / max(1, len(errors))
    print(f"\nBrier score on the test half (0 is perfect, 0.25 is a coin toss): {brier:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=("calibration", "test", "all"), default="all")
    parser.add_argument("--fleets", type=int, default=8, help="random fleets per target and size")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--anycast", action="store_true", help="evaluate pings to anycast servers")
    parser.add_argument(
        "--fit-accuracy", action="store_true", help="fit geolocation/accuracy.py's table"
    )
    args = parser.parse_args()
    if args.fit_accuracy:
        fit_accuracy(args.fleets, args.seed)
        return 0
    if args.anycast:
        anycast(args.fleets, args.seed)
        return 0

    started = time.time()
    rows = run(args.set, args.fleets, args.seed)
    targets = len({r["target"] for r in rows})
    print(
        f"model {E.MODEL_VERSION}: {len(rows)} estimates, {targets} targets, set={args.set}, "
        f"{time.time() - started:.0f}s\n"
    )

    for label, subset in (
        ("all fleet sizes", rows),
        *((f"fleets of {n}", [r for r in rows if r["size"] == n]) for n in FLEET_SIZES),
    ):
        print(f"== {label}")
        print(table(subset, "strength"))
        print()
    print("== by the closest node's round trip (estimated runs)")
    print(table(rows, "nearest"))
    print()
    estimated = [r for r in rows if r["status"] == "estimated"]
    errors = [r["error_km"] for r in estimated]
    print(
        "status of all runs:",
        {s: sum(r["status"] == s for r in rows) for s in sorted({r["status"] for r in rows})},
    )
    if errors:
        print(
            f"every estimated run: median {statistics.median(errors):.0f} km, p90 {percentile(errors, 0.9):.0f} km; "
            f"median floor {statistics.median(r['floor_km'] for r in estimated):.0f} km"
        )
    worst = sorted(estimated, key=lambda r: -r["error_km"])[:6]
    print("\nlargest errors:")
    for r in worst:
        print(
            f"  {r['error_km']:7.0f} km  {r['target']:<28} fleet {r['size']:>2} {r['strength']:<9} nearest {r['nearest_ms']:.1f} ms"
        )
    if args.json:
        args.json.write_text(json.dumps(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
