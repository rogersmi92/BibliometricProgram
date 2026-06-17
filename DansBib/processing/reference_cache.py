"""Local reference-cache builders and descriptive term extraction."""

from __future__ import annotations

import csv
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT / "data" / "reference"
CACHE_DIR = ROOT / "data" / "cache"
OUTPUTS_DIR = Path(os.getenv("DANSBIB_OUTPUT_DIR", str(ROOT / "data" / "outputs")))
GEOGRAPHIC_CACHE_PATH = CACHE_DIR / "geographic_cache.json"
DEMOGRAPHIC_CACHE_PATH = CACHE_DIR / "demographic_cache.json"

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

CENSUS_GEO_TYPES = {
    "state": "state",
    "counties": "county",
    "place": "place",
    "tracts": "tract",
    "zcta": "zcta",
    "ua": "urban_area",
    "cbsa": "cbsa",
    "cousubs": "county_subdivision",
}
CENSUS_GEO_TYPE_PRIORITY = {
    "state": 100,
    "place": 90,
    "zcta": 80,
    "county": 70,
    "county_subdivision": 60,
    "cbsa": 55,
    "urban_area": 50,
    "tract": 40,
    "region": 10,
}

NOISY_SINGLE_WORD_GEO_TERMS = {
    "area",
    "care",
    "case",
    "east",
    "health",
    "home",
    "north",
    "place",
    "south",
    "study",
    "trial",
    "west",
}

DEMOGRAPHIC_ROOT_NAMES = {
    "Population Groups": "population_group",
    "Age Groups": "age_group",
    "Sex": "sex",
    "Ethnic Groups": "ethnic_group",
    "Socioeconomic Factors": "socioeconomic_factor",
}

DEMOGRAPHIC_FOCUS_NAMES = {
    "Rural Population": "population_group",
    "Medically Underserved Area": "access_context",
    "Health Status Disparities": "health_disparity",
    "Vulnerable Populations": "population_group",
}


def _norm_key(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text


def _dedupe(values: Iterable[str]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)
    return items


def _state_map(census_dir: Path) -> dict[str, str]:
    path = census_dir / "2025_Gaz_state_national.txt"
    if not path.exists():
        return {}
    mapping: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="|")
        for row in reader:
            code = str(row.get("USPS") or "").strip()
            name = str(row.get("NAME") or "").strip()
            if code and name:
                mapping[code] = name
    return mapping


def _geo_type_from_census_path(path: Path) -> str:
    stem = path.stem.lower()
    for marker, geo_type in CENSUS_GEO_TYPES.items():
        if f"gaz_{marker}_" in stem:
            return geo_type
    return "region"


def _clean_census_aliases(name: str, geo_type: str) -> list[str]:
    aliases = []
    suffixes = {
        "place": (r"\s+(city|town|village|cdp|borough)$",),
        "urban_area": (r"\s+urban area$",),
        "cbsa": (r"\s+(metro area|micro area)$",),
    }
    for suffix in suffixes.get(geo_type, ()):
        alias = re.sub(suffix, "", name, flags=re.IGNORECASE).strip()
        if alias and alias.casefold() != name.casefold():
            aliases.append(alias)
    return aliases


def _census_entries(census_dir: Path) -> list[tuple[str, str, dict[str, str]]]:
    entries: list[tuple[str, str, dict[str, str]]] = []
    if not census_dir.exists():
        return entries
    for path in sorted(census_dir.glob("*.txt")):
        if path.name == ".DS_Store" or not path.is_file():
            continue
        geo_type = _geo_type_from_census_path(path)
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="|")
            for row in reader:
                name = str(row.get("NAME") or "").strip()
                if name:
                    entries.append((name, geo_type, dict(row)))
    return entries


def _census_entry_id(name: str, row: dict[str, str]) -> str:
    return f"{_norm_key(name)}|{str(row.get('USPS') or '').strip()}"


