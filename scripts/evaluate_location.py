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

from ipping_backend.geolocation import estimate as E
from ipping_backend.geolocation.gazetteer import cities

DATA = Path(__file__).resolve().parent.parent / "calibration" / "atlas-mesh.json.gz"
ANYCAST = Path(__file__).resolve().parent.parent / "calibration" / "atlas-anycast.json.gz"
FLEET_SIZES = (4, 8, 16, 32)
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
    """Anycast targets have no single place. A good estimator says so instead of picking one."""
    with gzip.open(ANYCAST, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    anchors = document["anchors"]
    gazetteer = cities()
    print(
        f"model {E.MODEL_VERSION}: anycast root servers, {fleets} random fleets per size and server\n"
    )
    print(
        f"{'target':<22} {'fleet':>5}  {'inconclusive':>12} {'insufficient':>12} {'estimated':>10}   estimated as (strength)"
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
                outcomes[result.status] += 1
                totals[result.status] += 1
                if result.status == "estimated":
                    outcomes[f"strength:{result.strength}"] += 1
                    totals[f"strength:{result.strength}"] += 1
            strengths = ", ".join(
                f"{k.split(':')[1]} {v}"
                for k, v in sorted(outcomes.items())
                if k.startswith("strength:")
            )
            print(
                f"{target['name']:<22} {size:>5}  {outcomes['inconclusive']:>12} {outcomes['insufficient_evidence']:>12} "
                f"{outcomes['estimated']:>10}   {strengths}"
            )
    runs = sum(v for k, v in totals.items() if not k.startswith("strength:"))
    print(f"\nall {runs} runs: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items())))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=("calibration", "test", "all"), default="all")
    parser.add_argument("--fleets", type=int, default=8, help="random fleets per target and size")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--anycast", action="store_true", help="evaluate pings to anycast servers")
    args = parser.parse_args()
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
