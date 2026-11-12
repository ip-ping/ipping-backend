"""Measure how often each IP data source is right, and whether the percentage of the vote means what it says.

    uv run python scripts/evaluate_ipdata.py --dir /var/lib/ipping-backend/geoip            # a report
    uv run python scripts/evaluate_ipdata.py --dir DIR --fit                                # and write ipdata/measured.py

The data is `calibration/atlas-anchors.json.gz` (see `scripts/collect_anchors.py`): ~1,100 RIPE Atlas anchors, each with an
IPv4 and often an IPv6 address, the country and position its host registered, and the AS number it is announced from. Every
source that has a database in `--dir` is asked about every address and its answer is compared with those:

* **country** is right when it is the anchor's country;
* **location** is right when the position is within `SAME_PLACE_KM` of the anchor's (the distance the vote counts as one place);
* **asn** is right when it is the AS number the anchor registered. This one is only shown, never fitted: the anchor registers the
  AS of its host, which is not always the AS the address is announced from (where three databases agree and the anchor says another,
  the databases are usually right), so the anchors are not ground truth for it.

Anchors are split in two by a fixed rule (every other one, in anchor order), as the location model's targets are: the
**calibration** half is what `--fit` takes the numbers from, the **test** half is only looked at, to report. Quote the test numbers.

Then the vote is run over the sources' answers, once with the priors and once with the measured numbers, and each answer's
percentage is checked against how often answers of that percentage were right (the Brier score: lower is better, 0.25 is a coin).

What this does not say: anchors are well-run hosts in data centres, easier than the typical address, and vendors know anchors (they
are a favourite reference for geolocation), so every accuracy here is an upper bound. Only what the data on disk can answer is measured:
a source that needs an account that was not given is not in the table and keeps its prior. Region, city, postal code, time zone
and organization have no ground truth in the anchors and keep their priors too.
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ipping_backend.geolocation.geoip import GeoIp
from ipping_backend.ipdata import consensus as C
from ipping_backend.ipdata.catalog import CATALOG
from ipping_backend.ipdata.options import IpDataOptions
from ipping_backend.ipdata.service import IpDataService

DATA = Path(__file__).resolve().parent.parent / "calibration" / "atlas-anchors.json.gz"
OUT = Path(__file__).resolve().parent.parent / "src" / "ipping_backend" / "ipdata" / "measured.py"
FIELDS = ("country", "location", "asn")
# What is fitted. The AS number is measured and shown but not fitted: where three databases agree on it and the anchor says another,
# the databases are usually right (the anchor registers the AS of its host, not the one the address is announced from: 7 % of the
# anchors), so the anchors are not ground truth for it.
FIT = ("country", "location")
BINS = ((0, 69), (70, 89), (90, 96), (97, 99))


@dataclass
class Target:
    address: str
    version: int
    country: str
    lat: float
    lon: float
    asn: int | None
    half: str
    anchor: int = 0


def load() -> list[Target]:
    with gzip.open(DATA, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    targets: list[Target] = []
    for index, (anchor, country, _city, lat, lon, v4, asn4, v6, asn6) in enumerate(
        document["anchors"]
    ):
        half = "calibration" if index % 2 == 0 else "test"
        if v4:
            targets.append(Target(v4, 4, country, lat, lon, asn4, half, anchor))
        if v6:
            targets.append(Target(v6, 6, country, lat, lon, asn6, half, anchor))
    return targets


def right(field: str, record, target: Target) -> bool | None:  # noqa: ANN001
    """Whether a record's answer for a field is right; `None` when it gave no answer for it."""
    if field == "country":
        return None if not record.country else record.country.upper() == target.country.upper()
    if field == "asn":
        return None if not record.asn or target.asn is None else record.asn == target.asn
    if record.latitude is None or record.longitude is None:
        return None
    return C._km((record.latitude, record.longitude), (target.lat, target.lon)) <= C.SAME_PLACE_KM


def right_voted(field: str, voted: C.Consensus, target: Target) -> bool | None:
    return right(field, voted.record, target)


def collect(service: IpDataService, targets: list[Target]):
    """Every ready source's record for every target: `{address: {source: Record}}`."""
    ready = [info.id for info in CATALOG if service.state(info.id)[0] == "ready"]
    records: dict[str, dict[str, object]] = {}
    for target in targets:
        found = {}
        for source in ready:
            record = service.local.lookup(source, target.address)
            if record is not None and not record.empty():
                found[source] = record
        records[target.address] = found
    return ready, records


def tally(ready, targets, records, half: str | None):
    """`{(source, field): [answered, right]}` and how many targets were asked."""
    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    asked = 0
    for target in targets:
        if half and target.half != half:
            continue
        asked += 1
        for source, record in records[target.address].items():
            for field in FIELDS:
                verdict = right(field, record, target)
                if verdict is None:
                    continue
                counts[(source, field)][0] += 1
                counts[(source, field)][1] += int(verdict)
    return counts, asked


