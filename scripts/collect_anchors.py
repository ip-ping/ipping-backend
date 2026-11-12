"""Collect the addresses, networks and places of RIPE Atlas anchors: addresses whose place and network are known.

    uv run python scripts/collect_anchors.py        # writes calibration/atlas-anchors.json.gz

`scripts/evaluate_ipdata.py` asks every IP data source about these addresses and checks the answers against what the anchors'
hosts registered: the country and position of the data centre, and the AS number the address is announced from. Only public
data is used (no API key). The snapshot is committed, so evaluating needs no network but the databases themselves.

An anchor is a well-run host in a data centre. It is an *easier* target than the typical address (and the vendors know anchors
well), so accuracy found with it is an upper bound; see the README of the IP data sources.
Data: RIPE NCC, RIPE Atlas (https://atlas.ripe.net), used under the RIPE Atlas terms of use.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_atlas import API, pages  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "calibration" / "atlas-anchors.json.gz"


def collect(out: Path) -> int:
    rows = []
    for anchor in pages(
        f"{API}/anchors/?page_size=500&fields=id,ip_v4,as_v4,ip_v6,as_v6,country,city,geometry,"
        "is_disabled,date_decommissioned"
    ):
        if anchor["is_disabled"] or anchor["date_decommissioned"] or not anchor.get("geometry"):
            continue
        lon, lat = anchor["geometry"]["coordinates"]
        # [id, country, city, latitude, longitude, IPv4, AS of the IPv4, IPv6, AS of the IPv6]
        rows.append(
            [
                anchor["id"],
                anchor["country"],
                anchor["city"],
                round(lat, 3),
                round(lon, 3),
                anchor.get("ip_v4"),
                anchor.get("as_v4"),
                anchor.get("ip_v6"),
                anchor.get("as_v6"),
            ]
        )
    rows.sort(key=lambda row: row[0])
    document = {
        "source": "RIPE Atlas anchors (https://atlas.ripe.net/api/v2/anchors/)",
        "attribution": "RIPE NCC, RIPE Atlas, under its terms of use",
        "collected_utc": int(time.time()),
        "columns": [
            "id",
            "country",
            "city",
            "latitude",
            "longitude",
            "ipv4",
            "asn4",
            "ipv6",
            "asn6",
        ],
        "anchors": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as handle:
        json.dump(document, handle, separators=(",", ":"))
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    print(f"{collect(args.out)} anchors written to {args.out}")
