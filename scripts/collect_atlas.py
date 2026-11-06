"""Collect real round-trip times between RIPE Atlas anchors, to check the location model against.

    uv run python scripts/collect_atlas.py                  # 60 target anchors, writes calibration/atlas-mesh.json.gz
    uv run python scripts/collect_atlas.py --targets 100 --out /tmp/atlas.json.gz

RIPE Atlas anchors are well-run measurement hosts in data centres around the world, with known
positions, and every anchor pings every other one all the time (the "anchoring mesh"). That makes
them what the estimator needs and a hosted network lacks: targets of *known* location, measured
over real paths from many places. Only public data is used: no API key is needed.

The snapshot is committed (`calibration/`), so evaluating the model needs no network.
Data: RIPE NCC, RIPE Atlas (https://atlas.ripe.net), used under the RIPE Atlas terms of use.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://atlas.ripe.net/api/v2"
OUT = Path(__file__).resolve().parent.parent / "calibration" / "atlas-mesh.json.gz"
ANYCAST_OUT = Path(__file__).resolve().parent.parent / "calibration" / "atlas-anycast.json.gz"
# Built-in RIPE Atlas pings to root servers, which are anycast: answered from hundreds of places.
ROOT_SERVERS = {
    "k.root-servers.net": 1001,
    "f.root-servers.net": 1004,
    "i.root-servers.net": 1005,
    "m.root-servers.net": 1006,
    "l.root-servers.net": 1008,
    "e.root-servers.net": 1013,
    "j.root-servers.net": 1016,
}
USER_AGENT = "ipping-calibration/1.0 (+https://github.com/ip-ping/ipping-backend)"


def fetch(url: str, retries: int = 5) -> object:
    for attempt in range(retries):
        try:
            assert url.startswith("https://atlas.ripe.net/"), url
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - https only
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt == retries - 1:
                raise
            print(f"  retrying after {error}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def pages(url: str) -> list[dict]:
    found: list[dict] = []
    while url:
        page = fetch(url)
        assert isinstance(page, dict)
        found.extend(page["results"])
        url = page.get("next") or ""
    return found


def collect_anycast(out: Path) -> int:
    """Round trips from the anchors to anycast addresses. There is no single true location."""
    anchors = [
        a
        for a in pages(
            f"{API}/anchors/?page_size=500&fields=id,probe,geometry,country,city,is_disabled,"
            "date_decommissioned,ip_v4"
        )
        if not a["is_disabled"] and not a["date_decommissioned"] and a["geometry"]
    ]
    by_probe = {a["probe"]: a for a in anchors}
    stop = int(time.time()) - 900
    start = stop - 600
    used: dict[int, int] = {}
    rows = []
    probe_ids = sorted(by_probe)
    for name, msm in ROOT_SERVERS.items():
        # Every probe in the world pings the root servers, so ask for the anchors only, in chunks.
        results: list[dict] = []
        for i in range(0, len(probe_ids), 200):
            chunk = ",".join(str(p) for p in probe_ids[i : i + 200])
            page = fetch(
                f"{API}/measurements/{msm}/results/?start={start}&stop={stop}&format=json&probe_ids={chunk}"
            )
            assert isinstance(page, list)
            results.extend(page)
        best: dict[int, float] = {}
        for result in results:
            source = by_probe.get(result["prb_id"])
            if (
                source is None
                or result.get("af") != 4
                or not result.get("rcvd")
                or result.get("min", -1) <= 0
            ):
                continue
            best[source["id"]] = min(best.get(source["id"], 1e9), result["min"])
        print(f"{name}: {len(best)} anchors answered")
        rows.append(
            {
                "name": name,
                "rtt": [
                    [used.setdefault(s, len(used)), round(r, 3)] for s, r in sorted(best.items())
                ],
            }
        )
    by_id = {a["id"]: a for a in anchors}
    document = {
        "source": "RIPE Atlas built-in pings to root servers (anycast), anchors only",
        "attribution": "RIPE NCC, RIPE Atlas, https://atlas.ripe.net",
        "window_utc": [start, stop],
        "anchors": [
            [
                by_id[i]["country"],
                by_id[i]["city"],
                round(by_id[i]["geometry"]["coordinates"][1], 2),
                round(by_id[i]["geometry"]["coordinates"][0], 2),
            ]
            for i, _ in sorted(used.items(), key=lambda kv: kv[1])
        ],
        "targets": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(document, handle, separators=(",", ":"))
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20261106)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--anycast",
        action="store_true",
        help="collect pings to anycast root servers instead (for the contradictory-evidence check)",
    )
    args = parser.parse_args()
    if args.anycast:
        return collect_anycast(args.out if args.out != OUT else ANYCAST_OUT)

    print("listing anchors…")
    anchors = [
        a
        for a in pages(
            f"{API}/anchors/?page_size=500&fields=id,probe,geometry,country,city,is_disabled,"
            "date_decommissioned,ip_v4"
        )
        if not a["is_disabled"] and not a["date_decommissioned"] and a["geometry"] and a["ip_v4"]
    ]
    by_id = {a["id"]: a for a in anchors}
    by_probe = {a["probe"]: a for a in anchors}
    print(f"{len(anchors)} live anchors")

    print("listing mesh measurements…")
    mesh: dict[int, list[int]] = {}  # target anchor -> its ping meshes (one per IP version)
    for m in pages(f"{API}/anchor-measurements/?page_size=500"):
        if m["type"] == "ping" and m["is_active"]:
            target = int(m["target"].rstrip("/").rsplit("/", 1)[1])
            mesh.setdefault(target, []).append(int(m["measurement"].rstrip("/").rsplit("/", 1)[1]))

    # Spread the targets over countries: at most two per country, in a fixed random order.
    rng = random.Random(args.seed)  # noqa: S311 - sampling, not security
    candidates = [a for a in anchors if a["id"] in mesh]
    rng.shuffle(candidates)
    per_country: dict[str, int] = {}
    chosen: list[dict] = []
    for anchor in candidates:
        if per_country.get(anchor["country"], 0) >= 2:
            continue
        per_country[anchor["country"]] = per_country.get(anchor["country"], 0) + 1
        chosen.append(anchor)
        if len(chosen) == args.targets:
            break
    print(f"{len(chosen)} targets in {len(per_country)} countries")

    stop = int(time.time()) - 1800
    start = stop - 1800
    used: dict[int, int] = {}  # anchor id -> index in the output list
    rows: list[dict] = []

    def index_of(anchor_id: int) -> int:
        if anchor_id not in used:
            used[anchor_id] = len(used)
        return used[anchor_id]

    def ipv4_mesh(ids: list[int]) -> int | None:
        for msm in ids:
            detail = fetch(f"{API}/measurements/{msm}/?fields=id,af,is_oneoff")
            assert isinstance(detail, dict)
            if detail.get("af") == 4:
                return msm
        return None

    for position, target in enumerate(chosen, 1):
        msm = ipv4_mesh(mesh[target["id"]])
        if msm is None:
            print(f"[{position}/{len(chosen)}] {target['city']}: no IPv4 ping mesh, skipped")
            continue
        results = fetch(f"{API}/measurements/{msm}/results/?start={start}&stop={stop}&format=json")
        assert isinstance(results, list)
        best: dict[int, float] = {}
        for result in results:
            source = by_probe.get(result["prb_id"])
            if (
                result.get("af") != 4
                or source is None
                or source["id"] == target["id"]
                or not result.get("rcvd")
                or result.get("min", -1) <= 0
            ):
                continue  # only anchors have trustworthy coordinates
            best[source["id"]] = min(best.get(source["id"], 1e9), result["min"])
        print(
            f"[{position}/{len(chosen)}] {target['city']}, {target['country']}: {len(best)} sources"
        )
        rows.append(
            {
                "anchor": index_of(target["id"]),
                "rtt": [[index_of(s), round(rtt, 3)] for s, rtt in sorted(best.items())],
            }
        )

    document = {
        "source": "RIPE Atlas anchoring mesh (ping, minimum round trip per source anchor)",
        "attribution": "RIPE NCC, RIPE Atlas, https://atlas.ripe.net",
        "window_utc": [start, stop],
        "anchors": [
            [
                by_id[i]["country"],
                by_id[i]["city"],
                round(by_id[i]["geometry"]["coordinates"][1], 2),
                round(by_id[i]["geometry"]["coordinates"][0], 2),
            ]
            for i, _ in sorted(used.items(), key=lambda kv: kv[1])
        ],
        "targets": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(document, handle, separators=(",", ":"))
    print(
        f"wrote {args.out} ({args.out.stat().st_size // 1024} KiB), {len(used)} anchors, {sum(len(r['rtt']) for r in rows)} measurements"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