def rate(answered: int, correct: int) -> float:
    """The share that was right, with one right and one wrong answer added (so no source is 0 % or 100 % on little data)."""
    return (correct + 1) / (answered + 2)


def brier(targets, records, half: str):
    """Calibration of the vote's percentages on a half: `{field: [(percent, was right), ...]}`."""
    rows: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    for target in targets:
        if target.half != half or not records[target.address]:
            continue
        voted = C.decide(records[target.address])
        for field in FIELDS:
            verdict = right_voted(field, voted, target)
            percent = voted.percent.get(field)
            if verdict is not None and percent is not None:
                rows[field].append((percent, verdict))
    return rows


def show_calibration(title: str, rows: dict[str, list[tuple[int, bool]]]) -> dict[str, float]:
    print(f"\n{title}")
    scores = {}
    for field in FIELDS:
        data = rows.get(field, [])
        if not data:
            continue
        score = sum((p / 100 - int(ok)) ** 2 for p, ok in data) / len(data)
        scores[field] = score
        cells = []
        for low, high in BINS:
            inside = [(p, ok) for p, ok in data if low <= p <= high]
            if inside:
                cells.append(
                    f"{low}-{high}%: {sum(ok for _, ok in inside) / len(inside):.1%} of {len(inside)}"
                )
        print(f"  {field:9s} Brier {score:.3f} over {len(data):4d}   " + "   ".join(cells))
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dir", type=Path, required=True, help="the directory the databases are in"
    )
    parser.add_argument("--fit", action="store_true", help=f"write the numbers to {OUT.name}")
    args = parser.parse_args()

    targets = load()
    service = IpDataService(lambda: GeoIp(directory=args.dir), args.dir, offline=True)
    service.apply(IpDataOptions(), {})
    ready, records = collect(service, targets)
    print(
        f"{len(targets)} addresses of RIPE Atlas anchors ({sum(t.version == 6 for t in targets)} of them IPv6); sources with data: {', '.join(ready)}"
    )

    both = {half: tally(ready, targets, records, half) for half in ("calibration", "test")}
    print("\nHow often each source is right (right / answered; answered / addresses asked)")
    print(f"  {'source':12s} {'field':9s} {'calibration':>22s} {'test':>22s}   prior")
    for source in ready:
        for field in FIELDS:
            cells = []
            for half in ("calibration", "test"):
                counts, asked = both[half]
                answered, correct = counts.get((source, field), (0, 0))
                cells.append(
                    f"{correct}/{answered} = {correct / answered:6.1%} ({answered / asked:4.0%})"
                    if answered
                    else "-"
                )
            prior = C.PRIORS.get(source, {}).get(field)
            print(
                f"  {source:12s} {field:9s} {cells[0]:>22s} {cells[1]:>22s}   {prior if prior is not None else ''}"
            )

    fitted: dict[str, dict[str, float]] = {}
    counts, _ = both["calibration"]
    for (source, field), (answered, correct) in sorted(counts.items()):
        if answered >= 30 and field in FIT:
            fitted.setdefault(source, {})[field] = round(
                min(C.CEILING, max(C.FLOOR, rate(answered, correct))), 3
            )

    before = show_calibration(
        "The vote with the priors, on the test half", brier(targets, records, "test")
    )
    saved = {s: dict(v) for s, v in C.MEASURED.items()}
    C.MEASURED.clear()
    C.MEASURED.update(fitted)
    C.RELIABILITY.clear()
    C.RELIABILITY.update(C.merged())
    after = show_calibration(
        "The vote with the measured numbers, on the test half", brier(targets, records, "test")
    )
    for field in before:
        if field in after:
            print(f"  {field}: Brier {before[field]:.3f} -> {after[field]:.3f}")
    C.MEASURED.clear()
    C.MEASURED.update(saved)
    C.RELIABILITY.clear()
    C.RELIABILITY.update(C.merged())

    if args.fit:
        lines = [
            '"""How often each source is right, MEASURED. GENERATED by `scripts/evaluate_ipdata.py --fit`: do not edit by hand.',
            "",
            f"From {sum(t.half == 'calibration' for t in targets)} addresses of the calibration half of the RIPE Atlas anchors",
            "(`calibration/atlas-anchors.json.gz`): the share of answers that were right (one right and one wrong answer added).",
            "Anchors are easier than the typical address, so these are upper bounds. A source or a field that is not here keeps its",
            'prior (`consensus.PRIORS`): a source that needs an account that was not available when this was made, or a field with no ground truth."""',
            "",
            "MEASURED: dict[str, dict[str, float]] = {",
        ]
        for source in sorted(fitted):
            body = ", ".join(
                f'"{field}": {value}' for field, value in sorted(fitted[source].items())
            )
            lines.append(f'    "{source}": {{{body}}},')
        lines.append("}")
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
