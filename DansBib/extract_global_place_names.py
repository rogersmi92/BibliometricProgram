#!/usr/bin/env python3
"""Build global place-name reference files from local GeoNames city data."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from utils.map_providers import expected_geonames_files, geonames_dir

ROOT = Path(__file__).resolve().parent
OUT_TXT = ROOT / "data/reference/geography/global_place_names.txt"
OUT_CSV = ROOT / "data/reference/geography/global_place_names.csv"


def clean(value: object) -> str:
    return str(value or "").strip()


def load_country_names(country_info_path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    if not country_info_path.exists():
        return names
    with country_info_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader((line for line in handle if not line.startswith("#")), delimiter="\t")
        for row in reader:
            if len(row) >= 5:
                names[row[0]] = row[4]
    return names


def load_admin1_names(admin1_path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    if not admin1_path.exists():
        return names
    with admin1_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            if len(row) >= 2:
                names[row[0]] = row[1]
    return names


def read_geonames_rows() -> list[dict[str, str]]:
    base = geonames_dir()
    cities_path = base / "cities5000.txt"
    country_names = load_country_names(base / "countryInfo.txt")
    admin1_names = load_admin1_names(base / "admin1CodesASCII.txt")
    if not cities_path.exists():
        missing = "\n".join(f"- {path}" for path in expected_geonames_files() if not path.exists())
        raise FileNotFoundError(f"GeoNames city files are missing. Add them under {base}:\n{missing}")

    rows: list[dict[str, str]] = []
    with cities_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            if len(row) < 19:
                continue
            name = clean(row[1])
            ascii_name = clean(row[2])
            alternates = [clean(value) for value in clean(row[3]).split(",") if clean(value)]
            latitude = clean(row[4])
            longitude = clean(row[5])
            country_code = clean(row[8])
            admin1_code = clean(row[10])
            country = country_names.get(country_code, country_code)
            admin1 = admin1_names.get(f"{country_code}.{admin1_code}", admin1_code)
            for term in sorted({name, ascii_name, *alternates}):
                if not term:
                    continue
                rows.append(
                    {
                        "term": term,
                        "canonical_name": name or ascii_name or term,
                        "geo_type": "place",
                        "map_level": "point",
                        "country": country,
                        "admin1_name": admin1,
                        "latitude": latitude,
                        "longitude": longitude,
                        "source": "geonames_cities5000",
                    }
                )
    return rows


def main() -> int:
    rows = read_geonames_rows()
    deduped = {}
    for row in rows:
        key = (row["term"].lower(), row["country"].lower(), row["admin1_name"].lower())
        deduped[key] = row

    final_rows = sorted(deduped.values(), key=lambda item: (item["country"], item["admin1_name"], item["term"]))
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "term",
                "canonical_name",
                "geo_type",
                "map_level",
                "country",
                "admin1_name",
                "latitude",
                "longitude",
                "source",
            ],
        )
        writer.writeheader()
        writer.writerows(final_rows)

    OUT_TXT.write_text("\n".join(sorted({row["term"] for row in final_rows if row["term"]})) + "\n", encoding="utf-8")
    print(f"Wrote {len(final_rows)} place-name rows from GeoNames.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
