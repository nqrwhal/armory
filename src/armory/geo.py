"""Location resolution for free-text listings + radius filter around a origin zip.

Data: bundled census gazetteer (all US ZCTA centroids + CA/AZ/NV places).
Resolution order: explicit zip → city/place name (longest match wins) →
common calguns region shorthand ("NorCal", "OC", "SF Bay"...). Region hits
are approximate on purpose — the quality tag travels with the listing so the
valuation stage knows how much to trust the distance.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Rough centroids for the region shorthand calguns sellers actually type.
# "SoCal" is anchored near Riverside — the middle of the populated blob —
# which keeps it honestly fuzzy rather than guessing LA or SD.
REGIONS: dict[str, tuple[float, float]] = {
    "socal": (33.95, -117.40),
    "norcal": (38.58, -121.49),
    "oc": (33.75, -117.87),
    "orange county": (33.75, -117.87),
    "la": (34.05, -118.24),
    "los angeles": (34.05, -118.24),
    "sf bay": (37.58, -122.35),
    "bay area": (37.58, -122.35),
    "ie": (33.95, -117.40),
    "inland empire": (33.95, -117.40),
    "sac": (38.58, -121.49),
    "sacramento": (38.58, -121.49),
    "central valley": (36.75, -119.77),
    "north county": (33.20, -117.15),   # SD North County context on calguns
    "east county": (32.79, -116.96),    # SD East County
    "south bay": (32.64, -117.08),      # SD South Bay
    "sd": (32.72, -117.16),
    "san diego": (32.72, -117.16),
}

_ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")

_EARTH_RADIUS_MI = 3958.8


@dataclass(slots=True)
class GeoHit:
    lat: float
    lon: float
    quality: str  # zip | place | region
    label: str


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_MI * math.asin(math.sqrt(a))


class GeoResolver:
    def __init__(self, data_path: str | Path | None = None):
        path = Path(data_path) if data_path else Path(__file__).parent / "data" / "gazetteer.csv"
        self._zips: dict[str, tuple[float, float]] = {}
        self._places: dict[str, list[tuple[str, float, float]]] = {}  # lower name -> [(state, lat, lon)]
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                lat, lon = float(row["lat"]), float(row["lon"])
                if row["kind"] == "zip":
                    self._zips[row["name"]] = (lat, lon)
                else:
                    self._places.setdefault(row["name"].lower(), []).append((row["state"], lat, lon))
        # one alternation, longest names first so "San Diego Country Estates"
        # wins over "San Diego" at the same position
        names = sorted(self._places, key=len, reverse=True)
        pattern = r"(?<!\w)(" + "|".join(re.escape(n) for n in names) + r")(?!\w)"
        self._place_re = re.compile(pattern, re.IGNORECASE)

    # --- resolution ---

    def resolve(self, text: str) -> GeoHit | None:
        if not text:
            return None
        m = _ZIP_RE.search(text)
        if m and m.group(1) in self._zips:
            lat, lon = self._zips[m.group(1)]
            return GeoHit(lat, lon, "zip", m.group(1))
        # longest match wins across places AND region terms, so
        # "Inland Empire" beats the town of Empire and "San Diego Country
        # Estates" beats San Diego
        best: tuple[int, GeoHit] | None = None
        for pm in self._place_re.finditer(text):
            name = pm.group(1)
            if best is None or len(name) > best[0]:
                candidates = self._places[name.lower()]
                # duplicate names across states: CA first (calguns is a CA site)
                state, lat, lon = sorted(candidates, key=lambda c: c[0] != "CA")[0]
                best = (len(name), GeoHit(lat, lon, "place", f"{name.title()}, {state}"))
        lower = text.lower()
        for term, (lat, lon) in REGIONS.items():
            tm = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", lower)
            if tm and (best is None or len(tm.group(0)) > best[0]):
                best = (len(tm.group(0)), GeoHit(lat, lon, "region", term))
        return best[1] if best else None

    def origin(self, zip_code: str) -> GeoHit:
        hit = self.resolve(zip_code)
        if hit is None or hit.quality != "zip":
            raise ValueError(f"origin zip {zip_code!r} not in gazetteer")
        return hit

    def stats(self) -> dict:
        return {"zips": len(self._zips), "places": len(self._places)}


@lru_cache(maxsize=1)
def default_resolver() -> GeoResolver:
    return GeoResolver()
