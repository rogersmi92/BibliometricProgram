"""Local map provider registry for bibliometric geography outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT / "data" / "reference"
MAPS_ROOT = REFERENCE_DIR / "maps"

IMPORTANT_COUNTRY_ISO3 = (
    "AUS",
    "AUT",
    "BEL",
    "BRA",
    "CAN",
    "CHE",
    "CHN",
    "DEU",
    "DNK",
    "ESP",
    "FIN",
    "FRA",
    "GBR",
    "IND",
    "ISR",
    "ITA",
    "JPN",
    "KOR",
    "LUX",
    "NLD",
    "NOR",
    "PRT",
    "RUS",
    "SGP",
    "SWE",
)
PRIORITY_GEBOUNDARIES_ISO3 = IMPORTANT_COUNTRY_ISO3

ALLOWED_MAP_DATA_EXTENSIONS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".geojson", ".gpkg"}
PREFERRED_TRACKED_MAP_EXTENSIONS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".txt", ".csv", ".md"}
DENIED_MAP_DATA_EXTENSIONS = {".zip", ".tif", ".tiff", ".xml", ".html", ".qgs", ".qgz", ".mxd", ".png", ".jpg", ".jpeg", ".gif"}
PROVIDER_README_NAME = "README.md"

EXPECTED_INSTITUTION_HEADERS = {
    "institution_geocache.csv": [
        "institution_raw",
        "institution_normalized",
        "city",
        "admin1",
        "country_iso3",
        "latitude",
        "longitude",
        "source",
        "confidence",
        "last_checked",
    ],
    "institution_aliases.csv": ["alias", "institution_normalized", "country_iso3", "notes"],
    "institution_overrides.csv": [
        "institution_raw",
        "institution_normalized",
        "city",
        "admin1",
        "country_iso3",
        "latitude",
        "longitude",
        "source",
        "confidence",
        "notes",
    ],
}


@dataclass(frozen=True)
class MapPackageStatus:
    label: str
    path: Path
    present: bool
    message: str


def census_boundary_dir(kind: str) -> Path:
    """Return the expected local folder for U.S. Census boundary kind."""
    normalized = kind.strip().lower()
    if normalized not in {"states", "counties", "places"}:
        raise ValueError(f"Unknown Census boundary kind: {kind}")
    return MAPS_ROOT / "boundaries" / "census" / "USA" / normalized


def geoboundaries_dir(iso3: str, admin_level: str) -> Path:
    """Return the expected local folder for geoBoundaries ISO3/admin level."""
    country = iso3.strip().upper()
    level = admin_level.strip().upper()
    if level not in {"ADM0", "ADM1", "ADM2"}:
        raise ValueError(f"Unknown geoBoundaries admin level: {admin_level}")
    return MAPS_ROOT / "boundaries" / "geoboundaries" / country / level


def geoboundaries_cache_dir(admin_level: str = "ADM0") -> Path:
    return ROOT / "data" / "cache" / "maps" / "geoboundaries" / admin_level.strip().upper()


def world_adm0_dir() -> Path:
    return geoboundaries_dir("ALL", "ADM0")


def world_adm0_index_path() -> Path:
    return world_adm0_dir() / "WORLD_ADM0_INDEX.json"


def world_adm0_geojson_path() -> Path:
    return world_adm0_dir() / "WORLD_ADM0.geojson"


def is_world_adm0_index(path: Path) -> bool:
    return path.name == "WORLD_ADM0_INDEX.json" and path.parent == world_adm0_dir()


def geoboundaries_geojson_path(iso3: str, admin_level: str) -> Path:
    country = iso3.strip().upper()
    level = admin_level.strip().upper()
    return geoboundaries_dir(country, level) / f"{country}_{level}.geojson"


def geonames_dir() -> Path:
    return MAPS_ROOT / "gazetteers" / "cities" / "geonames"


def census_city_gazetteer_dir() -> Path:
    return MAPS_ROOT / "gazetteers" / "cities" / "census"


def custom_gazetteer_dir() -> Path:
    return MAPS_ROOT / "gazetteers" / "custom"


def institutions_dir() -> Path:
    return MAPS_ROOT / "institutions"


def expected_geonames_files() -> list[Path]:
    base = geonames_dir()
    return [
        base / "cities5000.txt",
        base / "admin1CodesASCII.txt",
        base / "admin2Codes.txt",
        base / "countryInfo.txt",
    ]


def expected_institution_files() -> list[Path]:
    base = institutions_dir()
    return [base / filename for filename in EXPECTED_INSTITUTION_HEADERS]


def expected_readme_paths() -> list[Path]:
    paths = [
        MAPS_ROOT / PROVIDER_README_NAME,
        MAPS_ROOT / "boundaries" / PROVIDER_README_NAME,
        MAPS_ROOT / "boundaries" / "census" / PROVIDER_README_NAME,
        MAPS_ROOT / "boundaries" / "census" / "USA" / PROVIDER_README_NAME,
        census_boundary_dir("states") / PROVIDER_README_NAME,
        census_boundary_dir("counties") / PROVIDER_README_NAME,
        census_boundary_dir("places") / PROVIDER_README_NAME,
        MAPS_ROOT / "boundaries" / "geoboundaries" / PROVIDER_README_NAME,
        world_adm0_dir() / PROVIDER_README_NAME,
        MAPS_ROOT / "gazetteers" / PROVIDER_README_NAME,
        MAPS_ROOT / "gazetteers" / "cities" / PROVIDER_README_NAME,
        geonames_dir() / PROVIDER_README_NAME,
        census_city_gazetteer_dir() / PROVIDER_README_NAME,
        custom_gazetteer_dir() / PROVIDER_README_NAME,
        institutions_dir() / PROVIDER_README_NAME,
    ]
    for iso3 in PRIORITY_GEBOUNDARIES_ISO3:
        paths.append(geoboundaries_dir(iso3, "ADM1") / PROVIDER_README_NAME)
        paths.append(geoboundaries_dir(iso3, "ADM2") / PROVIDER_README_NAME)
    return paths


def find_first_boundary_file(folder: Path) -> Path | None:
    if not folder.exists():
        return None
    candidates = []
    for extension in (".geojson", ".shp", ".gpkg"):
        candidates.extend(sorted(path for path in folder.glob(f"*{extension}") if path.is_file()))
    return candidates[0] if candidates else None


def find_first_shapefile(folder: Path) -> Path | None:
    if not folder.exists():
        return None
    candidates = sorted(path for path in folder.glob("*.shp") if path.is_file())
    return candidates[0] if candidates else None


def census_boundary_shapefile(kind: str) -> Path | None:
    return find_first_shapefile(census_boundary_dir(kind))


def geoboundaries_boundary_file(iso3: str, admin_level: str) -> Path | None:
    expected = geoboundaries_geojson_path(iso3, admin_level)
    if expected.exists():
        return expected
    return find_first_boundary_file(geoboundaries_dir(iso3, admin_level))


def geoboundaries_shapefile(iso3: str, admin_level: str) -> Path | None:
    return geoboundaries_boundary_file(iso3, admin_level)


def country_admin_boundary_file(iso3: str) -> Path | None:
    """Return the default country-specific boundary layer, preferring ADM1."""
    return geoboundaries_boundary_file(iso3, "ADM1")


def expected_geoboundaries_files() -> list[Path]:
    paths = [world_adm0_index_path(), world_adm0_geojson_path()]
    for iso3 in PRIORITY_GEBOUNDARIES_ISO3:
        paths.append(geoboundaries_geojson_path(iso3, "ADM1"))
        paths.append(geoboundaries_geojson_path(iso3, "ADM2"))
    return paths


def _index_records(index_payload: object) -> list[dict[str, object]]:
    if isinstance(index_payload, list):
        return [item for item in index_payload if isinstance(item, dict)]
    if isinstance(index_payload, dict):
        for key in ("data", "records", "boundaries", "features"):
            value = index_payload.get(key)
            if isinstance(value, list):
                records: list[dict[str, object]] = []
                for item in value:
                    if isinstance(item, dict) and "properties" in item and isinstance(item["properties"], dict):
                        records.append(item["properties"])
                    elif isinstance(item, dict):
                        records.append(item)
                return records
    return []


def _record_download_url(record: dict[str, object]) -> str:
    for key in ("simplifiedGeometryGeoJSON", "simplifiedGeometryGeojson", "gjDownloadURL", "downloadURL", "downloadUrl"):
        value = record.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


def _feature_from_payload(payload: object, record: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    if payload.get("type") == "FeatureCollection" and isinstance(payload.get("features"), list):
        return [feature for feature in payload["features"] if isinstance(feature, dict)]
    if payload.get("type") == "Feature":
        return [payload]
    if isinstance(payload.get("type"), str) and "coordinates" in payload:
        properties = {
            "boundaryName": record.get("boundaryName", ""),
            "boundaryISO": record.get("boundaryISO", ""),
            "boundaryType": record.get("boundaryType", "ADM0"),
        }
        return [{"type": "Feature", "properties": properties, "geometry": payload}]
    return []


def build_world_adm0_geojson(warnings: list[str] | None = None, timeout: int = 30) -> Path | None:
    """Build the drawable world ADM0 GeoJSON from the geoBoundaries ALL/ADM0 index."""
    if world_adm0_geojson_path().exists():
        return world_adm0_geojson_path()
    index_path = world_adm0_index_path()
    if not index_path.exists():
        if warnings is not None:
            warnings.append(f"world ADM0 index missing: {index_path}")
        return None
    try:
        records = _index_records(json.loads(index_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        if warnings is not None:
            warnings.append(f"world ADM0 index unreadable: {index_path}: {exc}")
        return None
    if not records:
        if warnings is not None:
            warnings.append(f"world ADM0 index has no records: {index_path}")
        return None

    cache_dir = geoboundaries_cache_dir("ADM0")
    cache_dir.mkdir(parents=True, exist_ok=True)
    features: list[dict[str, object]] = []
    for record in records:
        iso = str(record.get("boundaryISO") or record.get("iso3") or record.get("ISO_A3") or "").upper()
        url = _record_download_url(record)
        if not iso or not url:
            if warnings is not None:
                warnings.append(f"world ADM0 record skipped; missing ISO or simplified GeoJSON URL: {record.get('boundaryName', 'unknown')}")
            continue
        cache_path = cache_dir / f"{iso}_ADM0.geojson"
        try:
            if cache_path.exists():
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                with urlopen(url, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                cache_path.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            if warnings is not None:
                warnings.append(f"world ADM0 download failed for {iso}: {exc}")
            continue
        features.extend(_feature_from_payload(payload, record))

    if not features:
        if warnings is not None:
            warnings.append("world ADM0 build produced no drawable country features")
        return None
    world_adm0_dir().mkdir(parents=True, exist_ok=True)
    world_adm0_geojson_path().write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return world_adm0_geojson_path()


def report_missing_map_packages() -> list[MapPackageStatus]:
    """Return clear package-status rows for expected local map inputs."""
    checks = [
        ("U.S. Census states", census_boundary_dir("states"), census_boundary_shapefile("states") is not None),
        ("U.S. Census counties", census_boundary_dir("counties"), census_boundary_shapefile("counties") is not None),
        ("U.S. Census places", census_boundary_dir("places"), census_boundary_shapefile("places") is not None),
    ]
    checks.append(("geoBoundaries ALL ADM0 index", world_adm0_index_path(), world_adm0_index_path().exists()))
    checks.append(("geoBoundaries ALL ADM0 drawable", world_adm0_geojson_path(), world_adm0_geojson_path().exists()))
    for iso3 in PRIORITY_GEBOUNDARIES_ISO3:
        checks.append((f"geoBoundaries {iso3} ADM1", geoboundaries_geojson_path(iso3, "ADM1"), geoboundaries_boundary_file(iso3, "ADM1") is not None))
        checks.append((f"geoBoundaries {iso3} ADM2", geoboundaries_geojson_path(iso3, "ADM2"), geoboundaries_boundary_file(iso3, "ADM2") is not None))
    checks.extend((f"GeoNames {path.name}", path, path.exists()) for path in expected_geonames_files())
    checks.extend((f"Institution {path.name}", path, path.exists()) for path in expected_institution_files())

    statuses = []
    for label, path, present in checks:
        message = "present" if present else f"missing; add files under {path}"
        statuses.append(MapPackageStatus(label=label, path=path, present=present, message=message))
    return statuses
