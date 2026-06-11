"""Shared pipeline capabilities used by the CLI and GUI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LIVE_API_DATABASES = ("openalex", "pubmed", "wos", "scopus")
DEFAULT_DATABASES = ["openalex", "pubmed", "scopus", "wos"]
RECOGNIZED_DATABASES = LIVE_API_DATABASES

RIS_SOURCE_LABELS = ("wos", "scopus", "pubmed", "openalex", "covidence", "unknown", "other")
LOCAL_FILE_INPUT_TYPES = ("ris",)

SOURCE_DISPLAY_LABELS = {
    "openalex": "OpenAlex",
    "pubmed": "PubMed",
    "wos": "Web of Science",
    "scopus": "Scopus",
    "covidence": "Covidence",
    "unknown": "Unknown",
    "other": "Other",
    "ris": "RIS file",
}

SCALING_MODES = ("small", "medium", "large", "extreme")
SCALING_PROFILES = {
    "small": {
        "openalex": 200,
        "pubmed": 100,
        "scopus": 100,
        "wos": 100,
    },
    "medium": {
        "openalex": 1000,
        "pubmed": 500,
        "scopus": 300,
        "wos": 300,
    },
    "large": {
        "openalex": 3000,
        "pubmed": 1500,
        "scopus": 800,
        "wos": 800,
    },
    "extreme": {
        "openalex": 10000,
        "pubmed": 5000,
        "scopus": 2000,
        "wos": 2000,
    },
}

CONCEPT_PROFILES = {
    "telehealth_cancer_treatment": {
        "telehealth": [
            "telemedicine",
            "telehealth",
            "telecommunication",
            "telecommunications",
            "e-health",
            "ehealth",
            "virtual health",
            "virtual care",
            "virtual consultation",
            "virtual medicine",
            "mobile health",
            "mhealth",
            "remote consultation",
            "remote care",
            "digital health",
            "video visit",
            "video visits",
            "remote monitoring",
        ],
        "cancer": [
            "cancer",
            "cancers",
            "oncology",
            "oncologic",
            "oncological",
            "neoplasm",
            "neoplasms",
            "tumor",
            "tumour",
            "tumors",
            "tumours",
            "carcinoma",
            "malignancy",
            "malignant",
            "chemotherapy",
            "radiation therapy",
            "radiotherapy",
            "cancer screening",
            "cancer diagnosis",
            "cancer treatment",
        ],
        "geography": [
            "texas",
            "west texas",
            "rural",
            "rural populations",
            "southwest united states",
            "southwestern united states",
            "north america",
        ],
    }
}

SUPPORTED_EXTRACTION_FLAGS = (
    "extract_geography",
    "extract_drugs",
    "extract_procedures",
    "extract_demographics",
    "extract_keywords",
    "rebuild_geo_cache",
    "rebuild_demographic_cache",
)
SUPPORTED_VISUALIZATION_OPTIONS = (
    "skip_rxnorm",
    "generate_maps",
    "generate_visuals",
    "generate_vos_networks",
    "convert_csv_to_vos",
)
SUPPORTED_ANALYSIS_OPTIONS = {
    "geography": "Run geography / GeoCensus extraction",
    "drugs": "Run drug term extraction",
    "procedures": "Run procedure term extraction",
    "demographics": "Run demographic term extraction",
    "keywords": "Run keyword extraction",
    "maps": "Generate maps",
    "standard_visuals": "Generate standard bibliometric visuals",
    "vos_networks": "Generate VOS/network visuals",
}
SUPPORTED_MAP_OPTIONS = {
    "world": "Generate world country heat map",
    "us": "Generate U.S. maps",
    "priority_adm1": "Generate priority-country ADM1 maps",
    "priority_adm2": "Generate priority-country ADM2 maps when available",
    "city_points": "Generate city point maps",
    "institution_points": "Generate institution point maps when institution cache data exists",
    "build_world_adm0": "Build WORLD_ADM0.geojson from WORLD_ADM0_INDEX.json if needed",
    "check_map_status": "Check map data status before running",
}
SUPPORTED_CAPABILITY_GROUPS = {
    "inputs": (*LIVE_API_DATABASES, *LOCAL_FILE_INPUT_TYPES),
    "analysis": tuple(SUPPORTED_ANALYSIS_OPTIONS),
    "geocensus": ("geography", "drugs", "procedures", "demographics"),
    "maps": tuple(SUPPORTED_MAP_OPTIONS),
    "visuals": ("maps", "standard_visuals", "vos_networks"),
    "exports": ("png_visuals", "processed_reports", "vos_txt"),
}
SUPPORTED_FILTER_FLAGS = ("review", "early_access", "open_access")


@dataclass(frozen=True)
class RisInput:
    path: Path
    source: str = "unknown"


def normalize_live_source(source: str) -> str:
    return source.strip().lower()


def normalize_ris_source(source: str | None) -> str:
    key = (source or "unknown").strip().lower().replace("web of science", "wos")
    aliases = {
        "wos_ris": "wos",
        "webofscience": "wos",
        "web_of_science": "wos",
        "scopus_ris": "scopus",
        "pubmed_ris": "pubmed",
        "openalex_ris": "openalex",
        "covidence_ris": "covidence",
        "ris": "unknown",
        "": "unknown",
    }
    key = aliases.get(key, key)
    return key if key in RIS_SOURCE_LABELS else "other"