def _unambiguous_census_aliases(entries: list[tuple[str, str, dict[str, str]]]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    aliases_by_name: dict[str, set[str]] = defaultdict(set)
    for name, geo_type, row in entries:
        entry_id = _census_entry_id(name, row)
        for alias in _clean_census_aliases(name, geo_type):
            alias_key = _norm_key(alias)
            if alias_key:
                owners[alias_key].add(entry_id)
                aliases_by_name[entry_id].add(alias)
    return {
        entry_id: aliases
        for entry_id, aliases in aliases_by_name.items()
        if all(len(owners[_norm_key(alias)]) == 1 for alias in aliases)
    }


def _merge_cache_entry(cache: dict[str, dict[str, object]], key: str, entry: dict[str, object]) -> None:
    if not key:
        return
    existing = cache.get(key)
    if existing and existing.get("source") == "us_census" and entry.get("source") != "us_census":
        return
    if existing and existing.get("source") == entry.get("source"):
        existing_priority = CENSUS_GEO_TYPE_PRIORITY.get(str(existing.get("geo_type") or ""), 0)
        entry_priority = CENSUS_GEO_TYPE_PRIORITY.get(str(entry.get("geo_type") or ""), 0)
        if existing_priority > entry_priority:
            aliases = _dedupe([*(existing.get("aliases") or []), *(entry.get("aliases") or [])])
            existing["aliases"] = aliases
            return
        if entry_priority > existing_priority:
            entry["aliases"] = _dedupe([*(entry.get("aliases") or []), *(existing.get("aliases") or [])])
            cache[key] = entry
            return
        aliases = _dedupe([*(existing.get("aliases") or []), *(entry.get("aliases") or [])])
        existing["aliases"] = aliases
        return
    cache[key] = entry


def build_geographic_cache(
    reference_dir: Path = REFERENCE_DIR,
    output_path: Path = GEOGRAPHIC_CACHE_PATH,
) -> dict[str, dict[str, object]]:
    """Build a local geography cache from official provider files."""
    census_dir = reference_dir / "geography" / "us_census"
    legacy_census_dir = reference_dir / "geography" / "census"
    if not census_dir.exists() and legacy_census_dir.exists():
        census_dir = legacy_census_dir
    state_names = _state_map(census_dir)
    cache: dict[str, dict[str, object]] = {}

    entries = _census_entries(census_dir)
    name_counts = Counter(_norm_key(name) for name, _, _ in entries)
    aliases_by_name = _unambiguous_census_aliases(entries)
    for name, geo_type, row in entries:
        state_code = str(row.get("USPS") or "").strip()
        state_name = state_names.get(state_code, state_code)
        place_kind = "cdp" if geo_type == "place" and name.lower().endswith(" cdp") else ""
        aliases = sorted(aliases_by_name.get(_census_entry_id(name, row), set()))
        entry = {
            "canonical_name": name,
            "resolved": True,
            "source": "us_census",
            "provider": "us_census",
            "geo_type": geo_type,
            "place_kind": place_kind,
            "state": state_name,
            "country": "United States",
            "latitude": str(row.get("INTPTLAT") or "").strip(),
            "longitude": str(row.get("INTPTLONG") or "").strip(),
            "aliases": aliases,
        }
        name_key = _norm_key(name)
        if name_counts[name_key] > 1 and state_name:
            _merge_cache_entry(cache, _norm_key(f"{name} {state_name}"), entry)
        else:
            _merge_cache_entry(cache, name_key, entry)
        for alias in entry["aliases"]:
            alias_entry = dict(entry)
            alias_entry["aliases"] = _dedupe([name, *entry["aliases"]])
            _merge_cache_entry(cache, _norm_key(alias), alias_entry)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(dict(sorted(cache.items())), handle, indent=2, sort_keys=True)
    return cache


def _mesh_text(element: ET.Element | None, path: str) -> str:
    found = element.find(path) if element is not None else None
    return str(found.text or "").strip() if found is not None else ""


def _parse_mesh_descriptors(mesh_path: Path) -> list[dict[str, object]]:
    descriptors: list[dict[str, object]] = []
    if not mesh_path.exists():
        return descriptors
    for _, record in ET.iterparse(mesh_path, events=("end",)):
        if record.tag != "DescriptorRecord":
            continue
        name = _mesh_text(record, "DescriptorName/String")
        tree_numbers = [item.text.strip() for item in record.findall("TreeNumberList/TreeNumber") if item.text]
        aliases = [
            item.text.strip()
            for item in record.findall(".//TermList/Term/String")
            if item.text and item.text.strip().casefold() != name.casefold()
        ]
        descriptors.append(
            {
                "mesh_ui": _mesh_text(record, "DescriptorUI"),
                "canonical_name": name,
                "tree_numbers": tree_numbers,
                "aliases": _dedupe(aliases),
                "scope_note": _mesh_text(record, "ConceptList/Concept/ScopeNote"),
            }
        )
        record.clear()
    return descriptors


def _demographic_categories(descriptors: list[dict[str, object]]) -> dict[str, str]:
    roots: dict[str, tuple[str, list[str]]] = {}
    for descriptor in descriptors:
        name = str(descriptor.get("canonical_name") or "")
        if name in DEMOGRAPHIC_ROOT_NAMES:
            roots[name] = (DEMOGRAPHIC_ROOT_NAMES[name], list(descriptor.get("tree_numbers") or []))

    categories: dict[str, str] = {}
    for descriptor in descriptors:
        name = str(descriptor.get("canonical_name") or "")
        if name in DEMOGRAPHIC_FOCUS_NAMES:
            categories[name] = DEMOGRAPHIC_FOCUS_NAMES[name]
            continue
        tree_numbers = list(descriptor.get("tree_numbers") or [])
        for _, (category, root_trees) in roots.items():
            if any(tree == root or tree.startswith(f"{root}.") for tree in tree_numbers for root in root_trees):
                categories[name] = category
                break
    return categories


def build_demographic_cache(
    reference_dir: Path = REFERENCE_DIR,
    output_path: Path = DEMOGRAPHIC_CACHE_PATH,
) -> dict[str, dict[str, object]]:
    """Build a local demographic cache from MeSH descriptors."""
    mesh_path = reference_dir / "demographics" / "mesh" / "desc2026.xml"
    descriptors = _parse_mesh_descriptors(mesh_path)
    categories = _demographic_categories(descriptors)
    cache: dict[str, dict[str, object]] = {}

    for descriptor in descriptors:
        name = str(descriptor.get("canonical_name") or "")
        category = categories.get(name)
        if not name or not category:
            continue
        entry = {
            "canonical_name": name,
            "resolved": True,
            "source": "mesh",
            "category": category,
            "mesh_ui": descriptor.get("mesh_ui", ""),
            "tree_numbers": descriptor.get("tree_numbers", []),
            "aliases": descriptor.get("aliases", []),
        }
        scope_note = str(descriptor.get("scope_note") or "").strip()
        if scope_note:
            entry["scope_note"] = scope_note
        _merge_cache_entry(cache, _norm_key(name), entry)
        for alias in entry["aliases"]:
            alias_entry = dict(entry)
            alias_entry["aliases"] = _dedupe([name, *entry["aliases"]])
            _merge_cache_entry(cache, _norm_key(alias), alias_entry)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(dict(sorted(cache.items())), handle, indent=2, sort_keys=True)
    return cache


def build_reference_cache(cache_type: str) -> dict[str, int]:
    """Build one or all local reference caches."""
    requested = cache_type.strip().lower()
    counts: dict[str, int] = {}
    if requested in {"geography", "geo", "all"}:
        counts["geography"] = len(build_geographic_cache())
    if requested in {"demographics", "demographic", "all"}:
        counts["demographics"] = len(build_demographic_cache())
    if not counts:
        raise ValueError("--type must be geography, demographics, or all")
    return counts


def load_cache(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _search_text(row: pd.Series) -> str:
    values = []
    for column in EXTRACTION_COLUMNS:
        if column in row.index:
            values.append(str(row.get(column) or ""))
    return " ".join(values)


def _matched_field(row: pd.Series, term: str) -> str:
    normalized_term = _norm_key(term)
    for column in EXTRACTION_COLUMNS:
        if column in row.index and normalized_term and normalized_term in _norm_key(row.get(column) or ""):
            return column
    return ""


def _source_reference(entry: dict[str, object], output_prefix: str) -> str:
    source = str(entry.get("source") or "")
    if source in {"us_census", "census_gazetteer"}:
        return "census"
    if source == "supplemental_alias":
        return "supplemental_alias"
    if output_prefix == "demographic":
        return "mesh"
    return source or output_prefix


def _match_method(term: str, canonical: str, entry: dict[str, object], output_prefix: str) -> str:
    if _norm_key(term) != _norm_key(canonical):
        return "alias"
    if output_prefix == "demographic":
        return "mesh_exact"
    return "exact_phrase"


def _confidence(entry: dict[str, object], matched_field: str, term: str, output_prefix: str) -> str:
    source_ref = _source_reference(entry, output_prefix)
    normalized = _norm_key(term)
    is_single_word = " " not in normalized
    strong_field = matched_field in {"title", "keywords", "author_keywords", "index_keywords", "mesh_terms", "mesh", "subject", "subjects"}
    if source_ref in {"census", "mesh", "supplemental_alias"} and (not is_single_word or strong_field):
        return "high"
    return "medium"


def _compile_terms(
    cache: dict[str, dict[str, object]],
    corpus_text: str | None = None,
    output_prefix: str = "reference",
    progress_fn: Callable | None = None,
) -> list[tuple[str, re.Pattern[str], dict[str, object]]]:
    terms: dict[str, dict[str, object]] = {}
    normalized_corpus = _norm_key(corpus_text) if corpus_text is not None else None
    iterator = cache.items()
    if progress_fn:
        iterator = progress_fn(iterator, desc=f"Filtering {output_prefix} terms", total=len(cache), unit="terms")
    for key, entry in iterator:
        lookup_terms = [key, str(entry.get("canonical_name") or ""), *(entry.get("aliases") or [])]
        for term in lookup_terms:
            normalized = _norm_key(term)
            if len(normalized) < 3:
                continue
            is_single_word = " " not in normalized and "-" not in normalized
            if is_single_word and normalized in NOISY_SINGLE_WORD_GEO_TERMS:
                continue
            if normalized_corpus is not None and normalized not in normalized_corpus:
                continue
            terms.setdefault(normalized, entry)
    compiled = []
    sorted_terms = sorted(terms.items(), key=lambda item: (len(item[0]), item[0]), reverse=True)
    iterator = sorted_terms
    if progress_fn:
        iterator = progress_fn(iterator, desc=f"Compiling {output_prefix} terms", total=len(sorted_terms), unit="terms")
    for term, entry in iterator:
        escaped = re.escape(term).replace(r"\ ", r"[\s\-]+")
        compiled.append((term, re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE), entry))
    return compiled


def _find_matches(
    text: str,
    normalized_text: str,
    compiled_terms: list[tuple[str, re.Pattern[str], dict[str, object]]],
    progress_check: Callable[[], None] | None = None,
) -> list[tuple[str, str, dict[str, object], int]]:
    occupied: list[tuple[int, int]] = []
    found: dict[str, tuple[str, dict[str, object], int]] = {}
    for index, (term, pattern, entry) in enumerate(compiled_terms, start=1):
        if progress_check and index % 1000 == 0:
            progress_check()
        if term not in normalized_text:
            continue
        count = 0
        for match in pattern.finditer(text):
            span = match.span()
            if any(max(span[0], used[0]) < min(span[1], used[1]) for used in occupied):
                continue
            occupied.append(span)
            count += 1
        if count:
            canonical = str(entry.get("canonical_name") or term)
            existing_term, existing_entry, existing_count = found.get(canonical, (term, entry, 0))
            found[canonical] = (existing_term, existing_entry, existing_count + count)
    return [(canonical, term, entry, count) for canonical, (term, entry, count) in found.items()]


def extract_reference_terms(
    dataframe: pd.DataFrame,
    slug: str,
    cache_path: Path,
    output_prefix: str,
    metadata_fields: tuple[str, ...],
    outputs_dir: Path = OUTPUTS_DIR,
) -> tuple[Path, Path]:
    """Extract descriptive reference-cache terms from bibliographic text fields."""
    cache = load_cache(cache_path)
    return extract_reference_terms_from_cache(dataframe, slug, cache, output_prefix, metadata_fields, outputs_dir)


def extract_reference_terms_from_cache(
    dataframe: pd.DataFrame,
    slug: str,
    cache: dict[str, dict[str, object]],
    output_prefix: str,
    metadata_fields: tuple[str, ...],
    outputs_dir: Path = OUTPUTS_DIR,
    progress_fn: Callable | None = None,
    heartbeat_seconds: int = 30,
    stage_timeout_seconds: int = 600,
) -> tuple[Path, Path]:
    """Extract descriptive terms from an already prepared in-memory cache."""
    reset_df = dataframe.reset_index(drop=True)
    iterator = reset_df.iterrows()
    if progress_fn:
        iterator = progress_fn(iterator, desc=f"Preparing {output_prefix} records", total=len(reset_df))
    record_texts = [_search_text(row) for _, row in iterator]
    normalized_record_texts = [_norm_key(text) for text in record_texts]
    compiled_terms = _compile_terms(cache, " ".join(record_texts), output_prefix=output_prefix, progress_fn=progress_fn)
    rows: list[dict[str, object]] = []
    counts: dict[str, Counter[str]] = defaultdict(Counter)

    iterator = reset_df.iterrows()
    if progress_fn:
        iterator = progress_fn(iterator, desc=f"Matching {output_prefix} terms across records", total=len(reset_df))
    stage_label = f"Matching {output_prefix} terms"
    stage_start = time.time()
    last_progress = stage_start
    last_heartbeat = stage_start
    for record_index, row in iterator:
        def check_match_progress() -> None:
            nonlocal last_heartbeat
            now = time.time()
            if heartbeat_seconds > 0 and now - last_heartbeat >= heartbeat_seconds:
                print(
                    f"Still matching {output_prefix} terms...\n"
                    f"Records processed: {record_index} / {len(reset_df)}\n"
                    f"Elapsed: {now - stage_start:.1f} seconds\n"
                    f"Current stage: {stage_label}\n"
                    f"Terms searched: {len(compiled_terms)}",
                    flush=True,
                )
                last_heartbeat = now
            if stage_timeout_seconds > 0 and now - last_progress >= stage_timeout_seconds:
                raise RuntimeError(
                    f"GeoCensus stage timed out: {stage_label}. "
                    f"No progress after {stage_timeout_seconds} seconds."
                )

        check_match_progress()
        text = record_texts[record_index]
        normalized_text = normalized_record_texts[record_index]
        if not text:
            last_progress = time.time()
            continue
        for canonical, matched_term, entry, mention_count in _find_matches(text, normalized_text, compiled_terms, check_match_progress):
            matched_field = _matched_field(row, matched_term)
            out_row = {
                "record_index": int(record_index),
                "title": row.get("title", ""),
                "term": canonical,
                "canonical_name": canonical,
                "source": entry.get("source", ""),
                "source_reference": _source_reference(entry, output_prefix),
                "matched_field": matched_field,
                "match_method": _match_method(matched_term, canonical, entry, output_prefix),
                "confidence": _confidence(entry, matched_field, matched_term, output_prefix),
                "mention_count": int(mention_count),
            }
            for field in metadata_fields:
                out_row[field] = entry.get(field, "")
            rows.append(out_row)
            counts[canonical]["record_count"] += 1
            counts[canonical]["mention_count"] += int(mention_count)
        last_progress = time.time()

    terms_df = pd.DataFrame(rows)
    count_rows = []
    for canonical, counter in counts.items():
        entry = next((row for row in rows if row["canonical_name"] == canonical), {})
        count_row = {
            "canonical_name": canonical,
            "record_count": int(counter["record_count"]),
            "mention_count": int(counter["mention_count"]),
            "source": entry.get("source", ""),
            "source_reference": entry.get("source_reference", ""),
            "matched_field": entry.get("matched_field", ""),
            "match_method": entry.get("match_method", ""),
            "confidence": entry.get("confidence", ""),
        }
        for field in metadata_fields:
            count_row[field] = entry.get(field, "")
        count_rows.append(count_row)
    counts_df = pd.DataFrame(count_rows).sort_values(["record_count", "mention_count", "canonical_name"], ascending=[False, False, True]) if count_rows else pd.DataFrame(columns=["canonical_name", "record_count", "mention_count", "source", "source_reference", "matched_field", "match_method", "confidence", *metadata_fields])

    outputs_dir.mkdir(parents=True, exist_ok=True)
    terms_path = outputs_dir / f"{slug}_{output_prefix}_terms.csv"
    counts_path = outputs_dir / f"{slug}_{output_prefix}_term_counts.csv"
    iterator = [("terms", terms_df, terms_path), ("counts", counts_df, counts_path)]
    if progress_fn:
        iterator = progress_fn(iterator, desc=f"Writing {output_prefix} outputs", total=2, unit="files")
    for _, dataframe_to_write, path in iterator:
        dataframe_to_write.to_csv(path, index=False)
    return terms_path, counts_path


def extract_geographic_terms(dataframe: pd.DataFrame, slug: str, outputs_dir: Path = OUTPUTS_DIR) -> tuple[Path, Path]:
    return extract_reference_terms(
        dataframe,
        slug,
        GEOGRAPHIC_CACHE_PATH,
        "geographic",
        ("geo_type", "state", "country", "latitude", "longitude"),
        outputs_dir,
    )


def extract_demographic_terms(dataframe: pd.DataFrame, slug: str, outputs_dir: Path = OUTPUTS_DIR) -> tuple[Path, Path]:
    return extract_reference_terms(
        dataframe,
        slug,
        DEMOGRAPHIC_CACHE_PATH,
        "demographic",
        ("category", "mesh_ui", "tree_numbers"),
        outputs_dir,
    )
