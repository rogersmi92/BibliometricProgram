from pathlib import Path
import csv
import sys

ROOT = Path("/Users/roger.smith/Desktop/BibliometricProgram/DansBib")

PLACE_SHP = ROOT / "data/reference/Maps/50m_cultural/ne_50m_populated_places.shp"
OUT_TXT = ROOT / "data/reference/geography/global_place_names.txt"
OUT_CSV = ROOT / "data/reference/geography/global_place_names.csv"

def clean(value):
    return str(value or "").strip()

def try_geopandas():
    import geopandas as gpd
    gdf = gpd.read_file(PLACE_SHP)
    rows = []

    for _, row in gdf.iterrows():
        name = clean(row.get("NAME") or row.get("name"))
        nameascii = clean(row.get("NAMEASCII") or row.get("nameascii"))
        country = clean(row.get("ADM0NAME") or row.get("adm0name"))
        admin1 = clean(row.get("ADM1NAME") or row.get("adm1name"))
        lat = clean(row.get("LATITUDE") or row.get("latitude"))
        lon = clean(row.get("LONGITUDE") or row.get("longitude"))

        for term in {name, nameascii}:
            if term:
                rows.append({
                    "term": term,
                    "canonical_name": name or term,
                    "geo_type": "place",
                    "map_level": "point",
                    "country": country,
                    "admin1_name": admin1,
                    "latitude": lat,
                    "longitude": lon,
                    "source": "natural_earth_populated_places",
                })

    return rows

def try_pyshp():
    import shapefile
    reader = shapefile.Reader(str(PLACE_SHP))
    fields = [field[0] for field in reader.fields[1:]]
    rows = []

    for record in reader.records():
        data = dict(zip(fields, record))

        name = clean(data.get("NAME") or data.get("name"))
        nameascii = clean(data.get("NAMEASCII") or data.get("nameascii"))
        country = clean(data.get("ADM0NAME") or data.get("adm0name"))
        admin1 = clean(data.get("ADM1NAME") or data.get("adm1name"))
        lat = clean(data.get("LATITUDE") or data.get("latitude"))
        lon = clean(data.get("LONGITUDE") or data.get("longitude"))

        for term in {name, nameascii}:
            if term:
                rows.append({
                    "term": term,
                    "canonical_name": name or term,
                    "geo_type": "place",
                    "map_level": "point",
                    "country": country,
                    "admin1_name": admin1,
                    "latitude": lat,
                    "longitude": lon,
                    "source": "natural_earth_populated_places",
                })

    return rows

def main():
    if not PLACE_SHP.exists():
        raise FileNotFoundError(f"Missing shapefile: {PLACE_SHP}")

    try:
        rows = try_geopandas()
        method = "geopandas"
    except Exception:
        try:
            rows = try_pyshp()
            method = "pyshp"
        except ImportError:
            print("Missing dependency. Run this, then retry:")
            print("python3 -m pip install pyshp")
            sys.exit(1)

    deduped = {}
    for row in rows:
        key = (
            row["term"].lower(),
            row["country"].lower(),
            row["admin1_name"].lower(),
        )
        deduped[key] = row

    final_rows = sorted(
        deduped.values(),
        key=lambda r: (r["country"], r["admin1_name"], r["term"])
    )

    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "term",
            "canonical_name",
            "geo_type",
            "map_level",
            "country",
            "admin1_name",
            "latitude",
            "longitude",
            "source",
        ])
        writer.writeheader()
        writer.writerows(final_rows)

    unique_terms = sorted({row["term"] for row in final_rows if row["term"]})
    OUT_TXT.write_text("\n".join(unique_terms) + "\n", encoding="utf-8")

    print(f"Used: {method}")
    print(f"Place rows written: {len(final_rows)}")
    print(f"Unique place names written: {len(unique_terms)}")
    print(f"Wrote: {OUT_TXT}")
    print(f"Wrote: {OUT_CSV}")

if __name__ == "__main__":
    main()
