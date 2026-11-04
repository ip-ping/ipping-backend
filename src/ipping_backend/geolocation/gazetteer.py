"""Cities used to name candidate regions (GeoNames, CC BY 4.0; see NOTICE)."""

from __future__ import annotations

import csv
import gzip
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources


@dataclass(frozen=True, slots=True)
class City:
    name: str
    country: str
    latitude: float
    longitude: float
    population: int


@lru_cache(maxsize=1)
def cities() -> tuple[City, ...]:
    data = resources.files(__package__).joinpath("cities.csv.gz").read_bytes()
    text = gzip.decompress(data).decode("utf-8").splitlines()
    return tuple(
        City(r["name"], r["country"], float(r["lat"]), float(r["lon"]), int(r["population"]))
        for r in csv.DictReader(text)
    )
