"""Local map provider registry for bibliometric geography outputs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT / "data" / "reference"
MAPS_ROOT = REFERENCE_DIR / "maps"
PROCESSED_DIR = Path(os.getenv("DANSBIB_PROCESSED_DIR", str(ROOT / "data" / "processed")))

IMPORTANT_COUNTRY_ISO3 = (
    "AUS",
    "AUT",
    "BEL",
    "BRA",
    "CAN",
    "CHE",
    "CHN",
    "COL",
    "DEU",
    "DNK",
    "ESP",
    "FIN",
    "FRA",
    "GBR",
    "HRV",
    "IND",
    "ISR",
    "ITA",
    "JPN",
    "KOR",
    "LUX",
    "NLD",
    "NOR",
    "POL",
    "PRT",
    "RUS",
    "SGP",
    "SWE",
    "TUR",
)
PRIORITY_GEBOUNDARIES_ISO3 = IMPORTANT_COUNTRY_ISO3
REQUIRED_WORLD_BASEMAP_COUNTRIES = {
    "USA": "United States",
    "CAN": "Canada",
    "MEX": "Mexico",
    "BRA": "Brazil",
    "GBR": "United Kingdom",
    "FRA": "France",
    "DEU": "Germany",
    "ESP": "Spain",
    "ITA": "Italy",
    "CHN": "China",
    "IND": "India",
    "JPN": "Japan",
    "AUS": "Australia",
    "ZAF": "South Africa",
}

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


@dataclass
class WorldAdm0BuildDiagnostics:
    status: str
    index_path: Path
    output_path: Path
    cache_path: Path
    record_count: int = 0
    url_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    feature_count: int = 0
    failures: list[dict[str, object]] | None = None
    warnings: list[str] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "index_path": str(self.index_path),
            "output_path": str(self.output_path),
            "cache_path": str(self.cache_path),
            "record_count": self.record_count,
            "url_count": self.url_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "feature_count": self.feature_count,
            "failures": self.failures or [],
            "warnings": self.warnings or [],
        }


@dataclass(frozen=True)
class BoundaryProviderSelection:
    iso3: str
    provider: str
    admin_level: str
    path: Path | None
    boundary_type: str
    title_suffix: str
    reason: str
    candidates: list[Path]
    fallback_used: bool = False


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


def gbr_ons_dir() -> Path:
    return MAPS_ROOT / "boundaries" / "geoboundaries" / "GBR" / "ONS.gov.uk"


def gbr_ons_dirs() -> list[Path]:
    return [
        gbr_ons_dir(),
        MAPS_ROOT / "boundaries" / "GBR" / "ONS.gov.uk",
    ]


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


def geoboundaries_api_url(iso3: str, admin_level: str) -> str:
    country = iso3.strip().upper()
    level = admin_level.strip().upper()
    return f"https://www.geoboundaries.org/api/current/gbOpen/{country}/{level}/"


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


def _boundary_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    files: list[Path] = []
    for extension in ("*.geojson", "*.json", "*.shp", "*.gpkg"):
        files.extend(path for path in folder.rglob(extension) if path.is_file())
    return sorted(files)


def _ons_boundary_score(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    full = str(path).lower()
    score = 0
    if any(token in name for token in ("lad", "local_authority_district", "local-authority-district", "local authority district")):
        score += 100
    if any(token in full for token in ("local authority district", "local_authority", "local-authority", "lad")):
        score += 70
    if any(token in name for token in ("generalised", "generalized", "gen", "bgc", "clipped", "bfc")):
        score += 35
    if any(token in name for token in ("county", "district", "unitary", "authority")):
        score += 20
    if path.suffix.lower() in {".geojson", ".json"}:
        score += 10
    elif path.suffix.lower() == ".shp":
        score += 6
    elif path.suffix.lower() == ".gpkg":
        score += 3
    return -score, str(path)


def ons_boundary_candidates() -> list[Path]:
    candidates: list[Path] = []
    for folder in gbr_ons_dirs():
        candidates.extend(_boundary_files(folder))
    return sorted(dict.fromkeys(candidates), key=_ons_boundary_score)


def select_country_boundary_provider(iso3: str) -> BoundaryProviderSelection:
    country = iso3.strip().upper()
    if country == "GBR":
        ons_candidates = ons_boundary_candidates()
        if ons_candidates:
            return BoundaryProviderSelection(
                iso3=country,
                provider="ONS.gov.uk",
                admin_level="ONS_LAD",
                path=ons_candidates[0],
                boundary_type="Local Authority District",
                title_suffix="by Local Authority District",
                reason="UK special-case provider selected from ONS.gov.uk folder",
                candidates=ons_candidates,
                fallback_used=False,
            )
        adm2 = geoboundaries_boundary_file(country, "ADM2")
        if adm2:
            return BoundaryProviderSelection(
                iso3=country,
                provider="geoBoundaries",
                admin_level="ADM2",
                path=adm2,
                boundary_type="geoBoundaries ADM2",
                title_suffix="by geoBoundaries ADM2",
                reason="ONS.gov.uk boundaries unavailable; falling back to geoBoundaries GBR ADM2.",
                candidates=[],
                fallback_used=True,
            )
        adm1 = geoboundaries_boundary_file(country, "ADM1")
        if adm1:
            return BoundaryProviderSelection(
                iso3=country,
                provider="geoBoundaries",
                admin_level="ADM1",
                path=adm1,
                boundary_type="Region",
                title_suffix="by Region",
                reason="ONS.gov.uk and GBR ADM2 boundaries unavailable; falling back to geoBoundaries GBR ADM1.",
                candidates=[],
                fallback_used=True,
            )
        return BoundaryProviderSelection(
            iso3=country,
            provider="skipped",
            admin_level="",
            path=None,
            boundary_type="",
            title_suffix="",
            reason="UK LAD map skipped: ONS Local Authority District boundaries unavailable.",
            candidates=[],
            fallback_used=False,
        )
    adm1 = country_admin_boundary_file(country)
    return BoundaryProviderSelection(
        iso3=country,
        provider="geoBoundaries",
        admin_level="ADM1",
        path=adm1,
        boundary_type="geoBoundaries ADM1",
        title_suffix="",
        reason="standard geoBoundaries ADM1 provider",
        candidates=[adm1] if adm1 else [],
        fallback_used=False,
    )


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


def _recordish(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    properties = item.get("properties")
    record = properties if isinstance(properties, dict) else item
    return any(key in record for key in ("boundaryISO", "boundaryName", "simplifiedGeometryGeoJSON"))


def _normalize_record(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("properties"), dict):
        merged = dict(item["properties"])  # type: ignore[index]
        for key, value in item.items():
            if key not in {"properties", "geometry"}:
                merged.setdefault(key, value)
        return merged
    return item


def _find_record_list(value: object) -> list[dict[str, object]] | None:
    if isinstance(value, list):
        records = [_normalize_record(item) for item in value if _recordish(item)]
        records = [record for record in records if record is not None]
        if records:
            return records
        for item in value:
            nested = _find_record_list(item)
            if nested:
                return nested
    if isinstance(value, dict):
        for key in ("data", "records", "boundaries", "features", "items", "results"):
            if key in value:
                nested = _find_record_list(value[key])
                if nested:
                    return nested
        for nested_value in value.values():
            nested = _find_record_list(nested_value)
            if nested:
                return nested
    return None


def _index_records(index_payload: object) -> list[dict[str, object]]:
    return _find_record_list(index_payload) or []


def _record_download_url(record: dict[str, object]) -> str:
    for key in ("simplifiedGeometryGeoJSON", "simplifiedGeometryGeojson", "gjDownloadURL", "downloadURL", "downloadUrl"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _valid_geojson_feature_collection(path: Path, min_features: int = 100) -> tuple[bool, str, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"unreadable GeoJSON: {exc}", 0
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return False, "not a GeoJSON FeatureCollection", 0
    features = payload.get("features")
    if not isinstance(features, list):
        return False, "FeatureCollection has no features list", 0
    usable = 0
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if isinstance(properties, dict) and geometry and (properties.get("boundaryISO") or properties.get("ISO_A3") or properties.get("shapeISO")):
            usable += 1
    if usable < min_features:
        return False, f"FeatureCollection has only {usable} usable features; expected at least {min_features}", usable
    missing_required = sorted(set(REQUIRED_WORLD_BASEMAP_COUNTRIES) - _feature_iso3_values(features))
    if missing_required:
        labels = ", ".join(f"{REQUIRED_WORLD_BASEMAP_COUNTRIES.get(iso, iso)} / {iso}" for iso in missing_required)
        return False, f"FeatureCollection missing required ADM0 polygons: {labels}", usable
    return True, "ok", usable


def _feature_iso3_values(features: list[object]) -> set[str]:
    values: set[str] = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict):
            continue
        for key in ("boundaryISO", "ISO_A3", "ADM0_A3", "shapeISO", "iso3"):
            value = str(props.get(key) or "").upper()
            if len(value) == 3:
                values.add(value)
    return values


def _feature_from_payload(payload: object, record: dict[str, object], source_url: str, record_index: int) -> tuple[list[dict[str, object]], str]:
    if not isinstance(payload, dict):
        return [], "non-GeoJSON JSON payload"
    raw_features: list[dict[str, object]]
    if payload.get("type") == "FeatureCollection" and isinstance(payload.get("features"), list):
        raw_features = [feature for feature in payload["features"] if isinstance(feature, dict)]
        if not raw_features:
            return [], "empty features list"
    elif payload.get("type") == "Feature":
        raw_features = [payload]
    elif isinstance(payload.get("type"), str) and "coordinates" in payload:
        raw_features = [{"type": "Feature", "properties": {}, "geometry": payload}]
    else:
        return [], f"unsupported GeoJSON type: {payload.get('type', 'missing')}"

    features = []
    for feature in raw_features:
        geometry = feature.get("geometry")
        if not geometry:
            continue
        properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        merged_properties = dict(properties)
        merged_properties.setdefault("boundaryISO", record.get("boundaryISO", record.get("iso3", "")))
        merged_properties.setdefault("boundaryName", record.get("boundaryName", record.get("name", "")))
        merged_properties.setdefault("boundaryType", record.get("boundaryType", "ADM0"))
        merged_properties["source_url"] = source_url
        merged_properties["source_record_index"] = record_index
        features.append({"type": "Feature", "properties": merged_properties, "geometry": geometry})
    if not features:
        return [], "missing geometry"
    return features, "ok"


def _polygon_coordinates_from_geometry(geometry: object) -> list[object]:
    if not isinstance(geometry, dict):
        return []
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        return [coordinates]
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        return coordinates
    return []


def _fallback_adm0_feature_from_local_boundary(iso3: str, name: str) -> tuple[dict[str, object] | None, str]:
    for level in ("ADM1", "ADM2"):
        path = geoboundaries_boundary_file(iso3, level)
        if not path or not path.exists() or path.suffix.lower() != ".geojson":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"local {iso3} {level} unreadable: {exc}"
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            continue
        polygons: list[object] = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            polygons.extend(_polygon_coordinates_from_geometry(feature.get("geometry")))
        if polygons:
            return (
                {
                    "type": "Feature",
                    "properties": {
                        "boundaryISO": iso3,
                        "boundaryName": name,
                        "boundaryType": "ADM0",
                        "source_url": str(path),
                        "source_record_index": "local_adm_fallback",
                        "fallback_source": f"local_{level}",
                    },
                    "geometry": {"type": "MultiPolygon", "coordinates": polygons},
                },
                f"added {iso3} {name} ADM0 fallback from {path}",
            )
    return None, f"missing local ADM1/ADM2 fallback boundary for {iso3} {name}"


def _failure(record: dict[str, object], record_index: int, url: str, reason: str, exc: BaseException | None = None, http_status: int | None = None) -> dict[str, object]:
    return {
        "record_index": record_index,
        "boundaryISO": str(record.get("boundaryISO") or ""),
        "boundaryName": str(record.get("boundaryName") or ""),
        "url": url,
        "reason": reason,
        "exception_type": exc.__class__.__name__ if exc else "",
        "exception_message": str(exc) if exc else "",
        "http_status": http_status,
    }


def _load_json_from_url(url: str, timeout: int, user_agent: str) -> object:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return json.loads(Path(parsed.path).read_text(encoding="utf-8"))
    if parsed.scheme in {"", "local"}:
        path = Path(url)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_bytes(url: str, timeout: int, user_agent: str) -> bytes:
    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _api_boundary_download_url(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("gjDownloadURL", "simplifiedGeometryGeoJSON", "downloadURL", "downloadUrl"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def download_geoboundaries_api_boundary(
    iso3: str,
    admin_level: str,
    warnings: list[str] | None = None,
    timeout: int = 30,
) -> Path | None:
    """Download a geoBoundaries file through the current API metadata endpoint."""
    country = iso3.strip().upper()
    level = admin_level.strip().upper()
    local_warnings: list[str] = []
    api_url = geoboundaries_api_url(country, level)
    output_path = geoboundaries_geojson_path(country, level)
    user_agent = "DansBib/1.0 geoboundaries-api"
    try:
        payload = _load_json_from_url(api_url, timeout=timeout, user_agent=user_agent)
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        local_warnings.append(f"geoBoundaries API metadata failed for {country} {level}: {exc}")
        if warnings is not None:
            warnings.extend(local_warnings)
        return None
    download_url = _api_boundary_download_url(payload)
    if not download_url:
        local_warnings.append(f"geoBoundaries API metadata for {country} {level} did not include a boundary download URL: {api_url}")
        if warnings is not None:
            warnings.extend(local_warnings)
        return None
    try:
        data = _download_bytes(download_url, timeout=timeout, user_agent=user_agent)
    except (OSError, URLError, TimeoutError) as exc:
        local_warnings.append(f"geoBoundaries API boundary download failed for {country} {level}: {exc}; url={download_url}")
        if warnings is not None:
            warnings.extend(local_warnings)
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    local_warnings.append(f"geoBoundaries {country} {level} downloaded via API: {api_url}")
    local_warnings.append(f"geoBoundaries download URL used: {download_url}")
    if warnings is not None:
        warnings.extend(local_warnings)
    return output_path


def ensure_priority_geoboundaries_from_api(
    countries: tuple[str, ...] = ("POL", "COL", "HRV", "TUR"),
    levels: tuple[str, ...] = ("ADM1", "ADM2"),
    warnings: list[str] | None = None,
    timeout: int = 30,
) -> list[Path]:
    """Ensure selected detailed country boundaries exist using geoBoundaries API metadata."""
    downloaded: list[Path] = []
    for iso3 in countries:
        for level in levels:
            existing = geoboundaries_boundary_file(iso3, level)
            if existing:
                downloaded.append(existing)
                continue
            path = download_geoboundaries_api_boundary(iso3, level, warnings=warnings, timeout=timeout)
            if path:
                downloaded.append(path)
    return downloaded


def world_adm0_report_txt_path() -> Path:
    return PROCESSED_DIR / "world_adm0_build_report.txt"


def world_adm0_report_json_path() -> Path:
    return PROCESSED_DIR / "world_adm0_build_report.json"


def _write_world_adm0_reports(diagnostics: WorldAdm0BuildDiagnostics, output_created: bool) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    data = diagnostics.as_dict()
    data["timestamp"] = dt.datetime.now(dt.timezone.utc).isoformat()
    data["output_created"] = output_created
    world_adm0_report_json_path().write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    lines = [
        "WORLD ADM0 build report",
        f"timestamp: {data['timestamp']}",
        f"status: {diagnostics.status}",
        f"index_path: {diagnostics.index_path}",
        f"output_path: {diagnostics.output_path}",
        f"cache_path: {diagnostics.cache_path}",
        f"records_read: {diagnostics.record_count}",
        f"records_with_urls: {diagnostics.url_count}",
        f"successful_downloads_or_cache_loads: {diagnostics.success_count}",
        f"failed_downloads_or_loads: {diagnostics.failure_count}",
        f"features_written: {diagnostics.feature_count}",
        f"output_created: {'yes' if output_created else 'no'}",
        "",
        "warnings:",
        *[f"- {warning}" for warning in (diagnostics.warnings or [])],
        "",
        "first failures:",
    ]
    for failure in (diagnostics.failures or [])[:25]:
        lines.append(
            "- "
            f"{failure.get('boundaryISO') or 'unknown'} {failure.get('boundaryName') or ''}: "
            f"{failure.get('reason')} {failure.get('exception_type')} {failure.get('exception_message')} "
            f"url={failure.get('url')}"
        )
    world_adm0_report_txt_path().write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_world_adm0_geojson(warnings: list[str] | None = None, force: bool = False, timeout: int = 30, retries: int = 3) -> Path | None:
    """Build the drawable world ADM0 GeoJSON from the geoBoundaries ALL/ADM0 index."""
    local_warnings: list[str] = []
    failures: list[dict[str, object]] = []
    index_path = world_adm0_index_path()
    output_path = world_adm0_geojson_path()
    cache_dir = geoboundaries_cache_dir("ADM0")
    diagnostics = WorldAdm0BuildDiagnostics(
        status="started",
        index_path=index_path,
        output_path=output_path,
        cache_path=cache_dir,
        failures=failures,
        warnings=local_warnings,
    )

    for message in (
        f"repo/root path: {ROOT}",
        f"index path: {index_path}",
        f"output path: {output_path}",
        f"cache path: {cache_dir}",
        f"index exists: {index_path.exists()}",
        f"index file size: {index_path.stat().st_size if index_path.exists() else 0}",
        f"output already exists: {output_path.exists()}",
    ):
        local_warnings.append(message)

    if output_path.exists() and not force:
        valid, reason, feature_count = _valid_geojson_feature_collection(output_path)
        diagnostics.feature_count = feature_count
        if valid:
            diagnostics.status = "existing_valid"
            _write_world_adm0_reports(diagnostics, output_created=False)
            if warnings is not None:
                warnings.extend(local_warnings)
            return output_path
        local_warnings.append(f"Existing WORLD_ADM0.geojson invalid; rebuilding: {reason}")

    if not index_path.exists():
        diagnostics.status = "missing_index"
        local_warnings.append(f"world ADM0 index missing: {index_path}")
        _write_world_adm0_reports(diagnostics, output_created=False)
        if warnings is not None:
            warnings.extend(local_warnings)
        return None

    try:
        index_text = index_path.read_text(encoding="utf-8")
        index_payload = json.loads(index_text)
    except json.JSONDecodeError as exc:
        preview = index_path.read_text(encoding="utf-8", errors="replace")[:500] if index_path.exists() else ""
        diagnostics.status = "malformed_index_json"
        local_warnings.append(f"JSON parse error in WORLD_ADM0_INDEX.json: {exc}")
        local_warnings.append(f"Index preview: {preview}")
        _write_world_adm0_reports(diagnostics, output_created=False)
        if warnings is not None:
            warnings.extend(local_warnings)
        return None
    except OSError as exc:
        diagnostics.status = "index_read_failed"
        local_warnings.append(f"world ADM0 index unreadable: {index_path}: {exc}")
        _write_world_adm0_reports(diagnostics, output_created=False)
        if warnings is not None:
            warnings.extend(local_warnings)
        return None

    records = _index_records(index_payload)
    if not records:
        diagnostics.status = "no_records"
        if isinstance(index_payload, dict):
            local_warnings.append(f"Could not locate country records in WORLD_ADM0_INDEX.json. Parsed object type=dict top-level keys={list(index_payload)[:20]}")
        elif isinstance(index_payload, list):
            sample = index_payload[0] if index_payload else None
            local_warnings.append(f"Could not locate country records in WORLD_ADM0_INDEX.json. Parsed object type=list first sample item={sample}")
        else:
            local_warnings.append(f"Could not locate country records in WORLD_ADM0_INDEX.json. Parsed object type={type(index_payload).__name__}")
        _write_world_adm0_reports(diagnostics, output_created=False)
        if warnings is not None:
            warnings.extend(local_warnings)
        return None

    diagnostics.record_count = len(records)
    cache_dir.mkdir(parents=True, exist_ok=True)
    features: list[dict[str, object]] = []
    records_with_url = []
    missing_url = []
    for idx, record in enumerate(records):
        if _record_download_url(record):
            records_with_url.append((idx, record))
        else:
            missing_url.append(record)
    diagnostics.url_count = len(records_with_url)
    local_warnings.append(f"total records found: {len(records)}")
    local_warnings.append(f"records with simplifiedGeometryGeoJSON: {len(records_with_url)}")
    local_warnings.append(f"records missing simplifiedGeometryGeoJSON: {len(missing_url)}")
    for record in missing_url[:10]:
        local_warnings.append(f"missing simplifiedGeometryGeoJSON: {record.get('boundaryISO', '')} {record.get('boundaryName', '')}")

    user_agent = "DansBib/1.0 world-adm0-builder"
    for record_index, record in records_with_url:
        iso = str(record.get("boundaryISO") or record.get("iso3") or record.get("ISO_A3") or "").upper()
        url = _record_download_url(record)
        if not iso:
            failures.append(_failure(record, record_index, url, "missing boundaryISO"))
            continue
        cache_path = cache_dir / f"{iso}_ADM0.geojson"
        payload = None
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                failures.append(_failure(record, record_index, url, "cached GeoJSON invalid", exc))
                payload = None
        if payload is None:
            last_exc: BaseException | None = None
            http_status: int | None = None
            for _attempt in range(max(1, retries)):
                try:
                    payload = _load_json_from_url(url, timeout=timeout, user_agent=user_agent)
                    cache_path.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
                    break
                except HTTPError as exc:
                    last_exc = exc
                    http_status = exc.code
                except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                    last_exc = exc
            if payload is None:
                failures.append(_failure(record, record_index, url, "download/load failed", last_exc, http_status))
                continue

        country_features, reason = _feature_from_payload(payload, record, url, record_index)
        if not country_features:
            failures.append(_failure(record, record_index, url, reason))
            continue
        diagnostics.success_count += 1
        features.extend(country_features)

    present_iso3 = _feature_iso3_values(features)
    for iso3, name in REQUIRED_WORLD_BASEMAP_COUNTRIES.items():
        if iso3 in present_iso3:
            continue
        fallback_feature, fallback_message = _fallback_adm0_feature_from_local_boundary(iso3, name)
        local_warnings.append(fallback_message)
        if fallback_feature:
            features.append(fallback_feature)
            present_iso3.add(iso3)

    diagnostics.failure_count = len(failures)
    diagnostics.feature_count = len(features)
    if len(features) < 100:
        diagnostics.status = "failed_insufficient_features"
        local_warnings.append(f"WORLD_ADM0 build failed: only {len(features)} drawable features loaded; expected at least 100.")
        _write_world_adm0_reports(diagnostics, output_created=False)
        if warnings is not None:
            warnings.extend(local_warnings)
        return None

    world_adm0_dir().mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    diagnostics.status = "created"
    _write_world_adm0_reports(diagnostics, output_created=True)
    local_warnings.append(f"WORLD_ADM0.geojson created: {output_path}")
    local_warnings.append(f"WORLD_ADM0 build report: {world_adm0_report_txt_path()}")
    if warnings is not None:
        warnings.extend(local_warnings)
    return output_path


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Map provider maintenance utilities.")
    parser.add_argument("--build-world-adm0", action="store_true", help="Build WORLD_ADM0.geojson from WORLD_ADM0_INDEX.json.")
    parser.add_argument("--ensure-priority-geoboundaries", action="store_true", help="Download POL/COL/HRV/TUR ADM1/ADM2 files through the current geoBoundaries API when missing.")
    parser.add_argument("--force", action="store_true", help="Rebuild even if WORLD_ADM0.geojson already exists.")
    parser.add_argument("--verbose", action="store_true", help="Print detailed diagnostics.")
    args = parser.parse_args(argv)
    if not args.build_world_adm0 and not args.ensure_priority_geoboundaries:
        parser.print_help()
        return 0
    warnings: list[str] = []
    if args.ensure_priority_geoboundaries:
        paths = ensure_priority_geoboundaries_from_api(warnings=warnings)
        if args.verbose:
            for warning in warnings:
                print(warning)
        print(f"Priority geoBoundaries files ready: {len(paths)}")
        if not args.build_world_adm0:
            return 0 if paths else 1
    path = build_world_adm0_geojson(warnings, force=args.force)
    if args.verbose or not path:
        for warning in warnings:
            print(warning)
        print(f"Report: {world_adm0_report_txt_path()}")
        print(f"JSON report: {world_adm0_report_json_path()}")
    if path:
        print(f"WORLD_ADM0.geojson ready: {path}")
        return 0
    print("WORLD_ADM0.geojson build failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
