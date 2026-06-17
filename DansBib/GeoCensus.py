#!/usr/bin/env python3
"""Build and apply local reference-based extraction layers before visualization."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Iterable

LOCAL_VENV_PYTHON = Path(__file__).resolve().parent / "venv" / "bin" / "python"
if LOCAL_VENV_PYTHON.exists() and Path(sys.executable).resolve() != LOCAL_VENV_PYTHON.resolve():
    os.execv(str(LOCAL_VENV_PYTHON), [str(LOCAL_VENV_PYTHON), *sys.argv])

import pandas as pd

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

from processing.reference_cache import (
    CACHE_DIR,
    DEMOGRAPHIC_CACHE_PATH,
    GEOGRAPHIC_CACHE_PATH,
    build_reference_cache,
    extract_reference_terms_from_cache,
    load_cache,
)
from utils.map_providers import geonames_dir


USE_PROGRESS = False
CURRENT_STAGE = "startup"
ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = Path(os.getenv("DANSBIB_OUTPUT_DIR", str(ROOT / "data" / "outputs")))
VISUALS_DIR = Path(os.getenv("DANSBIB_VISUALS_DIR", str(ROOT / "data" / "visuals")))
PROCESSED_DIR = Path(os.getenv("DANSBIB_PROCESSED_DIR", str(ROOT / "data" / "processed")))
VOS_DIR = Path(os.getenv("DANSBIB_VOS_DIR", str(ROOT / "data" / "VOS")))
REFERENCE_DIR = ROOT / "data" / "reference"
RXNORM_CACHE_PATH = REFERENCE_DIR / "drugs" / "rxnorm" / "rxnorm_cache.json"
SUPPLEMENTAL_ALIAS_PATH = REFERENCE_DIR / "supplemental_aliases.csv"
GEOGRAPHY_STOPWORDS_PATH = REFERENCE_DIR / "geography" / "supplemental" / "geography_stopwords.txt"
GEOGRAPHY_ALIASES_JSON_PATH = REFERENCE_DIR / "geography" / "supplemental" / "geography_aliases.json"
GLOBAL_PLACE_NAMES_CSV_PATH = REFERENCE_DIR / "geography" / "global_place_names.csv"
GLOBAL_PLACE_NAMES_TXT_PATH = REFERENCE_DIR / "geography" / "global_place_names.txt"
LEGACY_GEOGRAPHY_STOPWORDS_PATH = REFERENCE_DIR / "geography_stopwords.txt"
DRUG_STOPWORDS_PATH = REFERENCE_DIR / "drug_stopwords.txt"
PROCEDURE_STOPWORDS_PATH = REFERENCE_DIR / "procedure_stopwords.txt"
DEMOGRAPHIC_STOPWORDS_PATH = REFERENCE_DIR / "demographic_stopwords.txt"
KEEP_TERMS_DIR = REFERENCE_DIR / "keep_terms"
GEOGRAPHY_KEEP_TERMS_PATH = REFERENCE_DIR / "geography" / "supplemental" / "geography_keep_terms.txt"
LEGACY_GEOGRAPHY_KEEP_TERMS_PATH = KEEP_TERMS_DIR / "geography_keep_terms.txt"
PROCEDURE_TERMS_PATH = REFERENCE_DIR / "procedures" / "procedure_terms.txt"
PROCEDURE_ALIASES_PATH = REFERENCE_DIR / "procedures" / "procedure_aliases.csv"
PROCEDURE_KEEP_TERMS_PATH = KEEP_TERMS_DIR / "procedure_keep_terms.txt"
INPUT_SEARCH_DIRS = (
    ROOT / "data" / "outputs",
    ROOT / "data" / "processed",
    ROOT / "data",
    ROOT / "outputs",
    ROOT,
)
INPUT_PATTERNS = (
    "{slug}.csv",
    "{slug}_year_limited_records.csv",
    "{slug}_raw.csv",
    "{slug}_cleaned.csv",
    "{slug}_target_year_range.csv",
    "*{slug}*.csv",
)
SLUG_SUFFIXES = (
    "_year_limited_records",
    "_target_year_range",
    "_raw",
    "_cleaned",
    "_vosviewer",
    "_debug_recent_years",
    "_author_matrix",
    "_qa_summary",
    "_wos_validation_log",
    "_institution_matrix",
    "_year_counts_all_ris_records",
    "_institution_edges",
    "_author_edges",
    "_year_trends_ea_comparison",
    "_year_counts_target_year_range",
    "_country_year_counts",
    "_deduplication_log",
    "_year_counts_concept_term_qa",
    "_missing_metadata_log",
    "_excluded_records",
    "_publication_years",
    "_country_unparsed_debug",
    "_institution_year_counts",
    "_covidence_ingestion_log",
    "_top_authors",
    "_country_list_debug",
)

EXTRACTION_COLUMNS = (
    "title",
    "abstract",
    "keywords",
    "author_keywords",
    "index_keywords",
    "keywords_plus",
    "mesh_terms",
    "mesh",
    "subject",
    "subjects",
    "category",
    "categories",
    "subject_category",
    "wos_categories",
    "research_areas",
)

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "into", "is", "it", "its", "of", "on", "or", "that", "the", "their",
    "this", "to", "with", "without", "using", "via", "patient", "patients", "study",
    "studies", "clinical", "case", "cases", "review", "reviews", "analysis", "results",
}

COMMON_ENGLISH_WORDS = {
    "abstract", "among", "analysis", "author", "background", "case", "clinical",
    "control", "disease", "factor", "health", "human", "method", "patient",
    "patients", "report", "result", "review", "risk", "study", "therapy", "treatment",
    "water", "perform", "today", "matrix", "sustain", "revolution", "liver",
    "alcohol", "tobacco", "research", "between", "impact", "center", "many",
    "early", "standard", "tool", "white", "black", "mobile",
}

GEOGRAPHIC_PLACE_SUFFIXES = (" city", " town", " village", " cdp", " borough")
GEONAMES_COLUMNS = (
    "geonameid",
    "name",
    "asciiname",
    "alternatenames",
    "latitude",
    "longitude",
    "feature_class",
    "feature_code",
    "country_code",
    "cc2",
    "admin1_code",
    "admin2_code",
    "admin3_code",
    "admin4_code",
    "population",
    "elevation",
    "dem",
    "timezone",
    "modification_date",
)
VAGUE_GEOGRAPHY_TERMS = {
    "administrative area",
    "administrative region",
    "axis cdp",
    "cdp",
    "census designated place",
    "middle city",
    "place",
    "standard village",
    "the village",
    "urban",
    "rural",
    "region",
    "area",
    "community",
    "county",
    "state",
    "country",
}
NAMED_REGION_TERMS = {
    "west texas",
    "sub saharan africa",
    "eastern europe",
}

DRUG_TERMS = {
    "abatacept", "adalimumab", "alemtuzumab", "amoxicillin", "anakinra", "aspirin",
    "eculizumab", "ravulizumab", "narsoplimab", "avacopan", "rituximab", "belimumab",
    "bevacizumab", "caplacizumab", "cyclosporine", "cyclophosphamide", "dabrafenib",
    "dexamethasone", "durvalumab", "emicizumab", "everolimus", "heparin",
    "hydroxychloroquine", "imatinib", "infliximab", "intravenous immunoglobulin",
    "ivig", "methylprednisolone", "mycophenolate", "nifedipine", "nivolumab",
    "pembrolizumab", "penicillin", "prednisone", "sirolimus", "tacrolimus",
    "tocilizumab", "trametinib", "tremelimumab", "warfarin",
}

DRUG_ALIASES = {
    "soliris": "eculizumab",
    "ultomiris": "ravulizumab",
    "ravulizumab-cwvz": "ravulizumab",
    "ravulizumab cwvz": "ravulizumab",
    "eculizimab": "eculizumab",
    "intravenous immunoglobulin": "ivig",
}

DRUG_SUFFIXES = ("mab", "zumab", "ximab", "nib", "cillin", "statin", "azole", "vir")
PREFERRED_RXNORM_TTYS = {"IN", "PIN", "MIN", "SCD", "SBD"}
NOISY_RXNORM_TTYS = {"BPCK", "GPCK"}
PROCEDURE_SEED_TERMS = {
    "dialysis",
    "hemodialysis",
    "plasmapheresis",
    "plasma exchange",
    "therapeutic plasma exchange",
    "transplantation",
    "kidney transplant",
    "renal biopsy",
    "biopsy",
    "genetic testing",
    "sequencing",
    "whole exome sequencing",
    "next generation sequencing",
    "polymerase chain reaction",
    "imaging",
    "mri",
    "ct",
    "ultrasound",
    "echocardiography",
    "intubation",
    "ventilation",
    "catheterization",
    "infusion",
    "injection",
    "surgery",
    "transplant",
    "screening",
    "diagnostic testing",
}
PROCEDURE_ALIASES = {
    "plex": "plasma exchange",
    "tpe": "therapeutic plasma exchange",
    "wes": "whole exome sequencing",
    "ngs": "next generation sequencing",
    "pcr": "polymerase chain reaction",
    "renal transplant": "kidney transplant",
}
PROCEDURE_GENERIC_TERMS = {
    "procedure",
    "treatment",
    "therapy",
    "intervention",
    "test",
    "testing",
    "method",
    "analysis",
}
DEMOGRAPHIC_CATEGORY_MAP = {
    "Age Groups": "age_group",
    "Sex": "sex_gender",
    "Gender Identity": "sex_gender",
    "Ethnic Groups": "race_ethnicity",
    "Continental Population Groups": "race_ethnicity",
    "Socioeconomic Factors": "socioeconomic_status",
    "Rural Population": "rurality",
    "Vulnerable Populations": "vulnerable_population",
    "Medically Underserved Area": "access_disparity",
    "Health Status Disparities": "access_disparity",
    "Family Characteristics": "family_structure",
    "Educational Status": "education_employment",
    "Employment": "education_employment",
}


def normalize_text(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    text = text.replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_drug(value: object) -> str:
    text = normalize_text(value)
    text = re.sub(r"\b\d+(\.\d+)?\b", " ", text)
    text = re.sub(r"\b(mg|ml|mcg|g|oral|tablet|injection|solution|capsule|prefilled|syringe|pack)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_procedure(value: object) -> str:
    text = normalize_text(value)
    text = re.sub(r"\b\d+(\.\d+)?\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_term_file(path: Path) -> set[str]:
    if not path.exists():
        return set()
    terms = set()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            value = normalize_text(line.split("#", 1)[0])
            if value:
                terms.add(value)
    return terms


def load_stoplists() -> dict[str, set[str]]:
    return {
        "geographic": load_term_file(GEOGRAPHY_STOPWORDS_PATH) | load_term_file(LEGACY_GEOGRAPHY_STOPWORDS_PATH),
        "drug": load_term_file(DRUG_STOPWORDS_PATH),
        "procedure": load_term_file(PROCEDURE_STOPWORDS_PATH),
        "demographic": load_term_file(DEMOGRAPHIC_STOPWORDS_PATH),
    }


def load_keep_terms() -> dict[str, set[str]]:
    return {
        "geographic": load_term_file(GEOGRAPHY_KEEP_TERMS_PATH) | load_term_file(LEGACY_GEOGRAPHY_KEEP_TERMS_PATH),
        "drug": load_term_file(KEEP_TERMS_DIR / "drug_keep_terms.txt"),
        "procedure": load_term_file(PROCEDURE_KEEP_TERMS_PATH),
        "demographic": load_term_file(KEEP_TERMS_DIR / "demographic_keep_terms.txt"),
    }


def search_text(row: pd.Series) -> str:
    return " ".join(str(row.get(column, "") or "") for column in EXTRACTION_COLUMNS if column in row.index)


def progress_iter(iterable, desc: str = "", total: int | None = None, unit: str = "records"):
    if USE_PROGRESS and tqdm is not None:
        return tqdm(iterable, desc=desc, total=total, unit=unit)
    if USE_PROGRESS and desc:
        print(f"{desc}...", flush=True)
    return iterable


class StepTimer:
    def __init__(self, label: str, enabled: bool | None = None):
        self.label = label
        self.enabled = USE_PROGRESS if enabled is None else enabled
        self.start = 0.0

    def __enter__(self):
        global CURRENT_STAGE
        CURRENT_STAGE = self.label
        self.start = time.time()
        if self.enabled:
            print(f"⏳ {self.label}...", flush=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.enabled:
            return False
        elapsed = time.time() - self.start
        if exc_type:
            print(f"❌ {self.label} failed after {elapsed:.1f}s", flush=True)
            print(f"Error: {exc}", flush=True)
        else:
            print(f"✅ {self.label} complete in {elapsed:.1f}s", flush=True)
        return False


def load_rxnorm_cache(path: Path = RXNORM_CACHE_PATH) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def load_supplemental_aliases(path: Path = SUPPLEMENTAL_ALIAS_PATH) -> dict[str, dict[str, str]]:
    aliases = {"geography": {}, "demographics": {}, "drugs": {}, "procedures": {}}
    if GEOGRAPHY_ALIASES_JSON_PATH.exists():
        try:
            with GEOGRAPHY_ALIASES_JSON_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                for alias, canonical in payload.items():
                    alias_key = normalize_text(alias)
                    canonical_name = str(canonical or "").strip()
                    if alias_key and canonical_name:
                        aliases["geography"][alias_key] = canonical_name
        except Exception as exc:
            print(f"Geography alias JSON skipped: {exc.__class__.__name__}", flush=True)
    if not path.exists():
        print("Supplemental alias file not found. Continuing without supplemental aliases.", flush=True)
        return aliases
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            domain = str(row.get("domain") or row.get("type") or "").strip().lower()
            alias = normalize_text(row.get("alias") or row.get("term") or "")
            canonical = str(row.get("canonical") or row.get("canonical_name") or "").strip()
            if domain == "procedure":
                domain = "procedures"
            if domain in aliases and alias and canonical:
                aliases[domain][alias] = canonical
    return aliases


def load_procedure_aliases(path: Path = PROCEDURE_ALIASES_PATH) -> dict[str, str]:
    aliases = dict(PROCEDURE_ALIASES)
    if not path.exists():
        return aliases
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            alias = normalize_procedure(row.get("alias") or row.get("term") or "")
            canonical = normalize_procedure(row.get("canonical") or row.get("canonical_name") or "")
            if alias and canonical:
                aliases[alias] = canonical
    return aliases


def cache_with_supplemental_aliases(
    cache_path: Path,
    aliases: dict[str, str],
) -> dict[str, dict[str, object]]:
    cache = load_cache(cache_path)
    for alias, canonical in aliases.items():
        canonical_key = normalize_text(canonical)
        alias_key = normalize_text(alias)
        if not alias_key or not canonical_key:
            continue
        entry = cache.get(canonical_key)
        if not entry:
            entry = {
                "canonical_name": canonical,
                "resolved": True,
                "source": "supplemental_alias",
                "aliases": [],
            }
        merged = dict(entry)
        merged["aliases"] = sorted(set([*(entry.get("aliases") or []), alias]))
        cache[alias_key] = merged
    return cache


def safe_read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def load_geonames_country_names(path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    if not path.exists():
        return names
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader((line for line in handle if not line.startswith("#")), delimiter="\t")
        for row in reader:
            if len(row) >= 5:
                names[row[0]] = row[4]
    return names


def load_geonames_admin_names(path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    if not path.exists():
        return names
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            if len(row) >= 2:
                names[row[0]] = row[1]
    return names


def geonames_city_entry(row: dict[str, str], term: str, country: str, admin1: str, admin2: str, ambiguous_count: int) -> dict[str, object]:
    population = str(row.get("population") or "0").strip()
    return {
        "canonical_name": str(row.get("name") or row.get("asciiname") or term).strip(),
        "resolved": True,
        "source": "geonames_cities5000",
        "source_reference": "geonames_cities5000",
        "geo_type": "place",
        "map_level": "point",
        "place_kind": "populated_place",
        "country": country,
        "country_code": str(row.get("country_code") or "").strip(),
        "admin1_code": str(row.get("admin1_code") or "").strip(),
        "admin2_code": str(row.get("admin2_code") or "").strip(),
        "admin1_name": admin1,
        "admin2_name": admin2,
        "state": admin1,
        "latitude": str(row.get("latitude") or "").strip(),
        "longitude": str(row.get("longitude") or "").strip(),
        "population": population,
        "ambiguity_flag": "yes" if ambiguous_count > 1 else "no",
        "ambiguity_count": str(ambiguous_count),
        "aliases": [],
    }


def safe_int_text(value: object) -> int:
    try:
        return int(str(value or "0").strip() or 0)
    except ValueError:
        return 0


def load_geonames_city_cache() -> dict[str, dict[str, object]]:
    base = geonames_dir()
    cities_path = base / "cities5000.txt"
    if not cities_path.exists() or cities_path.stat().st_size == 0:
        return {}
    country_names = load_geonames_country_names(base / "countryInfo.txt")
    admin1_names = load_geonames_admin_names(base / "admin1CodesASCII.txt")
    admin2_names = load_geonames_admin_names(base / "admin2Codes.txt")
    rows: list[dict[str, str]] = []
    with cities_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, fieldnames=GEONAMES_COLUMNS, delimiter="\t")
        for row in reader:
            if not row.get("name") and not row.get("asciiname"):
                continue
            rows.append({key: str(value or "").strip() for key, value in row.items()})

    owners: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        names = {row.get("name", ""), row.get("asciiname", "")}
        for name in names:
            key = normalize_text(name)
            if key:
                owners.setdefault(key, []).append(row)

    cache: dict[str, dict[str, object]] = {}
    for row in rows:
        country_code = row.get("country_code", "")
        admin1_code = row.get("admin1_code", "")
        admin2_code = row.get("admin2_code", "")
        country = country_names.get(country_code, country_code)
        admin1 = admin1_names.get(f"{country_code}.{admin1_code}", admin1_code)
        admin2 = admin2_names.get(f"{country_code}.{admin1_code}.{admin2_code}", admin2_code)
        aliases = {row.get("name", ""), row.get("asciiname", "")}
        canonical = row.get("name") or row.get("asciiname") or ""
        if canonical and country:
            aliases.add(f"{canonical}, {country}")
        if canonical and admin1:
            aliases.add(f"{canonical}, {admin1}")
        for raw_term in aliases:
            term = normalize_text(raw_term)
            if not term:
                continue
            ambiguous_count = len({(owner.get("country_code"), owner.get("admin1_code"), owner.get("admin2_code")) for owner in owners.get(normalize_text(raw_term), [])}) or 1
            entry = geonames_city_entry(row, raw_term, country, admin1, admin2, ambiguous_count)
            existing = cache.get(term)
            if existing:
                existing_population = safe_int_text(existing.get("population"))
                current_population = safe_int_text(entry.get("population"))
                if current_population <= existing_population:
                    continue
            cache[term] = entry
    return cache


def load_global_place_cache(
    csv_path: Path = GLOBAL_PLACE_NAMES_CSV_PATH,
    txt_path: Path = GLOBAL_PLACE_NAMES_TXT_PATH,
) -> dict[str, dict[str, object]]:
    cache: dict[str, dict[str, object]] = {}
    cache.update(load_geonames_city_cache())
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                term = normalize_text(row.get("term") or row.get("canonical_name") or "")
                canonical = str(row.get("canonical_name") or row.get("term") or "").strip()
                if not term or not canonical:
                    continue
                entry = {
                    "canonical_name": canonical,
                    "resolved": True,
                    "source": str(row.get("source") or "local_city_gazetteer").strip(),
                    "source_reference": str(row.get("source") or "local_city_gazetteer").strip(),
                    "geo_type": "place",
                    "map_level": "point",
                    "place_kind": "populated_place",
                    "country": str(row.get("country") or "").strip(),
                    "admin1_name": str(row.get("admin1_name") or "").strip(),
                    "state": str(row.get("admin1_name") or "").strip(),
                    "latitude": str(row.get("latitude") or "").strip(),
                    "longitude": str(row.get("longitude") or "").strip(),
                    "aliases": [],
                }
                cache.setdefault(term, entry)
                canonical_key = normalize_text(canonical)
                if canonical_key and canonical_key != term:
                    alias_entry = dict(entry)
                    alias_entry["aliases"] = [str(row.get("term") or "").strip()]
                    cache.setdefault(canonical_key, alias_entry)

    if txt_path.exists():
        for term in load_term_file(txt_path):
            cache.setdefault(term, {
                "canonical_name": term.title(),
                "resolved": True,
                "source": "local_city_gazetteer",
                "source_reference": "local_city_gazetteer",
                "geo_type": "place",
                "map_level": "point",
                "place_kind": "populated_place",
                "country": "",
                "admin1_name": "",
                "state": "",
                "latitude": "",
                "longitude": "",
                "aliases": [],
            })
    return cache


def merge_global_place_cache(cache: dict[str, dict[str, object]]) -> tuple[dict[str, dict[str, object]], int]:
    global_places = load_global_place_cache()
    merged = dict(cache)
    for key, entry in global_places.items():
        merged.setdefault(key, entry)
    return merged, len(global_places)


def count_terms_by_source(path: Path, source_name: str) -> int:
    df = safe_read_csv(path)
    if df.empty or "source" not in df.columns:
        return 0
    return int((df["source"].astype(str) == source_name).sum())


def build_drug_lookup(supplemental: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    with StepTimer("Loading RxNorm drug cache"):
        rx_cache = load_rxnorm_cache()

    def add(term: object, canonical: object, tty: object = "", source: str = "local") -> None:
        key = normalize_drug(term)
        name = normalize_drug(canonical)
        if not key or len(key) < 3 or key in COMMON_ENGLISH_WORDS:
            return
        lookup[key] = {
            "canonical_name": name or key,
            "tty": str(tty or ""),
            "source": source,
        }

    for term in DRUG_TERMS:
        add(term, term, "LOCAL", "seed")
    for alias, canonical in DRUG_ALIASES.items():
        add(alias, canonical, "ALIAS", "alias")
    for term, payload in rx_cache.items():
        if not isinstance(payload, dict):
            continue
        tty = payload.get("tty") or ""
        canonical = normalize_drug(term)
        add(term, canonical, tty, "rxnorm_cache")
        if payload.get("name"):
            add(payload.get("name"), canonical, tty, "rxnorm_cache")
    for alias, canonical in (supplemental or {}).items():
        add(alias, canonical, "SUPPLEMENTAL", "supplemental_alias")
    return lookup


def compile_lookup_terms(lookup: dict[str, dict[str, str]], corpus_text: str) -> list[tuple[str, re.Pattern[str], dict[str, str]]]:
    normalized_corpus = normalize_text(corpus_text)
    compiled = []
    for term, entry in sorted(lookup.items(), key=lambda item: (len(item[0]), item[0]), reverse=True):
        if term not in normalized_corpus:
            continue
        escaped = re.escape(term).replace(r"\ ", r"[\s\-]+")
        compiled.append((term, re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE), entry))
    return compiled


def drug_suffix_matches(text: str) -> list[tuple[str, dict[str, str], int]]:
    tokens = [token for token in normalize_text(text).split() if token not in STOPWORDS]
    matches = []
    for token in tokens:
        if token in COMMON_ENGLISH_WORDS or len(token) < 5:
            continue
        if any(token.endswith(suffix) for suffix in DRUG_SUFFIXES):
            matches.append((token, {"canonical_name": token, "tty": "PATTERN", "source": "suffix_pattern"}, 1))
    return matches


def find_drug_matches(text: str, compiled: list[tuple[str, re.Pattern[str], dict[str, str]]]) -> list[tuple[str, dict[str, str], int]]:
    occupied: list[tuple[int, int]] = []
    found: Counter[str] = Counter()
    entries: dict[str, dict[str, str]] = {}
    for _, pattern, entry in compiled:
        canonical = str(entry.get("canonical_name") or "").strip()
        count = 0
        for match in pattern.finditer(text):
            span = match.span()
            if any(max(span[0], used[0]) < min(span[1], used[1]) for used in occupied):
                continue
            occupied.append(span)
            count += 1
        if count and canonical:
            found[canonical] += count
            entries[canonical] = entry
    for canonical, entry, count in drug_suffix_matches(text):
        if canonical not in found:
            found[canonical] += count
            entries[canonical] = entry
    return [(canonical, entries[canonical], count) for canonical, count in found.items()]


def matched_field_for_text(row: pd.Series, term: str) -> str:
    normalized = normalize_text(term)
    for column in EXTRACTION_COLUMNS:
        if column in row.index and normalized and normalized in normalize_text(row.get(column, "")):
            return column
    return ""


def drug_confidence(term: str, entry: dict[str, str], matched_field: str, mention_count: int) -> str:
    normalized = normalize_drug(term)
    tty = str(entry.get("tty") or "")
    strong_field = matched_field in {"title", "keywords", "author_keywords", "index_keywords", "mesh_terms", "mesh"}
    if tty in {"IN", "PIN", "MIN"} and (strong_field or mention_count > 1):
        return "high"
    if normalized in COMMON_ENGLISH_WORDS and not strong_field:
        return "low"
    return "medium"


def load_procedure_terms(path: Path = PROCEDURE_TERMS_PATH) -> set[str]:
    terms = {normalize_procedure(term) for term in PROCEDURE_SEED_TERMS}
    terms.update(normalize_procedure(value) for value in PROCEDURE_ALIASES.values())
    terms.update(load_term_file(path))
    return {term for term in terms if term and len(term) >= 2}


def build_procedure_lookup(supplemental: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    aliases = load_procedure_aliases()
    for alias, canonical in (supplemental or {}).items():
        aliases[normalize_procedure(alias)] = normalize_procedure(canonical)

    def add(term: object, canonical: object, source: str = "local") -> None:
        key = normalize_procedure(term)
        name = normalize_procedure(canonical)
        if not key or len(key) < 2:
            return
        lookup[key] = {
            "canonical_name": name or key,
            "source": source,
        }

    for term in load_procedure_terms():
        add(term, term, "seed")
    for alias, canonical in aliases.items():
        add(alias, canonical, "alias")
    return lookup


def find_procedure_matches(text: str, compiled: list[tuple[str, re.Pattern[str], dict[str, str]]]) -> list[tuple[str, dict[str, str], int]]:
    occupied: list[tuple[int, int]] = []
    found: Counter[str] = Counter()
    entries: dict[str, dict[str, str]] = {}
    for _, pattern, entry in compiled:
        canonical = str(entry.get("canonical_name") or "").strip()
        count = 0
        for match in pattern.finditer(text):
            span = match.span()
            if any(max(span[0], used[0]) < min(span[1], used[1]) for used in occupied):
                continue
            occupied.append(span)
            count += 1
        if count and canonical:
            found[canonical] += count
            entries[canonical] = entry
    return [(canonical, entries[canonical], count) for canonical, count in found.items()]


def procedure_confidence(term: str, entry: dict[str, str], matched_field: str, mention_count: int) -> str:
    normalized = normalize_procedure(term)
    strong_field = matched_field in {"title", "keywords", "author_keywords", "index_keywords", "mesh_terms", "mesh"}
    if normalized in PROCEDURE_GENERIC_TERMS and not strong_field:
        return "low"
    if " " in normalized or str(entry.get("source") or "") == "alias" or strong_field or mention_count > 1:
        return "high"
    return "medium"


def build_term_edges(per_record_terms: list[list[str]]) -> pd.DataFrame:
    edge_counts: Counter[tuple[str, str]] = Counter()
    for terms in per_record_terms:
        for left, right in combinations(sorted(set(terms)), 2):
            edge_counts[(left, right)] += 1
    return (
        pd.DataFrame([(left, right, weight) for (left, right), weight in edge_counts.items()], columns=["source", "target", "weight"])
        .sort_values(["weight", "source", "target"], ascending=[False, True, True])
        if edge_counts
        else pd.DataFrame(columns=["source", "target", "weight"])
    )


def write_semicolon_network(edges: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for row in edges.itertuples(index=False):
            source = str(row.source).strip()
            target = str(row.target).strip()
            if source and target and source != target:
                handle.write(f"{source};{target}\n")


def check_stage_progress(
    stage_label: str,
    stage_start: float,
    last_progress: float,
    last_heartbeat: float,
    processed: int,
    total: int,
    heartbeat_seconds: int,
    stage_timeout_seconds: int,
    terms_searched: int | None = None,
) -> float:
    now = time.time()
    if heartbeat_seconds > 0 and now - last_heartbeat >= heartbeat_seconds:
        lines = [
            f"Still {stage_label.lower()}...",
            f"Records processed: {processed} / {total}",
            f"Elapsed: {now - stage_start:.1f} seconds",
            f"Current stage: {stage_label}",
        ]
        if terms_searched is not None:
            lines.append(f"Terms searched: {terms_searched}")
        print("\n".join(lines), flush=True)
        last_heartbeat = now
    if stage_timeout_seconds > 0 and now - last_progress >= stage_timeout_seconds:
        raise RuntimeError(
            f"GeoCensus stage timed out: {stage_label}. "
            f"No progress after {stage_timeout_seconds} seconds."
        )
    return last_heartbeat


def extract_procedure_terms(
    dataframe: pd.DataFrame,
    slug: str,
    supplemental: dict[str, str] | None = None,
    heartbeat_seconds: int = 30,
    stage_timeout_seconds: int = 600,
) -> tuple[Path, Path]:
    lookup = build_procedure_lookup(supplemental)
    reset_df = dataframe.reset_index(drop=True)
    record_texts = [
        search_text(row)
        for _, row in progress_iter(reset_df.iterrows(), desc="Preparing procedure records", total=len(reset_df))
    ]
    compiled = compile_lookup_terms(lookup, " ".join(record_texts))
    rows: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    mention_counts: Counter[str] = Counter()
    source_by_term: dict[str, str] = {}
    per_record_terms: list[list[str]] = []

    stage_label = "Matching procedure terms"
    stage_start = time.time()
    last_progress = stage_start
    last_heartbeat = stage_start
    for record_index, row in progress_iter(reset_df.iterrows(), desc="Extracting procedure terms", total=len(reset_df)):
        last_heartbeat = check_stage_progress(
            stage_label,
            stage_start,
            last_progress,
            last_heartbeat,
            int(record_index),
            len(reset_df),
            heartbeat_seconds,
            stage_timeout_seconds,
            len(compiled),
        )
        matches = find_procedure_matches(record_texts[record_index], compiled)
        terms = []
        for canonical, entry, mention_count in matches:
            matched_field = matched_field_for_text(row, canonical)
            terms.append(canonical)
            counts[canonical] += 1
            mention_counts[canonical] += mention_count
            source_by_term.setdefault(canonical, str(entry.get("source") or ""))
            rows.append(
                {
                    "record_index": int(record_index),
                    "title": row.get("title", ""),
                    "term": canonical,
                    "canonical_name": canonical,
                    "procedure_name": canonical,
                    "mention_count": int(mention_count),
                    "source": source_by_term[canonical],
                    "source_reference": source_by_term[canonical],
                    "matched_field": matched_field,
                    "match_method": "exact_phrase" if source_by_term[canonical] != "alias" else "alias",
                    "confidence": procedure_confidence(canonical, entry, matched_field, int(mention_count)),
                }
            )
        per_record_terms.append(sorted(set(terms)))
        last_progress = time.time()

    count_rows = [
        {
            "canonical_name": term,
            "procedure_name": term,
            "record_count": int(counts[term]),
            "mention_count": int(mention_counts[term]),
            "count": int(counts[term]),
            "source": source_by_term.get(term, ""),
            "source_reference": source_by_term.get(term, ""),
            "matched_field": next((row["matched_field"] for row in rows if row["canonical_name"] == term), ""),
            "match_method": next((row["match_method"] for row in rows if row["canonical_name"] == term), ""),
            "confidence": next((row["confidence"] for row in rows if row["canonical_name"] == term), ""),
        }
        for term in counts
    ]
    terms_df = pd.DataFrame(rows)
    counts_df = (
        pd.DataFrame(count_rows).sort_values(["record_count", "mention_count", "canonical_name"], ascending=[False, False, True])
        if count_rows
        else pd.DataFrame(columns=["canonical_name", "procedure_name", "record_count", "mention_count", "count", "source"])
    )
    edges = build_term_edges(per_record_terms)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    VOS_DIR.mkdir(parents=True, exist_ok=True)
    terms_path = OUTPUTS_DIR / f"{slug}_procedure_terms.csv"
    counts_path = OUTPUTS_DIR / f"{slug}_procedure_term_counts.csv"
    for _, dataframe_to_write, path in progress_iter(
        [("terms", terms_df, terms_path), ("counts", counts_df, counts_path)],
        desc="Writing procedure outputs",
        total=2,
        unit="files",
    ):
        dataframe_to_write.to_csv(path, index=False)

    compat_counts = counts_df[["procedure_name", "count"]].copy() if not counts_df.empty else pd.DataFrame(columns=["procedure_name", "count"])
    for out_dir in progress_iter((PROCESSED_DIR, VOS_DIR), desc="Writing procedure compatibility outputs", total=2, unit="dirs"):
        compat_counts.to_csv(out_dir / f"{slug}_interventions_procedures.csv", index=False)
        compat_counts.to_csv(out_dir / f"{slug}_keywords_procedures.csv", index=False)
        write_semicolon_network(edges, out_dir / f"{slug}_network_procedures.txt")
        write_semicolon_network(edges, out_dir / f"{slug}_keyword_network_procedures.txt")
    return terms_path, counts_path


def extract_drug_terms(
    dataframe: pd.DataFrame,
    slug: str,
    supplemental: dict[str, str] | None = None,
    heartbeat_seconds: int = 30,
    stage_timeout_seconds: int = 600,
) -> tuple[Path, Path]:
    lookup = build_drug_lookup(supplemental)
    reset_df = dataframe.reset_index(drop=True)
    record_texts = [
        search_text(row)
        for _, row in progress_iter(reset_df.iterrows(), desc="Preparing drug records", total=len(reset_df))
    ]
    compiled = compile_lookup_terms(lookup, " ".join(record_texts))
    rows: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    mention_counts: Counter[str] = Counter()
    tty_by_drug: dict[str, str] = {}
    source_by_drug: dict[str, str] = {}
    per_record_terms: list[list[str]] = []

    stage_label = "Matching drug terms"
    stage_start = time.time()
    last_progress = stage_start
    last_heartbeat = stage_start
    for record_index, row in progress_iter(reset_df.iterrows(), desc="Extracting drug terms", total=len(reset_df)):
        last_heartbeat = check_stage_progress(
            stage_label,
            stage_start,
            last_progress,
            last_heartbeat,
            int(record_index),
            len(reset_df),
            heartbeat_seconds,
            stage_timeout_seconds,
            len(compiled),
        )
        matches = find_drug_matches(record_texts[record_index], compiled)
        terms = []
        for canonical, entry, mention_count in matches:
            matched_field = matched_field_for_text(row, canonical)
            terms.append(canonical)
            counts[canonical] += 1
            mention_counts[canonical] += mention_count
            tty_by_drug.setdefault(canonical, str(entry.get("tty") or ""))
            source_by_drug.setdefault(canonical, str(entry.get("source") or ""))
            rows.append(
                {
                    "record_index": int(record_index),
                    "title": row.get("title", ""),
                    "term": canonical,
                    "canonical_name": canonical,
                    "drug_name": canonical,
                    "mention_count": int(mention_count),
                    "tty": tty_by_drug[canonical],
                    "source": source_by_drug[canonical],
                    "source_reference": "rxnorm" if source_by_drug[canonical] == "rxnorm_cache" else source_by_drug[canonical],
                    "matched_field": matched_field,
                    "match_method": "exact_phrase",
                    "confidence": drug_confidence(canonical, entry, matched_field, int(mention_count)),
                }
            )
        per_record_terms.append(sorted(set(terms)))
        last_progress = time.time()

    count_rows = [
        {
            "canonical_name": drug,
            "drug_name": drug,
            "record_count": int(counts[drug]),
            "mention_count": int(mention_counts[drug]),
            "count": int(counts[drug]),
            "tty": tty_by_drug.get(drug, ""),
            "source": source_by_drug.get(drug, ""),
            "source_reference": "rxnorm" if source_by_drug.get(drug, "") == "rxnorm_cache" else source_by_drug.get(drug, ""),
            "matched_field": next((row["matched_field"] for row in rows if row["canonical_name"] == drug), ""),
            "match_method": next((row["match_method"] for row in rows if row["canonical_name"] == drug), ""),
            "confidence": next((row["confidence"] for row in rows if row["canonical_name"] == drug), ""),
        }
        for drug in counts
    ]
    terms_df = pd.DataFrame(rows)
    counts_df = (
        pd.DataFrame(count_rows).sort_values(["record_count", "mention_count", "canonical_name"], ascending=[False, False, True])
        if count_rows
        else pd.DataFrame(columns=["canonical_name", "drug_name", "record_count", "mention_count", "count", "tty", "source"])
    )

    edges = build_term_edges(per_record_terms)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    VOS_DIR.mkdir(parents=True, exist_ok=True)
    terms_path = OUTPUTS_DIR / f"{slug}_drug_terms.csv"
    counts_path = OUTPUTS_DIR / f"{slug}_drug_term_counts.csv"
    for _, dataframe_to_write, path in progress_iter(
        [("terms", terms_df, terms_path), ("counts", counts_df, counts_path)],
        desc="Writing drug outputs",
        total=2,
        unit="files",
    ):
        dataframe_to_write.to_csv(path, index=False)

    compat_counts = counts_df[["drug_name", "count", "tty"]].copy() if not counts_df.empty else pd.DataFrame(columns=["drug_name", "count", "tty"])
    for out_dir in progress_iter((PROCESSED_DIR, VOS_DIR), desc="Writing drug compatibility outputs", total=2, unit="dirs"):
        compat_counts.to_csv(out_dir / f"{slug}_interventions_drugs.csv", index=False)
        compat_counts.to_csv(out_dir / f"{slug}_keywords_drugs.csv", index=False)
        write_semicolon_network(edges, out_dir / f"{slug}_network_drugs.txt")
        write_semicolon_network(edges, out_dir / f"{slug}_keyword_network_drugs.txt")
        counts_df[["drug_name", "tty"]].to_csv(out_dir / f"{slug}_drug_types.csv", index=False)
    return terms_path, counts_path


def read_cleaned_domain_terms(slug: str, domain: str, name_column: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    terms_path = OUTPUTS_DIR / f"{slug}_{domain}_terms_cleaned.csv"
    counts_path = OUTPUTS_DIR / f"{slug}_{domain}_term_counts_cleaned.csv"
    if not terms_path.exists():
        terms_path = OUTPUTS_DIR / f"{slug}_{domain}_terms.csv"
    if not counts_path.exists():
        counts_path = OUTPUTS_DIR / f"{slug}_{domain}_term_counts.csv"
    terms = safe_read_csv(terms_path)
    counts = safe_read_csv(counts_path)
    if not counts.empty and name_column not in counts.columns and "canonical_name" in counts.columns:
        counts[name_column] = counts["canonical_name"]
    if not terms.empty and name_column not in terms.columns and "canonical_name" in terms.columns:
        terms[name_column] = terms["canonical_name"]
    return terms, counts


def write_combined_intervention_outputs(slug: str) -> list[Path]:
    drug_terms, drug_counts = read_cleaned_domain_terms(slug, "drug", "drug_name")
    procedure_terms, procedure_counts = read_cleaned_domain_terms(slug, "procedure", "procedure_name")
    generated: list[Path] = []

    count_rows: list[dict[str, object]] = []
    for domain, counts_df, name_col in (("drug", drug_counts, "drug_name"), ("procedure", procedure_counts, "procedure_name")):
        if counts_df.empty:
            continue
        for row in counts_df.itertuples(index=False):
            data = dict(zip(counts_df.columns, row, strict=False))
            name = str(data.get(name_col) or data.get("canonical_name") or "").strip()
            if not name:
                continue
            count_rows.append(
                {
                    "canonical_name": name,
                    "intervention_name": name,
                    "domain": domain,
                    "type": domain,
                    "record_count": row_number(pd.Series(data), "record_count", "count"),
                    "mention_count": row_number(pd.Series(data), "mention_count"),
                    "count": row_number(pd.Series(data), "count", "record_count"),
                }
            )
    combined_counts = pd.DataFrame(count_rows)
    if not combined_counts.empty:
        combined_counts = combined_counts.sort_values(["count", "domain", "intervention_name"], ascending=[False, True, True])

    term_frames = []
    for domain, terms_df, name_col in (("drug", drug_terms, "drug_name"), ("procedure", procedure_terms, "procedure_name")):
        if terms_df.empty:
            continue
        frame = terms_df.copy()
        frame["domain"] = domain
        frame["type"] = domain
        frame["intervention_name"] = frame.get(name_col, frame.get("canonical_name", ""))
        term_frames.append(frame)
    combined_terms = pd.concat(term_frames, ignore_index=True) if term_frames else pd.DataFrame(columns=["record_index", "canonical_name", "intervention_name", "domain", "type"])

    per_record: dict[str, list[str]] = {}
    if not combined_terms.empty:
        for row in combined_terms.itertuples(index=False):
            data = dict(zip(combined_terms.columns, row, strict=False))
            record_index = str(data.get("record_index") or "")
            name = str(data.get("intervention_name") or data.get("canonical_name") or "").strip()
            domain = str(data.get("domain") or "").strip()
            if record_index and name and domain:
                per_record.setdefault(record_index, []).append(f"{domain}:{name}")
    edges = build_term_edges(list(per_record.values()))

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    VOS_DIR.mkdir(parents=True, exist_ok=True)
    terms_path = OUTPUTS_DIR / f"{slug}_intervention_terms.csv"
    counts_path = OUTPUTS_DIR / f"{slug}_intervention_term_counts.csv"
    combined_terms.to_csv(terms_path, index=False)
    combined_counts.to_csv(counts_path, index=False)
    generated.extend([terms_path, counts_path])

    compat = combined_counts[["intervention_name", "domain", "type", "count"]].copy() if not combined_counts.empty else pd.DataFrame(columns=["intervention_name", "domain", "type", "count"])
    for out_dir in (PROCESSED_DIR, VOS_DIR):
        combined_path = out_dir / f"{slug}_interventions_combined.csv"
        network_path = out_dir / f"{slug}_keyword_network_interventions.txt"
        compat.to_csv(combined_path, index=False)
        write_semicolon_network(edges, network_path)
        generated.extend([combined_path, network_path])
    return generated


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def all_search_csvs() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in INPUT_SEARCH_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.csv")):
            resolved = path.resolve()
            if path.is_file() and resolved not in seen:
                candidates.append(path)
                seen.add(resolved)
    return candidates


def infer_slug_from_csv(path: Path) -> str:
    stem = path.stem
    for suffix in sorted(SLUG_SUFFIXES, key=len, reverse=True):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def discovered_slugs() -> list[str]:
    slugs = sorted({infer_slug_from_csv(path) for path in all_search_csvs() if infer_slug_from_csv(path)})
    return slugs


def candidate_input_csvs(slug: str) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in INPUT_SEARCH_DIRS:
        if not directory.exists():
            continue
        for pattern in INPUT_PATTERNS:
            for path in sorted(directory.glob(pattern.format(slug=slug))):
                resolved = path.resolve()
                if path.is_file() and resolved not in seen:
                    candidates.append(path)
                    seen.add(resolved)
    return candidates


def prompt_menu(title: str, options: list[str], allow_custom: bool = False) -> str:
    print("")
    print(title)
    for index, option in enumerate(options, start=1):
        print(f"{index}. {option}")
    if allow_custom:
        print(f"{len(options) + 1}. Enter another value")

    while True:
        try:
            choice = input("Choose a number: ").strip()
        except EOFError as exc:
            raise ValueError("Interactive input ended before a selection was made.") from exc
        if choice.isdigit():
            number = int(choice)
            if 1 <= number <= len(options):
                return options[number - 1]
            if allow_custom and number == len(options) + 1:
                try:
                    custom = input("Enter value: ").strip()
                except EOFError as exc:
                    raise ValueError("Interactive input ended before a value was entered.") from exc
                if custom:
                    return custom
        print("Please enter one of the listed numbers.")


def prompt_input_path(slug: str) -> Path:
    candidates = candidate_input_csvs(slug)
    if len(candidates) == 1:
        print(f"Using discovered input CSV: {display_path(candidates[0])}")
        return candidates[0]
    if candidates:
        choice = prompt_menu(
            f"Select input CSV for slug: {slug}",
            [display_path(path) for path in candidates],
            allow_custom=True,
        )
    else:
        print("")
        print(f"No matching CSV files found for slug: {slug}")
        try:
            choice = input("Enter input CSV path: ").strip()
        except EOFError as exc:
            raise ValueError("Interactive input ended before an input CSV path was entered.") from exc

    path = Path(choice).expanduser()
    if path.is_file():
        return path
    root_path = ROOT / choice
    if root_path.is_file():
        return root_path
    raise ValueError(f"Input file not found: {choice}")


def prompt_actions(args: argparse.Namespace) -> None:
    choice = prompt_menu(
        "Select GeoCensus actions",
        [
            "all",
            "geography",
            "demographics",
            "drugs",
            "procedures",
            "geography + demographics",
            "geography + drugs",
            "geography + procedures",
            "demographics + drugs",
            "demographics + procedures",
            "drugs + procedures",
            "geography + demographics + drugs",
            "geography + demographics + procedures",
            "geography + drugs + procedures",
            "demographics + drugs + procedures",
        ],
    )
    args.all = choice == "all"
    args.geography = "geography" in choice
    args.demographics = "demographics" in choice
    args.drugs = "drugs" in choice
    args.procedures = "procedures" in choice


def prompt_slug() -> str:
    slugs = discovered_slugs()
    if slugs:
        return prompt_menu("Select project slug", slugs, allow_custom=True)
    try:
        slug = input("Project slug: ").strip()
    except EOFError as exc:
        raise ValueError("Interactive input ended before a project slug was entered.") from exc
    return slug


def interactive_setup(args: argparse.Namespace) -> tuple[argparse.Namespace, Path]:
    print("GeoCensus interactive setup")
    if not args.slug:
        args.slug = prompt_slug()
    if not args.slug:
        raise ValueError("Project slug is required.")

    input_path = prompt_input_path(args.slug)
    if not (args.all or args.geography or args.demographics or args.drugs or args.procedures):
        prompt_actions(args)
    return args, input_path


def input_resolution_error(slug: str, candidates: list[Path]) -> ValueError:
    lines = [
        f"Could not find input CSV for slug: {slug}.",
        "",
        "Searched:",
        *[f"- {display_path(path)}/" if display_path(path) != "." else "- ." for path in INPUT_SEARCH_DIRS],
        "",
        "Found possible matches:",
    ]
    if candidates:
        lines.extend(f"{index}. {display_path(path)}" for index, path in enumerate(candidates, start=1))
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "Please rerun with:",
            f"python GeoCensus.py --slug {slug} --input [chosen file] --all",
        ]
    )
    return ValueError("\n".join(lines))


def resolve_input_csv(slug: str, input_path: str | None = None) -> Path:
    if input_path:
        path = Path(input_path).expanduser()
        if path.is_file():
            return path
        print(f"Input file not found: {input_path}")

    candidates = candidate_input_csvs(slug)
    if len(candidates) == 1:
        print(f"Using discovered input CSV: {display_path(candidates[0])}")
        return candidates[0]
    raise input_resolution_error(slug, candidates)


def print_input_candidates(slug: str) -> None:
    candidates = candidate_input_csvs(slug)
    print(f"Input CSV candidates for slug: {slug}")
    if not candidates:
        print("- None")
        return
    for index, path in enumerate(candidates, start=1):
        print(f"{index}. {display_path(path)}")


def save_summary(slug: str, generated: list[Path], dataframe: pd.DataFrame, detected_counts: dict[str, int] | None = None) -> Path:
    summary_path = OUTPUTS_DIR / f"{slug}_geocensus_summary.txt"
    detected_counts = detected_counts or {}
    map_files = [
        VISUALS_DIR / f"{slug}_geography_heatmap_world.png",
        VISUALS_DIR / f"{slug}_geography_heatmap_us.png",
        VISUALS_DIR / f"{slug}_geography_heatmap_texas.png",
        VISUALS_DIR / f"{slug}_geography_heatmap_europe.png",
        VISUALS_DIR / f"{slug}_geography_heatmap_east_asia.png",
        VISUALS_DIR / f"{slug}_geography_heatmap_world_regions.png",
    ]
    validation_path = VISUALS_DIR / "visualization_validation_report.txt"
    cleanup_qa_path = OUTPUTS_DIR / f"{slug}_geocensus_cleanup_qa.csv"
    unmapped_path = OUTPUTS_DIR / f"{slug}_unmapped_geography_terms.csv"
    unmapped_count = count_detected_terms(unmapped_path)
    lines = [
        f"Project slug: {slug}",
        f"Input records: {len(dataframe)}",
        "Reference extraction is descriptive only; no records are filtered or excluded.",
        "",
        f"Geographic terms detected: {detected_counts.get('geographic', 0)}",
        f"Demographic terms detected: {detected_counts.get('demographic', 0)}",
        f"Drug terms detected: {detected_counts.get('drug', 0)}",
        f"Procedure terms detected: {detected_counts.get('procedure', 0)}",
        f"Intervention terms detected: {detected_counts.get('intervention', 0)}",
        f"Geography maps created: {sum(1 for path in map_files if path.exists())} / {len(map_files)}",
        f"Unmapped geography term count: {unmapped_count}",
        f"Cleanup QA file path: {cleanup_qa_path if cleanup_qa_path.exists() else 'not found'}",
        f"Visualization validation file path: {validation_path if validation_path.exists() else 'not found'}",
        "",
        "Generated files:",
        *[f"- {path}" for path in generated],
    ]
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return summary_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build/apply local geography, demographic, drug, and procedure reference extraction layers.")
    parser.add_argument("--slug", help="Project/output slug.")
    parser.add_argument("--input", help="Input CSV from main bibliometric processing.")
    parser.add_argument("--list-inputs", action="store_true", help="List matching candidate input CSV files and exit.")
    parser.add_argument("--all", action="store_true", help="Extract geography, demographics, drugs, and procedures.")
    parser.add_argument("--geography", action="store_true", help="Extract geographic terms.")
    parser.add_argument("--demographics", action="store_true", help="Extract demographic terms.")
    parser.add_argument("--drugs", action="store_true", help="Extract drug terms.")
    parser.add_argument("--procedures", action="store_true", help="Extract procedure terms.")
    parser.add_argument("--run-all-terms", action="store_true", help="Alias for --all.")
    parser.add_argument("--run-geography", action="store_true", help="Alias for --geography.")
    parser.add_argument("--run-demographics", action="store_true", help="Alias for --demographics.")
    parser.add_argument("--run-drugs", action="store_true", help="Alias for --drugs.")
    parser.add_argument("--run-procedures", action="store_true", help="Alias for --procedures.")
    parser.add_argument("--run-keywords", action="store_true", help="Accepted for GUI orchestration; keyword extraction is handled by visualize_bibliometrics.py.")
    parser.add_argument("--output-dir", type=Path, help="Directory for term extraction CSV outputs.")
    parser.add_argument("--qa-dir", type=Path, help="Directory for QA reports and summaries.")
    parser.add_argument("--rebuild-caches", action="store_true", help="Rebuild geography and demographic caches before extraction.")
    parser.add_argument("--rebuild-geo-cache", action="store_true", help="Rebuild geography cache before extraction.")
    parser.add_argument("--rebuild-demographic-cache", action="store_true", help="Rebuild demographic cache before extraction.")
    parser.add_argument("--supplemental-aliases", default=str(SUPPLEMENTAL_ALIAS_PATH), help="Optional CSV of supplemental aliases.")
    parser.add_argument("--stage-timeout-seconds", type=int, default=600, help="Seconds without stage progress before failing clearly.")
    parser.add_argument("--heartbeat-seconds", type=int, default=30, help="Seconds between long-running stage heartbeat messages.")
    parser.add_argument("--limit-records", type=int, help="Limit input records for debugging.")
    parser.add_argument("--max-geo-terms", type=int, help="Limit U.S. Census geographic cache terms for debugging.")
    parser.add_argument("--include-cdps", dest="include_cdps", action="store_true", help="Include Census Designated Places in cleaned geography outputs.")
    parser.add_argument("--exclude-cdps", dest="include_cdps", action="store_false", help="Exclude Census Designated Places from cleaned geography outputs.")
    parser.set_defaults(include_cdps=False)
    parser.add_argument("--clean-terms", action="store_true", default=True, help="Apply term ambiguity cleanup filters.")
    parser.add_argument("--no-clean-terms", dest="clean_terms", action="store_false", help="Disable term cleanup filters.")
    parser.add_argument("--geo-scope", choices=("texas", "us", "global", "all"), default="global", help="Geographic matching scope.")
    parser.add_argument("--use-stoplists", action="store_true", default=True, help="Use configurable stoplist files.")
    parser.add_argument("--no-stoplists", dest="use_stoplists", action="store_false", help="Ignore configurable stoplist files.")
    parser.add_argument("--strict-drug-filtering", action="store_true", help="Apply stricter RxNorm ambiguity filtering.")
    parser.add_argument("--strict-procedure-filtering", action="store_true", help="Apply stricter procedure ambiguity filtering.")
    parser.add_argument("--strict-geo-filtering", action="store_true", help="Apply stricter geographic ambiguity filtering.")
    parser.add_argument("--strict-demographic-filtering", action="store_true", help="Apply stricter demographic ambiguity filtering.")
    parser.add_argument("--save-raw-and-cleaned", action="store_true", default=True, help="Save raw and cleaned extraction outputs.")
    parser.add_argument("--debug", action="store_true", help="Show full tracebacks for failures.")
    progress_group = parser.add_mutually_exclusive_group()
    progress_group.add_argument("--progress", dest="progress", action="store_true", default=None, help="Show progress indicators when stdout is interactive.")
    progress_group.add_argument("--no-progress", dest="progress", action="store_false", help="Disable progress indicators.")
    args = parser.parse_args()
    if args.run_all_terms:
        args.all = True
    if args.run_geography:
        args.geography = True
    if args.run_demographics:
        args.demographics = True
    if args.run_drugs:
        args.drugs = True
    if args.run_procedures:
        args.procedures = True
    return args


def selected_actions(args: argparse.Namespace) -> str:
    if args.all:
        return "all"
    actions = [
        name
        for name, selected in (
            ("geography", args.geography),
            ("demographics", args.demographics),
            ("drugs", args.drugs),
            ("procedures", args.procedures),
        )
        if selected
    ]
    return "/".join(actions) if actions else "all"


def print_startup_details(args: argparse.Namespace, input_path: Path) -> None:
    print("GeoCensus enrichment run", flush=True)
    print("Project slug:", args.slug, flush=True)
    print("Input CSV:", display_path(input_path), flush=True)
    print("Reference directory:", display_path(REFERENCE_DIR), flush=True)
    print("Cache directory:", display_path(CACHE_DIR), flush=True)
    print("Supplemental aliases:", display_path(Path(args.supplemental_aliases).expanduser()), flush=True)
    print("Actions:", selected_actions(args).replace("/", ", "), flush=True)
    print("Progress display:", "enabled" if USE_PROGRESS else "disabled", flush=True)


def resolve_progress_setting(args: argparse.Namespace) -> bool:
    if args.progress is None:
        return sys.stdout.isatty()
    return bool(args.progress and sys.stdout.isatty())


def count_detected_terms(path: Path) -> int:
    if not path.exists():
        return 0
    return len(safe_read_csv(path))


def row_term(row: pd.Series) -> str:
    return str(row.get("canonical_name") or row.get("drug_name") or row.get("term") or "").strip()


def row_number(row: pd.Series, *columns: str) -> int:
    for column in columns:
        value = row.get(column)
        if value not in (None, ""):
            numeric = pd.to_numeric(pd.Series([value]), errors="coerce").fillna(0).iloc[0]
            return int(numeric)
    return 0


def suppression_reason(prefix: str, row: pd.Series, stoplist: set[str], keep_terms: set[str], args: argparse.Namespace) -> str:
    term = normalize_text(row_term(row))
    if not term:
        return "empty_term"
    if term in keep_terms:
        return ""
    source_ref = str(row.get("source_reference") or row.get("source") or "").lower()
    confidence = str(row.get("confidence") or "").lower()
    matched_field = str(row.get("matched_field") or "").lower()
    is_single_word = " " not in term
    record_count = row_number(row, "record_count", "count")
    mention_count = row_number(row, "mention_count")

    if term in stoplist:
        if prefix == "demographic" and source_ref == "mesh" and not args.strict_demographic_filtering:
            return ""
        return "stoplist"

    if prefix == "geographic":
        state = str(row.get("state") or "").lower()
        geo_type = str(row.get("geo_type") or "").lower()
        place_kind = str(row.get("place_kind") or "").lower()
        match_method = str(row.get("match_method") or "").lower()
        if term in VAGUE_GEOGRAPHY_TERMS:
            return "not_proper_place_name"
        if term in {"county", "state", "country"}:
            return "generic_geography_type"
        if term.endswith(" county") is False and term == "county":
            return "generic_geography_type"
        if geo_type == "region" and term not in NAMED_REGION_TERMS:
            return "unnamed_region"
        if place_kind == "cdp" and not args.include_cdps:
            return "cdp_excluded"
        if args.geo_scope == "texas":
            if state and state not in {"texas", "tx"} and geo_type not in {"state", "country"}:
                return "geo_scope_texas"
        if geo_type == "place" and match_method == "alias":
            if source_ref == "geonames_cities5000":
                return ""
            place_base = term
            for suffix in GEOGRAPHIC_PLACE_SUFFIXES:
                if place_base.endswith(suffix):
                    place_base = place_base[: -len(suffix)].strip()
                    break
            if place_base in {"china", "italy", "post"}:
                return ""
            if place_base in keep_terms:
                return ""
            base_words = set(place_base.split())
            if is_single_word or place_base in stoplist or place_base in COMMON_ENGLISH_WORDS or base_words & COMMON_ENGLISH_WORDS:
                return "ambiguous_place_alias"
        if is_single_word and term in COMMON_ENGLISH_WORDS and source_ref not in {"census", "geonames_cities5000"}:
            return "common_word_ambiguity"
        if confidence == "low" and args.strict_geo_filtering:
            return "low_confidence_text_match"

    if prefix == "drug":
        tty = str(row.get("tty") or "").upper()
        if tty in NOISY_RXNORM_TTYS:
            return "ambiguous_rxnorm_term"
        if args.strict_drug_filtering and tty and tty not in PREFERRED_RXNORM_TTYS and source_ref == "rxnorm":
            return "ambiguous_rxnorm_term"
        if is_single_word and term in COMMON_ENGLISH_WORDS:
            strong_field = matched_field in {"title", "keywords", "author_keywords", "index_keywords", "mesh_terms", "mesh"}
            if not strong_field and mention_count < 2:
                return "ambiguous_rxnorm_term"
        if confidence == "low":
            return "low_confidence_text_match"

    if prefix == "procedure":
        strong_field = matched_field in {"title", "keywords", "author_keywords", "index_keywords", "mesh_terms", "mesh"}
        if term in PROCEDURE_GENERIC_TERMS:
            return "generic_procedure_term"
        if is_single_word and term in COMMON_ENGLISH_WORDS:
            return "common_word_ambiguity"
        if args.strict_procedure_filtering and confidence == "low" and not (strong_field or mention_count > 1):
            return "low_confidence_text_match"

    if prefix == "demographic":
        if confidence == "low" and args.strict_demographic_filtering:
            return "low_confidence_text_match"

    return ""


def add_demographic_grouping(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    grouped = df.copy()
    if "demographic_category" not in grouped.columns:
        grouped["demographic_category"] = ""
    for index, row in grouped.iterrows():
        category = str(row.get("category") or row.get("canonical_name") or "")
        grouped.at[index, "demographic_category"] = DEMOGRAPHIC_CATEGORY_MAP.get(category, grouped.at[index, "demographic_category"] or "other")
    return grouped


def cleanup_extraction_outputs(
    slug: str,
    prefix: str,
    paths: tuple[Path, Path],
    args: argparse.Namespace,
    stoplists: dict[str, set[str]],
    keep_terms: dict[str, set[str]],
) -> tuple[tuple[Path, Path], pd.DataFrame]:
    terms_path, counts_path = paths
    terms_df = safe_read_csv(terms_path)
    counts_df = safe_read_csv(counts_path)
    if prefix == "demographic":
        terms_df = add_demographic_grouping(terms_df)
        counts_df = add_demographic_grouping(counts_df)

    raw_terms_path = OUTPUTS_DIR / f"{slug}_{prefix}_terms_raw.csv"
    raw_counts_path = OUTPUTS_DIR / f"{slug}_{prefix}_term_counts_raw.csv"
    cleaned_terms_path = OUTPUTS_DIR / f"{slug}_{prefix}_terms_cleaned.csv"
    cleaned_counts_path = OUTPUTS_DIR / f"{slug}_{prefix}_term_counts_cleaned.csv"
    terms_df.to_csv(raw_terms_path, index=False)
    counts_df.to_csv(raw_counts_path, index=False)

    suppressions: list[dict[str, object]] = []
    if counts_df.empty:
        counts_df.to_csv(cleaned_counts_path, index=False)
        terms_df.to_csv(cleaned_terms_path, index=False)
        counts_df.to_csv(counts_path, index=False)
        terms_df.to_csv(terms_path, index=False)
        return (cleaned_terms_path, cleaned_counts_path), pd.DataFrame(columns=["domain", "term", "reason", "count"])

    cleaned_counts = counts_df.copy()
    reasons = []
    for _, row in cleaned_counts.iterrows():
        reasons.append(suppression_reason(prefix, row, stoplists.get(prefix, set()), keep_terms.get(prefix, set()), args))
    cleaned_counts["suppression_reason"] = reasons
    suppressed_counts = cleaned_counts[cleaned_counts["suppression_reason"] != ""].copy()
    cleaned_counts = cleaned_counts[cleaned_counts["suppression_reason"] == ""].drop(columns=["suppression_reason"])

    suppressed_terms = set(normalize_text(row_term(row)) for _, row in suppressed_counts.iterrows())
    cleaned_terms = terms_df.copy()
    if not cleaned_terms.empty:
        cleaned_terms["suppression_reason"] = [
            suppression_reason(prefix, row, stoplists.get(prefix, set()), keep_terms.get(prefix, set()), args)
            for _, row in cleaned_terms.iterrows()
        ]
        cleaned_terms = cleaned_terms[cleaned_terms["suppression_reason"] == ""].drop(columns=["suppression_reason"])

    for _, row in suppressed_counts.iterrows():
        suppressions.append(
            {
                "domain": prefix,
                "term": row_term(row),
                "reason": row.get("suppression_reason", ""),
                "count": row.get("record_count") or row.get("count") or row.get("mention_count") or 0,
            }
        )

    cleaned_terms.to_csv(cleaned_terms_path, index=False)
    cleaned_counts.to_csv(cleaned_counts_path, index=False)
    cleaned_terms.to_csv(terms_path, index=False)
    cleaned_counts.to_csv(counts_path, index=False)
    return (cleaned_terms_path, cleaned_counts_path), pd.DataFrame(suppressions)


def write_cleanup_qa(slug: str, suppressions: list[pd.DataFrame], args: argparse.Namespace, aliases: dict[str, dict[str, str]]) -> Path:
    qa_path = OUTPUTS_DIR / f"{slug}_geocensus_cleanup_qa.csv"
    qa_summary_path = OUTPUTS_DIR / f"{slug}_geocensus_cleanup_qa_summary.txt"
    frames = [frame for frame in suppressions if not frame.empty]
    qa_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["domain", "term", "reason", "count"])
    if not qa_df.empty:
        qa_df["count"] = pd.to_numeric(qa_df["count"], errors="coerce").fillna(0).astype(int)
        qa_df = qa_df.sort_values(["domain", "count", "term"], ascending=[True, False, True])
    qa_df.to_csv(qa_path, index=False)
    raw_geo = count_detected_terms(OUTPUTS_DIR / f"{slug}_geographic_term_counts_raw.csv")
    cleaned_geo = count_detected_terms(OUTPUTS_DIR / f"{slug}_geographic_term_counts_cleaned.csv")
    suppressed_geo = len(qa_df[qa_df["domain"] == "geographic"]) if not qa_df.empty else 0
    city_loaded = len(load_global_place_cache())
    city_detected = count_terms_by_source(OUTPUTS_DIR / f"{slug}_geographic_term_counts_raw.csv", "geonames_cities5000")
    city_suppressed = 0
    if not qa_df.empty and {"domain", "term"}.issubset(qa_df.columns):
        raw_terms = safe_read_csv(OUTPUTS_DIR / f"{slug}_geographic_term_counts_raw.csv")
        if not raw_terms.empty and {"canonical_name", "source"} <= set(raw_terms.columns):
            city_terms = set(raw_terms[raw_terms["source"].isin({"geonames_cities5000", "local_city_gazetteer"})]["canonical_name"].astype(str))
            city_suppressed = len(qa_df[(qa_df["domain"] == "geographic") & (qa_df["term"].astype(str).isin(city_terms))])
    reason_counts = Counter(qa_df["reason"]) if not qa_df.empty and "reason" in qa_df.columns else Counter()
    summary_lines = [
        "GeoCensus cleanup QA summary",
        "",
        "Geography provider(s) used:",
        "- us_census: yes",
        f"- supplemental_aliases: {'yes' if bool(aliases.get('geography')) else 'no'}",
        "",
        "GeoNames used:",
        f"- {'yes' if city_loaded else 'no, add files under data/reference/maps/gazetteers/cities/geonames'}",
        "",
        f"Geo scope: {args.geo_scope}",
        f"CDPs included: {'yes' if args.include_cdps else 'no'}",
        f"Raw geographic terms: {raw_geo}",
        f"Cleaned geographic terms: {cleaned_geo}",
        f"Suppressed geographic terms: {suppressed_geo}",
        f"City gazetteer terms loaded: {city_loaded}",
        f"City gazetteer terms detected: {city_detected}",
        f"City gazetteer terms suppressed: {city_suppressed}",
        "",
        "Suppression reasons:",
        *[f"- {reason}: {count}" for reason, count in reason_counts.most_common()],
    ]
    qa_summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print("GeoCensus cleanup QA:", flush=True)
    for line in summary_lines[2:14]:
        print(line, flush=True)
    for domain in ("geographic", "drug", "procedure", "demographic"):
        top = qa_df[qa_df["domain"] == domain].head(10) if not qa_df.empty else pd.DataFrame()
        print(f"Top suppressed {domain} terms:", flush=True)
        if top.empty:
            print("- None", flush=True)
        else:
            for row in top.itertuples(index=False):
                print(f"- {row.term}: {row.reason} ({row.count})", flush=True)
    return qa_path


def cache_source_counts(cache: dict[str, dict[str, object]]) -> Counter[str]:
    return Counter(str(entry.get("source") or "unknown") for entry in cache.values())


def term_lengths(cache: dict[str, dict[str, object]]) -> list[int]:
    lengths = []
    for key, entry in cache.items():
        for term in [key, str(entry.get("canonical_name") or ""), *(entry.get("aliases") or [])]:
            normalized = normalize_text(term)
            if normalized:
                lengths.append(len(normalized.split()))
    return lengths


def filter_geographic_cache(
    cache: dict[str, dict[str, object]],
    supplemental_aliases: int,
    geo_scope: str = "us",
    max_geo_terms: int | None = None,
) -> dict[str, dict[str, object]]:
    filtered: dict[str, dict[str, object]] = {}
    for key, entry in cache.items():
        if normalize_text(key) in VAGUE_GEOGRAPHY_TERMS:
            continue
        source = str(entry.get("source") or "")
        if source not in {"us_census", "census_gazetteer", "supplemental_alias", "local_city_gazetteer", "geonames_cities5000"}:
            continue
        if source in {"local_city_gazetteer", "geonames_cities5000"}:
            country = normalize_text(entry.get("country") or "")
            state = normalize_text(entry.get("state") or entry.get("admin1_name") or "")
            if geo_scope == "texas" and not (country in {"united states", "usa", "us"} and state in {"texas", "tx"}):
                continue
            if geo_scope == "us" and country not in {"united states", "usa", "us"}:
                continue
        if geo_scope == "texas":
            state = str(entry.get("state") or "").lower()
            geo_type = str(entry.get("geo_type") or "").lower()
            if state and state not in {"texas", "tx"} and geo_type not in {"state", "country"}:
                continue
        filtered[key] = entry

    scoped_alias_owners: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for entry in filtered.values():
        canonical = str(entry.get("canonical_name") or "").strip()
        if str(entry.get("geo_type") or "").lower() != "place":
            continue
        for suffix in GEOGRAPHIC_PLACE_SUFFIXES:
            if canonical.lower().endswith(suffix):
                alias = canonical[: -len(suffix)].strip()
                alias_key = normalize_text(alias)
                if alias_key:
                    scoped_alias_owners.setdefault(alias_key, []).append((alias, entry))
                break
    for alias_key, owners in scoped_alias_owners.items():
        canonical_names = {str(entry.get("canonical_name") or "").lower() for _, entry in owners}
        if alias_key in filtered or len(canonical_names) != 1:
            continue
        alias, entry = owners[0]
        alias_entry = dict(entry)
        alias_entry["aliases"] = sorted(set([*(entry.get("aliases") or []), alias]))
        filtered[alias_key] = alias_entry

    if max_geo_terms and max_geo_terms > 0 and len(filtered) > max_geo_terms:
        priority = {"us_census": 0, "census_gazetteer": 0, "supplemental_alias": 1}
        items = sorted(
            filtered.items(),
            key=lambda item: (
                priority.get(str(item[1].get("source") or ""), 9),
                -len(str(item[0])),
                str(item[0]),
            ),
        )
        filtered = dict(items[:max_geo_terms])

    print_geographic_cache_report(filtered, supplemental_aliases)
    return filtered


def print_geographic_cache_report(cache: dict[str, dict[str, object]], supplemental_aliases: int) -> None:
    sources = cache_source_counts(cache)
    lengths = term_lengths(cache)
    print("Geographic cache loaded:", flush=True)
    print(f"- total terms: {len(cache)}", flush=True)
    print(f"- U.S. Census terms: {sources.get('us_census', 0) + sources.get('census_gazetteer', 0)}", flush=True)
    print(f"- GeoNames terms: {sources.get('geonames_cities5000', 0)}", flush=True)
    print(f"- supplemental aliases: {supplemental_aliases}", flush=True)
    print(f"- longest term length: {max(lengths) if lengths else 0}", flush=True)
    print(f"- shortest term length: {min(lengths) if lengths else 0}", flush=True)


def print_final_summary(
    slug: str,
    input_path: Path,
    records_processed: int,
    detected_counts: dict[str, int],
    outputs_written: list[Path],
    started_at: float,
) -> None:
    elapsed = time.time() - started_at
    print("", flush=True)
    print("GeoCensus complete", flush=True)
    print("Project slug:", slug, flush=True)
    print("Input CSV:", display_path(input_path), flush=True)
    print("Records processed:", records_processed, flush=True)
    print("Geographic terms detected:", detected_counts.get("geographic", 0), flush=True)
    print("Demographic terms detected:", detected_counts.get("demographic", 0), flush=True)
    print("Drug terms detected:", detected_counts.get("drug", 0), flush=True)
    print("Procedure terms detected:", detected_counts.get("procedure", 0), flush=True)
    print("Intervention terms detected:", detected_counts.get("intervention", 0), flush=True)
    print("Outputs written:", len(outputs_written), flush=True)
    print("Total runtime:", f"{elapsed:.1f}s", flush=True)


def print_failure_summary(
    args: argparse.Namespace,
    input_path: Path | None,
    records_loaded: int | None,
    started_at: float,
    error: Exception,
) -> None:
    print("", flush=True)
    print("GeoCensus failed.", flush=True)
    print("Stage:", CURRENT_STAGE, flush=True)
    print("Project slug:", args.slug or "", flush=True)
    print("Input CSV:", display_path(input_path) if input_path else "", flush=True)
    print("Records loaded:", records_loaded if records_loaded is not None else "unknown", flush=True)
    print("Elapsed time:", f"{time.time() - started_at:.1f}s", flush=True)
    print("Error:", error, flush=True)


def main() -> None:
    global CURRENT_STAGE, USE_PROGRESS, OUTPUTS_DIR, PROCESSED_DIR
    started_at = time.time()
    args = parse_args()
    if args.output_dir:
        OUTPUTS_DIR = args.output_dir.expanduser().resolve()
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.qa_dir:
        PROCESSED_DIR = args.qa_dir.expanduser().resolve()
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    USE_PROGRESS = resolve_progress_setting(args)
    input_path: Path | None = None
    records_loaded: int | None = None
    if args.list_inputs:
        if not args.slug:
            print("GeoCensus interactive setup", flush=True)
            try:
                args.slug = prompt_slug()
            except ValueError as exc:
                print(exc, flush=True)
                raise SystemExit(1) from None
        if not args.slug:
            print("Project slug is required to list inputs.", flush=True)
            raise SystemExit(1) from None
        print_input_candidates(args.slug)
        return

    try:
        interactive = len(sys.argv) == 1 or not args.slug

        if interactive:
            CURRENT_STAGE = "Interactive setup"
            args, input_path = interactive_setup(args)

        if input_path is None:
            CURRENT_STAGE = "Resolving input CSV"
            input_path = resolve_input_csv(args.slug, args.input)

        print_startup_details(args, input_path)
        with StepTimer("Loading input records"):
            df = pd.read_csv(input_path, dtype=str, keep_default_na=False)
        if args.limit_records and args.limit_records > 0:
            df = df.head(args.limit_records).copy()
            print(f"Limited records for debugging: {len(df)}", flush=True)
        records_loaded = len(df)

        if args.rebuild_caches or args.rebuild_geo_cache or not GEOGRAPHIC_CACHE_PATH.exists():
            with StepTimer("Building geography cache"):
                count = build_reference_cache("geography")["geography"]
            print(f"Geography cache ready: {count} entries", flush=True)
        if args.rebuild_caches or args.rebuild_demographic_cache or not DEMOGRAPHIC_CACHE_PATH.exists():
            with StepTimer("Building demographic cache"):
                count = build_reference_cache("demographics")["demographics"]
            print(f"Demographic cache ready: {count} entries", flush=True)

        with StepTimer("Loading supplemental aliases"):
            aliases = load_supplemental_aliases(Path(args.supplemental_aliases).expanduser())
        stoplists = load_stoplists() if args.use_stoplists else {"geographic": set(), "drug": set(), "procedure": set(), "demographic": set()}
        keep_terms = load_keep_terms()
        run_all = args.all or not (args.geography or args.demographics or args.drugs or args.procedures)
        generated: list[Path] = []
        cleanup_suppressions: list[pd.DataFrame] = []
        detected_counts = {"geographic": 0, "demographic": 0, "drug": 0, "procedure": 0, "intervention": 0}
        global_place_terms_loaded = 0

        if run_all or args.geography:
            with StepTimer("Loading geographic cache"):
                geo_cache = cache_with_supplemental_aliases(GEOGRAPHIC_CACHE_PATH, aliases.get("geography", {}))
                geo_cache, global_place_terms_loaded = merge_global_place_cache(geo_cache)
                geo_cache = filter_geographic_cache(
                    geo_cache,
                    supplemental_aliases=len(aliases.get("geography", {})),
                    geo_scope=args.geo_scope,
                    max_geo_terms=args.max_geo_terms,
                )
            with StepTimer("Matching geographic terms"):
                geo_paths = extract_reference_terms_from_cache(
                    df,
                    args.slug,
                    geo_cache,
                    "geographic",
                    (
                        "geo_type",
                        "map_level",
                        "place_kind",
                        "state",
                        "admin1_name",
                        "admin2_name",
                        "country",
                        "country_code",
                        "latitude",
                        "longitude",
                        "population",
                        "ambiguity_flag",
                        "ambiguity_count",
                    ),
                    progress_fn=progress_iter,
                    heartbeat_seconds=args.heartbeat_seconds,
                    stage_timeout_seconds=args.stage_timeout_seconds,
                )
            if args.clean_terms:
                geo_paths, suppressed = cleanup_extraction_outputs(args.slug, "geographic", geo_paths, args, stoplists, keep_terms)
                cleanup_suppressions.append(suppressed)
            generated.extend(geo_paths)
            detected_counts["geographic"] = count_detected_terms(geo_paths[1])
        if run_all or args.demographics:
            with StepTimer("Loading demographic cache"):
                demographic_cache = cache_with_supplemental_aliases(DEMOGRAPHIC_CACHE_PATH, aliases.get("demographics", {}))
            with StepTimer("Matching demographic terms"):
                demographic_paths = extract_reference_terms_from_cache(
                    df,
                    args.slug,
                    demographic_cache,
                    "demographic",
                    ("category", "mesh_ui", "tree_numbers"),
                    progress_fn=progress_iter,
                    heartbeat_seconds=args.heartbeat_seconds,
                    stage_timeout_seconds=args.stage_timeout_seconds,
                )
            if args.clean_terms:
                demographic_paths, suppressed = cleanup_extraction_outputs(args.slug, "demographic", demographic_paths, args, stoplists, keep_terms)
                cleanup_suppressions.append(suppressed)
            generated.extend(demographic_paths)
            detected_counts["demographic"] = count_detected_terms(demographic_paths[1])
        if run_all or args.drugs:
            with StepTimer("Matching drug terms"):
                drug_paths = extract_drug_terms(
                    df,
                    args.slug,
                    aliases.get("drugs", {}),
                    heartbeat_seconds=args.heartbeat_seconds,
                    stage_timeout_seconds=args.stage_timeout_seconds,
                )
            if args.clean_terms:
                drug_paths, suppressed = cleanup_extraction_outputs(args.slug, "drug", drug_paths, args, stoplists, keep_terms)
                cleanup_suppressions.append(suppressed)
            generated.extend(drug_paths)
            detected_counts["drug"] = count_detected_terms(drug_paths[1])
        if run_all or args.procedures:
            with StepTimer("Matching procedure terms"):
                procedure_paths = extract_procedure_terms(
                    df,
                    args.slug,
                    aliases.get("procedures", {}),
                    heartbeat_seconds=args.heartbeat_seconds,
                    stage_timeout_seconds=args.stage_timeout_seconds,
                )
            if args.clean_terms:
                procedure_paths, suppressed = cleanup_extraction_outputs(args.slug, "procedure", procedure_paths, args, stoplists, keep_terms)
                cleanup_suppressions.append(suppressed)
            generated.extend(procedure_paths)
            detected_counts["procedure"] = count_detected_terms(procedure_paths[1])
        if run_all or args.drugs or args.procedures:
            generated.extend(write_combined_intervention_outputs(args.slug))
            detected_counts["intervention"] = count_detected_terms(OUTPUTS_DIR / f"{args.slug}_intervention_term_counts.csv")

        with StepTimer("Saving GeoCensus outputs"):
            if args.clean_terms:
                generated.append(write_cleanup_qa(args.slug, cleanup_suppressions, args, aliases))
            summary_path = save_summary(args.slug, generated, df, detected_counts)
        print(f"GeoCensus summary saved: {summary_path}", flush=True)
        print_final_summary(args.slug, input_path, len(df), detected_counts, [*generated, summary_path], started_at)
    except Exception as exc:
        if args.debug:
            raise
        print_failure_summary(args, input_path, records_loaded, started_at, exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
