"""Build the city list the location estimator names regions with.

    uv run python scripts/build_gazetteer.py

Source: GeoNames "cities15000" (https://www.geonames.org, CC BY 4.0, see NOTICE). Cities of at least
100,000 people, with suburbs of a larger city within 40 km dropped so candidates are spread out.
The result is committed (src/ipping_backend/geolocation/cities.csv.gz) so builds need no network.
"""

from __future__ import annotations

import csv
import gzip
import io
import math
import urllib.request
import zipfile
from pathlib import Path

URL = "https://download.geonames.org/export/dump/cities15000.zip"
MIN_POPULATION = 100_000
MERGE_KM = 40.0
OUT = Path(__file__).resolve().parent.parent / "src/ipping_backend/geolocation/cities.csv.gz"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 12742 * math.asin(math.sqrt(a))


def main() -> None:
    with urllib.request.urlopen(URL, timeout=120) as response:  # noqa: S310 - fixed https URL
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    rows = []
    with archive.open("cities15000.txt") as handle:
        for line in io.TextIOWrapper(handle, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            population = int(f[14] or 0)
            if population >= MIN_POPULATION:
                rows.append((f[2], f[8], float(f[4]), float(f[5]), population))
    rows.sort(key=lambda r: -r[4])
    kept: list[tuple[str, str, float, float, int]] = []
    for row in rows:
        if all(haversine_km(row[2], row[3], k[2], k[3]) > MERGE_KM for k in kept):
            kept.append(row)
    with gzip.open(OUT, "wt", encoding="utf-8", newline="", compresslevel=9) as out:
        writer = csv.writer(out)
        writer.writerow(["name", "country", "lat", "lon", "population"])
        for name, cc, lat, lon, pop in kept:
            writer.writerow([name, cc, f"{lat:.3f}", f"{lon:.3f}", pop])
    print(
        f"{len(rows)} cities of {MIN_POPULATION}+ people, {len(kept)} after merging suburbs -> {OUT} ({OUT.stat().st_size // 1024} KiB)"
    )


if __name__ == "__main__":
    main()
