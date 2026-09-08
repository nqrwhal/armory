#!/usr/bin/env python3
"""Build src/armory/data/gazetteer.csv from census gazetteer files.

Sources (2024):
  https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip
  https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_gaz_place_{06,04,32}.txt

All US ZCTA centroids (zip resolution anywhere) + CA/AZ/NV place centroids
(the states a 100-mile ring around San Diego touches, plus full CA coverage
so out-of-area listings resolve and get excluded by distance, not by guess).

Usage: download the files to a dir, then
  python scripts/build_gazetteer.py <dir-with-gazetteer-files>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "armory" / "data" / "gazetteer.csv"

_PLACE_SUFFIXES = (" city", " town", " CDP", " village", " municipality", " urbana")


def _clean_place(name: str) -> str:
    for suffix in _PLACE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _reader(f):
    # census pads header cells with trailing spaces; normalize before DictReader
    header = f.readline().rstrip("\n").split("\t")
    reader = csv.DictReader(f, delimiter="\t", fieldnames=[h.strip() for h in header])
    return reader


def main(src_dir: str) -> None:
    src = Path(src_dir)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    zcta = src / "2024_Gaz_zcta_national.txt"
    with zcta.open(encoding="utf-8") as f:
        for r in _reader(f):
            rows.append(("zip", r["GEOID"].strip(), "", r["INTPTLAT"].strip(), r["INTPTLONG"].strip()))
    for fips in ("06", "04", "32"):
        place = src / f"2024_gaz_place_{fips}.txt"
        with place.open(encoding="utf-8") as f:
            for r in _reader(f):
                name = _clean_place(r["NAME"].strip())
                if name:
                    rows.append(("place", name, r["USPS"].strip(), r["INTPTLAT"].strip(), r["INTPTLONG"].strip()))
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "name", "state", "lat", "lon"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
