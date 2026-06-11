#!/usr/bin/env python3
"""Create publication-ready bibliometric visualizations from pipeline outputs.

This script is intentionally standalone: it only reads from data/outputs and
data/VOS, and writes visualization artifacts to data/visuals.
"""

from __future__ import annotations

import csv
import difflib
import argparse
import json
import math
import os
import re
import shutil
import struct
import textwrap
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote_plus

from utils.map_providers import (
    IMPORTANT_COUNTRY_ISO3,
    MAPS_ROOT,
    REQUIRED_WORLD_BASEMAP_COUNTRIES,
    build_world_adm0_geojson,
    country_admin_boundary_file,
    census_boundary_dir,
    census_boundary_shapefile,
    census_city_gazetteer_dir,
    custom_gazetteer_dir,
    geoboundaries_boundary_file,
    geonames_dir,
    report_missing_map_packages,
    select_country_boundary_provider,
    world_adm0_geojson_path,
)
from utils.runtime_paths import configure_matplotlib_cache

ROOT = Path(__file__).resolve().parent
configure_matplotlib_cache(ROOT)

try:
    from rapidfuzz import fuzz, process
except ImportError:  # pragma: no cover - optional acceleration
    fuzz = None
    process = None

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.patheffects as path_effects
    import matplotlib.pyplot as plt
    import networkx as nx
    import pandas as pd
    import plotly.express as px
    import plotly.graph_objects as go
    import requests
    import seaborn as sns
    from tqdm import tqdm
except ImportError as exc:  # pragma: no cover - user-facing startup guard
    raise SystemExit(
        "Missing visualization dependency: "
        f"{exc.name}. Install requirements with `pip install -r requirements.txt`."
    ) from exc


OUTPUTS_DIR = ROOT / "data" / "outputs"
VOS_DIR = ROOT / "data" / "VOS"
VISUALS_DIR = ROOT / "data" / "visuals"
PROCESSED_DIR = ROOT / "data" / "processed"
REFERENCE_DIR = ROOT / "data" / "reference"
RXNORM_CACHE_PATH = REFERENCE_DIR / "rxnorm_cache.json"
DEBUG = False
EXPORT_ONLY_PNG = True
EXPORT_UNLABELED = False
ENABLE_HINDEX_AUTHOR_VIS = True
ENABLE_GEO_MAPS = True
VALIDATION_REPORT = PROCESSED_DIR / "visualization_validation_report.txt"
MAP_COLORMAP = "RdPu"
MAP_ZERO_COLOR_INCLUDED = True
MAP_NODATA_COLOR = "#eef0f4"
MAP_ZERO_COLOR = "#e9e4f0"
MAP_CAPTION_CHOROPLETH = "Shading reflects detected publication geography, not prevalence or disease burden."
MAP_CAPTION_POINTS = "Points reflect detected publication locations, not prevalence or disease burden."
ENABLE_WORLD_REGIONS_OVERVIEW = False
MAP_USE_LOG_SCALE = False
MAX_WORLD_MAP_LABELS = 5
MAX_REGIONAL_MAP_LABELS = 5
MAX_COUNTRY_MAP_LABELS = 10
MAX_US_MAP_LABELS = 10
MAX_TEXAS_MAP_LABELS = 8
MAX_MAP_LABELS = {"world": MAX_WORLD_MAP_LABELS, "us": MAX_US_MAP_LABELS, "texas": MAX_TEXAS_MAP_LABELS, "country": MAX_COUNTRY_MAP_LABELS, "region": MAX_REGIONAL_MAP_LABELS}
COUNTRY_MAP_MIN_FREQUENCY = 3
COUNTRY_MAP_MIN_SUBNATIONAL_TERMS = 2
PRIORITY_COUNTRY_MAPS = {
    "united states",
    "united kingdom",
    "canada",
    "germany",
    "spain",
    "france",
    "italy",
    "belgium",
    "australia",
    "japan",
    "china",
    "brazil",
    "colombia",
    "croatia",
    "netherlands",
    "poland",
    "portugal",
    "sweden",
    "norway",
    "turkey",
    "turkiye",
}
INCLUDE_POINT_MAPS = False
INCLUDE_LABELED_POINT_MAPS = True
LABEL_CITY_POINTS = True
LABEL_INSTITUTION_POINTS = True
LABEL_TOP_CITIES = 5
LABEL_TOP_INSTITUTIONS = 5

SKIP_EXACT_STEMS = {
    "country_list_debug",
    "country_unparsed_debug",
    "debug_recent_years",
    "deduplication_log",
    "covidence_ingestion_log",
    "missing_metadata_log",
    "wos_validation_log",
}
SKIP_STEM_SUFFIXES = (
    "_debug",
    "_log",
    "_validation",
    "_validation_log",
    "_raw",
    "_excluded_records",
    "_qa_summary",
    "_vosviewer",
)
REQUIRED_CORE_COLUMNS = ("title", "authors", "year")
DERIVED_ONLY_COLUMNS = (
    "h_index",
    "year_count",
    "edge",
    "rank",
)
TOP_COUNTRIES = 8
TOP_AUTHORS = 20
TOP_PAPERS = 20
TOP_KEYWORDS = 75
MIN_KEYWORD_FREQUENCY = 3
KEYWORD_PIPELINE_MIN_FREQUENCY = 2
MAX_KEYWORDS_PER_RECORD = 12
FINAL_KEYWORD_TOP_NODES = 50
FINAL_KEYWORD_LABEL_TOP_N = 20
FINAL_KEYWORD_MIN_EDGE_WEIGHT = 2
STATIC_DPI = 320
BACKGROUND = "#f8f9fa"
INK = "#111827"
MUTED = "#6b7280"
GRID = "#dbe2ea"
EMPHASIS = "#ff3b30"
DRUG_COLOR = "#d946ef"
PROCEDURE_COLOR = "#0f766e"
COMBINED_EDGE_COLOR = "#f97316"

COLOR_SEQUENCE = [
    "#2563eb",
    "#f97316",
    "#10b981",
    "#a855f7",
    "#ef4444",
    "#06b6d4",
    "#eab308",
    "#ec4899",
    "#64748b",
    "#84cc16",
]

STOPWORDS = {
    "a",
    "an",
    "among",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "with",
    "without",
    "using",
    "via",
    "disease",
    "diseases",
    "patient",
    "patients",
    "study",
    "studies",
    "clinical",
    "case",
    "cases",
    "review",
    "reviews",
    "analysis",
    "analyses",
    "results",
}

DOMAIN_GENERIC_TERMS = {
    "analysis",
    "article",
    "associated",
    "case",
    "cases",
    "clinical",
    "cohort",
    "data",
    "disease",
    "effect",
    "effects",
    "evidence",
    "findings",
    "human",
    "humans",
    "management",
    "outcome",
    "outcomes",
    "patient",
    "patients",
    "report",
    "reports",
    "research",
    "related",
    "review",
    "risk",
    "series",
    "study",
    "syndrome",
    "therapy",
    "treatment",
    "trial",
}

FILTERED_DOMAIN_TERMS = DOMAIN_GENERIC_TERMS | {"disease", "patient", "study", "clinical", "case"}

KEYWORD_NETWORK_JUNK_TERMS = {
    "any",
    "best",
    "central",
    "made",
    "male",
    "model town",
    "most",
    "new",
    "paper",
    "same",
    "than",
    "time",
    "after",
    "during",
    "introduction",
    "healthy village",
    "standard village",
    "the village",
    "village",
    "city",
    "town",
    "place",
}

KEYWORD_NETWORK_JUNK_WORDS = {
    "any",
    "best",
    "central",
    "made",
    "male",
    "most",
    "new",
    "paper",
    "same",
    "than",
    "time",
    "after",
    "during",
    "introduction",
}

LOW_VALUE_PLACE_SUFFIXES = {"city", "town", "village", "street", "central", "place"}

MEANINGFUL_SINGLE_TERMS = {
    "adults",
    "antibody",
    "autoantibodies",
    "children",
    "complement",
    "covid",
    "covid-19",
    "eculizumab",
    "genetics",
    "kidney",
    "mutation",
    "mutations",
    "nephrology",
    "pediatric",
    "pregnancy",
    "ravulizumab",
    "recurrence",
    "renal",
    "thrombotic",
    "transplant",
    "transplantation",
}

DRUG_TERMS = {
    "abatacept",
    "adalimumab",
    "alemtuzumab",
    "amoxicillin",
    "anakinra",
    "aspirin",
    "eculizumab",
    "ravulizumab",
    "narsoplimab",
    "avacopan",
    "rituximab",
    "belimumab",
    "bevacizumab",
    "caplacizumab",
    "cyclosporine",
    "cyclophosphamide",
    "dabrafenib",
    "dexamethasone",
    "durvalumab",
    "emicizumab",
    "everolimus",
    "heparin",
    "hydroxychloroquine",
    "imatinib",
    "infliximab",
    "intravenous immunoglobulin",
    "ivig",
    "methylprednisolone",
    "mycophenolate",
    "nifedipine",
    "nivolumab",
    "pembrolizumab",
    "penicillin",
    "prednisone",
    "sirolimus",
    "tacrolimus",
    "tocilizumab",
    "trametinib",
    "tremelimumab",
    "warfarin",
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
PATTERN_DRUG_SUFFIXES = ("mab", "zumab", "nib", "statin", "azole", "vir")
TTY_COLOR_MAP = {
    "IN": "#1d4ed8",
    "SCD": "#0f766e",
    "SBD": "#f97316",
    "default": "#6b7280",
}
NODE_TYPE_COLORS = {
    "Drug": DRUG_COLOR,
    "Procedural": PROCEDURE_COLOR,
    "Both": "#7c3aed",
    "Unknown": "#9ca3af",
    "Institution": "#2563eb",
    "Keyword": "#10b981",
    "Author": "#a855f7",
    "Geography": "#2563eb",
    "Demographic": "#0f766e",
    "Procedure": PROCEDURE_COLOR,
    "Intervention": "#f97316",
    "General/topic keyword": "#10b981",
    "Demographic/population": "#0f766e",
    "Drug/substance": DRUG_COLOR,
    "Procedure/intervention": PROCEDURE_COLOR,
}
NODE_TYPE_SHAPES = {
    "Drug": "D",
    "Procedural": "o",
    "Both": "h",
    "Unknown": "o",
    "Institution": "o",
    "Keyword": "o",
    "Author": "o",
    "Geography": "o",
    "Demographic": "s",
    "Procedure": "^",
    "Intervention": "h",
    "General/topic keyword": "o",
    "Demographic/population": "s",
    "Drug/substance": "D",
    "Procedure/intervention": "^",
}
PLOTLY_SYMBOLS = {
    "Drug": "diamond",
    "Procedural": "circle",
    "Both": "hexagon",
    "Unknown": "circle",
    "Institution": "circle",
    "Keyword": "circle",
    "Author": "circle",
}
DRUG_CONTEXT_TERMS = {
    "administered",
    "antibody",
    "dose",
    "doses",
    "drug",
    "inhibitor",
    "medication",
    "pharmacologic",
    "therapy",
    "therapeutic",
    "treated",
    "treatment",
}
COMMON_ENGLISH_WORDS = {
    "abstract",
    "among",
    "analysis",
    "author",
    "background",
    "children",
    "clinical",
    "control",
    "disease",
    "factor",
    "health",
    "human",
    "kidney",
    "method",
    "patient",
    "patients",
    "protein",
    "report",
    "result",
    "review",
    "risk",
    "study",
    "syndrome",
    "therapy",
    "transplant",
}
PROCEDURAL_SEED_TERMS = {
    "ablation",
    "airway management",
    "angioplasty",
    "biopsy",
    "bronchoscopy",
    "cardiac catheterization",
    "catheterization",
    "chemotherapy",
    "dialysis",
    "endoscopy",
    "exchange transfusion",
    "gene therapy",
    "hemodialysis",
    "immunotherapy",
    "intubation",
    "kidney transplantation",
    "laparoscopy",
    "mechanical ventilation",
    "occupational therapy",
    "oxygen therapy",
    "peritoneal dialysis",
    "physical therapy",
    "plasma exchange",
    "plasmapheresis",
    "radiation therapy",
    "radiotherapy",
    "rehabilitation",
    "renal replacement therapy",
    "resection",
    "surgery",
    "surgical procedure",
    "transfusion",
    "transplant",
    "transplantation",
    "ventilation",
}
PROCEDURAL_HEAD_TERMS = {
    "ablation",
    "biopsy",
    "catheterization",
    "dialysis",
    "endoscopy",
    "exchange",
    "intubation",
    "laparoscopy",
    "plasmapheresis",
    "procedure",
    "procedures",
    "rehabilitation",
    "resection",
    "stimulation",
    "surgery",
    "therapy",
    "therapies",
    "transfusion",
    "transplant",
    "transplantation",
    "ventilation",
}
PROCEDURAL_CONTEXT_TERMS = {
    "intervention",
    "interventions",
    "operative",
    "procedure",
    "procedures",
    "rehabilitation",
    "surgical",
    "therapeutic",
    "therapies",
    "therapy",
    "treatment",
}
PROCEDURAL_GENERIC_TERMS = {
    "intervention",
    "interventions",
    "procedure",
    "procedures",
    "therapy",
    "therapies",
    "treatment",
    "treatments",
}
PROCEDURAL_RELATION_TERMS = {
    "after",
    "before",
    "between",
    "during",
    "following",
    "versus",
    "with",
    "without",
}
DRUG_VOCAB_CANDIDATES = (
    ROOT / "data" / "reference" / "drug_dictionary.csv",
    ROOT / "data" / "drug_vocab" / "rxnorm.csv",
    ROOT / "data" / "drug_vocab" / "drugbank.csv",
    ROOT / "data" / "drug_vocab" / "drug_names.csv",
    ROOT / "data" / "raw" / "rxnorm.csv",
    ROOT / "data" / "raw" / "drugbank.csv",
    ROOT / "data" / "raw" / "drug_names.csv",
    ROOT / "data" / "raw" / "RXNCONSO.RRF",
)
PROCEDURE_VOCAB_CANDIDATES = (
    ROOT / "data" / "reference" / "procedure_dictionary.csv",
    ROOT / "data" / "reference" / "procedural_interventions.csv",
    ROOT / "data" / "reference" / "snomed_procedures.csv",
    ROOT / "data" / "reference" / "umls_procedures.csv",
    ROOT / "data" / "umls" / "procedures.csv",
    ROOT / "data" / "snomed" / "procedures.csv",
)
_DRUG_VOCAB_CACHE: set[str] | None = None
_PROCEDURE_VOCAB_CACHE: set[str] | None = None
_RXNORM_CACHE: dict[str, dict[str, str | bool | None]] | None = None
_RXNORM_UNAVAILABLE = False
RUN_FILE_SUFFIXES = (
    "_year_limited_records",
    "_target_year_range",
    "_raw",
    "_cleaned",
    "_publication_years",
    "_year_counts",
    "_year_counts_target_year_range",
    "_country_year_counts",
    "_institution_year_counts",
    "_top_authors",
    "_top_papers",
    "_country_publication_rankings",
    "_country_citation_rankings",
    "_institution_publication_rankings",
    "_institution_citation_rankings",
    "_institution_locations",
    "_author_edges",
    "_institution_edges",
    "_geographic_terms",
    "_geographic_term_counts",
    "_geographic_terms_cleaned",
    "_geographic_term_counts_cleaned",
    "_demographic_terms_cleaned",
    "_demographic_term_counts_cleaned",
    "_drug_terms_cleaned",
    "_drug_term_counts_cleaned",
    "_procedure_terms_cleaned",
    "_procedure_term_counts_cleaned",
    "_intervention_terms",
    "_intervention_term_counts",
)


def safe_read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


@contextmanager
def timed_stage(label: str):
    print(f"{label}...")
    start = time.time()
    try:
        yield
    except Exception as exc:
        print(f"[ERROR] {label} failed after {time.time() - start:.2f}s: {exc}")
        raise
    print(f"{label} completed in {time.time() - start:.2f}s")


def debug_print(message: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {message}")


def run_optional_visual(label: str, callback):
    try:
        return callback()
    except Exception as exc:
        print(f"WARNING: Skipping optional visual family '{label}' after error: {exc.__class__.__name__}: {exc}")
        return None


def require_core_columns(df: pd.DataFrame, core_path: Path) -> None:
    columns = {col.lower() for col in df.columns}
    keyword_columns = {"keywords", "keyword", "author_keywords", "index_keywords", "mesh_terms", "mesh"}
    missing = []
    if "title" not in columns:
        missing.append("title")
    if missing:
        raise ValueError(
            f"Required column(s) missing from {core_path}: {', '.join(missing)}. "
            "Expected at least title; abstract and keyword columns are optional."
        )
    if not columns & keyword_columns:
        print(f"WARNING: {core_path.name} has no keyword columns; keyword extraction will use title/abstract text where available.")


def clean_file_candidates(paths: Iterable[Path]) -> list[Path]:
    kept = []
    for path in paths:
        stem = path.stem.lower()
        if stem in SKIP_EXACT_STEMS or any(stem.endswith(suffix) for suffix in SKIP_STEM_SUFFIXES):
            continue
        kept.append(path)
    return sorted(kept)


def active_slug_from_core(core_path: Path) -> str:
    stem = core_path.stem
    for suffix in ("_year_limited_records", "_target_year_range", "_cleaned", "_raw"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def exact_slug_file(path: Path, slug: str) -> bool:
    stem = path.stem
    return stem == slug or any(stem == f"{slug}{suffix}" or stem.startswith(f"{slug}{suffix}_") for suffix in RUN_FILE_SUFFIXES)


def detect_files(slug: str = "", core: Path | None = None) -> dict[str, Path | list[Path] | None]:
    active_slug = slug or (active_slug_from_core(core) if core else "")
    output_csvs = clean_file_candidates(OUTPUTS_DIR.glob("*.csv")) if OUTPUTS_DIR.exists() else []
    vos_networks = clean_file_candidates(VOS_DIR.glob("*_network.txt")) if VOS_DIR.exists() else []
    if active_slug:
        output_csvs = [path for path in output_csvs if exact_slug_file(path, active_slug)]
        vos_networks = [path for path in vos_networks if exact_slug_file(path, active_slug)]
    if core:
        output_csvs = sorted(set([*output_csvs, core]))
    detected_core = core or detect_core_dataset(output_csvs)

    return {
        "core": detected_core,
        "networks": vos_networks,
        "year_counts": find_publication_year_file(output_csvs),
        "country_year_counts": find_first(output_csvs, ("_country_year_counts.csv",)),
        "institution_year_counts": find_first(output_csvs, ("_institution_year_counts.csv",)),
        "top_authors": find_first(output_csvs, ("_top_authors.csv",)),
        "top_papers": find_first(output_csvs, ("_top_papers.csv",)),
        "country_publication_rankings": find_first(output_csvs, ("_country_publication_rankings.csv",)),
        "country_citation_rankings": find_first(output_csvs, ("_country_citation_rankings.csv",)),
        "institution_publication_rankings": find_first(output_csvs, ("_institution_publication_rankings.csv",)),
        "institution_citation_rankings": find_first(output_csvs, ("_institution_citation_rankings.csv",)),
        "author_edges": find_first(output_csvs, ("_author_edges.csv",)),
        "institution_edges": find_first(output_csvs, ("_institution_edges.csv",)),
    }


def detect_core_dataset(output_csvs: list[Path]) -> Path | None:
    candidates: list[tuple[Path, int]] = []
    inspected: list[str] = []
    for path in output_csvs:
        try:
            sample = pd.read_csv(path, nrows=5)
        except Exception as exc:
            inspected.append(f"{path.name}: unreadable ({exc.__class__.__name__})")
            continue

        columns = {str(column).strip().lower() for column in sample.columns}
        missing = [column for column in REQUIRED_CORE_COLUMNS if column not in columns]
        derived = [column for column in columns if any(marker in column for marker in DERIVED_ONLY_COLUMNS)]
        if missing:
            inspected.append(f"{path.name}: missing {', '.join(missing)}")
            continue
        if derived:
            inspected.append(f"{path.name}: derived-only columns {', '.join(sorted(derived))}")
            continue

        candidates.append((path, count_csv_rows(path)))

    if len(candidates) == 1:
        return candidates[0][0]
    if len(candidates) > 1:
        year_limited_candidates = [item for item in candidates if item[0].stem.endswith("_year_limited_records")]
        if len(year_limited_candidates) == 1:
            return year_limited_candidates[0][0]
        if len(year_limited_candidates) > 1:
            candidates = year_limited_candidates
        cleaned_candidates = [item for item in candidates if item[0].stem.endswith("_cleaned")]
        if len(cleaned_candidates) == 1:
            return cleaned_candidates[0][0]
        if len(cleaned_candidates) > 1:
            candidates = cleaned_candidates
        candidates.sort(key=lambda item: (item[0].stem.endswith("_cleaned"), item[1]), reverse=True)
        print("Multiple core dataset candidates found; using the preferred cleaned/largest dataset:")
        for idx, (path, row_count) in enumerate(candidates, start=1):
            print(f"  {idx}. {path} ({row_count:,} rows)")
        return candidates[0][0]
    if inspected:
        print("Inspected CSV files:")
        for detail in inspected:
            print(f"  {detail}")
    return None


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        row_count = sum(1 for _ in handle)
    return max(0, row_count - 1)


def find_first(paths: Iterable[Path], suffixes: tuple[str, ...]) -> Path | None:
    return next((path for path in paths if path.name.endswith(suffixes)), None)


def find_publication_year_file(paths: Iterable[Path]) -> Path | None:
    paths = list(paths)
    preferred = next((path for path in paths if path.name.endswith("_year_counts_target_year_range.csv")), None)
    if preferred:
        return preferred
    return next(
        (
            path
            for path in paths
            if path.name.endswith(("_publication_years.csv", "_year_counts.csv"))
            and "_country_year_counts.csv" not in path.name
            and "_institution_year_counts.csv" not in path.name
        ),
        None,
    )


def configure_plotly_layout(fig: go.Figure, title: str, x_title: str = "", y_title: str = "") -> go.Figure:
    fig.update_layout(
        title={"text": title, "x": 0.02, "xanchor": "left"},
        template="plotly_white",
        colorway=COLOR_SEQUENCE,
        font={"family": "Inter, DejaVu Sans, Arial, sans-serif", "size": 15, "color": INK},
        title_font={"size": 26},
        legend={"title": None, "orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        margin={"l": 72, "r": 36, "t": 92, "b": 64},
        plot_bgcolor=BACKGROUND,
        paper_bgcolor=BACKGROUND,
    )
    fig.update_xaxes(title=x_title, showgrid=False, zeroline=False, linecolor="#cbd5e1", ticks="outside")
    fig.update_yaxes(title=y_title, gridcolor=GRID, zeroline=False, linecolor="#cbd5e1", ticks="outside")
    return fig


def remember_generated(generated: list[Path], path: Path) -> None:
    if path not in generated:
        generated.append(path)


def save_figure(fig: go.Figure, stem: str, generated: list[Path]) -> None:
    png_path = VISUALS_DIR / f"{stem}.png"
    try:
        fig.write_image(png_path, scale=2)
        remember_generated(generated, png_path)
    except Exception as exc:
        print(f"Skipping Plotly PNG export for {stem}: {exc.__class__.__name__}. Matplotlib/static companion will be used when available.")


def apply_static_style() -> None:
    sns.set_theme(
        context="talk",
        style="ticks",
        font="DejaVu Sans",
        rc={
            "axes.edgecolor": "#cbd5e1",
            "axes.labelcolor": INK,
            "axes.titlecolor": INK,
            "axes.titlesize": 26,
            "axes.titleweight": "bold",
            "axes.labelsize": 16,
            "xtick.color": MUTED,
            "ytick.color": INK,
            "grid.color": GRID,
            "figure.facecolor": BACKGROUND,
            "axes.facecolor": BACKGROUND,
            "savefig.facecolor": BACKGROUND,
        },
    )


def save_static_formats(fig: plt.Figure, stem: str, generated: list[Path]) -> None:
    png_path = VISUALS_DIR / f"{stem}.png"
    fig.savefig(png_path, dpi=STATIC_DPI, bbox_inches="tight", facecolor=fig.get_facecolor())
    remember_generated(generated, png_path)


def processed_path(name: str) -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    return PROCESSED_DIR / name


def add_subtitle(ax: plt.Axes, subtitle: str) -> None:
    ax.text(0, 1.01, subtitle, transform=ax.transAxes, ha="left", va="bottom", fontsize=13, color=MUTED)


def save_static_line(
    df: pd.DataFrame,
    stem: str,
    title: str,
    x: str,
    y: str,
    generated: list[Path],
    hue: str | None = None,
) -> None:
    apply_static_style()
    width = 14 if hue else 12
    fig, ax = plt.subplots(figsize=(width, 8), dpi=STATIC_DPI)
    palette = COLOR_SEQUENCE
    if hue:
        palette = COLOR_SEQUENCE[: max(1, df[hue].nunique())]
    sns.lineplot(
        data=df,
        x=x,
        y=y,
        hue=hue,
        marker="o",
        linewidth=2.6,
        markersize=7,
        palette=palette if hue else None,
        color=COLOR_SEQUENCE[0] if hue is None else None,
        ax=ax,
    )
    ax.set_title(title, loc="left", fontsize=26, pad=22, weight="bold")
    ax.set_xlabel(x.replace("_", " ").title())
    ax.set_ylabel(y.replace("_", " ").title())
    ax.spines[["top", "right"]].set_visible(False)
    if hue:
        ax.legend(title="", bbox_to_anchor=(0, 1.02, 1, 0.2), loc="lower left", mode="expand", ncol=4, frameon=False)
    fig.tight_layout()
    save_static_formats(fig, stem, generated)
    plt.close(fig)


def save_static_bar(
    df: pd.DataFrame,
    stem: str,
    title: str,
    label_col: str,
    value_col: str,
    generated: list[Path],
    color: str = "#2563eb",
    highlight_n: int = 0,
    subtitle: str = "",
) -> None:
    apply_static_style()
    plot_df = df.sort_values(value_col, ascending=False).reset_index(drop=True)
    fig_height = max(7, 0.50 * len(plot_df) + 2)
    fig, ax = plt.subplots(figsize=(13.5, fig_height), dpi=STATIC_DPI)
    values = plot_df[value_col].astype(float).to_numpy()
    norm = plt.Normalize(values.min() if len(values) else 0, values.max() if len(values) else 1)
    colors = [plt.cm.viridis(norm(value)) for value in values]
    for idx in range(min(highlight_n, len(colors))):
        colors[idx] = EMPHASIS
    bars = ax.barh(plot_df[label_col], plot_df[value_col], color=colors, height=0.68)
    ax.invert_yaxis()
    ax.set_title(title, loc="left", fontsize=26, pad=24, weight="bold")
    if subtitle:
        add_subtitle(ax, subtitle)
    ax.set_xlabel(value_col.replace("_", " ").title())
    ax.set_ylabel("")
    ax.grid(False)
    ax.xaxis.grid(True, color=GRID, linewidth=0.7, alpha=0.55)
    ax.tick_params(axis="y", labelsize=12)
    ax.tick_params(axis="x", labelsize=12)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#cbd5e1")
    max_value = max(values) if len(values) else 1
    for idx, (bar, value) in enumerate(zip(bars, values, strict=False)):
        ax.text(
            bar.get_width() + max_value * 0.012,
            bar.get_y() + bar.get_height() / 2,
            f"{int(value):,}",
            va="center",
            ha="left",
            fontsize=11,
            color=INK,
            weight="bold" if idx < highlight_n else "normal",
        )
    for idx, label in enumerate(ax.get_yticklabels()):
        if idx < highlight_n:
            label.set_weight("bold")
            label.set_color(INK)
    fig.tight_layout()
    save_static_formats(fig, stem, generated)
    plt.close(fig)


def publication_trends(path: Path, generated: list[Path]) -> None:
    df = safe_read_csv(path)
    if "year" not in df.columns or "publications" not in df.columns:
        print(f"Skipping publication trends; missing year/publications in {path.name}.")
        return
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["publications"] = pd.to_numeric(df["publications"], errors="coerce")
    df = df.dropna(subset=["year", "publications"]).sort_values("year")
    df["rolling_average"] = df["publications"].rolling(3, center=True, min_periods=1).mean()
    peak = df.loc[df["publications"].idxmax()]
    recent_year = df["year"].max()
    recent_cutoff = recent_year - 5

    fig = px.line(df, x="year", y=["publications", "rolling_average"], markers=True)
    fig.update_traces(line={"width": 3}, marker={"size": 8})
    fig.add_annotation(
        x=peak["year"],
        y=peak["publications"],
        text=f"Peak: {int(peak['publications']):,}",
        showarrow=True,
        arrowhead=2,
        bgcolor="rgba(248,249,250,0.92)",
    )
    configure_plotly_layout(fig, "Publication Trends", "Year", "Publications")
    save_figure(fig, "publication_trends", generated)
    time_fig = go.Figure(fig)
    time_fig.update_layout(title={"text": "Time Evolution of Publications", "x": 0.02, "xanchor": "left"})
    save_figure(time_fig, "time_evolution", generated)
    save_publication_trend_static(df, peak, recent_cutoff, "publication_trends", generated)
    save_publication_trend_static(df, peak, recent_cutoff, "time_evolution", generated, title="Time Evolution of Publications")


def save_publication_trend_static(
    df: pd.DataFrame,
    peak: pd.Series,
    recent_cutoff: float,
    stem: str,
    generated: list[Path],
    title: str = "Publication Trends",
) -> None:
    apply_static_style()
    fig, ax = plt.subplots(figsize=(14, 8), dpi=STATIC_DPI)
    years = df["year"].astype(float)
    pubs = df["publications"].astype(float)
    colors = plt.cm.plasma((years - years.min()) / max(years.max() - years.min(), 1))
    ax.vlines(years, 0, pubs, colors=colors, linewidth=2.2, alpha=0.72)
    ax.scatter(years, pubs, c=colors, s=62, edgecolors=BACKGROUND, linewidths=1.2, zorder=3)
    ax.plot(years, df["rolling_average"], color=INK, linewidth=3.2, label="3-year rolling average", zorder=4)
    recent = df[df["year"] >= recent_cutoff]
    if not recent.empty:
        ax.fill_between(recent["year"].astype(float), recent["publications"].astype(float), color=EMPHASIS, alpha=0.13)
        ax.text(
            recent["year"].min(),
            max(pubs) * 0.92,
            "Recent period",
            fontsize=12,
            weight="bold",
            color=EMPHASIS,
        )
    ax.annotate(
        f"Peak year: {int(peak['year'])}\n{int(peak['publications']):,} publications",
        xy=(peak["year"], peak["publications"]),
        xytext=(peak["year"], peak["publications"] + max(pubs) * 0.18),
        arrowprops={"arrowstyle": "->", "color": EMPHASIS, "lw": 1.8},
        fontsize=12,
        color=INK,
        weight="bold",
        bbox={"boxstyle": "round,pad=0.35", "fc": "white", "ec": "#e5e7eb", "alpha": 0.94},
    )
    ax.set_title(title, loc="left", pad=24)
    add_subtitle(ax, "Annual output with smoothed trajectory and recent activity highlighted")
    ax.set_xlabel("Year")
    ax.set_ylabel("Publications")
    ax.legend(frameon=False, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.55)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    save_static_formats(fig, stem, generated)
    plt.close(fig)


def country_trends(path: Path, generated: list[Path], top_n: int = TOP_COUNTRIES) -> None:
    df = safe_read_csv(path)
    required = {"year", "country", "publications"}
    if not required.issubset(df.columns):
        print(f"Skipping country trends; missing {required - set(df.columns)} in {path.name}.")
        return
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["publications"] = pd.to_numeric(df["publications"], errors="coerce")
    df = df.dropna(subset=["year", "publications"])
    df = df[df["publications"] > 0]
    if df.empty or df["country"].dropna().empty:
        reason = "empty table after cleaning"
        if not path.exists():
            reason = "country_year_counts.csv missing"
        elif "country" not in safe_read_csv(path).columns:
            reason = "missing country column"
        elif "year" not in safe_read_csv(path).columns:
            reason = "missing year column"
        elif "publications" not in safe_read_csv(path).columns:
            reason = "missing publications column"
        elif not pd.to_numeric(safe_read_csv(path).get("publications", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0).any():
            reason = "all counts zero"
        print(f"Country Publication Trends skipped: no usable country-year data found. Source problem: {reason}.")
        return
    top_n = min(top_n, max(3, df["country"].nunique()))
    top = df.groupby("country")["publications"].sum().nlargest(top_n).index
    df = df[df["country"].isin(top)].sort_values(["country", "year"])
    fig = px.line(df, x="year", y="publications", color="country", markers=True, color_discrete_sequence=COLOR_SEQUENCE)
    fig.update_traces(line={"width": 2.8}, marker={"size": 7})
    configure_plotly_layout(fig, f"Country Publication Trends: Top {top_n}", "Year", "Publications")
    save_figure(fig, "country_trends", generated)
    save_static_line(
        df,
        "country_trends",
        f"Country Publication Trends: Top {top_n}",
        "year",
        "publications",
        generated,
        hue="country",
    )


def horizontal_bar(
    path: Path,
    label_col: str,
    value_col: str,
    title: str,
    stem: str,
    generated: list[Path],
    top_n: int,
) -> None:
    df = safe_read_csv(path)
    if label_col not in df.columns or value_col not in df.columns:
        print(f"Skipping {title}; missing {label_col}/{value_col} in {path.name}.")
        return
    df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
    top_n = min(top_n, max(8, len(df)))
    df = df.dropna(subset=[value_col]).nlargest(top_n, value_col).reset_index(drop=True)
    width = 86 if label_col == "title" else 64
    df[label_col] = df[label_col].map(lambda value: textwrap.shorten(str(value), width=width, placeholder="..."))
    highlight_n = 3 if label_col == "title" else 5
    df["rank"] = range(1, len(df) + 1)
    df["emphasis"] = df["rank"].le(highlight_n).map({True: "Highlighted", False: "Ranked"})
    fig = px.bar(
        df.sort_values(value_col, ascending=True),
        x=value_col,
        y=label_col,
        orientation="h",
        color=value_col,
        color_continuous_scale="Plasma",
        text=value_col,
    )
    fig.update_traces(texttemplate="%{text:,}", textposition="outside", marker_line_width=0)
    fig.update_layout(coloraxis_showscale=False)
    configure_plotly_layout(fig, title, value_col.replace("_", " ").title(), "")
    fig.update_yaxes(automargin=True)
    save_figure(fig, stem, generated)
    subtitle = "Most cited papers in dataset" if label_col == "title" else ""
    save_static_bar(df, stem, title, label_col, value_col, generated, highlight_n=highlight_n, subtitle=subtitle)


def read_edge_csv(path: Path) -> pd.DataFrame:
    df = safe_read_csv(path)
    required = {"source", "target"}
    if not required.issubset(df.columns):
        return pd.DataFrame(columns=["source", "target", "weight"])
    if "weight" not in df.columns:
        df["weight"] = 1
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce").fillna(1)
    return df[["source", "target", "weight"]]


@dataclass
class NetworkPlotConfig:
    title: str
    stem: str
    node_kind: str = "Keyword"
    min_node_frequency: int = 1
    min_edge_weight: int = 2
    top_n_nodes: int | None = 50
    top_n_edges: int | None = None
    label_top_n: int = 15
    drop_isolates: bool = True
    min_component_size: int = 3
    keep_largest_component: bool = True
    include_unknown: bool = True
    unknown_top_n: int = 20
    node_metric: str = "weighted_degree"
    label_width: int = 32
    edge_alpha: float = 0.18
    max_edge_width: float = 2.4
    subtitle: str = ""
    size_metric: str = "frequency"
    border_metric: str = ""
    network_type: str = ""
    input_keyword_table: str = ""
    terms_suppressed: int = 0
    node_debug_stem: str = ""
    edge_debug_stem: str = ""


def truncate_label(value: object, width: int = 32) -> str:
    return textwrap.shorten(str(value), width=width, placeholder="...")


def normalize_node_term(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.endswith("ies") and len(text) > 4:
        return f"{text[:-3]}y"
    if text.endswith("s") and len(text) > 3 and not text.endswith("ss"):
        singular = text[:-1]
        if singular in DRUG_TERMS or singular in PROCEDURAL_SEED_TERMS or singular in PROCEDURAL_HEAD_TERMS:
            return singular
    return text


def normalize_institution_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\b(univ)\b\.?", "University", text, flags=re.I)
    text = re.sub(r"\b(dept|department)\b.*$", "", text, flags=re.I).strip(" ,;")
    text = re.sub(r"\b(school|faculty) of (medicine|medical sciences)\b", "", text, flags=re.I).strip(" ,;")
    text = re.sub(r"\b(inc|ltd|llc)\b\.?", "", text, flags=re.I).strip(" ,;")
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "harvard med sch": "Harvard Medical School",
        "university of washington seattle": "University of Washington",
        "mayo clin": "Mayo Clinic",
    }
    key = normalize_node_term(text)
    return aliases.get(key, text.title())


def classify_intervention_node(
    label: object,
    drug_vocab: set[str] | None = None,
    procedure_vocab: set[str] | None = None,
) -> tuple[str, str, str]:
    normalized = normalize_node_term(label)
    if not normalized:
        return "Unknown", "", "empty"

    drug_vocab = drug_vocab or load_drug_vocab()
    procedure_vocab = procedure_vocab or load_procedure_vocab()
    variants = expand_simple_variants([normalized])
    drug_aliases = {normalize_node_term(key): value for key, value in DRUG_ALIASES.items()}
    drug_hit = ""
    procedure_hit = ""

    if normalized in drug_aliases:
        drug_hit = f"drug_alias:{normalized}->{drug_aliases[normalized]}"
    elif normalized in drug_vocab or normalized in DRUG_TERMS:
        drug_hit = f"drug_list:{normalized}"
    elif any(part in drug_vocab or part in DRUG_TERMS for part in normalized.split()):
        drug_hit = "drug_token"
    elif looks_like_drug_pattern(normalized):
        drug_hit = "drug_suffix"

    if normalized in procedure_vocab or normalized in PROCEDURAL_SEED_TERMS:
        procedure_hit = f"procedure_list:{normalized}"
    elif variants & procedure_vocab:
        procedure_hit = f"procedure_variant:{sorted(variants & procedure_vocab)[0]}"
    elif any(part in PROCEDURAL_HEAD_TERMS for part in normalized.split()):
        procedure_hit = "procedure_head_term"

    if drug_hit and procedure_hit:
        return "Both", normalized, f"{drug_hit};{procedure_hit}"
    if drug_hit:
        return "Drug", normalized, drug_hit
    if procedure_hit:
        return "Procedural", normalized, procedure_hit
    return "Unknown", normalized, "no_match"


def graph_from_edges(
    edges: pd.DataFrame,
    max_edges: int | None = 350,
    node_weights: dict[str, float] | None = None,
    min_edge_weight: float = 1,
    node_normalizer=None,
) -> nx.Graph:
    if edges.empty:
        return nx.Graph()
    edges = edges.copy()
    edges["source"] = edges["source"].map(lambda value: str(value).strip())
    edges["target"] = edges["target"].map(lambda value: str(value).strip())
    if node_normalizer:
        edges["source"] = edges["source"].map(node_normalizer)
        edges["target"] = edges["target"].map(node_normalizer)
    edges["weight"] = pd.to_numeric(edges.get("weight", 1), errors="coerce").fillna(1)
    edges = edges[(edges["source"] != "") & (edges["target"] != "") & (edges["source"] != edges["target"])]
    edges = edges[edges["weight"] >= min_edge_weight]
    edges = edges.groupby(["source", "target"], as_index=False)["weight"].sum()
    edges = edges.sort_values("weight", ascending=False)
    if max_edges:
        edges = edges.head(max_edges)
    graph = nx.Graph()
    for row in edges.itertuples(index=False):
        graph.add_edge(str(row.source), str(row.target), weight=float(row.weight))
    if node_weights:
        normalized_weights = {}
        for node, weight in node_weights.items():
            normalized_node = node_normalizer(node) if node_normalizer else str(node)
            normalized_weights[normalized_node] = normalized_weights.get(normalized_node, 0) + float(weight)
        for node, weight in normalized_weights.items():
            if node in graph:
                graph.nodes[node]["frequency"] = float(weight)
    weighted_degree = dict(graph.degree(weight="weight"))
    degree = dict(graph.degree())
    for node in graph.nodes:
        if "frequency" not in graph.nodes[node] or not float(graph.nodes[node].get("frequency") or 0):
            fallback = float(weighted_degree.get(node, 0) or degree.get(node, 0) or 1)
            graph.nodes[node]["frequency"] = fallback
            graph.nodes[node]["publication_count"] = fallback
    graph.remove_nodes_from(list(nx.isolates(graph)))
    return graph


def author_metric_columns(df: pd.DataFrame) -> tuple[str | None, str | None, str | None]:
    columns = {str(column).strip().lower(): column for column in df.columns}
    author_col = next((columns[name] for name in ("author", "authors", "name", "node") if name in columns), None)
    publication_col = next((columns[name] for name in ("publications", "publication_count", "count", "record_count") if name in columns), None)
    h_index_col = next((columns[name] for name in ("h_index", "h-index", "hindex") if name in columns), None)
    return author_col, publication_col, h_index_col


def load_author_metrics(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None or not path.exists():
        return {}
    df = safe_read_csv(path)
    if df.empty:
        return {}
    author_col, publication_col, h_index_col = author_metric_columns(df)
    if author_col is None:
        return {}
    metrics: dict[str, dict[str, object]] = {}
    for row in df.itertuples(index=False):
        row_data = dict(zip(df.columns, row, strict=False))
        author = str(row_data.get(author_col) or "").strip()
        if not author:
            continue
        publication_count = pd.to_numeric(pd.Series([row_data.get(publication_col)]), errors="coerce").iloc[0] if publication_col else math.nan
        h_index = pd.to_numeric(pd.Series([row_data.get(h_index_col)]), errors="coerce").iloc[0] if h_index_col else math.nan
        metrics[author] = {
            "publication_count": 0 if pd.isna(publication_count) else float(publication_count),
            "h_index": 0 if pd.isna(h_index) else float(h_index),
            "h_index_known": bool(h_index_col and not pd.isna(h_index)),
        }
    return metrics


def apply_author_metrics(graph: nx.Graph, metrics: dict[str, dict[str, object]]) -> None:
    for node in graph.nodes:
        data = metrics.get(str(node), {})
        publication_count = float(data.get("publication_count", 0) or 0)
        if publication_count:
            graph.nodes[node]["frequency"] = publication_count
            graph.nodes[node]["publication_count"] = publication_count
        else:
            fallback = float(graph.nodes[node].get("frequency", 0) or graph.degree(node, weight="weight") or graph.degree(node) or 1)
            graph.nodes[node]["frequency"] = fallback
            graph.nodes[node]["publication_count"] = fallback
        graph.nodes[node]["h_index"] = float(data.get("h_index", 0) or 0)
        graph.nodes[node]["h_index_known"] = bool(data.get("h_index_known", False))
        graph.nodes[node]["node_type"] = "Author"


def read_vos_network(path: Path) -> pd.DataFrame:
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        delimiter = "\t" if "\t" in sample else ";"
        reader = csv.reader(handle, delimiter=delimiter)
        for row in reader:
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                rows.append((row[0].strip(), row[1].strip(), 1))
    return pd.DataFrame(rows, columns=["source", "target", "weight"])


def prune_network(
    graph: nx.Graph,
    top_nodes: int,
    node_metric: str = "degree",
    min_degree: int = 2,
    min_remaining: int = 8,
) -> nx.Graph:
    """Remove low-information nodes and cap graph size before layout."""
    if graph.number_of_nodes() == 0:
        return graph.copy()

    pruned = graph.copy()
    pruned.remove_nodes_from(list(nx.isolates(pruned)))
    if pruned.number_of_nodes() == 0:
        return pruned

    degree_filtered_nodes = [node for node, degree in pruned.degree() if degree >= min_degree]
    if len(degree_filtered_nodes) >= min_remaining:
        pruned = pruned.subgraph(degree_filtered_nodes).copy()

    if pruned.number_of_nodes() > top_nodes:
        if node_metric == "frequency":
            ranked = sorted(
                pruned.nodes,
                key=lambda node: (float(pruned.nodes[node].get("frequency", 0)), pruned.degree(node, weight="weight")),
                reverse=True,
            )[:top_nodes]
        else:
            ranked = [node for node, _ in sorted(pruned.degree(weight="weight"), key=lambda item: item[1], reverse=True)[:top_nodes]]
        pruned = pruned.subgraph(ranked).copy()

    pruned.remove_nodes_from(list(nx.isolates(pruned)))
    return pruned


def layout_spacing(node_count: int) -> float:
    """Choose readable spring spacing in the requested 0.3-0.8 range."""
    if node_count <= 35:
        return 0.8
    if node_count <= 75:
        return 0.6
    if node_count <= 120:
        return 0.45
    return 0.3


def compute_spring_layout(
    graph: nx.Graph,
    k: float,
    iterations: int,
    seed: int = 42,
    weight: str = "weight",
) -> dict[object, object]:
    """Compute a deterministic NetworkX spring layout without requiring SciPy."""
    try:
        return nx.spring_layout(graph, k=k, iterations=iterations, seed=seed, weight=weight)
    except ModuleNotFoundError as exc:
        if exc.name != "scipy":
            raise

    import numpy as np
    from networkx.drawing.layout import _fruchterman_reingold, rescale_layout

    adjacency = nx.to_numpy_array(graph, weight=weight)
    positions = _fruchterman_reingold(
        adjacency,
        k,
        None,
        None,
        iterations,
        1e-4,
        2,
        np.random.RandomState(seed),
    )
    positions = rescale_layout(positions, scale=1)
    return dict(zip(graph, positions, strict=False))


def community_colors(graph: nx.Graph) -> dict[str, int]:
    if graph.number_of_nodes() == 0:
        return {}
    try:
        communities = nx.algorithms.community.greedy_modularity_communities(graph, weight="weight")
    except Exception:
        communities = []
    if len(communities) <= 1:
        degrees = dict(graph.degree(weight="weight"))
        if not degrees:
            return {}
        values = sorted(degrees.values())
        low = values[len(values) // 3]
        high = values[(2 * len(values)) // 3]
        return {
            node: 0 if value <= low else 1 if value <= high else 2
            for node, value in degrees.items()
        }
    colors = {}
    for idx, community in enumerate(communities):
        for node in community:
            colors[node] = idx
    return colors


def connected_component_count(graph: nx.Graph) -> int:
    return nx.number_connected_components(graph) if graph.number_of_nodes() else 0


def largest_component(graph: nx.Graph) -> nx.Graph:
    if graph.number_of_nodes() == 0:
        return graph.copy()
    component = max(nx.connected_components(graph), key=len)
    return graph.subgraph(component).copy()


def filter_network_graph(graph: nx.Graph, config: NetworkPlotConfig) -> tuple[nx.Graph, dict[str, object]]:
    stats: dict[str, object] = {
        "before_nodes": graph.number_of_nodes(),
        "before_edges": graph.number_of_edges(),
        "before_components": connected_component_count(graph),
        "min_edge_weight_used": config.min_edge_weight,
        "automatic_simplification": "none",
    }
    filtered = graph.copy()
    if filtered.number_of_nodes() == 0:
        stats.update({"after_nodes": 0, "after_edges": 0, "after_components": 0})
        return filtered, stats

    weak_edges = [
        (source, target)
        for source, target, data in filtered.edges(data=True)
        if float(data.get("weight", 1)) < config.min_edge_weight
    ]
    filtered.remove_edges_from(weak_edges)

    low_frequency = [
        node
        for node, data in filtered.nodes(data=True)
        if float(data.get("frequency", 0)) and float(data.get("frequency", 0)) < config.min_node_frequency
    ]
    filtered.remove_nodes_from(low_frequency)
    if config.drop_isolates:
        filtered.remove_nodes_from(list(nx.isolates(filtered)))

    if filtered.number_of_nodes():
        keep_components = [
            component
            for component in nx.connected_components(filtered)
            if len(component) >= config.min_component_size
        ]
        if keep_components:
            keep_nodes = set().union(*keep_components)
            filtered = filtered.subgraph(keep_nodes).copy()
        elif config.min_component_size > 1:
            filtered.clear()

    if config.keep_largest_component and filtered.number_of_nodes():
        filtered = largest_component(filtered)

    if config.top_n_edges and filtered.number_of_edges() > config.top_n_edges:
        ranked_edges = sorted(
            filtered.edges(data=True),
            key=lambda edge: float(edge[2].get("weight", 1)),
            reverse=True,
        )[: config.top_n_edges]
        edge_graph = nx.Graph()
        for source, target, data in ranked_edges:
            edge_graph.add_node(source, **filtered.nodes[source])
            edge_graph.add_node(target, **filtered.nodes[target])
            edge_graph.add_edge(source, target, **data)
        filtered = edge_graph
        if config.drop_isolates:
            filtered.remove_nodes_from(list(nx.isolates(filtered)))

    if config.top_n_nodes and filtered.number_of_nodes() > config.top_n_nodes:
        weighted = dict(filtered.degree(weight="weight"))
        ranked = sorted(
            filtered.nodes,
            key=lambda node: (
                weighted.get(node, 0),
                float(filtered.nodes[node].get("frequency", 0)),
                filtered.degree(node),
            ),
            reverse=True,
        )[: config.top_n_nodes]
        filtered = filtered.subgraph(ranked).copy()
        if config.drop_isolates:
            filtered.remove_nodes_from(list(nx.isolates(filtered)))
        if config.keep_largest_component and filtered.number_of_nodes():
            filtered = largest_component(filtered)

    if config.node_kind.lower() == "keyword" and filtered.number_of_nodes():
        simplifications: list[str] = []
        for raised_min in (3, 4):
            density = nx.density(filtered) if filtered.number_of_nodes() > 1 else 0
            if filtered.number_of_edges() <= 130 and density <= 0.14:
                break
            candidate = filtered.copy()
            candidate.remove_edges_from(
                [
                    (source, target)
                    for source, target, data in candidate.edges(data=True)
                    if float(data.get("weight", 1)) < raised_min
                ]
            )
            if config.drop_isolates:
                candidate.remove_nodes_from(list(nx.isolates(candidate)))
            if config.keep_largest_component and candidate.number_of_nodes():
                candidate = largest_component(candidate)
            if candidate.number_of_nodes() >= 12 and candidate.number_of_edges() > 0:
                filtered = candidate
                stats["min_edge_weight_used"] = raised_min
                simplifications.append(f"raised min edge weight to {raised_min}")
        for capped_nodes in (40, 30):
            density = nx.density(filtered) if filtered.number_of_nodes() > 1 else 0
            if filtered.number_of_nodes() <= capped_nodes or (filtered.number_of_edges() <= 130 and density <= 0.14):
                break
            weighted = dict(filtered.degree(weight="weight"))
            ranked = sorted(
                filtered.nodes,
                key=lambda node: (
                    weighted.get(node, 0),
                    float(filtered.nodes[node].get("frequency", 0)),
                    filtered.degree(node),
                ),
                reverse=True,
            )[:capped_nodes]
            filtered = filtered.subgraph(ranked).copy()
            if config.drop_isolates:
                filtered.remove_nodes_from(list(nx.isolates(filtered)))
            if config.keep_largest_component and filtered.number_of_nodes():
                filtered = largest_component(filtered)
            simplifications.append(f"reduced top nodes to {capped_nodes}")
        if simplifications:
            stats["automatic_simplification"] = "; ".join(simplifications)
            print(f"{config.title}: automatic simplification applied: {stats['automatic_simplification']}")

    stats.update(
        {
            "after_nodes": filtered.number_of_nodes(),
            "after_edges": filtered.number_of_edges(),
            "after_components": connected_component_count(filtered),
            "removed_weak_edges": len(weak_edges),
            "removed_low_frequency_nodes": len(low_frequency),
        }
    )
    return filtered, stats


def network_layout(graph: nx.Graph, seed: int = 42) -> dict[str, tuple[float, float]]:
    node_count = graph.number_of_nodes()
    if node_count == 0:
        return {}
    if node_count <= 18:
        try:
            return nx.kamada_kawai_layout(graph, weight="weight")
        except Exception:
            pass
    k = max(layout_spacing(node_count), 1.25 / math.sqrt(max(node_count, 1)))
    return compute_spring_layout(graph, k=k, iterations=320, seed=seed, weight="weight")


def figure_size_for_graph(graph: nx.Graph) -> tuple[float, float]:
    node_count = graph.number_of_nodes()
    width = min(18.5, max(11.5, 8.5 + node_count * 0.105))
    height = min(15.5, max(8.5, 7.2 + node_count * 0.085))
    return width, height


def node_table(graph: nx.Graph, clusters: dict[str, int]) -> pd.DataFrame:
    degree = dict(graph.degree())
    weighted = dict(graph.degree(weight="weight"))
    rows = []
    for node in graph.nodes:
        rows.append(
            {
                "node": node,
                "node_type": graph.nodes[node].get("node_type", "Unknown"),
                "frequency": float(graph.nodes[node].get("frequency", 0)),
                "publication_count": float(graph.nodes[node].get("publication_count", graph.nodes[node].get("frequency", 0)) or 0),
                "h_index": float(graph.nodes[node].get("h_index", 0) or 0),
                "h_index_known": bool(graph.nodes[node].get("h_index_known", False)),
                "variant_count": float(graph.nodes[node].get("variant_count", graph.nodes[node].get("frequency", 0)) or 0),
                "total_frequency": float(graph.nodes[node].get("total_frequency", graph.nodes[node].get("frequency", 0)) or 0),
                "degree": int(degree.get(node, 0)),
                "weighted_degree": float(weighted.get(node, 0)),
                "community": int(clusters.get(node, 0)),
            }
        )
    return pd.DataFrame(rows).sort_values(["weighted_degree", "frequency", "node"], ascending=[False, False, True])


def edge_table(graph: nx.Graph) -> pd.DataFrame:
    rows = [
        {
            "source": source,
            "target": target,
            "weight": float(data.get("weight", 1)),
            "edge_type": data.get("edge_type", ""),
        }
        for source, target, data in graph.edges(data=True)
    ]
    return pd.DataFrame(rows).sort_values(["weight", "source", "target"], ascending=[False, True, True])


def save_institution_rankings(graph: nx.Graph, generated: list[Path]) -> None:
    if graph.number_of_nodes() == 0:
        return
    rows = []
    for node in graph.nodes:
        rows.append(
            {
                "institution": node,
                "publication_count": float(graph.nodes[node].get("frequency", 0)),
                "degree": int(graph.degree(node)),
                "weighted_degree": float(graph.degree(node, weight="weight")),
            }
        )
    df = pd.DataFrame(rows).sort_values(["publication_count", "weighted_degree"], ascending=[False, False])
    path = processed_path("institution_rankings.csv")
    df.to_csv(path, index=False)
    generated.append(path)


def save_author_hindex_audit(slug: str, graph: nx.Graph, generated: list[Path]) -> None:
    if graph.number_of_nodes() == 0:
        return
    known_values = [float(graph.nodes[node].get("h_index", 0) or 0) for node in graph.nodes if bool(graph.nodes[node].get("h_index_known", False))]
    min_h = min(known_values) if known_values else 0.0
    max_h = max(known_values) if known_values else 1.0
    max_pub = max([float(graph.nodes[node].get("publication_count", graph.nodes[node].get("frequency", 0)) or 0) for node in graph.nodes] or [1])
    rows = []
    for author in graph.nodes:
        h_known = bool(graph.nodes[author].get("h_index_known", False))
        h_index = float(graph.nodes[author].get("h_index", 0) or 0)
        pub_count = float(graph.nodes[author].get("publication_count", graph.nodes[author].get("frequency", 0)) or 0)
        base_size = 120 + 780 * math.sqrt(max(pub_count, 1) / max(max_pub, 1))
        if h_known and max_h > min_h:
            h_norm = (math.sqrt(h_index) - math.sqrt(min_h)) / max(math.sqrt(max_h) - math.sqrt(min_h), 1e-9)
        elif h_known:
            h_norm = 1.0
        else:
            h_norm = 0.0
        fallback_border = 0.8 if not h_known else 1.2 + 5.8 * h_norm
        fallback_size = max(base_size, 260 + 1050 * h_norm if h_known else base_size)
        rows.append(
            {
                "author": author,
                "h_index": "" if not h_known else h_index,
                "h_index_missing_yes_no": "no" if h_known else "yes",
                "publication_count": pub_count,
                "connection_count": int(graph.degree(author)),
                "weighted_degree": float(graph.degree(author, weight="weight")),
                "node_size": float(graph.nodes[author].get("node_size_used", fallback_size) or fallback_size),
                "border_width": float(graph.nodes[author].get("border_width_used", fallback_border) or fallback_border),
                "visual_group": "h_index_unknown" if not h_known else "h_index_high" if h_index >= 20 else "h_index_medium" if h_index >= 10 else "h_index_low",
                "reason": "H-index missing; neutral border used" if not h_known else "Border width and minimum node size scaled by sqrt-normalized H-index",
            }
        )
    df = pd.DataFrame(rows).sort_values(["weighted_degree", "publication_count", "author"], ascending=[False, False, True])
    audit_path = processed_path(f"{slug}_author_hindex_network_audit.csv")
    validation_path = processed_path(f"{slug}_author_hindex_validation.txt")
    df.to_csv(audit_path, index=False)
    known = df[df["h_index_missing_yes_no"] == "no"].copy()
    known_h = pd.to_numeric(known["h_index"], errors="coerce") if not known.empty else pd.Series(dtype=float)
    lines = [
        "Author H-index network validation",
        "",
        f"authors total: {len(df)}",
        f"authors with h-index: {len(known)}",
        f"authors missing h-index: {len(df) - len(known)}",
        f"min h-index: {known_h.min() if not known_h.empty else 'n/a'}",
        f"max h-index: {known_h.max() if not known_h.empty else 'n/a'}",
        f"node size range: {df['node_size'].min():.2f} - {df['node_size'].max():.2f}",
        f"border width range: {df['border_width'].min():.2f} - {df['border_width'].max():.2f}",
        "visual encoding used: node size = publication count with H-index minimum boost; border width = sqrt-normalized H-index; unknown H-index = neutral thin border",
    ]
    validation_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    generated.extend([audit_path, validation_path])


def filter_subtitle(config: NetworkPlotConfig) -> str:
    bits = []
    if config.top_n_nodes:
        bits.append(f"top {config.top_n_nodes} nodes")
    if config.min_edge_weight:
        bits.append(f"min edge weight >= {config.min_edge_weight}")
    if config.min_node_frequency > 1:
        bits.append(f"min node frequency >= {config.min_node_frequency}")
    if config.keep_largest_component:
        bits.append("largest connected component")
    return ", ".join(bits)


def save_network_stats(
    graph: nx.Graph,
    original_graph: nx.Graph,
    config: NetworkPlotConfig,
    stats: dict[str, object],
    node_df: pd.DataFrame,
    generated: list[Path],
    output_path: Path | None = None,
    labels_drawn: int | None = None,
) -> None:
    path = processed_path(f"{config.stem}_qc_stats.txt")
    final_edges = graph.number_of_edges()
    final_nodes = graph.number_of_nodes()
    label_count = labels_drawn if labels_drawn is not None else min(config.label_top_n, final_nodes)
    lines = [
        config.title,
        filter_subtitle(config),
        "",
        f"network type: {config.network_type or config.title}",
        f"input keyword table: {config.input_keyword_table or 'n/a'}",
        f"candidate nodes: {stats.get('before_nodes', original_graph.number_of_nodes())}",
        f"final plotted nodes: {final_nodes}",
        f"candidate edges: {stats.get('before_edges', original_graph.number_of_edges())}",
        f"final plotted edges: {final_edges}",
        f"min edge weight used: {stats.get('min_edge_weight_used', config.min_edge_weight)}",
        f"label_top_n used: {label_count}",
        f"terms suppressed: {config.terms_suppressed}",
        f"automatic simplification: {stats.get('automatic_simplification', 'none')}",
        f"output path: {output_path if output_path else VISUALS_DIR / f'{config.stem}.png'}",
        "",
        f"Graph before filtering: {stats.get('before_nodes', original_graph.number_of_nodes())} nodes, {stats.get('before_edges', original_graph.number_of_edges())} edges",
        f"Connected components before filtering: {stats.get('before_components', connected_component_count(original_graph))}",
        f"Graph after filtering: {final_nodes} nodes, {final_edges} edges",
        f"Connected components after filtering: {connected_component_count(graph)}",
        f"Labels drawn: {label_count}",
        "",
        "Top nodes by frequency:",
    ]
    if not node_df.empty:
        for row in node_df.sort_values(["frequency", "weighted_degree"], ascending=[False, False]).head(20).itertuples(index=False):
            lines.append(f"  {row.node}: frequency={row.frequency:g}, degree={row.degree}, weighted_degree={row.weighted_degree:g}, type={row.node_type}")
        lines.append("")
        lines.append("Top nodes by weighted degree:")
        for row in node_df.head(20).itertuples(index=False):
            lines.append(f"  {row.node}: weighted_degree={row.weighted_degree:g}, frequency={row.frequency:g}, degree={row.degree}, type={row.node_type}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    generated.append(path)
    print(f"{config.title}: {stats.get('before_nodes')} nodes/{stats.get('before_edges')} edges before filtering; {final_nodes} nodes/{final_edges} edges after filtering.")
    print(f"{config.title}: {connected_component_count(graph)} connected component(s); drawing {label_count} labels.")
    if not node_df.empty:
        print("Top 20 nodes by weighted degree:")
        for row in node_df.head(20).itertuples(index=False):
            print(f"  {row.node}: freq={row.frequency:g}, degree={row.degree}, weighted={row.weighted_degree:g}")


def readable_label_nodes(node_df: pd.DataFrame, config: NetworkPlotConfig, graph: nx.Graph) -> list[str]:
    if node_df.empty:
        return []
    label_top_n = min(config.label_top_n, FINAL_KEYWORD_LABEL_TOP_N, len(node_df))
    if config.node_kind.lower() == "keyword":
        density = nx.density(graph) if graph.number_of_nodes() > 1 else 0
        if graph.number_of_nodes() > 45 or graph.number_of_edges() > 120 or density > 0.14:
            label_top_n = min(label_top_n, 15)
        if graph.number_of_nodes() > 55 or graph.number_of_edges() > 165:
            label_top_n = min(label_top_n, 12)
    return node_df.head(label_top_n)["node"].tolist()


def write_network_debug_csvs(
    original_graph: nx.Graph,
    final_graph: nx.Graph,
    config: NetworkPlotConfig,
    labels: set[str],
    generated: list[Path],
) -> None:
    node_rows: list[dict[str, object]] = []
    final_nodes = set(final_graph.nodes)
    degree = dict(original_graph.degree())
    weighted = dict(original_graph.degree(weight="weight"))
    try:
        centrality = nx.betweenness_centrality(final_graph, weight="weight") if final_graph.number_of_nodes() else {}
    except Exception:
        centrality = {}
    max_score = max([float(weighted.get(node, 0)) for node in original_graph.nodes] or [1])
    for node in sorted(original_graph.nodes):
        included = node in final_nodes
        frequency = float(original_graph.nodes[node].get("frequency", 0) or 0)
        score = max(float(weighted.get(node, 0)), frequency, 1)
        node_rows.append(
            {
                "term": node,
                "cleaned_term": normalize_keyword(str(node)),
                "category": original_graph.nodes[node].get("node_type", config.node_kind),
                "frequency": frequency,
                "degree": int(degree.get(node, 0)),
                "weighted_degree": float(weighted.get(node, 0)),
                "centrality": float(centrality.get(node, 0.0)) if included else 0.0,
                "node_size": 120 + 780 * math.sqrt(score / max(max_score, 1)),
                "labeled": "yes" if node in labels else "no",
                "included": "yes" if included else "no",
                "exclusion_reason": "" if included else "filtered by final network thresholds or largest-component pruning",
            }
        )
    node_path = processed_path(f"{config.node_debug_stem or config.stem}_debug_nodes.csv")
    pd.DataFrame(node_rows).to_csv(node_path, index=False)

    final_edge_keys = {tuple(sorted((str(source), str(target)))) for source, target in final_graph.edges}
    edge_rows: list[dict[str, object]] = []
    min_edge = float(config.min_edge_weight)
    for source, target, data in sorted(original_graph.edges(data=True), key=lambda item: (str(item[0]), str(item[1]))):
        key = tuple(sorted((str(source), str(target))))
        weight = float(data.get("weight", 1))
        included = key in final_edge_keys
        if included:
            reason = ""
        elif weight < min_edge:
            reason = f"edge weight below threshold {min_edge:g}"
        elif source not in final_nodes or target not in final_nodes:
            reason = "one or both nodes excluded from final network"
        else:
            reason = "filtered by final network thresholds"
        edge_rows.append(
            {
                "term_a": source,
                "term_b": target,
                "cooccurrence_weight": weight,
                "included": "yes" if included else "no",
                "exclusion_reason": reason,
            }
        )
    edge_path = processed_path(f"{config.edge_debug_stem or config.stem}_debug_edges.csv")
    pd.DataFrame(edge_rows).to_csv(edge_path, index=False)
    generated.extend([node_path, edge_path])


def add_adjusted_labels(ax: plt.Axes, pos: dict[str, tuple[float, float]], labels: dict[str, str]) -> None:
    texts = []
    for idx, (node, label) in enumerate(labels.items()):
        x, y = pos[node]
        offset = 0.012 + 0.006 * (idx % 5)
        text = ax.text(
            x + offset,
            y + offset,
            label,
            fontsize=8.8,
            color=INK,
            ha="left",
            va="center",
            path_effects=[path_effects.Stroke(linewidth=3.4, foreground=BACKGROUND), path_effects.Normal()],
            zorder=5,
        )
        texts.append(text)
    try:
        from adjustText import adjust_text  # type: ignore

        adjust_text(texts, ax=ax, expand_points=(1.15, 1.25), arrowprops={"arrowstyle": "-", "lw": 0.35, "color": MUTED, "alpha": 0.45})
    except Exception:
        return


def draw_static_network(
    graph: nx.Graph,
    pos: dict[str, tuple[float, float]],
    node_df: pd.DataFrame,
    title: str,
    subtitle: str,
    output_stem: str,
    generated: list[Path],
    label_nodes: list[str] | None,
    config: NetworkPlotConfig,
) -> None:
    apply_static_style()
    fig, ax = plt.subplots(figsize=figure_size_for_graph(graph), dpi=STATIC_DPI)
    weights = [float(graph.edges[edge].get("weight", 1)) for edge in graph.edges]
    max_weight = max(weights) if weights else 1
    edge_widths = [min(config.max_edge_width, 0.2 + config.max_edge_width * (weight / max_weight)) for weight in weights]
    edge_colors = [
        COMBINED_EDGE_COLOR
        if graph.edges[edge].get("edge_type") == "drug_procedure"
        else "#374151"
        for edge in graph.edges
    ]
    nx.draw_networkx_edges(graph, pos, ax=ax, width=edge_widths, alpha=config.edge_alpha, edge_color=edge_colors)

    size_column = config.size_metric if config.size_metric in node_df.columns else "frequency"
    max_value = max(node_df["weighted_degree"].max(), node_df[size_column].max(), 1) if not node_df.empty else 1
    known_h = node_df[node_df.get("h_index_known", False) == True]["h_index"] if "h_index_known" in node_df.columns and "h_index" in node_df.columns else pd.Series(dtype=float)
    min_h_index = float(known_h.min()) if not known_h.empty else 0.0
    max_h_index = float(known_h.max()) if not known_h.empty else 1.0
    for node_type in sorted(set(node_df["node_type"]) if not node_df.empty else {"Unknown"}):
        nodes = node_df[node_df["node_type"] == node_type]["node"].tolist()
        if not nodes:
            continue
        sizes = []
        line_widths = []
        for node in nodes:
            value = max(float(graph.nodes[node].get(size_column, 0)), graph.degree(node, weight="weight"))
            base_size = 120 + 780 * math.sqrt(max(value, 1) / max_value)
            if config.border_metric == "h_index":
                h_index = float(graph.nodes[node].get("h_index", 0) or 0)
                known = bool(graph.nodes[node].get("h_index_known", False))
                if known and max_h_index > min_h_index:
                    h_norm = (math.sqrt(h_index) - math.sqrt(min_h_index)) / max(math.sqrt(max_h_index) - math.sqrt(min_h_index), 1e-9)
                elif known:
                    h_norm = 1.0
                else:
                    h_norm = 0.0
                line_width = 0.8 if not known else 1.2 + 5.8 * h_norm
                base_size = max(base_size, 260 + 1050 * h_norm if known else base_size)
                line_widths.append(line_width)
                graph.nodes[node]["node_size_used"] = float(base_size)
                graph.nodes[node]["border_width_used"] = float(line_width)
            else:
                line_widths.append(1.1)
                graph.nodes[node]["node_size_used"] = float(base_size)
                graph.nodes[node]["border_width_used"] = 1.1
            sizes.append(base_size)
        nx.draw_networkx_nodes(
            graph,
            pos,
            nodelist=nodes,
            node_size=sizes,
            node_color=NODE_TYPE_COLORS.get(node_type, NODE_TYPE_COLORS["Unknown"]),
            node_shape=NODE_TYPE_SHAPES.get(node_type, "o"),
            linewidths=line_widths,
            edgecolors="#111827" if config.border_metric == "h_index" else "white",
            alpha=0.94,
            label=node_type,
            ax=ax,
        )

    if label_nodes:
        label_map = {node: truncate_label(node, config.label_width) for node in label_nodes if node in graph}
        add_adjusted_labels(ax, pos, label_map)
    if any(value != config.node_kind for value in node_df.get("node_type", [])):
        ax.legend(frameon=False, loc="lower left", fontsize=10)
    if config.border_metric == "h_index":
        handles = [
            plt.Line2D([0], [0], marker="o", color="none", markerfacecolor="#c4b5fd", markeredgecolor="#6b7280", markeredgewidth=0.8, markersize=9, label="H-index unknown"),
            plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=NODE_TYPE_COLORS.get("Author", "#a855f7"), markeredgecolor="#111827", markeredgewidth=1.6, markersize=9, label="Lower H-index"),
            plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=NODE_TYPE_COLORS.get("Author", "#a855f7"), markeredgecolor="#111827", markeredgewidth=4.2, markersize=11, label="Medium H-index"),
            plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=NODE_TYPE_COLORS.get("Author", "#a855f7"), markeredgecolor="#111827", markeredgewidth=7.0, markersize=13, label="Higher H-index"),
        ]
        ax.legend(handles=handles, frameon=False, loc="lower left", fontsize=10)
    ax.set_title(title, loc="left", fontsize=24, pad=24, weight="bold")
    add_subtitle(ax, subtitle)
    ax.set_axis_off()
    fig.tight_layout()
    save_static_formats(fig, output_stem, generated)
    plt.close(fig)


def save_interactive_network(
    graph: nx.Graph,
    pos: dict[str, tuple[float, float]],
    node_df: pd.DataFrame,
    title: str,
    stem: str,
    generated: list[Path],
    label_nodes: set[str],
) -> None:
    if EXPORT_ONLY_PNG:
        return
    edge_x, edge_y = [], []
    for source, target in graph.edges:
        x0, y0 = pos[source]
        x1, y1 = pos[target]
        edge_x.extend([x0, x1, None])
        edge_y.extend([y0, y1, None])
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=edge_x,
            y=edge_y,
            mode="lines",
            line={"width": 0.65, "color": "rgba(55,65,81,0.18)"},
            hoverinfo="skip",
            showlegend=False,
        )
    )
    max_value = max(node_df["weighted_degree"].max(), node_df["frequency"].max(), 1) if not node_df.empty else 1
    for node_type in sorted(node_df["node_type"].unique()) if not node_df.empty else []:
        type_df = node_df[node_df["node_type"] == node_type]
        fig.add_trace(
            go.Scatter(
                x=[pos[node][0] for node in type_df["node"]],
                y=[pos[node][1] for node in type_df["node"]],
                mode="markers+text",
                text=[truncate_label(node, 26) if node in label_nodes else "" for node in type_df["node"]],
                textposition="top center",
                hovertext=[
                    f"{row.node}<br>Type: {row.node_type}<br>Frequency: {row.frequency:g}<br>Degree: {row.degree}<br>Weighted degree: {row.weighted_degree:g}<br>Community: {row.community}"
                    for row in type_df.itertuples(index=False)
                ],
                hoverinfo="text",
                marker={
                    "size": [9 + 34 * math.sqrt(max(row.weighted_degree, row.frequency, 1) / max_value) for row in type_df.itertuples(index=False)],
                    "color": NODE_TYPE_COLORS.get(node_type, NODE_TYPE_COLORS["Unknown"]),
                    "symbol": PLOTLY_SYMBOLS.get(node_type, "circle"),
                    "line": {"width": 1.0, "color": "white"},
                    "opacity": 0.92,
                },
                name=node_type,
            )
        )
    configure_plotly_layout(fig, title)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(height=900, margin={"l": 24, "r": 24, "t": 88, "b": 24})
    html_path = processed_path(f"{stem}_interactive.html")
    fig.write_html(html_path, include_plotlyjs="cdn", full_html=True)
    generated.append(html_path)


def export_filtered_network(original_graph: nx.Graph, config: NetworkPlotConfig, generated: list[Path]) -> nx.Graph:
    if original_graph.number_of_nodes() == 0:
        print(f"Skipping {config.title}; no graph edges available.")
        return nx.Graph()
    graph, stats = filter_network_graph(original_graph, config)
    if graph.number_of_nodes() == 0:
        print(f"Skipping {config.title}; no connected nodes remain after filtering.")
        return graph

    clusters = community_colors(graph)
    for node in graph.nodes:
        graph.nodes[node].setdefault("node_type", config.node_kind)
    node_df = node_table(graph, clusters)
    edge_df = edge_table(graph)
    node_path = processed_path(f"{config.stem}_nodes.csv")
    edge_path = processed_path(f"{config.stem}_edges.csv")
    legend_path = processed_path(f"{config.stem}_legend.csv")
    node_df.to_csv(node_path, index=False)
    edge_df.to_csv(edge_path, index=False)
    node_df.to_csv(legend_path, index=False)
    generated.extend([node_path, edge_path, legend_path])

    label_nodes = readable_label_nodes(node_df, config, graph)
    output_path = VISUALS_DIR / f"{config.stem}.png"
    save_network_stats(graph, original_graph, config, stats, node_df, generated, output_path=output_path, labels_drawn=len(label_nodes))
    write_network_debug_csvs(original_graph, graph, config, set(label_nodes), generated)
    subtitle = config.subtitle or filter_subtitle(config)
    pos = network_layout(graph, seed=42)
    draw_static_network(graph, pos, node_df, f"{config.title}", subtitle, config.stem, generated, label_nodes, config)
    lcc = largest_component(graph)
    if lcc.number_of_nodes() != graph.number_of_nodes():
        lcc_clusters = community_colors(lcc)
        lcc_df = node_table(lcc, lcc_clusters)
        lcc_pos = network_layout(lcc, seed=42)
        draw_static_network(lcc, lcc_pos, lcc_df, f"{config.title} (Largest Component)", subtitle, f"{config.stem}_largest_component", generated, readable_label_nodes(lcc_df, config, lcc), config)
    return graph


def plot_network(
    graph: nx.Graph,
    title: str,
    stem: str,
    generated: list[Path],
    top_nodes: int = 80,
    label_top_n: int = 18,
    node_metric: str = "degree",
    min_node_frequency: int = 1,
    min_edge_weight: int = 2,
    top_n_edges: int | None = None,
    keep_largest_component: bool = True,
    node_kind: str = "Keyword",
    size_metric: str = "frequency",
    border_metric: str = "",
    network_type: str = "",
    input_keyword_table: str = "",
    terms_suppressed: int = 0,
) -> None:
    filters = filter_subtitle(
        NetworkPlotConfig(
            title=title,
            stem=stem,
            top_n_nodes=top_nodes,
            min_edge_weight=min_edge_weight,
            min_node_frequency=min_node_frequency,
            keep_largest_component=keep_largest_component,
        )
    )
    encoding = "Node size reflects keyword frequency; edge thickness reflects co-occurrence strength."
    subtitle = f"{encoding} {filters}" if node_kind.lower() == "keyword" and filters else filters
    config = NetworkPlotConfig(
        title=title,
        stem=stem,
        node_kind=node_kind,
        min_node_frequency=min_node_frequency,
        min_edge_weight=min_edge_weight,
        top_n_nodes=top_nodes,
        top_n_edges=top_n_edges,
        label_top_n=label_top_n,
        keep_largest_component=keep_largest_component,
        node_metric=node_metric,
        size_metric=size_metric,
        border_metric=border_metric,
        subtitle=subtitle,
        network_type=network_type or title,
        input_keyword_table=input_keyword_table,
        terms_suppressed=terms_suppressed,
    )
    for node in graph.nodes:
        graph.nodes[node].setdefault("node_type", node_kind)
    export_filtered_network(graph, config, generated)


def save_static_network(
    graph: nx.Graph,
    pos: dict[str, tuple[float, float]],
    clusters: dict[str, int],
    degrees: dict[str, int],
    node_values: dict[str, float],
    title: str,
    stem: str,
    generated: list[Path],
    label_top_n: int = 22,
) -> None:
    apply_static_style()
    fig, ax = plt.subplots(figsize=(14.5, 11.5), dpi=STATIC_DPI)
    weights = [graph.edges[edge].get("weight", 1) for edge in graph.edges]
    max_weight = max(weights) if weights else 1
    max_node_value = max(node_values.values()) if node_values else 1
    edge_widths = [0.25 + 1.0 * (weight / max_weight) for weight in weights]
    node_sizes = [65 + 760 * math.sqrt(node_values[node] / max_node_value) for node in graph.nodes]
    node_colors = [clusters.get(node, 0) for node in graph.nodes]

    nx.draw_networkx_edges(graph, pos, ax=ax, width=edge_widths, alpha=0.28, edge_color="#374151")
    nx.draw_networkx_nodes(
        graph,
        pos,
        ax=ax,
        node_size=node_sizes,
        node_color=node_colors,
        cmap=plt.cm.Set2,
        alpha=0.92,
        linewidths=1.1,
        edgecolors=BACKGROUND,
    )
    drug_nodes = [node for node in graph.nodes if node.lower() in DRUG_TERMS]
    if drug_nodes:
        nx.draw_networkx_nodes(
            graph,
            pos,
            nodelist=drug_nodes,
            node_size=[node_sizes[list(graph.nodes).index(node)] * 1.18 for node in drug_nodes],
            node_color=DRUG_COLOR,
            node_shape="D",
            linewidths=1.2,
            edgecolors="white",
            ax=ax,
        )
    label_nodes = {
        node: textwrap.shorten(node, width=28, placeholder="...")
        for node in sorted(node_values, key=node_values.get, reverse=True)[:label_top_n]
    }
    labels = nx.draw_networkx_labels(graph, pos, labels=label_nodes, font_size=8.5, font_color=INK, ax=ax)
    for text in labels.values():
        text.set_path_effects([path_effects.Stroke(linewidth=3.2, foreground=BACKGROUND), path_effects.Normal()])

    ax.set_title(title, loc="left", fontsize=26, pad=22, weight="bold")
    add_subtitle(ax, "Node size reflects connection strength; colors indicate detected communities")
    ax.set_axis_off()
    fig.tight_layout()
    save_static_formats(fig, stem, generated)
    plt.close(fig)


def plot_drug_network(
    graph: nx.Graph,
    drug_counts: pd.DataFrame,
    title: str,
    stem: str,
    generated: list[Path],
    label_top_n: int = 15,
    min_frequency: int = 1,
    min_degree: int = 1,
) -> None:
    if graph.number_of_nodes() == 0 or drug_counts.empty:
        print(f"Skipping {title}; no drug graph edges available.")
        return
    counts = {
        str(row.drug_name): float(row.count)
        for row in drug_counts.itertuples(index=False)
        if float(row.count) >= min_frequency
    }
    ttys = {str(row.drug_name): str(row.tty or "") for row in drug_counts.itertuples(index=False)}
    graph = graph.subgraph(
        [
            node
            for node in graph.nodes
            if counts.get(node, 0) >= min_frequency and graph.degree(node) >= min_degree
        ]
    ).copy()
    if graph.number_of_nodes() == 0:
        print(f"Skipping {title}; no connected drugs remain after filtering.")
        return
    for node in graph.nodes:
        graph.nodes[node]["frequency"] = float(counts.get(node, 1))
        graph.nodes[node]["node_type"] = "Drug"
        graph.nodes[node]["tty"] = ttys.get(node, "")
    export_filtered_network(
        graph,
        NetworkPlotConfig(
            title=title,
            stem=stem,
            node_kind="Drug",
            min_node_frequency=min_frequency,
            min_edge_weight=2,
            top_n_nodes=75,
            label_top_n=label_top_n,
            keep_largest_component=False,
            subtitle=f"top 75 drug nodes, min edge weight >= 2, labels limited to top {label_top_n}",
        ),
        generated,
    )


def plot_combined_intervention_network(
    edges: pd.DataFrame,
    drug_counts: pd.DataFrame,
    procedure_counts: pd.DataFrame,
    title: str,
    stem: str,
    generated: list[Path],
    label_top_n: int = 18,
    include_unknown: bool = False,
    unknown_top_n: int = 20,
) -> None:
    if edges.empty:
        print(f"Skipping {title}; no combined drug-procedure pairs available.")
        return

    drug_weights = (
        dict(zip(drug_counts["drug_name"], drug_counts["count"], strict=False))
        if not drug_counts.empty and {"drug_name", "count"} <= set(drug_counts.columns)
        else {}
    )
    procedure_weights = (
        dict(zip(procedure_counts["procedure_name"], procedure_counts["count"], strict=False))
        if not procedure_counts.empty and {"procedure_name", "count"} <= set(procedure_counts.columns)
        else {}
    )
    graph = nx.Graph()
    drug_vocab = load_drug_vocab()
    procedure_vocab = load_procedure_vocab()
    debug_rows: list[dict[str, object]] = []
    node_types: dict[str, str] = {}
    for node in sorted(set(edges["source"].astype(str)) | set(edges["target"].astype(str))):
        assigned, normalized, rule = classify_intervention_node(node, drug_vocab, procedure_vocab)
        if node in drug_weights and assigned == "Unknown":
            assigned, rule = "Drug", "count_table_drug"
        if node in procedure_weights and assigned == "Unknown":
            assigned, rule = "Procedural", "count_table_procedure"
        node_types[node] = assigned
        debug_rows.append({"node": node, "normalized_label": normalized, "assigned_type": assigned, "matched_rule": rule})
    debug_df = pd.DataFrame(debug_rows).sort_values(["assigned_type", "node"])
    debug_path = processed_path(f"{stem}_classification_debug.csv")
    debug_df.to_csv(debug_path, index=False)
    generated.append(debug_path)
    summary = debug_df["assigned_type"].value_counts().to_dict() if not debug_df.empty else {}
    print("Combined intervention node classification:")
    print(f"  Drug nodes: {summary.get('Drug', 0)}")
    print(f"  Procedural nodes: {summary.get('Procedural', 0)}")
    print(f"  Both nodes: {summary.get('Both', 0)}")
    print(f"  Unknown nodes: {summary.get('Unknown', 0)}")

    allowed_unknowns: set[str] = set()
    if include_unknown:
        unknown_scores: Counter[str] = Counter()
        for row in edges.itertuples(index=False):
            weight = float(row.weight)
            for node in (str(row.source), str(row.target)):
                if node_types.get(node) == "Unknown":
                    unknown_scores[node] += weight
        allowed_unknowns = {node for node, _ in unknown_scores.most_common(unknown_top_n)}

    for row in edges.sort_values("weight", ascending=False).head(800).itertuples(index=False):
        source = str(row.source)
        target = str(row.target)
        source_type = node_types.get(source, "Unknown")
        target_type = node_types.get(target, "Unknown")
        if not include_unknown and ("Unknown" in {source_type, target_type}):
            continue
        if include_unknown and any(node_types.get(node) == "Unknown" and node not in allowed_unknowns for node in (source, target)):
            continue
        source_frequency = float(drug_weights.get(source, procedure_weights.get(source, 1)))
        target_frequency = float(drug_weights.get(target, procedure_weights.get(target, 1)))
        graph.add_node(source, node_type=source_type, frequency=source_frequency)
        graph.add_node(target, node_type=target_type, frequency=target_frequency)
        edge_type = "drug_procedure" if {source_type, target_type} <= {"Drug", "Procedural", "Both"} else "other"
        graph.add_edge(source, target, weight=float(row.weight), edge_type=edge_type)
    graph.remove_nodes_from(list(nx.isolates(graph)))
    if graph.number_of_nodes() == 0:
        print(f"Skipping {title}; no connected combined nodes remain.")
        return
    export_filtered_network(
        graph,
        NetworkPlotConfig(
            title=title,
            stem=stem,
            node_kind="Unknown",
            min_node_frequency=1,
            min_edge_weight=2,
            top_n_nodes=75,
            top_n_edges=250,
            label_top_n=label_top_n,
            keep_largest_component=False,
            include_unknown=include_unknown,
            unknown_top_n=unknown_top_n,
            edge_alpha=0.22,
            subtitle=f"focused drug/procedural bipartite graph, top 75 nodes, min edge weight >= 2, include_unknown={include_unknown}",
        ),
        generated,
    )


def tokenize_query(query: str) -> set[str]:
    terms = {token for token in clean_text_tokens(query) if len(token) > 1}
    return expand_simple_variants(terms)


def clean_text_tokens(value: object) -> list[str]:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    text = text.replace("-", " ")
    return [token.strip() for token in re.findall(r"[a-z][a-z0-9]*", text) if token.strip()]


def build_ngrams(tokens: list[str], max_n: int = 3) -> list[str]:
    grams: list[str] = []
    for n in range(1, max_n + 1):
        for idx in range(max(0, len(tokens) - n + 1)):
            gram = " ".join(tokens[idx : idx + n]).strip()
            if gram:
                grams.append(gram)
    return grams


def expand_simple_variants(terms: Iterable[str]) -> set[str]:
    variants: set[str] = set()
    for term in terms:
        term = normalize_keyword(term)
        if not term:
            continue
        variants.add(term)
        if term.endswith("ies") and len(term) > 4:
            variants.add(f"{term[:-3]}y")
        elif term.endswith("s") and len(term) > 3:
            variants.add(term[:-1])
        else:
            variants.add(f"{term}s")
    return variants


def remove_stopword_grams(terms: Iterable[str], extra_stopwords: set[str] | None = None) -> list[str]:
    stopwords = STOPWORDS | (extra_stopwords or set())
    cleaned: list[str] = []
    for term in terms:
        term = normalize_keyword(term)
        words = set(clean_text_tokens(term))
        if not term or not words or words <= stopwords:
            continue
        cleaned.append(term)
    return dedupe_terms(cleaned)


def tokenize_all(row: pd.Series) -> list[str]:
    chunks: list[str] = []
    for name, value in row.items():
        lower = str(name).lower()
        if lower in {"keywords", "keyword", "author_keywords", "index_keywords", "mesh_terms", "mesh", "title", "abstract"}:
            chunks.append(str(value or ""))
    tokens = [token for token in clean_text_tokens(" ".join(chunks)) if token not in STOPWORDS]
    return remove_stopword_grams(build_ngrams(tokens))


def infer_query(core_path: Path | None) -> str:
    if core_path is None:
        return ""
    stem = re.sub(r"\.csv$", "", core_path.name)
    return stem.replace("_", " ")


def split_keyword_cell(value: str) -> list[str]:
    parts = re.split(r";|\||,", value)
    return [normalize_keyword(part) for part in parts if normalize_keyword(part)]


def normalize_keyword(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", value)
    return value


GEO_KEYWORD_ALIASES = {
    "usa": "united states",
    "u s": "united states",
    "u s a": "united states",
    "us": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "u k": "united kingdom",
    "britain": "united kingdom",
    "tx": "texas",
    "tex": "texas",
    "ca": "california",
    "ny": "new york",
}


GEOGRAPHY_KEYWORD_NAMES = {
    "united states",
    "united kingdom",
    "germany",
    "france",
    "brazil",
    "texas",
    "california",
    "new york",
    "lubbock",
    "amarillo",
    "dallas",
    "berlin",
    "tokyo",
    "west texas",
    "sub saharan africa",
    "eastern europe",
}


def normalize_keyword_for_network(value: object, field_name: str = "") -> str:
    original = str(value or "").strip()
    normalized = normalize_keyword(original.replace(".", " "))
    if not normalized:
        return ""
    field = field_name.lower()
    if normalized in GEO_KEYWORD_ALIASES and ("keyword" in field or "mesh" in field or re.fullmatch(r"[A-Z]{2,3}|U\.S\.A?\.?|U\.K\.", original.strip())):
        normalized = GEO_KEYWORD_ALIASES[normalized]
    return normalized


def keyword_category(keyword: str) -> str:
    if keyword in DRUG_TERMS:
        return "drug"
    if keyword in GEOGRAPHY_KEYWORD_NAMES or keyword.endswith(" county"):
        return "geography"
    if keyword in PROCEDURAL_SEED_TERMS or any(part in PROCEDURAL_HEAD_TERMS for part in keyword.split()):
        return "procedure"
    return "keyword"


def extract_all_keyword_network(core_path: Path, generated: list[Path], include_debug_visual: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = safe_read_csv(core_path)
    count_columns = ["keyword", "normalized_keyword", "variants", "variant_count", "total_frequency", "count", "category"]
    edge_columns = ["source", "target", "weight"]
    stem = core_path.with_suffix("").name
    slug = stem.replace("_year_limited_records", "").replace("_cleaned", "")
    keyword_cols = [
        column
        for column in df.columns
        if str(column).lower() in {"keywords", "keyword", "author_keywords", "index_keywords", "keywords_plus", "mesh_terms", "mesh"}
    ]
    if not keyword_cols:
        print("No core keyword columns found; building all-keyword network from GeoCensus domains when available.")

    variants: dict[str, Counter[str]] = {}
    frequencies: Counter[str] = Counter()
    categories: dict[str, str] = {}
    per_record: list[list[str]] = []
    suppressed_rows: list[dict[str, object]] = []
    for _, row in df.iterrows():
        terms: list[str] = []
        for column in keyword_cols:
            for raw in re.split(r";|\||,", str(row.get(column) or "")):
                raw = raw.strip()
                normalized = normalize_keyword_for_network(raw, str(column))
                reason = keyword_network_exclusion_reason(normalized, keyword_category(normalized), high_confidence=False)
                if not normalized or not basic_keyword_allowed(normalized) or reason:
                    if normalized:
                        suppressed_rows.append({"record_index": len(per_record), "source": str(column), "term": raw or normalized, "cleaned_term": normalized, "category": keyword_category(normalized), "reason": reason or "basic keyword filter"})
                    continue
                terms.append(normalized)
                variants.setdefault(normalized, Counter())[raw or normalized] += 1
                frequencies[normalized] += 1
                categories.setdefault(normalized, keyword_category(normalized))
        per_record.append(sorted(set(terms)))

    for domain, path, name_cols in [
        ("geography", OUTPUTS_DIR / f"{slug}_geographic_terms_cleaned.csv", ("canonical_name", "term")),
        ("demographic", OUTPUTS_DIR / f"{slug}_demographic_terms_cleaned.csv", ("canonical_name", "term")),
        ("drug", OUTPUTS_DIR / f"{slug}_drug_terms_cleaned.csv", ("drug_name", "canonical_name", "term")),
        ("procedure", OUTPUTS_DIR / f"{slug}_procedure_terms_cleaned.csv", ("procedure_name", "canonical_name", "term")),
        ("intervention", OUTPUTS_DIR / f"{slug}_intervention_terms.csv", ("intervention_name", "canonical_name", "term")),
    ]:
        if not path.exists():
            continue
        term_df = safe_read_csv(path)
        if term_df.empty or "record_index" not in term_df.columns:
            continue
        for row in term_df.itertuples(index=False):
            data = dict(zip(term_df.columns, row, strict=False))
            record_number = pd.to_numeric(pd.Series([data.get("record_index")]), errors="coerce").iloc[0]
            if pd.isna(record_number):
                continue
            record_index = int(record_number)
            if record_index < 0 or record_index >= len(per_record):
                continue
            raw = next((str(data.get(column) or "").strip() for column in name_cols if str(data.get(column) or "").strip()), "")
            normalized = normalize_keyword_for_network(raw)
            confidence = str(data.get("confidence") or "").strip().lower()
            matched_field = str(data.get("matched_field") or "").strip().lower()
            match_method = str(data.get("match_method") or "").strip().lower()
            domain_high_confidence = domain != "geography" or confidence == "high" or matched_field in {"affiliation", "affiliations", "address", "location", "country"} or match_method == "supplemental_alias"
            reason = keyword_network_exclusion_reason(normalized, domain, high_confidence=domain_high_confidence)
            if not normalized or reason:
                if normalized:
                    suppressed_rows.append({"record_index": record_index, "source": domain, "term": raw or normalized, "cleaned_term": normalized, "category": domain, "reason": reason or "basic keyword filter"})
                continue
            per_record[record_index].append(normalized)
            variants.setdefault(normalized, Counter())[raw or normalized] += 1
            frequencies[normalized] += 1
            categories[normalized] = domain

    edges = build_keyword_network(per_record, set(frequencies))
    if edges.empty:
        edges = pd.DataFrame(columns=edge_columns)
    rows = [
        {
            "keyword": keyword,
            "normalized_keyword": keyword,
            "variants": "; ".join(sorted(variant_counts)),
            "variant_count": len(variant_counts),
            "total_frequency": int(frequencies[keyword]),
            "count": int(frequencies[keyword]),
            "category": categories.get(keyword, keyword_category(keyword)),
        }
        for keyword, variant_counts in variants.items()
    ]
    counts = pd.DataFrame(rows, columns=count_columns)
    if not counts.empty:
        counts = counts.sort_values(["total_frequency", "variant_count", "normalized_keyword"], ascending=[False, False, True])
    counts_path = processed_path(f"{stem}_keyword_network_all_keywords_nodes.csv")
    edges_path = processed_path(f"{stem}_keyword_network_all_keywords_edges.csv")
    counts.to_csv(counts_path, index=False)
    edges.to_csv(edges_path, index=False)
    remember_generated(generated, counts_path)
    remember_generated(generated, edges_path)
    write_suppressed_keyword_debug(f"{stem}_all_keywords", suppressed_rows, generated)
    if include_debug_visual and not counts.empty:
        graph = graph_from_edges(edges, max_edges=None, node_weights=dict(zip(counts["keyword"], counts["variant_count"], strict=False))) if not edges.empty else nx.Graph()
        metadata = counts.set_index("keyword").to_dict("index")
        for keyword in counts["keyword"]:
            if keyword not in graph:
                graph.add_node(str(keyword))
        for node in graph.nodes:
            node_meta = metadata.get(node, {})
            graph.nodes[node]["frequency"] = float(node_meta.get("variant_count", 1) or 1)
            graph.nodes[node]["total_frequency"] = float(node_meta.get("total_frequency", 0) or 0)
            graph.nodes[node]["variant_count"] = float(node_meta.get("variant_count", 1) or 1)
            graph.nodes[node]["node_type"] = str(node_meta.get("category") or "keyword").title()
        export_filtered_network(
            graph,
            NetworkPlotConfig(
                title="All Keyword Co-occurrence Network (Debug)",
                stem="keyword_network_all_keywords",
                node_kind="Keyword",
                min_node_frequency=1,
                min_edge_weight=1,
                top_n_nodes=None,
                label_top_n=min(20, len(counts)),
                drop_isolates=False,
                min_component_size=1,
                keep_largest_component=False,
                node_metric="frequency",
                size_metric="variant_count",
                subtitle="Debug view of all cleaned normalized keywords; node size reflects keyword frequency; edge thickness reflects co-occurrence strength.",
                network_type="All Keyword Co-occurrence Network (Debug)",
                input_keyword_table=str(counts_path),
                terms_suppressed=len(suppressed_rows),
            ),
            generated,
        )
    return counts, edges


def display_keyword_category(category: object) -> str:
    normalized = normalize_keyword(str(category or "keyword")).replace(" ", "_")
    if normalized in {"geography", "geographic"}:
        return "Geography"
    if normalized in {"demographic", "demographics", "population"}:
        return "Demographic/population"
    if normalized in {"drug", "drugs", "substance", "substances"}:
        return "Drug/substance"
    if normalized in {"procedure", "procedural", "intervention", "interventions"}:
        return "Procedure/intervention"
    return "General/topic keyword"


def plot_topic_category_keyword_network(
    counts: pd.DataFrame,
    edges: pd.DataFrame,
    stem: str,
    generated: list[Path],
    input_keyword_table: Path | str = "",
) -> None:
    if counts.empty or edges.empty:
        print("Skipping Topic Category Keyword Co-occurrence Network; no cleaned category keyword edges available.")
        return
    weight_col = "total_frequency" if "total_frequency" in counts.columns else "count"
    weights = dict(zip(counts["keyword"].astype(str), pd.to_numeric(counts[weight_col], errors="coerce").fillna(1), strict=False))
    graph = graph_from_edges(edges, max_edges=700, node_weights=weights, min_edge_weight=1)
    if graph.number_of_nodes() == 0:
        print("Skipping Topic Category Keyword Co-occurrence Network; no connected nodes remain.")
        return
    suppressed_count = csv_record_count(processed_path(f"{stem}_all_keywords_suppressed_keyword_network_terms_debug.csv"))
    metadata = counts.set_index("keyword").to_dict("index")
    for node in graph.nodes:
        meta = metadata.get(node, {})
        graph.nodes[node]["frequency"] = float(meta.get(weight_col, weights.get(node, 1)) or 1)
        graph.nodes[node]["node_type"] = display_keyword_category(meta.get("category", "keyword"))
    export_filtered_network(
        graph,
        NetworkPlotConfig(
            title="Topic Category Keyword Co-occurrence Network",
            stem=f"{stem}_keyword_network_topic_categories",
            node_kind="Keyword",
            min_node_frequency=1,
            min_edge_weight=FINAL_KEYWORD_MIN_EDGE_WEIGHT,
            top_n_nodes=FINAL_KEYWORD_TOP_NODES,
            label_top_n=FINAL_KEYWORD_LABEL_TOP_N,
            keep_largest_component=True,
            node_metric="frequency",
            size_metric="frequency",
            subtitle="Node size reflects keyword frequency; edge thickness reflects co-occurrence strength; colors show extracted topic category.",
            network_type="Topic Category Keyword Co-occurrence Network",
            input_keyword_table=str(input_keyword_table),
            terms_suppressed=suppressed_count,
        ),
        generated,
    )


def title_abstract_terms(text: str, query_terms: set[str]) -> list[str]:
    text = normalize_keyword(text)
    tokens = [token for token in re.findall(r"[a-z][a-z0-9-]+", text) if keyword_allowed(token, query_terms)]
    phrases: list[str] = []
    for n in (2, 3):
        for gram in zip(*(tokens[i:] for i in range(n))):
            phrase = " ".join(gram)
            if any(term in MEANINGFUL_SINGLE_TERMS for term in gram):
                phrases.append(phrase)
    phrases.extend(token for token in tokens if token in MEANINGFUL_SINGLE_TERMS or len(token) >= 8)
    return phrases


def title_keyword_terms(text: str) -> list[str]:
    """Extract candidate keyword terms from titles without using abstracts."""
    text = normalize_keyword(text)
    tokens = [
        token
        for token in re.findall(r"[a-z][a-z0-9-]+", text)
        if len(token) >= 3 and token not in STOPWORDS
    ]
    terms: list[str] = []
    terms.extend(tokens)
    for n in (2, 3):
        for gram in zip(*(tokens[i:] for i in range(n))):
            if any(token in MEANINGFUL_SINGLE_TERMS or token in DRUG_TERMS for token in gram):
                terms.append(" ".join(gram))
    return terms


def normalize_drug_text(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = value.replace("-", " ")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def drug_tokens_and_ngrams(text: str) -> tuple[list[str], list[tuple[str, int, int]]]:
    normalized = normalize_drug_text(text)
    tokens = re.findall(r"[a-z][a-z0-9]+", normalized)
    candidates: list[tuple[str, int, int]] = []
    for idx, token in enumerate(tokens):
        candidates.append((token, idx, idx + 1))
    for n in (2, 3):
        for idx in range(max(0, len(tokens) - n + 1)):
            candidates.append((" ".join(tokens[idx : idx + n]), idx, idx + n))
    return tokens, candidates


def has_drug_context(tokens: list[str], start: int, end: int, window: int = 5) -> bool:
    left = max(0, start - window)
    right = min(len(tokens), end + window)
    context = set(tokens[left:start] + tokens[end:right])
    return bool(context & DRUG_CONTEXT_TERMS)


def canonical_drug_name(value: str, drug_vocab: set[str] | None = None) -> str:
    normalized = normalize_drug_text(value)
    if not normalized:
        return ""
    if normalized in DRUG_ALIASES:
        return DRUG_ALIASES[normalized]
    for alias, canonical in DRUG_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            return canonical
    vocab = drug_vocab or load_drug_vocab()
    if normalized in vocab:
        return normalized
    # Collapse salt/suffix variants such as ravulizumab-cwvz -> ravulizumab.
    for token in normalized.split():
        if token in vocab:
            return token
    return normalized


def load_drug_vocab() -> set[str]:
    """Load RxNorm/DrugBank/curated drug names from local files, with curated fallback."""
    global _DRUG_VOCAB_CACHE
    if _DRUG_VOCAB_CACHE is not None:
        return _DRUG_VOCAB_CACHE

    vocab = {normalize_drug_text(term) for term in DRUG_TERMS | set(DRUG_ALIASES)}
    vocab.update(normalize_drug_text(value) for value in DRUG_ALIASES.values())

    paths: list[Path] = []
    env_path = os.getenv("DRUG_VOCAB_PATH")
    if env_path:
        paths.append(Path(env_path).expanduser())
    paths.extend(DRUG_VOCAB_CANDIDATES)

    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            loaded = load_drug_vocab_file(path)
        except Exception as exc:
            print(f"Drug vocabulary skipped {path}: {exc.__class__.__name__}")
            continue
        vocab.update(loaded)
        print(f"Loaded drug vocabulary: {path} ({len(loaded):,} names)")

    _DRUG_VOCAB_CACHE = {term for term in vocab if term and len(term) >= 3}
    return _DRUG_VOCAB_CACHE


def load_drug_vocab_file(path: Path) -> set[str]:
    names: set[str] = set()
    if path.suffix.lower() == ".rrf":
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("|")
                if len(parts) > 14:
                    names.add(normalize_drug_text(parts[14]))
        return {name for name in names if name}

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = "\t" if "\t" in sample else ","
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames:
            preferred = [
                field
                for field in reader.fieldnames
                if field and field.lower() in {"drug", "drug_name", "name", "ingredient", "rxnorm_name", "generic_name"}
            ]
            fields = preferred or reader.fieldnames[:3]
            for row in reader:
                for field in fields:
                    names.add(normalize_drug_text(row.get(field, "")))
        else:
            handle.seek(0)
            for row in csv.reader(handle, delimiter=delimiter):
                if row:
                    names.add(normalize_drug_text(row[0]))
    return {name for name in names if name}


def normalize_procedure_text(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = value.replace("-", " ")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def canonical_procedure_name(value: str) -> str:
    normalized = normalize_procedure_text(value)
    if not normalized:
        return ""
    replacements = {
        "haemodialysis": "hemodialysis",
        "renal transplant": "kidney transplantation",
        "kidney transplant": "kidney transplantation",
    }
    return replacements.get(normalized, normalized)


def load_procedure_vocab() -> set[str]:
    """Load optional UMLS/SNOMED/procedure dictionaries, with a broad curated fallback."""
    global _PROCEDURE_VOCAB_CACHE
    if _PROCEDURE_VOCAB_CACHE is not None:
        return _PROCEDURE_VOCAB_CACHE

    vocab = {canonical_procedure_name(term) for term in PROCEDURAL_SEED_TERMS}
    paths: list[Path] = []
    env_path = os.getenv("PROCEDURE_VOCAB_PATH") or os.getenv("UMLS_PROCEDURE_VOCAB_PATH")
    if env_path:
        paths.append(Path(env_path).expanduser())
    paths.extend(PROCEDURE_VOCAB_CANDIDATES)

    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            loaded = load_procedure_vocab_file(path)
        except Exception as exc:
            print(f"Procedure vocabulary skipped {path}: {exc.__class__.__name__}")
            continue
        vocab.update(loaded)
        print(f"Loaded procedure vocabulary: {path} ({len(loaded):,} names)")

    _PROCEDURE_VOCAB_CACHE = {term for term in vocab if term and len(term) >= 4 and term not in PROCEDURAL_GENERIC_TERMS}
    return _PROCEDURE_VOCAB_CACHE


def load_procedure_vocab_file(path: Path) -> set[str]:
    names: set[str] = set()
    if path.suffix.lower() == ".rrf":
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("|")
                if len(parts) > 14:
                    names.add(canonical_procedure_name(parts[14]))
        return {name for name in names if name}

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = "\t" if "\t" in sample else ","
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames:
            preferred = [
                field
                for field in reader.fieldnames
                if field
                and field.lower()
                in {
                    "procedure",
                    "procedure_name",
                    "intervention",
                    "therapy",
                    "name",
                    "term",
                    "preferred_name",
                    "snomed_name",
                    "umls_name",
                }
            ]
            fields = preferred or reader.fieldnames[:3]
            for row in reader:
                for field in fields:
                    names.add(canonical_procedure_name(row.get(field, "")))
        else:
            handle.seek(0)
            for row in csv.reader(handle, delimiter=delimiter):
                if row:
                    names.add(canonical_procedure_name(row[0]))
    return {name for name in names if name}


def keyword_allowed(keyword: str, query_terms: set[str], generic_terms: set[str] | None = None) -> bool:
    if not keyword or len(keyword) < 3:
        return False
    words = set(re.findall(r"[a-z][a-z0-9-]+", keyword.lower()))
    if not words:
        return False
    if words & query_terms:
        return False
    if words <= STOPWORDS:
        return False
    generic_terms = generic_terms or DOMAIN_GENERIC_TERMS
    if words <= generic_terms:
        return False
    return True


def build_keyword_network(per_record_keywords: list[list[str]], keep: set[str]) -> pd.DataFrame:
    edge_counts: Counter[tuple[str, str]] = Counter()
    for terms in tqdm(per_record_keywords, desc="Building networks"):
        terms = sorted(set(term for term in terms if term in keep))
        for left, right in combinations(terms, 2):
            if left == right:
                continue
            edge_counts[(left, right)] += 1
    return pd.DataFrame(
        [(left, right, weight) for (left, right), weight in edge_counts.items()],
        columns=["source", "target", "weight"],
    )


def keyword_counts_frame(frequencies: Counter[str], keep: set[str]) -> pd.DataFrame:
    keyword_counts = pd.DataFrame(frequencies.most_common(), columns=["keyword", "count"])
    if keyword_counts.empty:
        return keyword_counts
    return keyword_counts[keyword_counts["keyword"].isin(keep)].reset_index(drop=True)


def write_semicolon_network(edges: pd.DataFrame, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        for row in edges.itertuples(index=False):
            source = str(row.source).strip()
            target = str(row.target).strip()
            if source and target and source != target:
                handle.write(f"{source};{target}\n")


def save_keyword_pipeline_files(stem: str, mode: str, counts: pd.DataFrame, edges: pd.DataFrame, generated: list[Path]) -> None:
    counts_path = processed_path(f"{stem}_keywords_{mode}.csv")
    vos_counts_path = VOS_DIR / f"{stem}_keywords_{mode}.csv"
    network_path = processed_path(f"{stem}_keyword_network_{mode}.txt")
    vos_network_path = VOS_DIR / f"{stem}_keyword_network_{mode}.txt"
    counts.to_csv(counts_path, index=False)
    VOS_DIR.mkdir(parents=True, exist_ok=True)
    counts.to_csv(vos_counts_path, index=False)
    write_semicolon_network(edges, network_path)
    write_semicolon_network(edges, vos_network_path)
    generated.extend([counts_path, vos_counts_path, network_path, vos_network_path])


def save_intervention_files(
    stem: str,
    mode: str,
    counts: pd.DataFrame,
    edges: pd.DataFrame,
    generated: list[Path],
) -> None:
    counts_path = processed_path(f"{stem}_interventions_{mode}.csv")
    vos_counts_path = VOS_DIR / f"{stem}_interventions_{mode}.csv"
    network_path = processed_path(f"{stem}_network_{mode}.txt")
    vos_network_path = VOS_DIR / f"{stem}_network_{mode}.txt"
    counts.to_csv(counts_path, index=False)
    VOS_DIR.mkdir(parents=True, exist_ok=True)
    counts.to_csv(vos_counts_path, index=False)
    write_semicolon_network(edges, network_path)
    write_semicolon_network(edges, vos_network_path)
    generated.extend([counts_path, vos_counts_path, network_path, vos_network_path])


def build_combined_intervention_edges(per_record_pairs: list[tuple[list[str], list[str]]]) -> pd.DataFrame:
    edge_counts: Counter[tuple[str, str]] = Counter()
    for drugs, procedures in per_record_pairs:
        for drug in sorted(set(drugs)):
            for procedure in sorted(set(procedures)):
                if drug and procedure and drug != procedure:
                    edge_counts[(drug, procedure)] += 1
    return pd.DataFrame(
        [(drug, procedure, weight) for (drug, procedure), weight in edge_counts.items()],
        columns=["source", "target", "weight"],
    ).sort_values(["weight", "source", "target"], ascending=[False, True, True], ignore_index=True)


def term_contains_query(term: str, query_terms: set[str]) -> bool:
    return bool(set(clean_text_tokens(term)) & query_terms)


def drug_candidates_from_text(text: str) -> list[str]:
    tokens = [token for token in clean_text_tokens(text) if token not in STOPWORDS]
    candidates = build_ngrams(tokens)
    return remove_stopword_grams(
        candidate
        for candidate in candidates
        if len(candidate) >= 3 and candidate not in COMMON_ENGLISH_WORDS
    )


def intervention_text_from_row(row: pd.Series, columns: Iterable[str]) -> str:
    useful_columns = {"keywords", "keyword", "author_keywords", "index_keywords", "mesh_terms", "mesh", "title", "abstract"}
    return " ".join(str(row.get(col, "")) for col in columns if str(col).lower() in useful_columns)


def procedure_candidates_from_text(text: str) -> list[str]:
    tokens = [token for token in clean_text_tokens(text) if token not in STOPWORDS]
    candidates = build_ngrams(tokens, max_n=4)
    return remove_stopword_grams(
        candidate
        for candidate in candidates
        if len(candidate) >= 4 and candidate not in COMMON_ENGLISH_WORDS
    )


def has_procedure_context(tokens: list[str], start: int, end: int, window: int = 5) -> bool:
    left = max(0, start - window)
    right = min(len(tokens), end + window)
    context = set(tokens[left:start] + tokens[end:right])
    return bool(context & PROCEDURAL_CONTEXT_TERMS)


def procedure_tokens_and_ngrams(text: str) -> tuple[list[str], list[tuple[str, int, int]]]:
    normalized = normalize_procedure_text(text)
    tokens = re.findall(r"[a-z][a-z0-9]+", normalized)
    candidates: list[tuple[str, int, int]] = []
    for idx, token in enumerate(tokens):
        candidates.append((token, idx, idx + 1))
    for n in (2, 3, 4):
        for idx in range(max(0, len(tokens) - n + 1)):
            candidates.append((" ".join(tokens[idx : idx + n]), idx, idx + n))
    return tokens, candidates


def detect_procedure_candidate(
    candidate: str,
    procedure_vocab: set[str],
    drug_vocab: set[str],
    tokens: list[str],
    spans: list[tuple[str, int, int]],
) -> dict[str, str]:
    normalized = canonical_procedure_name(candidate)
    if not normalized or normalized in PROCEDURAL_GENERIC_TERMS or normalized in COMMON_ENGLISH_WORDS:
        return {}
    words = clean_text_tokens(normalized)
    if not words:
        return {}
    if set(words) & PROCEDURAL_RELATION_TERMS:
        return {}
    if any(word in drug_vocab or word in DRUG_TERMS or word in DRUG_ALIASES for word in words):
        return {}

    if normalized in procedure_vocab:
        return {"procedure_name": normalized, "source": "procedure_dictionary"}

    if words[0] in PROCEDURAL_HEAD_TERMS and len(words) > 2:
        return {}

    if words[-1] in PROCEDURAL_HEAD_TERMS and len(words) > 1:
        return {"procedure_name": normalized, "source": "keyword_pattern"}

    if len(words) == 1 and words[0] in PROCEDURAL_HEAD_TERMS - PROCEDURAL_GENERIC_TERMS:
        return {"procedure_name": normalized, "source": "keyword"}

    matching_spans = [(start, end) for cand, start, end in spans if cand == normalized]
    if matching_spans and any(has_procedure_context(tokens, start, end) for start, end in matching_spans):
        if words[-1] in PROCEDURAL_HEAD_TERMS:
            return {"procedure_name": normalized, "source": "context_keyword"}
    return {}


def prefer_specific_procedure_terms(terms: list[str], match_rows: list[dict[str, str]]) -> tuple[list[str], list[dict[str, str]]]:
    ordered = sorted(dedupe_terms(terms), key=lambda term: (-len(clean_text_tokens(term)), term))
    selected: list[str] = []
    for term in ordered:
        term_words = clean_text_tokens(term)
        if any(re.search(rf"\b{re.escape(term)}\b", existing) for existing in selected if existing != term):
            continue
        if len(term_words) <= 2 and any(set(term_words) < set(clean_text_tokens(existing)) for existing in selected):
            continue
        selected.append(term)
    selected = sorted(selected)
    selected_set = set(selected)
    filtered_matches = [row for row in match_rows if row.get("canonical") in selected_set]
    return selected, filtered_matches


def extract_procedure_terms_with_matches(text: str, procedure_vocab: set[str]) -> tuple[list[str], list[dict[str, str]]]:
    tokens, spans = procedure_tokens_and_ngrams(text)
    drug_vocab = load_drug_vocab()
    detected: list[str] = []
    match_rows: list[dict[str, str]] = []
    for candidate in procedure_candidates_from_text(text):
        match = detect_procedure_candidate(candidate, procedure_vocab, drug_vocab, tokens, spans)
        if not match:
            continue
        procedure_name = canonical_procedure_name(match["procedure_name"])
        detected.append(procedure_name)
        match_rows.append(
            {
                "matched_text": candidate,
                "canonical": procedure_name,
                "method": match.get("source", ""),
            }
        )
    return prefer_specific_procedure_terms(detected, match_rows)


def extract_keyword_pipelines(
    core_path: Path,
    query: str,
    generated: list[Path],
    use_rxnorm: bool = True,
) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    print("Drug/procedure extraction moved to GeoCensus.py; building general keyword outputs only.")
    return extract_visual_keyword_pipelines(core_path, query, generated)


def print_keyword_pipeline_summary(
    outputs: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
    match_samples: list[dict[str, str]],
    procedure_match_samples: list[dict[str, str]],
    raw_unique: int,
) -> None:
    print("Keyword pipeline summary:")
    for mode in ("general", "filtered", "drugs", "procedural", "combined"):
        counts = outputs[mode][0]
        label = "pairs" if mode == "combined" else "keywords"
        print(f"  {mode}: {len(counts)} {label}")
    drug_counts = outputs["drugs"][0]
    print(f"  unique drugs: {raw_unique}")
    if not drug_counts.empty:
        print("Top 10 drugs + types:")
        for row in drug_counts.head(10).itertuples(index=False):
            print(f"  {row.drug_name}: {int(row.count)} ({row.tty or 'untyped'})")


def extract_visual_keyword_pipelines(
    core_path: Path,
    query: str,
    generated: list[Path],
) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    """Build only general/filtered keyword outputs; reference layers come from GeoCensus.py."""
    df = safe_read_csv(core_path)
    require_core_columns(df, core_path)
    print(f"Loaded dataset: {len(df):,} rows")
    query_terms = tokenize_query(query)
    stem = core_path.with_suffix("").name
    modes = {
        "general": {"records": [], "frequencies": Counter()},
        "filtered": {"records": [], "frequencies": Counter()},
    }
    suppressed_rows: list[dict[str, object]] = []

    for record_index, row in tqdm(df.iterrows(), total=len(df), desc="Extracting general keywords"):
        general_terms = tokenize_all(row)
        filtered_terms = dedupe_terms(term for term in general_terms if not term_contains_query(term, query_terms))
        for mode, selected_terms in (("general", general_terms), ("filtered", filtered_terms)):
            cleaned_terms: list[str] = []
            for term in selected_terms:
                reason = keyword_network_exclusion_reason(term)
                if reason:
                    suppressed_rows.append({"record_index": record_index, "mode": mode, "term": term, "cleaned_term": normalize_keyword(term), "category": "keyword", "reason": reason})
                    continue
                cleaned_terms.append(term)
            selected_terms = cleaned_terms[:MAX_KEYWORDS_PER_RECORD]
            if selected_terms:
                modes[mode]["records"].append(selected_terms)
                modes[mode]["frequencies"].update(selected_terms)

    outputs: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for mode, data in modes.items():
        frequencies: Counter[str] = data["frequencies"]
        keep = {term for term, count in frequencies.items() if count >= KEYWORD_PIPELINE_MIN_FREQUENCY}
        counts = keyword_counts_frame(frequencies, keep)
        edges = build_keyword_network(data["records"], keep)
        save_keyword_pipeline_files(stem, mode, counts, edges, generated)
        outputs[mode] = (counts, edges)
    write_suppressed_keyword_debug(stem, suppressed_rows, generated)
    return outputs


def load_geocensus_drug_outputs(slug: str, stem: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load drug extraction outputs generated by GeoCensus.py, if available."""
    count_candidates = [
        OUTPUTS_DIR / f"{slug}_drug_term_counts_cleaned.csv",
        OUTPUTS_DIR / f"{slug}_drug_term_counts.csv",
        PROCESSED_DIR / f"{slug}_interventions_drugs.csv",
        PROCESSED_DIR / f"{stem}_interventions_drugs.csv",
    ]
    edge_candidates = [
        PROCESSED_DIR / f"{slug}_network_drugs.txt",
        PROCESSED_DIR / f"{stem}_network_drugs.txt",
        VOS_DIR / f"{slug}_network_drugs.txt",
        VOS_DIR / f"{stem}_network_drugs.txt",
    ]
    counts_path = next((path for path in count_candidates if path.exists()), None)
    edges_path = next((path for path in edge_candidates if path.exists()), None)
    if counts_path is None:
        return pd.DataFrame(columns=["drug_name", "count", "tty"]), pd.DataFrame(columns=["source", "target", "weight"])
    counts = safe_read_csv(counts_path)
    if "drug_name" not in counts.columns and "canonical_name" in counts.columns:
        counts["drug_name"] = counts["canonical_name"]
    if "count" not in counts.columns:
        if "record_count" in counts.columns:
            counts["count"] = counts["record_count"]
        else:
            counts["count"] = 1
    if "tty" not in counts.columns:
        counts["tty"] = ""
    edges = read_vos_network(edges_path) if edges_path and edges_path.suffix == ".txt" else read_edge_csv(edges_path) if edges_path else pd.DataFrame(columns=["source", "target", "weight"])
    return counts[["drug_name", "count", "tty"]], edges


def plot_reference_term_counts(slug: str, generated: list[Path]) -> None:
    """Visualize GeoCensus geography/demographic summaries when present."""
    specs = [
        ("geographic", "Geographic Terms", "#2563eb"),
        ("demographic", "Demographic Terms", "#0f766e"),
        ("drug", "Drug Terms", DRUG_COLOR),
        ("procedure", "Procedure Terms", PROCEDURE_COLOR),
    ]
    for prefix, title, color in specs:
        cleaned_path = OUTPUTS_DIR / f"{slug}_{prefix}_term_counts_cleaned.csv"
        path = cleaned_path if cleaned_path.exists() else OUTPUTS_DIR / f"{slug}_{prefix}_term_counts.csv"
        if not path.exists():
            print(f"Skipping {title}; GeoCensus output not found: {path.name}")
            continue
        df = geography_count_rows(slug) if prefix == "geographic" else safe_read_csv(path)
        if df.empty or "canonical_name" not in df.columns:
            print(f"Skipping {title}; no extracted terms available.")
            continue
        value_col = "record_count" if "record_count" in df.columns else "count" if "count" in df.columns else "mention_count"
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce").fillna(0)
        chart_df = df.sort_values([value_col, "canonical_name"], ascending=[False, True]).head(25)
        out_path = processed_path(f"{slug}_{prefix}_term_counts_top.csv")
        chart_df.to_csv(out_path, index=False)
        generated.append(out_path)
        save_static_bar(
            chart_df,
            f"{slug}_{prefix}_term_counts",
            title,
            "canonical_name",
            value_col,
            generated,
            color=color,
            highlight_n=min(5, len(chart_df)),
            subtitle="Cleaned extracted terms after ambiguity filtering" if path == cleaned_path else "Extracted by GeoCensus.py; descriptive layer only",
        )


COUNTRY_CENTROIDS = {
    "united states": (39.8283, -98.5795),
    "united kingdom": (55.3781, -3.4360),
    "germany": (51.1657, 10.4515),
    "france": (46.2276, 2.2137),
    "brazil": (-14.2350, -51.9253),
    "japan": (36.2048, 138.2529),
}
REGION_CENTROIDS = {
    "west texas": (31.9686, -102.5000),
    "sub saharan africa": (-2.0, 20.0),
    "eastern europe": (50.0, 25.0),
}
STATE_CENTROIDS = {
    "texas": (31.0, -99.0),
    "california": (36.7783, -119.4179),
    "new york": (43.0, -75.0),
}
WORLD_REGIONS_TEMPLATE = [
    ("North America", 50, -105),
    ("Latin America", -15, -60),
    ("Europe", 52, 15),
    ("Sub-Saharan Africa", -5, 20),
    ("Middle East & North Africa", 28, 35),
    ("South Asia", 22, 78),
    ("East Asia & Pacific", 25, 120),
]
GEOMETRY_SOURCE_CANDIDATES = {
    "world": MAPS_ROOT / "boundaries" / "geoboundaries",
    "us": census_boundary_dir("states"),
    "texas": census_boundary_dir("counties"),
    "world_regions": MAPS_ROOT / "boundaries" / "geoboundaries",
}
EAST_ASIA_COUNTRIES = {
    "china",
    "japan",
    "south korea",
    "north korea",
    "korea, republic of",
    "korea, democratic people's republic of",
    "taiwan",
    "mongolia",
    "hong kong",
    "macau",
}
MIDDLE_EAST_COUNTRIES = {
    "turkey", "iran", "iraq", "israel", "palestine", "jordan", "lebanon", "syria",
    "saudi arabia", "yemen", "oman", "united arab emirates", "qatar", "bahrain", "kuwait",
}
NORTH_AMERICA_COUNTRIES = {"united states", "canada", "mexico"}
LATIN_AMERICA_SUBREGIONS = {"south america", "central america", "caribbean"}
OCEANIA_COUNTRIES = {"australia", "new zealand", "fiji", "papua new guinea", "samoa", "tonga", "vanuatu"}
NORTH_AFRICA_COUNTRIES = {"egypt", "libya", "tunisia", "algeria", "morocco", "western sahara", "sudan"}
SUB_SAHARAN_AFRICA_EXCLUDED = NORTH_AFRICA_COUNTRIES
SOUTH_ASIA_COUNTRIES = {"india", "pakistan", "bangladesh", "nepal", "sri lanka", "bhutan", "maldives", "afghanistan"}
SOUTH_ASIA_REQUIRED_ISO3 = {
    "IND": "India",
    "PAK": "Pakistan",
    "BGD": "Bangladesh",
    "NPL": "Nepal",
    "LKA": "Sri Lanka",
    "BTN": "Bhutan",
    "MDV": "Maldives",
    "AFG": "Afghanistan",
}
NORTH_AMERICA_ISO3 = {"USA", "CAN", "MEX"}
LATIN_AMERICA_ISO3 = {
    "ARG", "BOL", "BRA", "CHL", "COL", "ECU", "GUY", "PRY", "PER", "SUR", "URY", "VEN",
    "BLZ", "CRI", "SLV", "GTM", "HND", "NIC", "PAN",
    "ATG", "BHS", "BRB", "CUB", "DMA", "DOM", "GRD", "HTI", "JAM", "KNA", "LCA", "VCT", "TTO",
}
EUROPE_ISO3 = {
    "ALB", "AND", "AUT", "BEL", "BIH", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA",
    "DEU", "GRC", "HUN", "ISL", "IRL", "ITA", "LVA", "LIE", "LTU", "LUX", "MLT", "MDA", "MCO",
    "MNE", "NLD", "MKD", "NOR", "POL", "PRT", "ROU", "RUS", "SMR", "SRB", "SVK", "SVN", "ESP",
    "SWE", "CHE", "UKR", "GBR", "VAT",
}
EAST_ASIA_ISO3 = {"CHN", "JPN", "KOR", "PRK", "MNG", "TWN", "HKG", "MAC"}
SOUTH_ASIA_ISO3 = set(SOUTH_ASIA_REQUIRED_ISO3)
MIDDLE_EAST_ISO3 = {"BHR", "IRN", "IRQ", "ISR", "JOR", "KWT", "LBN", "OMN", "PSE", "QAT", "SAU", "SYR", "TUR", "ARE", "YEM"}
NORTH_AFRICA_ISO3 = {"DZA", "EGY", "LBY", "MAR", "SDN", "TUN", "ESH"}
AFRICA_ISO3 = {
    "AGO", "BEN", "BWA", "BFA", "BDI", "CPV", "CMR", "CAF", "TCD", "COM", "COG", "COD", "CIV",
    "DJI", "GNQ", "ERI", "SWZ", "ETH", "GAB", "GMB", "GHA", "GIN", "GNB", "KEN", "LSO", "LBR",
    "MDG", "MWI", "MLI", "MRT", "MUS", "MOZ", "NAM", "NER", "NGA", "RWA", "STP", "SEN", "SYC",
    "SLE", "SOM", "ZAF", "SSD", "TZA", "TGO", "UGA", "ZMB", "ZWE", *NORTH_AFRICA_ISO3,
}
SUB_SAHARAN_AFRICA_ISO3 = AFRICA_ISO3 - NORTH_AFRICA_ISO3
OCEANIA_ISO3 = {
    "AUS", "NZL", "FJI", "PNG", "WSM", "TON", "VUT", "KIR", "MHL", "FSM", "NRU", "PLW", "SLB",
    "TUV", "COK", "NIU",
}
REGION_ISO3: dict[str, set[str]] = {
    "north_america": NORTH_AMERICA_ISO3,
    "latin_america": LATIN_AMERICA_ISO3,
    "europe": EUROPE_ISO3,
    "east_asia": EAST_ASIA_ISO3,
    "south_asia": SOUTH_ASIA_ISO3,
    "middle_east": MIDDLE_EAST_ISO3,
    "north_africa": NORTH_AFRICA_ISO3,
    "sub_saharan_africa": SUB_SAHARAN_AFRICA_ISO3,
    "oceania": OCEANIA_ISO3,
}
REGIONAL_MAPS_REQUIRED = {
    "north_america": {"title": "North America", "extent": (-172, -50, 7, 84), "countries": NORTH_AMERICA_ISO3},
    "latin_america": {"title": "Latin America", "extent": (-118, -30, -58, 35), "countries": LATIN_AMERICA_ISO3},
    "europe": {"title": "Europe", "extent": (-25, 45, 34, 72), "countries": EUROPE_ISO3},
    "east_asia": {"title": "East Asia", "extent": (72, 150, 15, 55), "countries": EAST_ASIA_ISO3},
    "south_asia": {"title": "South Asia", "extent": (58, 98, -2, 38), "countries": SOUTH_ASIA_ISO3},
    "middle_east": {"title": "Middle East", "extent": (25, 65, 10, 43), "countries": MIDDLE_EAST_ISO3},
    "north_africa": {"title": "North Africa", "extent": (-20, 40, 15, 38), "countries": NORTH_AFRICA_ISO3},
    "sub_saharan_africa": {"title": "Sub-Saharan Africa", "extent": (-20, 55, -36, 18), "countries": SUB_SAHARAN_AFRICA_ISO3},
    "oceania": {"title": "Oceania", "extent": (105, 180, -50, 5), "countries": OCEANIA_ISO3},
}
EXPECTED_REGION_KEYS = (
    "north_america",
    "latin_america",
    "europe",
    "east_asia",
    "south_asia",
    "middle_east",
    "north_africa",
    "sub_saharan_africa",
    "oceania",
)
SUSPICIOUS_REGIONAL_LABELS = {
    "armenia",
    "zaragoza",
    "friendly",
    "mars borough",
    "shinas",
    "shinas",
    "mile",
    "kato",
    "jordan",
    "pilon",
    "pedro",
    "america",
}
COUNTRY_ALIASES = {
    "usa": "united states",
    "u s": "united states",
    "u s a": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "u k": "united kingdom",
    "britain": "united kingdom",
    "south korea": "korea, republic of",
    "north korea": "korea, democratic people's republic of",
    "türkiye": "turkey",
    "turkiye": "turkey",
}
COUNTRY_NAME_TERMS = {
    "australia", "austria", "brazil", "canada", "china", "colombia", "croatia", "cuba",
    "czechia", "egypt", "france", "germany", "india", "italy", "japan", "mexico",
    "poland", "spain", "turkey", "ukraine", "united kingdom", "united states",
}
WEAK_AMBIGUOUS_PLACE_TERMS = {
    "aga", "gar", "hat", "hāt", "mitu", "much", "ray", "rāy", "bank", "bānk",
}
US_STATE_ABBREVIATIONS = {
    "tx": "texas",
    "ca": "california",
    "ny": "new york",
}


@dataclass
class ShapeLayer:
    path: Path
    shape_type: int
    fields: list[str]
    records: list[dict[str, object]]


@dataclass
class MapDiscovery:
    country_layer: ShapeLayer | None
    admin1_layer: ShapeLayer | None
    place_layer: ShapeLayer | None
    census_state_layer: ShapeLayer | None
    census_county_layer: ShapeLayer | None
    census_place_layer: ShapeLayer | None
    missing_expected: list[Path]
    invalid_layers: list[str]
    fallback_choices: list[str]
    missing_packages: list[str]
    boundary_folders: list[Path]


def map_norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower()).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return COUNTRY_ALIASES.get(text, US_STATE_ABBREVIATIONS.get(text, text))


def dbf_fields_and_records(dbf_path: Path) -> tuple[list[str], list[dict[str, object]]]:
    with dbf_path.open("rb") as handle:
        header = handle.read(32)
        if len(header) < 32:
            raise ValueError("invalid DBF header")
        record_count = struct.unpack("<I", header[4:8])[0]
        header_length = struct.unpack("<H", header[8:10])[0]
        record_length = struct.unpack("<H", header[10:12])[0]
        field_descriptors = handle.read(header_length - 33)
        handle.read(1)
        fields: list[tuple[str, str, int, int]] = []
        for idx in range(0, len(field_descriptors), 32):
            descriptor = field_descriptors[idx : idx + 32]
            if len(descriptor) < 32 or descriptor[0] == 0x0D:
                continue
            name = descriptor[:11].split(b"\x00", 1)[0].decode("latin1", errors="replace").strip()
            field_type = chr(descriptor[11])
            length = descriptor[16]
            decimals = descriptor[17]
            if name:
                fields.append((name, field_type, length, decimals))
        records: list[dict[str, object]] = []
        names = [field[0] for field in fields]
        for _ in range(record_count):
            raw = handle.read(record_length)
            if not raw or raw[:1] == b"*":
                continue
            offset = 1
            record: dict[str, object] = {}
            for name, field_type, length, _decimals in fields:
                value = raw[offset : offset + length].decode("latin1", errors="replace").replace("\x00", "").strip()
                offset += length
                if field_type in {"N", "F"} and value:
                    try:
                        record[name] = float(value)
                    except ValueError:
                        record[name] = value
                else:
                    record[name] = value
            records.append(record)
        return names, records


def shp_geometries(shp_path: Path) -> tuple[int, list[dict[str, object]]]:
    geometries: list[dict[str, object]] = []
    with shp_path.open("rb") as handle:
        header = handle.read(100)
        if len(header) < 100:
            raise ValueError("invalid SHP header")
        shape_type = struct.unpack("<i", header[32:36])[0]
        while True:
            rec_header = handle.read(8)
            if len(rec_header) == 0:
                break
            if len(rec_header) < 8:
                raise ValueError("invalid SHP record header")
            content_length = struct.unpack(">i", rec_header[4:8])[0] * 2
            content = handle.read(content_length)
            if len(content) < 4:
                continue
            rec_type = struct.unpack("<i", content[:4])[0]
            if rec_type in {1, 11, 21} and len(content) >= 20:
                x, y = struct.unpack("<dd", content[4:20])
                geometries.append({"point": (x, y), "parts": []})
            elif rec_type in {5, 15, 25} and len(content) >= 44:
                num_parts = struct.unpack("<i", content[36:40])[0]
                num_points = struct.unpack("<i", content[40:44])[0]
                parts_offset = 44
                points_offset = parts_offset + 4 * num_parts
                parts = list(struct.unpack(f"<{num_parts}i", content[parts_offset:points_offset])) if num_parts else [0]
                points = [
                    struct.unpack("<dd", content[points_offset + idx * 16 : points_offset + (idx + 1) * 16])
                    for idx in range(num_points)
                ]
                rings = []
                for idx, start in enumerate(parts):
                    end = parts[idx + 1] if idx + 1 < len(parts) else len(points)
                    ring = points[start:end]
                    if len(ring) >= 3:
                        rings.append(ring)
                geometries.append({"point": None, "parts": rings})
            else:
                geometries.append({"point": None, "parts": []})
    return shape_type, geometries


def geojson_rings(geometry: dict[str, object]) -> list[list[tuple[float, float]]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        return [
            [(float(x), float(y)) for x, y, *_rest in ring]
            for ring in coordinates
            if isinstance(ring, list) and len(ring) >= 3
        ]
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        rings: list[list[tuple[float, float]]] = []
        for polygon in coordinates:
            if not isinstance(polygon, list):
                continue
            rings.extend(
                [(float(x), float(y)) for x, y, *_rest in ring]
                for ring in polygon
                if isinstance(ring, list) and len(ring) >= 3
            )
        return rings
    return []


def geojson_point(geometry: dict[str, object]) -> tuple[float, float] | None:
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "Point" and isinstance(coordinates, list) and len(coordinates) >= 2:
        return float(coordinates[0]), float(coordinates[1])
    if geometry.get("type") == "MultiPoint" and isinstance(coordinates, list) and coordinates:
        point = coordinates[0]
        if isinstance(point, list) and len(point) >= 2:
            return float(point[0]), float(point[1])
    return None


def read_geojson_layer(path: Path) -> ShapeLayer:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        features = payload.get("features", [])
    elif isinstance(payload, dict) and payload.get("type") == "Feature":
        features = [payload]
    else:
        features = []
    if not isinstance(features, list) or not features:
        raise ValueError("empty GeoJSON feature collection")
    records: list[dict[str, object]] = []
    fields: set[str] = set()
    shape_type = 0
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
        row = dict(properties)
        fields.update(str(key) for key in row)
        rings = geojson_rings(geometry)
        point = geojson_point(geometry)
        row["_parts"] = rings
        row["_point"] = point
        if rings:
            shape_type = 5
        elif point and shape_type == 0:
            shape_type = 1
        records.append(row)
    if not records:
        raise ValueError("GeoJSON has no usable features")
    return ShapeLayer(path=path, shape_type=shape_type, fields=sorted(fields), records=records)


def read_gpkg_layer(path: Path) -> ShapeLayer:
    try:
        import geopandas as geopandas  # type: ignore
    except Exception as exc:
        raise ValueError(f"GeoPackage support requires geopandas: {exc}") from exc
    frame = geopandas.read_file(path)
    if frame.empty or "geometry" not in frame:
        raise ValueError("empty GeoPackage layer")
    features = json.loads(frame.to_json()).get("features", [])
    tmp_path = path.with_suffix(".geojson")
    records: list[dict[str, object]] = []
    fields: set[str] = set()
    shape_type = 0
    for feature in features:
        properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
        row = dict(properties)
        fields.update(str(key) for key in row)
        rings = geojson_rings(geometry)
        point = geojson_point(geometry)
        row["_parts"] = rings
        row["_point"] = point
        if rings:
            shape_type = 5
        elif point and shape_type == 0:
            shape_type = 1
        records.append(row)
    if not records:
        raise ValueError("GeoPackage has no usable features")
    return ShapeLayer(path=path if path.exists() else tmp_path, shape_type=shape_type, fields=sorted(fields), records=records)


def validate_map_layer(path: Path, expected_columns: tuple[str, ...] = ()) -> tuple[bool, str, ShapeLayer | None]:
    if path.suffix.lower() in {".geojson", ".json"}:
        try:
            layer = read_geojson_layer(path)
        except Exception as exc:
            return False, f"open failed: {exc.__class__.__name__}: {exc}", None
        field_keys = {field.lower() for field in layer.fields}
        if expected_columns and not any(column.lower() in field_keys for column in expected_columns):
            return False, f"missing expected name columns from {expected_columns}", None
        return True, "ok", layer
    if path.suffix.lower() == ".gpkg":
        try:
            layer = read_gpkg_layer(path)
        except Exception as exc:
            return False, f"open failed: {exc.__class__.__name__}: {exc}", None
        field_keys = {field.lower() for field in layer.fields}
        if expected_columns and not any(column.lower() in field_keys for column in expected_columns):
            return False, f"missing expected name columns from {expected_columns}", None
        return True, "ok", layer
    required = [path.with_suffix(ext) for ext in (".shp", ".dbf", ".shx", ".prj")]
    missing = [candidate.name for candidate in required if not candidate.exists()]
    if missing:
        return False, f"missing sidecar files: {', '.join(missing)}", None
    try:
        fields, attrs = dbf_fields_and_records(path.with_suffix(".dbf"))
        shape_type, geometries = shp_geometries(path)
    except Exception as exc:
        return False, f"open failed: {exc.__class__.__name__}: {exc}", None
    if not attrs or not geometries:
        return False, "empty shapefile", None
    if len(attrs) != len(geometries):
        return False, "DBF/SHP record count mismatch", None
    field_keys = {field.lower() for field in fields}
    if expected_columns and not any(column.lower() in field_keys for column in expected_columns):
        return False, f"missing expected name columns from {expected_columns}", None
    records = []
    for attr, geom in zip(attrs, geometries, strict=False):
        row = dict(attr)
        row["_parts"] = geom.get("parts", [])
        row["_point"] = geom.get("point")
        records.append(row)
    return True, "ok", ShapeLayer(path=path, shape_type=shape_type, fields=fields, records=records)


def find_first_valid_layer(paths: Iterable[Path | None], expected_columns: tuple[str, ...], invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    for candidate in paths:
        if candidate is None or not candidate.exists():
            continue
        valid, reason, layer = validate_map_layer(candidate, expected_columns)
        if valid and layer:
            return layer
        invalid.append(f"{candidate}: {reason}")
    return None


def find_country_layer(invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    warnings: list[str] = []
    world_path = world_adm0_geojson_path() if world_adm0_geojson_path().exists() else build_world_adm0_geojson(warnings)
    if world_path and world_path.exists():
        valid, reason, preflight_layer = validate_map_layer(world_path, ("boundaryName", "boundaryISO", "shapeName", "shapeISO", "NAME", "ADMIN", "ISO_A3"))
        if valid and preflight_layer:
            required_ok, required_errors = validate_required_basemap_countries("World city", preflight_layer.records, REQUIRED_WORLD_BASEMAP_COUNTRIES)
            if not required_ok:
                fallbacks.extend(required_errors)
                rebuild_path = build_world_adm0_geojson(warnings, force=True)
                if rebuild_path:
                    world_path = rebuild_path
        else:
            fallbacks.append(f"World ADM0 drawable preflight failed: {reason}")
            rebuild_path = build_world_adm0_geojson(warnings, force=True)
            if rebuild_path:
                world_path = rebuild_path
    fallbacks.extend(warnings)
    layer = find_first_valid_layer([world_path], ("boundaryName", "boundaryISO", "shapeName", "shapeISO", "NAME", "ADMIN", "ISO_A3"), invalid, fallbacks)
    if not layer:
        fallbacks.append("World ADM0 drawable file unavailable; add WORLD_ADM0.geojson or WORLD_ADM0_INDEX.json to build it.")
    return layer


def find_admin1_layer(invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    paths = [census_boundary_shapefile("states")]
    paths.extend(geoboundaries_boundary_file(iso3, "ADM1") for iso3 in IMPORTANT_COUNTRY_ISO3)
    return find_first_valid_layer(paths, ("NAME", "STUSPS", "shapeName", "shapeISO", "shapeGroup", "admin1"), invalid, fallbacks)


def find_populated_places_layer(invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    return find_census_layer("place", invalid)


def find_census_layer(kind: str, invalid: list[str]) -> ShapeLayer | None:
    folders = {
        "state": [census_boundary_dir("states")],
        "county": [census_boundary_dir("counties")],
        "place": [census_boundary_dir("places"), census_city_gazetteer_dir(), geonames_dir(), custom_gazetteer_dir()],
    }.get(kind, [])
    for folder in folders:
        if not folder.exists():
            continue
        paths = sorted(path for extension in ("*.geojson", "*.shp") for path in folder.rglob(extension))
        for path in paths:
            valid, reason, layer = validate_map_layer(path, ("NAME", "STUSPS", "STATEFP", "COUNTYFP", "PLACEFP", "shapeName", "shapeISO"))
            if valid and layer:
                return layer
            invalid.append(f"{path}: {reason}")
    return None


def find_country_admin_layer(iso3: str, invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    if not iso3:
        fallbacks.append("country-specific map skipped; missing ISO3 code")
        return None
    selection = select_country_boundary_provider(iso3)
    if not selection.path:
        fallbacks.append(selection.reason)
        return None
    expected = (
        "LAD24NM", "LAD23NM", "LAD22NM", "LAD21NM", "LADCD", "LAD24CD", "LAD23CD",
        "lad_name", "lad_code", "local authority", "local_authority", "name", "NAME",
        "shapeName", "shapeISO", "shapeGroup", "boundaryName", "boundaryISO", "admin1",
    ) if iso3.strip().upper() == "GBR" and selection.provider == "ONS.gov.uk" else (
        "shapeName", "shapeISO", "shapeGroup", "boundaryName", "boundaryISO", "NAME", "admin1",
    )
    candidate_paths = selection.candidates or ([selection.path] if selection.path else [])
    layer = find_first_valid_layer(candidate_paths, expected, invalid, fallbacks)
    if not layer and iso3.strip().upper() == "GBR" and selection.provider == "ONS.gov.uk":
        adm2 = geoboundaries_boundary_file("GBR", "ADM2")
        if adm2:
            fallback_selection = type(selection)(
                iso3="GBR",
                provider="geoBoundaries",
                admin_level="ADM2",
                path=adm2,
                boundary_type="geoBoundaries ADM2",
                title_suffix="by geoBoundaries ADM2",
                reason="ONS.gov.uk boundaries unavailable; falling back to geoBoundaries GBR ADM2.",
                candidates=[adm2],
                fallback_used=True,
            )
            layer = find_first_valid_layer([adm2], ("shapeName", "shapeISO", "shapeGroup", "boundaryName", "boundaryISO", "NAME", "admin1"), invalid, fallbacks)
            selection = fallback_selection
        if not layer:
            adm1 = geoboundaries_boundary_file("GBR", "ADM1")
            if adm1:
                fallback_selection = type(selection)(
                    iso3="GBR",
                    provider="geoBoundaries",
                    admin_level="ADM1",
                    path=adm1,
                    boundary_type="Region",
                    title_suffix="by Region",
                    reason="ONS.gov.uk and GBR ADM2 boundaries unavailable; falling back to geoBoundaries GBR ADM1.",
                    candidates=[adm1],
                    fallback_used=True,
                )
                layer = find_first_valid_layer([adm1], ("shapeName", "shapeISO", "shapeGroup", "boundaryName", "boundaryISO", "NAME", "admin1"), invalid, fallbacks)
                selection = fallback_selection
    if layer:
        setattr(layer, "provider_selection", selection)
        fallbacks.append(f"Detailed boundary provider selected for {iso3.upper()}: {selection.provider} {selection.boundary_type}; file={selection.path}")
        if iso3.strip().upper() == "GBR":
            if selection.provider == "ONS.gov.uk":
                fallbacks.append("UK detailed map provider: ONS.gov.uk Local Authority Districts. Fallback not used.")
            elif selection.admin_level == "ADM2":
                fallbacks.append("ONS.gov.uk boundaries unavailable; falling back to geoBoundaries GBR ADM2.")
            elif selection.admin_level == "ADM1":
                fallbacks.append("ONS.gov.uk boundaries unavailable; falling back to geoBoundaries GBR ADM1.")
    if layer and selection.provider == "geoBoundaries" and not geoboundaries_boundary_file(iso3, "ADM2"):
        fallbacks.append(f"geoBoundaries {iso3} ADM2 missing; using ADM1 for country-specific map")
    return layer


def report_map_availability(discovery: MapDiscovery, slug: str, maps_generated: list[str], skipped: list[str]) -> Path:
    report_path = processed_path(f"{slug}_map_discovery_report.txt")
    try:
        import geopandas as _geopandas  # type: ignore  # noqa: F401

        geopandas_status = "available"
    except Exception:
        geopandas_status = "unavailable; validated with built-in local shapefile reader"
    lines = [
        "Map discovery report",
        "",
        f"Maps root path checked: {MAPS_ROOT}",
        f"GeoPandas validation: {geopandas_status}",
        "Boundary folders checked:",
        *[f"- {path}" for path in discovery.boundary_folders],
        "",
        f"Selected country layer: {discovery.country_layer.path if discovery.country_layer else 'none'}",
        f"Selected admin-1 layer: {discovery.admin1_layer.path if discovery.admin1_layer else 'none'}",
        f"Selected populated places layer: {discovery.place_layer.path if discovery.place_layer else 'none'}",
        f"Census state layer: {discovery.census_state_layer.path if discovery.census_state_layer else 'missing'}",
        f"Census county layer: {discovery.census_county_layer.path if discovery.census_county_layer else 'missing'}",
        f"Census place layer: {discovery.census_place_layer.path if discovery.census_place_layer else 'missing'}",
        "",
        "Missing expected files:",
        *[f"- {path}" for path in discovery.missing_expected],
        "Invalid shapefiles:",
        *[f"- {item}" for item in discovery.invalid_layers],
        "Fallback choices used:",
        *[f"- {item}" for item in discovery.fallback_choices],
        "Missing map packages:",
        *[f"- {item}" for item in discovery.missing_packages],
        "Maps generated:",
        *[f"- {item}" for item in maps_generated],
        "Maps skipped and why:",
        *[f"- {item}" for item in skipped],
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def write_geography_map_validation(
    slug: str,
    discovery: MapDiscovery,
    maps_generated: list[str],
    skipped: list[str],
    mapped_count: int,
    unmapped_count: int,
    generated: list[Path],
    map_metadata: list[dict[str, object]] | None = None,
) -> Path:
    path = processed_path(f"{slug}_geography_map_validation.txt")
    generated_names = {Path(item).name for item in maps_generated}
    generated_paths = [Path(item) for item in maps_generated]

    def yes_no(name: str) -> str:
        return "yes" if f"{slug}_{name}.png" in generated_names else "no"

    texas_outputs = {
        f"{slug}_geography_points_texas_cities.png",
        f"{slug}_geography_heatmap_texas_counties.png",
        f"{slug}_geography_heatmap_texas.png",
    }
    regional_keys = ("europe", "east_asia", "south_asia", "latin_america", "africa", "middle_east", "north_america")
    regional_generated = sorted(name for name in generated_names if f"{slug}_geography_heatmap_" in name and any(region in name for region in regional_keys))
    country_generated = sorted(name for name in generated_names if name.startswith(f"{slug}_geography_heatmap_country_"))
    final_institution_maps = sorted(name for name in generated_names if name.startswith(f"{slug}_geography_points_") and name.endswith("_institutions.png"))
    text_debug_maps = sorted(name for name in generated_names if name.startswith(f"{slug}_geography_points_") and name.endswith("_text_mentions_debug.png"))
    regional_skipped = [reason for reason in skipped if any(region in reason for region in regional_keys)]
    country_skipped = [reason for reason in skipped if reason.startswith("country_") or "country-level only" in reason or "geometry unavailable" in reason]

    lines = [
        "Geography map validation",
        "",
        f"world map generated: {yes_no('geography_heatmap_world')}",
        f"world_map_generated: {yes_no('geography_heatmap_world')}",
        f"Europe map generated: {yes_no('geography_heatmap_europe')}",
        f"East Asia map generated: {yes_no('geography_heatmap_east_asia')}",
        f"South Asia map generated: {yes_no('geography_heatmap_south_asia')}",
        f"Latin America map generated: {yes_no('geography_heatmap_latin_america')}",
        f"Africa map generated: {yes_no('geography_heatmap_africa')}",
        f"Middle East map generated: {yes_no('geography_heatmap_middle_east')}",
        f"North America map generated: {yes_no('geography_heatmap_north_america')}",
        f"U.S. map generated: {yes_no('geography_heatmap_us')}",
        f"Texas map generated: {'yes' if generated_names & texas_outputs else 'no'}",
        f"world regions map generated: {'yes' if f'{slug}_geography_overview_world_regions.png' in generated_names else 'no'}",
        f"world_regions_overview_generated: {'yes' if f'{slug}_geography_overview_world_regions.png' in generated_names else 'no'}",
        "world_regions_overview_required: no",
        f"Texas city point map generated: {yes_no('geography_points_texas_cities')}",
        f"Texas county heatmap generated: {yes_no('geography_heatmap_texas_counties')}",
        f"country-specific maps generated: {sum(1 for item in generated_names if item.startswith(f'{slug}_geography_heatmap_country_'))}",
        f"regional_maps_generated: {', '.join(regional_generated) if regional_generated else 'none'}",
        f"regional_maps_skipped: {' | '.join(regional_skipped) if regional_skipped else 'none'}",
        f"country_maps_generated: {', '.join(country_generated) if country_generated else 'none'}",
        f"country_maps_skipped: {' | '.join(country_skipped) if country_skipped else 'none'}",
        f"final_institution_maps_generated: {', '.join(final_institution_maps) if final_institution_maps else 'none'}",
        f"text_debug_maps_generated: {', '.join(text_debug_maps) if text_debug_maps else 'none'}",
        "final map validation rule: institution maps are user-facing; text-mentioned geography maps are debug-only and do not satisfy final city-placement validation.",
        f"map_plan_path: {processed_path(f'{slug}_geography_map_plan.txt')}",
        f"mapping_audit_path: {processed_path(f'{slug}_geography_mapping_audit.csv')}",
        f"ambiguous_geography_matches_path: {processed_path(f'{slug}_ambiguous_geography_matches.csv')}",
        f"visuals_folder_validation_path: {processed_path(f'{slug}_visuals_folder_validation.txt')}",
        f"PNG-only validation result: {'pass' if all(path.suffix.lower() == '.png' and path.exists() for path in generated_paths) else 'fail'}",
        f"selected country layer path: {discovery.country_layer.path if discovery.country_layer else 'none'}",
        f"selected admin-1 layer path: {discovery.admin1_layer.path if discovery.admin1_layer else 'none'}",
        f"selected populated places layer path: {discovery.place_layer.path if discovery.place_layer else 'none'}",
        f"Census state layer available: {'yes' if discovery.census_state_layer else 'no'}",
        f"Census county layer available: {'yes' if discovery.census_county_layer else 'no'}",
        f"Census place layer available: {'yes' if discovery.census_place_layer else 'no'}",
        "Natural Earth required: no",
        "physical layers used: no",
        "raster layers used: no",
        f"number of mapped terms: {mapped_count}",
        f"number of unmapped terms: {unmapped_count}",
        f"number of map files skipped: {len(skipped)}",
        "map level used for generated maps: country/admin/place as applicable",
        "polygon choropleth used: yes",
        "point layer used: yes when detected place coordinates exist; detected terms only",
        "labels limited: yes",
        "output PNG exists:",
        *[f"- {Path(item).name}: {'yes' if Path(item).exists() else 'no'}" for item in maps_generated],
        "",
        "Map metadata:",
        *[
            "- "
            + "; ".join(
                f"{key}: {value}"
                for key, value in item.items()
                if value not in (None, "")
            )
            for item in (map_metadata or [])
        ],
        "skip reasons:",
        *[f"- {reason}" for reason in skipped],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    remember_generated(generated, path)
    return path


def build_map_discovery() -> MapDiscovery:
    invalid: list[str] = []
    fallbacks: list[str] = []
    package_statuses = report_missing_map_packages()
    missing_packages = [status.message for status in package_statuses if not status.present]
    boundary_folders = [
        MAPS_ROOT / "boundaries" / "census" / "USA",
        MAPS_ROOT / "boundaries" / "geoboundaries",
        MAPS_ROOT / "gazetteers" / "cities",
        MAPS_ROOT / "institutions",
    ]
    expected = [
        census_boundary_dir("states"),
        census_boundary_dir("counties"),
        census_boundary_dir("places"),
        geonames_dir(),
    ]
    return MapDiscovery(
        country_layer=find_country_layer(invalid, fallbacks),
        admin1_layer=find_admin1_layer(invalid, fallbacks),
        place_layer=find_populated_places_layer(invalid, fallbacks),
        census_state_layer=find_census_layer("state", invalid),
        census_county_layer=find_census_layer("county", invalid),
        census_place_layer=find_census_layer("place", invalid),
        missing_expected=[path for path in expected if not path.exists()],
        invalid_layers=invalid,
        fallback_choices=fallbacks,
        missing_packages=missing_packages,
        boundary_folders=boundary_folders,
    )


def title_case_place(value: object) -> str:
    return str(value or "").strip().title()


def geography_count_column(df: pd.DataFrame) -> str:
    for column in ("record_count", "count", "mention_count"):
        if column in df.columns:
            return column
    return df.columns[1] if len(df.columns) > 1 else df.columns[0]


def geography_count_rows(slug: str) -> pd.DataFrame:
    candidates = [
        OUTPUTS_DIR / f"{slug}_geographic_terms_cleaned.csv",
        OUTPUTS_DIR / f"{slug}_geographic_term_counts_cleaned.csv",
        OUTPUTS_DIR / f"{slug}_geographic_term_counts.csv",
    ]
    for path in candidates:
        if path.exists():
            frame = safe_read_csv(path)
            if not frame.empty:
                frame.attrs["source_path"] = str(path)
                df = frame.copy()
                break
    else:
        return pd.DataFrame()
    raw_count = 0
    for raw_path in (
        OUTPUTS_DIR / f"{slug}_geographic_terms_raw.csv",
        OUTPUTS_DIR / f"{slug}_geographic_term_counts_raw.csv",
        OUTPUTS_DIR / f"{slug}_geographic_terms.csv",
        OUTPUTS_DIR / f"{slug}_geographic_term_counts.csv",
    ):
        if raw_path.exists():
            try:
                raw_count = max(raw_count, count_csv_rows(raw_path))
            except Exception:
                continue
    if df.empty:
        return df
    count_col = geography_count_column(df)
    df["count"] = pd.to_numeric(df[count_col], errors="coerce").fillna(0).astype(int)
    df["count"] = df["count"].where(df["count"] > 0, 1)
    dedupe_cols = [column for column in ("canonical_name", "term", "country", "state", "admin1_name", "latitude", "longitude", "geo_type") if column in df.columns]
    if dedupe_cols:
        df = df.drop_duplicates(dedupe_cols, keep="first")
    suppressed_rows: list[pd.Series] = []

    def suppress(mask: pd.Series, reason: str) -> None:
        nonlocal df, suppressed_rows
        if mask.any():
            removed = df[mask].copy()
            removed["suppression_reason"] = reason
            suppressed_rows.extend([row for _idx, row in removed.iterrows()])
            df = df[~mask].copy()

    bad_terms = {
        "the village",
        "standard village",
        "axis cdp",
        "village",
        "cdp",
        "census designated place",
        "place",
        "administrative area",
        "administrative region",
        "hiv",
        "male",
        "time",
        "condom",
        "justice",
        "street",
        "central",
        "ande",
        "hub",
        "natal",
    }
    labels = df.get("canonical_name", df.get("term", pd.Series("", index=df.index))).astype(str).map(map_norm)
    suppress(labels.isin(bad_terms), "generic/biomedical/common false-positive place term")
    labels = df.get("canonical_name", df.get("term", pd.Series("", index=df.index))).astype(str).map(map_norm)
    geo_types = df.get("geo_type", pd.Series("", index=df.index)).astype(str).str.lower()
    place_kinds = df.get("place_kind", pd.Series("", index=df.index)).astype(str).str.lower()
    suppress((place_kinds == "cdp") & labels.str.contains(r"\b(?:cdp|village)\b", regex=True), "generic CDP/village artifact")
    labels = df.get("canonical_name", df.get("term", pd.Series("", index=df.index))).astype(str).map(map_norm)
    geo_types = df.get("geo_type", pd.Series("", index=df.index)).astype(str).str.lower()
    suppress((geo_types == "place") & labels.str.fullmatch(r"(?:the )?village|place|cdp", na=False), "generic place word")
    labels = df.get("canonical_name", df.get("term", pd.Series("", index=df.index))).astype(str).map(map_norm)
    suffix_artifacts = labels.str.fullmatch(r".+\b(?:city|town|village|street|central|point|delta)\b", na=False)
    has_coordinates = pd.to_numeric(df.get("latitude", pd.Series("", index=df.index)), errors="coerce").notna() & pd.to_numeric(df.get("longitude", pd.Series("", index=df.index)), errors="coerce").notna()
    confident_context = df.get("matched_field", pd.Series("", index=df.index)).astype(str).str.lower().isin({"affiliation", "address", "location", "country", "state", "admin1"})
    suppress(suffix_artifacts & ~(has_coordinates & confident_context), "suffix/phrase-fragment place alias without confident context")
    labels = df.get("canonical_name", df.get("term", pd.Series("", index=df.index))).astype(str).map(map_norm)
    suppress(labels.str.contains(r"\b(?:meta|model|healthy|lead|burden|international|point)\s+(?:city|town|village)\b", regex=True, na=False), "known false city/town/village alias")
    labels = df.get("canonical_name", df.get("term", pd.Series("", index=df.index))).astype(str).map(map_norm)
    geo_types = df.get("geo_type", pd.Series("", index=df.index)).astype(str).str.lower()
    confidence = df.get("confidence", pd.Series("", index=df.index)).astype(str).str.lower()
    matched_field = df.get("matched_field", pd.Series("", index=df.index)).astype(str).str.lower()
    match_method = df.get("match_method", pd.Series("", index=df.index)).astype(str).str.lower()
    population = pd.to_numeric(df.get("population", pd.Series(0, index=df.index)), errors="coerce").fillna(0)
    conservative_place = (confidence == "high") | matched_field.isin({"affiliation", "address", "location"}) | ((match_method == "exact_phrase") & (population >= 100000))
    suppress((geo_types == "place") & (matched_field == "title") & ~conservative_place, "low-confidence title-only place mention suppressed from final geography chart")
    if suppressed_rows:
        suppressed_path = processed_path(f"{slug}_suppressed_geographic_terms_debug.csv")
        pd.DataFrame(suppressed_rows).to_csv(suppressed_path, index=False)
    df.attrs["source_path"] = frame.attrs.get("source_path", "")
    df.attrs["suppressed_count"] = max(0, raw_count - len(df)) + len(suppressed_rows)
    return df


def record_value(record: dict[str, object], *names: str) -> object:
    lowered = {key.lower(): key for key in record if not key.startswith("_")}
    for name in names:
        key = lowered.get(name.lower())
        if key:
            value = record.get(key)
            if isinstance(value, str):
                return value.replace("\x00", "").strip()
            return value
    return ""


def record_name_keys(record: dict[str, object], fields: tuple[str, ...]) -> set[str]:
    return {map_norm(record_value(record, field)) for field in fields if map_norm(record_value(record, field))}


def geography_term_keys(row: pd.Series) -> set[str]:
    return {
        map_norm(value)
        for value in (row.get("canonical_name", ""), row.get("term", ""), row.get("country", ""), row.get("state", ""))
        if map_norm(value)
    }


def point_from_row(row: pd.Series) -> tuple[float, float] | None:
    lat = pd.to_numeric(pd.Series([row.get("latitude")]), errors="coerce").iloc[0]
    lon = pd.to_numeric(pd.Series([row.get("longitude")]), errors="coerce").iloc[0]
    if pd.isna(lat) or pd.isna(lon):
        return None
    return float(lon), float(lat)


def match_records(layer: ShapeLayer | None, fields: tuple[str, ...]) -> dict[str, int]:
    if layer is None:
        return {}
    lookup: dict[str, int] = {}
    for idx, record in enumerate(layer.records):
        for key in record_name_keys(record, fields):
            lookup.setdefault(key, idx)
    return lookup


def country_metadata(record: dict[str, object], index: int) -> dict[str, object]:
    name = str(record_value(record, "boundaryName", "NAME_LONG", "NAME", "ADMIN", "SOVEREIGNT")).strip()
    return {
        "country_key": map_norm(name),
        "country_index": index,
        "country": name,
        "iso_a2": str(record_value(record, "ISO_A2", "WB_A2")).strip(),
        "iso_a3": str(record_value(record, "boundaryISO", "ISO_A3", "ADM0_A3", "WB_A3")).strip().upper(),
        "continent": str(record_value(record, "CONTINENT")).strip(),
        "region_un": str(record_value(record, "REGION_UN")).strip(),
        "subregion": str(record_value(record, "SUBREGION")).strip(),
        "region_wb": str(record_value(record, "REGION_WB")).strip(),
    }


def build_country_metadata(layer: ShapeLayer | None) -> dict[int, dict[str, object]]:
    if layer is None:
        return {}
    return {idx: country_metadata(record, idx) for idx, record in enumerate(layer.records)}


def basemap_country_name(record: dict[str, object]) -> str:
    return str(record_value(record, "boundaryName", "NAME_LONG", "NAME", "ADMIN", "SOVEREIGNT", "shapeName")).strip()


def basemap_country_iso3(record: dict[str, object]) -> str:
    return str(record_value(record, "boundaryISO", "ISO_A3", "ADM0_A3", "shapeISO", "WB_A3")).strip().upper()


def basemap_geometry_status(record: dict[str, object]) -> tuple[bool, bool]:
    parts = record.get("_parts", []) or []
    point = record.get("_point")
    geometry_empty = not bool(parts or point)
    geometry_valid = bool(parts and any(len(ring) >= 3 for ring in parts))
    return geometry_valid, geometry_empty


def basemap_polygon_count(record: dict[str, object]) -> int:
    return sum(1 for ring in record.get("_parts", []) or [] if len(ring) >= 3)


def extents_overlap(left: tuple[float, float, float, float] | None, right: tuple[float, float, float, float] | None) -> bool:
    if not left or not right:
        return False
    lxmin, lxmax, lymin, lymax = left
    rxmin, rxmax, rymin, rymax = right
    return not (lxmax < rxmin or lxmin > rxmax or lymax < rymin or lymin > rymax)


def expected_regions_for_country(country_key: str, iso3: str = "", meta: dict[str, object] | None = None) -> set[str]:
    meta = meta or {}
    normalized = map_norm(country_key)
    iso = str(iso3 or meta.get("iso_a3") or "").upper()
    continent = map_norm(meta.get("continent", ""))
    subregion = map_norm(meta.get("subregion", ""))
    region_wb = map_norm(meta.get("region_wb", ""))
    region_un = map_norm(meta.get("region_un", ""))
    regions: set[str] = set()
    for region_key, iso_set in REGION_ISO3.items():
        if iso and iso in iso_set:
            regions.add(region_key)
    if regions:
        return regions
    if normalized in NORTH_AMERICA_COUNTRIES or iso in {"USA", "CAN", "MEX"}:
        regions.add("north_america")
    if normalized in EAST_ASIA_COUNTRIES or "east asia" in {subregion, region_wb, region_un}:
        regions.add("east_asia")
    if normalized in SOUTH_ASIA_COUNTRIES or "south asia" in {subregion, region_wb, region_un}:
        regions.add("south_asia")
    if normalized in MIDDLE_EAST_COUNTRIES or "middle east" in {subregion, region_wb, region_un}:
        regions.add("middle_east")
    if normalized in NORTH_AFRICA_COUNTRIES or "north africa" in {subregion, region_wb, region_un}:
        regions.add("north_africa")
    if normalized in OCEANIA_COUNTRIES or continent == "oceania" or subregion in {"australia and new zealand", "melanesia", "polynesia", "micronesia"}:
        regions.add("oceania")
    if continent == "europe" or "europe" in subregion or "europe" in region_wb or "europe" in region_un:
        regions.add("europe")
    if subregion in LATIN_AMERICA_SUBREGIONS or "latin america" in region_wb or normalized == "mexico":
        regions.add("latin_america")
    if continent == "africa" and normalized not in SUB_SAHARAN_AFRICA_EXCLUDED:
        regions.add("sub_saharan_africa")
    return regions


def point_source_type(point: dict[str, object]) -> str:
    if point.get("institution") or point.get("institution_name"):
        return "institution"
    feature_type = str(point.get("feature_type") or "").lower()
    if str(point.get("source_context") or "").lower().find("affiliation") >= 0:
        return "affiliation_geography"
    if feature_type in CITY_FEATURE_TYPES:
        return "city"
    return "text_geography"


def suspicious_regional_reason(point: dict[str, object]) -> str:
    label_key = map_norm(point.get("label") or point.get("resolved_name") or point.get("original_term") or "")
    if label_key in SUSPICIOUS_REGIONAL_LABELS:
        return "listed suspicious regional label requires manual review"
    if label_key in COUNTRY_NAME_TERMS:
        return "city label is also a country name; requires strong local context"
    if str(point.get("feature_type") or "").lower() in NON_CITY_FEATURE_TYPES:
        return "non-city feature type plotted or considered for regional point map"
    if str(point.get("exclusion_reason") or "").strip():
        return str(point.get("exclusion_reason"))
    return ""


def validate_required_basemap_countries(
    title: str,
    records: list[dict[str, object]],
    required: dict[str, str],
) -> tuple[bool, list[str]]:
    present_iso3 = {basemap_country_iso3(record) for record in records if basemap_country_iso3(record)}
    present_names = {map_norm(basemap_country_name(record)) for record in records if basemap_country_name(record)}
    errors: list[str] = []
    for iso3, country_name in required.items():
        if iso3 in present_iso3 or map_norm(country_name) in present_names:
            continue
        message = f"ERROR: {title} basemap missing required ADM0 polygon: {country_name} / {iso3}."
        print(message)
        errors.append(message)
    return not errors, errors


def validate_adm0_world_layer(title: str, records: list[dict[str, object]]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for record in records:
        boundary_type = str(record_value(record, "boundaryType", "shapeType", "admin_level")).upper()
        if boundary_type and boundary_type not in {"ADM0", "0"}:
            errors.append(f"ERROR: {title} contains non-ADM0 polygon layer: {basemap_country_name(record)} {boundary_type}.")
    india_records = [record for record in records if basemap_country_iso3(record) == "IND" or map_norm(basemap_country_name(record)) == "india"]
    if len(india_records) != 1:
        errors.append(f"ERROR: {title} expected one India / IND ADM0 polygon record; found {len(india_records)}.")
    for error in errors:
        print(error)
    return not errors, errors


def write_world_city_basemap_debug(slug: str, records: list[dict[str, object]], generated: list[Path]) -> Path:
    rows = []
    present_iso3 = {basemap_country_iso3(record) for record in records if basemap_country_iso3(record)}
    present_names = {map_norm(basemap_country_name(record)) for record in records if basemap_country_name(record)}
    for record in records:
        geometry_valid, geometry_empty = basemap_geometry_status(record)
        rows.append(
            {
                "country_name": basemap_country_name(record),
                "iso3": basemap_country_iso3(record),
                "included_in_basemap": "yes",
                "geometry_valid": "yes" if geometry_valid else "no",
                "geometry_empty": "yes" if geometry_empty else "no",
                "reason_excluded": "" if geometry_valid else "geometry has no drawable polygon rings",
            }
        )
    for iso3, country_name in REQUIRED_WORLD_BASEMAP_COUNTRIES.items():
        if iso3 in present_iso3 or map_norm(country_name) in present_names:
            continue
        rows.append(
            {
                "country_name": country_name,
                "iso3": iso3,
                "included_in_basemap": "no",
                "geometry_valid": "no",
                "geometry_empty": "yes",
                "reason_excluded": "required ADM0 polygon missing from basemap layer",
            }
        )
    path = processed_path(f"{slug}_world_city_basemap_countries_debug.csv")
    pd.DataFrame(
        rows,
        columns=["country_name", "iso3", "included_in_basemap", "geometry_valid", "geometry_empty", "reason_excluded"],
    ).sort_values(["included_in_basemap", "country_name"], ascending=[False, True]).to_csv(path, index=False)
    remember_generated(generated, path)
    return path


ONS_NAME_FIELDS = ("LAD24NM", "LAD23NM", "LAD22NM", "LAD21NM", "LAD20NM", "LAD19NM", "LADNM", "LAD_NAME", "lad_name", "NAME", "Name", "name")
ONS_CODE_FIELDS = ("LAD24CD", "LAD23CD", "LAD22CD", "LAD21CD", "LAD20CD", "LAD19CD", "LADCD", "LAD_CODE", "lad_code", "CODE", "Code", "code")
ONS_DATE_FIELDS = ("BNG_E", "YEAR", "Year", "year", "DATE", "Date", "boundaryYear", "boundary_year")


def layer_field(layer: ShapeLayer, candidates: tuple[str, ...]) -> str:
    lowered = {field.lower(): field for field in layer.fields}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return ""


def uk_boundary_type_from_layer(layer: ShapeLayer, provider: object) -> str:
    selection_type = str(getattr(provider, "boundary_type", "") or "")
    if selection_type:
        return selection_type
    text = " ".join([layer.path.name, *layer.fields]).lower()
    if "lad" in text or "local authority" in text:
        return "Local Authority District"
    if "county" in text:
        return "County"
    if "district" in text:
        return "District"
    return "ONS local administrative boundary"


def uk_boundary_title(country_name: str, provider: object, layer: ShapeLayer) -> str:
    provider_name = str(getattr(provider, "provider", "") or "")
    admin_level = str(getattr(provider, "admin_level", "") or "")
    boundary_type = uk_boundary_type_from_layer(layer, provider)
    if provider_name == "ONS.gov.uk" and "local authority" in boundary_type.lower():
        return f"{country_name} Publication Geography by Local Authority District"
    if provider_name == "ONS.gov.uk":
        return f"{country_name} Publication Geography by {boundary_type}"
    if admin_level == "ADM2":
        return f"{country_name} Publication Geography by geoBoundaries ADM2"
    return f"{country_name} Publication Geography by Region"


def write_uk_boundary_provider_debug(slug: str, provider: object, layer: ShapeLayer | None, generated: list[Path], notes: list[str]) -> Path:
    name_field = layer_field(layer, ONS_NAME_FIELDS) if layer else ""
    code_field = layer_field(layer, ONS_CODE_FIELDS) if layer else ""
    date_field = layer_field(layer, ONS_DATE_FIELDS) if layer else ""
    row = {
        "provider_selected": str(getattr(provider, "provider", "skipped") or "skipped"),
        "boundary_file_used": str(layer.path if layer else getattr(provider, "path", "") or ""),
        "boundary_type_detected": uk_boundary_type_from_layer(layer, provider) if layer else str(getattr(provider, "boundary_type", "") or ""),
        "boundary_date_or_version": str(record_value(layer.records[0], date_field) if layer and date_field and layer.records else ""),
        "polygons_loaded": len(layer.records) if layer else 0,
        "name_field_used": name_field,
        "code_field_used": code_field,
        "source_url": "",
        "fallback_used": "yes" if bool(getattr(provider, "fallback_used", False)) else "no",
        "notes": " | ".join(notes),
    }
    path = processed_path(f"{slug}_uk_boundary_provider_debug.csv")
    pd.DataFrame([row]).to_csv(path, index=False)
    remember_generated(generated, path)
    return path


def join_points_to_polygons(
    points_to_join: list[dict[str, object]],
    records: list[dict[str, object]],
    name_field: str,
    code_field: str,
) -> tuple[dict[int, int], list[dict[str, object]], list[dict[str, object]]]:
    counts: dict[int, int] = {}
    joined: list[dict[str, object]] = []
    unmatched: list[dict[str, object]] = []
    for point in points_to_join:
        match_index = next((idx for idx, record in enumerate(records) if point_in_record(point, record)), None)
        row = {
            "point_label": point.get("label", ""),
            "longitude": point.get("lon", ""),
            "latitude": point.get("lat", ""),
            "publication_geography_count": int(point.get("count") or 1),
            "matched_area_name": "",
            "matched_area_code": "",
            "join_method": "point_in_polygon",
            "matched_yes_no": "no",
            "unmatched_reason": "",
        }
        if match_index is None:
            row["unmatched_reason"] = "point not inside selected UK boundary polygons"
            unmatched.append(row)
            continue
        record = records[match_index]
        counts[match_index] = counts.get(match_index, 0) + int(point.get("count") or 1)
        row["matched_area_name"] = record_value(record, name_field) if name_field else basemap_country_name(record)
        row["matched_area_code"] = record_value(record, code_field) if code_field else ""
        row["matched_yes_no"] = "yes"
        joined.append(row)
    return counts, joined, unmatched


def write_uk_ons_lad_join_debug(slug: str, joined: list[dict[str, object]], unmatched: list[dict[str, object]], generated: list[Path]) -> Path:
    path = processed_path(f"{slug}_uk_ons_lad_join_debug.csv")
    pd.DataFrame(
        joined + unmatched,
        columns=[
            "point_label", "longitude", "latitude", "publication_geography_count", "matched_area_name",
            "matched_area_code", "join_method", "matched_yes_no", "unmatched_reason",
        ],
    ).to_csv(path, index=False)
    remember_generated(generated, path)
    return path


def write_subnational_join_debug(
    slug: str,
    country_key: str,
    rows: list[dict[str, object]],
    generated: list[Path],
) -> Path:
    output_key = re.sub(r"[^a-z0-9]+", "_", country_key).strip("_") or "country"
    path = processed_path(f"{slug}_{output_key}_subnational_join_debug.csv")
    if country_key == "austria":
        path = processed_path(f"{slug}_austria_subnational_join_debug.csv")
    pd.DataFrame(
        rows,
        columns=[
            "source_city", "source_institution", "latitude", "longitude", "publication_geography_count",
            "joined_admin_name", "joined_admin_code", "joined_boundary_level", "join_method",
            "join_confidence", "included_in_choropleth", "exclusion_reason",
        ],
    ).to_csv(path, index=False)
    remember_generated(generated, path)
    return path


def spatial_subnational_counts(
    source_points: list[dict[str, object]],
    records: list[dict[str, object]],
    name_field: str,
    code_field: str,
    boundary_level: str,
) -> tuple[dict[int, int], list[dict[str, object]]]:
    counts: dict[int, int] = {}
    rows: list[dict[str, object]] = []
    for point in source_points:
        match_index = next((idx for idx, record in enumerate(records) if point_in_record(point, record)), None)
        row = {
            "source_city": point.get("city", point.get("label", "")),
            "source_institution": point.get("institution", point.get("label", "")) if point.get("institution") else "",
            "latitude": point.get("lat", ""),
            "longitude": point.get("lon", ""),
            "publication_geography_count": int(point.get("count") or 1),
            "joined_admin_name": "",
            "joined_admin_code": "",
            "joined_boundary_level": boundary_level,
            "join_method": "spatial" if match_index is not None else "unresolved",
            "join_confidence": "high" if match_index is not None else "low",
            "included_in_choropleth": "yes" if match_index is not None else "no",
            "exclusion_reason": "" if match_index is not None else "point not inside selected boundary polygons",
        }
        if match_index is not None:
            record = records[match_index]
            counts[match_index] = counts.get(match_index, 0) + int(point.get("count") or 1)
            row["joined_admin_name"] = record_value(record, name_field) if name_field else basemap_country_name(record)
            row["joined_admin_code"] = record_value(record, code_field) if code_field else ""
        rows.append(row)
    return counts, rows


def record_country_key(record: dict[str, object]) -> str:
    return map_norm(record_value(record, "boundaryName", "ADMIN", "ADM0NAME", "adm0_name", "geonunit", "SOVEREIGNT", "COUNTRY", "shapeGroup"))


def admin1_record_key(record: dict[str, object]) -> str:
    return map_norm(record_value(record, "NAME", "name", "name_en", "region", "postal", "iso_3166_2", "STUSPS"))


def us_admin1_records(layer: ShapeLayer | None) -> list[dict[str, object]]:
    if layer is None:
        return []
    return [
        record for record in layer.records
        if record_country_key(record) == "united states"
        or str(record_value(record, "iso_3166_2")).upper().startswith("US-")
        or str(record_value(record, "STUSPS")).strip()
    ]


def lower48_us_admin1_records(layer: ShapeLayer | None) -> list[dict[str, object]]:
    excluded = {"AK", "HI", "PR", "GU", "VI", "MP", "AS"}
    return [
        record for record in us_admin1_records(layer)
        if str(record_value(record, "STUSPS", "postal")).strip().upper() not in excluded
        and record_intersects_extent(record, (-126, -66, 24, 50))
    ]


def point_in_extent(point: dict[str, object], extent: tuple[float, float, float, float]) -> bool:
    lon = float(point["lon"])
    lat = float(point["lat"])
    xmin, xmax, ymin, ymax = extent
    return xmin <= lon <= xmax and ymin <= lat <= ymax


def point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        if ((y1 > lat) != (y2 > lat)) and (lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1):
            inside = not inside
        x1, y1 = x2, y2
    return inside


def point_in_record(point: dict[str, object], record: dict[str, object]) -> bool:
    lon = float(point["lon"])
    lat = float(point["lat"])
    for ring in record.get("_parts", []) or []:
        if point_in_ring(lon, lat, ring):
            return True
    return False


def layer_extent(records: list[dict[str, object]]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for record in records:
        point = record.get("_point")
        if point:
            x, y = point
            xs.append(float(x))
            ys.append(float(y))
        for ring in record.get("_parts", []) or []:
            for x, y in ring:
                xs.append(float(x))
                ys.append(float(y))
    if not xs or not ys:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def record_intersects_extent(record: dict[str, object], extent: tuple[float, float, float, float]) -> bool:
    record_extent = layer_extent([record])
    if not record_extent:
        return False
    xmin, xmax, ymin, ymax = extent
    rxmin, rxmax, rymin, rymax = record_extent
    return not (rxmax < xmin or rxmin > xmax or rymax < ymin or rymin > ymax)


def state_fips(record: dict[str, object]) -> str:
    return str(record_value(record, "STATEFP", "STATEFP20", "STATEFP10")).strip().zfill(2)


def country_map_usefulness(country_key: str, total_frequency: int, subnational_count: int) -> tuple[bool, str]:
    if country_key in PRIORITY_COUNTRY_MAPS:
        return True, "priority country"
    if total_frequency >= COUNTRY_MAP_MIN_FREQUENCY:
        return True, f"frequency >= {COUNTRY_MAP_MIN_FREQUENCY}"
    if subnational_count >= COUNTRY_MAP_MIN_SUBNATIONAL_TERMS:
        return True, f"subnational/place detail >= {COUNTRY_MAP_MIN_SUBNATIONAL_TERMS}"
    if INCLUDE_POINT_MAPS and subnational_count > 0:
        return True, "detailed country point maps explicitly enabled"
    return False, "below country-map usefulness threshold; shown on world map only"


def country_publication_title(country_name: str) -> str:
    key = map_norm(country_name)
    suffixes = {
        "united states": "by State",
        "canada": "by Province/Territory",
        "brazil": "by State",
        "australia": "by State/Territory",
        "united kingdom": "by Region",
    }
    suffix = suffixes.get(key, "")
    return f"{country_name} Publication Geography {suffix}".strip()


def summarize_detected_countries(
    slug: str,
    country_meta: dict[int, dict[str, object]],
    country_counts: Counter[int],
    country_level_counts: Counter[int],
    admin1_counts_by_country: Counter[int],
    place_counts_by_country: Counter[int],
    record_counts_by_country: Counter[int],
) -> pd.DataFrame:
    rows = []
    for country_index, count in country_counts.items():
        meta = country_meta.get(country_index, {"country": f"country_{country_index}", "country_key": f"country_{country_index}"})
        subnational_count = int(admin1_counts_by_country.get(country_index, 0) + place_counts_by_country.get(country_index, 0))
        country_key = str(meta.get("country_key", ""))
        total_frequency = int(count)
        map_needed, usefulness_reason = country_map_usefulness(country_key, total_frequency, subnational_count)
        reasons = [usefulness_reason]
        if subnational_count > 0 and "subnational/place" not in usefulness_reason:
            reasons.append("subnational/place detail detected")
        if country_key == "united kingdom" and subnational_count == 0:
            reasons.append("UK country map requires admin/city detail")
        rows.append(
            {
                "country": meta.get("country", ""),
                "country_key": meta.get("country_key", ""),
                "country_index": country_index,
                "iso_a2": meta.get("iso_a2", ""),
                "iso_a3": meta.get("iso_a3", ""),
                "continent": meta.get("continent", ""),
                "region_un": meta.get("region_un", ""),
                "subregion": meta.get("subregion", ""),
                "region_wb": meta.get("region_wb", ""),
                "country_level_count": int(country_level_counts.get(country_index, 0)),
                "admin1_count": int(admin1_counts_by_country.get(country_index, 0)),
                "place_count": int(place_counts_by_country.get(country_index, 0)),
                "total_geography_frequency": total_frequency,
                "total_records": int(record_counts_by_country.get(country_index, count)),
                "map_needed_yes_no": "yes" if map_needed else "no",
                "reason": "; ".join(reasons) if reasons else "country-level only; no subnational/place detail to map",
            }
        )
    columns = [
        "country", "country_key", "country_index", "iso_a2", "iso_a3", "continent", "region_un", "subregion",
        "region_wb", "country_level_count", "admin1_count", "place_count", "total_geography_frequency",
        "total_records", "map_needed_yes_no", "reason",
    ]
    summary = pd.DataFrame(rows, columns=columns)
    if not summary.empty:
        summary = summary.sort_values(["total_geography_frequency", "country"], ascending=[False, True])
    path = processed_path(f"{slug}_geography_country_summary.csv")
    summary.to_csv(path, index=False)
    return summary


def country_indices_with_generated_detail(map_metadata: list[dict[str, object]], country_meta: dict[int, dict[str, object]]) -> set[int]:
    generated: set[int] = set()
    for item in map_metadata:
        name = str(item.get("name") or "")
        if not name.startswith("country_"):
            continue
        for idx, meta in country_meta.items():
            country_key = str(meta.get("country_key") or "")
            output_key = re.sub(r"[^a-z0-9]+", "_", country_key).strip("_")
            if output_key and f"country_{output_key}" in name:
                generated.add(idx)
    return generated


def institution_counts_by_country_index(institution_points: list[dict[str, object]], country_meta: dict[int, dict[str, object]]) -> Counter[int]:
    counts: Counter[int] = Counter()
    for point in aggregate_points(institution_points):
        point_country = map_norm(point.get("country", ""))
        if not point_country:
            continue
        for idx, meta in country_meta.items():
            if point_country in {map_norm(meta.get("country", "")), map_norm(meta.get("iso_a2", "")), map_norm(meta.get("iso_a3", ""))}:
                counts[idx] += int(point.get("count", 1) or 1)
                break
    return counts


def country_institution_side_panel_rows(
    country_name: str,
    country_key: str,
    country_iso3: str,
    institution_points: list[dict[str, object]],
    total_publication_count: int,
    detected_cities_count: int,
) -> tuple[list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    grouped: dict[str, dict[str, object]] = {}
    for point in institution_points:
        point_country = map_norm(point.get("country", ""))
        if point_country not in {country_key, map_norm(country_name), map_norm(country_iso3)}:
            continue
        name = str(point.get("institution") or point.get("label") or "").strip()
        normalized = normalize_institution_name(name)
        if not normalized:
            continue
        record_ids = {item.strip() for item in str(point.get("record_ids") or "").split(";") if item.strip()}
        count = len(record_ids) if record_ids else int(point.get("count") or 1)
        entry = grouped.setdefault(
            normalized,
            {
                "country": country_name,
                "institution_name": name,
                "normalized_institution": normalized,
                "city": str(point.get("city") or "").strip(),
                "publication_count": 0,
                "record_ids": set(),
                "enrichment_source_summary": str(point.get("enrichment_source_summary") or ""),
            },
        )
        if record_ids:
            existing = entry.setdefault("record_ids", set())
            if isinstance(existing, set):
                existing.update(record_ids)
                entry["publication_count"] = len(existing)
        else:
            entry["publication_count"] = int(entry.get("publication_count") or 0) + count
        if not entry.get("city") and point.get("city"):
            entry["city"] = str(point.get("city"))
    ranked = sorted(grouped.values(), key=lambda item: (-int(item.get("publication_count") or 0), str(item.get("institution_name") or "")))
    rows: list[dict[str, object]] = []
    debug_rows: list[dict[str, object]] = []
    for rank, row in enumerate(ranked, start=1):
        output = {
            "country": country_name,
            "map_filename": "",
            "institution_name": row.get("institution_name", ""),
            "normalized_institution": row.get("normalized_institution", ""),
            "city": row.get("city", ""),
            "publication_count": int(row.get("publication_count") or 0),
            "rank": rank,
            "included_in_side_panel": "yes" if rank <= 10 else "no",
            "exclusion_reason": "" if rank <= 10 else "outside top 10",
            "enrichment_source_summary": row.get("enrichment_source_summary", ""),
        }
        debug_rows.append(output.copy())
        if rank <= 10:
            rows.append(output)
    meta = {
        "total_country_publication_count": total_publication_count,
        "detected_cities_count": detected_cities_count,
        "institutions_listed_count": len(rows),
        "more_institutions": max(0, len(ranked) - 10),
    }
    return rows, meta, debug_rows


def institution_side_panel_rows(
    scope_name: str,
    institution_points: list[dict[str, object]],
    total_publication_count: int | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for point in institution_points:
        name = str(point.get("institution") or point.get("institution_name") or point.get("label") or "").strip()
        normalized = normalize_institution_name(name)
        if not normalized:
            continue
        record_ids = {item.strip() for item in str(point.get("record_ids") or "").split(";") if item.strip()}
        count = len(record_ids) if record_ids else int(point.get("count") or 1)
        entry = grouped.setdefault(
            normalized,
            {
                "institution_name": name,
                "normalized_institution": normalized,
                "city": str(point.get("city") or "").strip(),
                "publication_count": 0,
                "record_ids": set(),
            },
        )
        if record_ids:
            existing = entry.setdefault("record_ids", set())
            if isinstance(existing, set):
                existing.update(record_ids)
                entry["publication_count"] = len(existing)
        else:
            entry["publication_count"] = int(entry.get("publication_count") or 0) + count
        if not entry.get("city") and point.get("city"):
            entry["city"] = str(point.get("city"))
    ranked = sorted(grouped.values(), key=lambda item: (-int(item.get("publication_count") or 0), str(item.get("institution_name") or "")))
    rows = [
        {
            "institution_name": row.get("institution_name", ""),
            "normalized_institution": row.get("normalized_institution", ""),
            "city": row.get("city", ""),
            "publication_count": int(row.get("publication_count") or 0),
            "rank": rank,
        }
        for rank, row in enumerate(ranked[:10], start=1)
    ]
    city_count = len({str(point.get("city") or "").strip().lower() for point in institution_points if str(point.get("city") or "").strip()})
    meta = {
        "scope": scope_name,
        "total_country_publication_count": total_publication_count if total_publication_count is not None else sum(int(row.get("publication_count") or 0) for row in ranked),
        "detected_cities_count": city_count,
        "institutions_listed_count": len(rows),
        "more_institutions": max(0, len(ranked) - 10),
    }
    return rows, meta


def final_city_exclusion_reason(point: dict[str, object], institution_count: int = 0) -> str:
    label_key = map_norm(point.get("city") or point.get("label") or point.get("resolved_name") or "")
    raw_evidence = str(point.get("raw_affiliation_evidence") or "").lower() == "yes" or bool(str(point.get("source_context") or "").strip())
    source_type = str(point.get("source_type") or point_source_type(point)).lower()
    if source_type == "institution" and not str(point.get("city") or "").strip():
        return "no city"
    if source_type == "institution" and not str(point.get("country") or "").strip():
        return "no country"
    if institution_count <= 0 and source_type != "institution":
        return "institution_count = 0"
    if source_type != "institution" and not raw_evidence:
        return "no raw affiliation evidence"
    if label_key in SUSPICIOUS_REGIONAL_LABELS or label_key in {"miami", "much", "university", "side", "lebanon", "college", "hospital", "center", "centre", "study"}:
        return "suspicious/common place label without direct institution support" if not raw_evidence else ""
    if label_key in COUNTRY_NAME_TERMS or label_key in US_STATE_TERMS:
        return "country/state name used as city label"
    if len(label_key) < 4 and not raw_evidence:
        return "short place label lacks institution/address context"
    return ""


def write_final_city_reliability_outputs(slug: str, institution_points: list[dict[str, object]], text_points: list[dict[str, object]], generated: list[Path]) -> None:
    suppression_rows: list[dict[str, object]] = []
    qa_rows: list[dict[str, object]] = []
    city_institution_names: dict[tuple[str, str], set[str]] = defaultdict(set)
    for point in institution_points:
        city_key = map_norm(point.get("city") or point.get("label") or "")
        country_key = map_norm(point.get("country") or "")
        institution = normalize_institution_name(point.get("institution") or point.get("institution_name") or point.get("label") or "")
        if city_key and institution:
            city_institution_names[(city_key, country_key)].add(institution)
    for point in [*institution_points, *text_points]:
        source_type = str(point.get("source_type") or point_source_type(point))
        city_key = map_norm(point.get("city") or point.get("label") or "")
        country_key = map_norm(point.get("country") or "")
        institution_count = len(city_institution_names.get((city_key, country_key), set())) if source_type == "institution" else 0
        exclusion = final_city_exclusion_reason(point, institution_count)
        included = source_type == "institution" and not exclusion
        raw_evidence = "yes" if str(point.get("raw_affiliation_evidence") or "").lower() == "yes" or str(point.get("source_context") or "").strip() else "no"
        row = {
            "candidate_city": point.get("label", point.get("city", "")),
            "resolved_city": point.get("city", point.get("label", "")),
            "country": point.get("country", ""),
            "source_type": source_type,
            "institution_count": institution_count,
            "raw_affiliation_evidence": raw_evidence,
            "location_method": point.get("location_method", ""),
            "included_in_final_maps": "yes" if included else "no",
            "exclusion_reason": exclusion,
        }
        suppression_rows.append(row)
        qa_status = "pass" if included and raw_evidence == "yes" else "warning" if included else "fail"
        qa_rows.append(
            {
                "map_file": "institution final maps" if source_type == "institution" else "text debug maps only",
                "city": row["resolved_city"],
                "country": row["country"],
                "institution_count": institution_count,
                "publication_count": point.get("count", ""),
                "source_type": source_type,
                "raw_affiliation_evidence_count": 1 if raw_evidence == "yes" else 0,
                "location_method": point.get("location_method", ""),
                "confidence": point.get("confidence_score", ""),
                "qa_status": qa_status,
                "warning_reason": exclusion or ("geocoded/trusted institution location without raw affiliation string" if qa_status == "warning" else ""),
            }
        )
    suppression_path = processed_path(f"{slug}_final_city_location_suppression_debug.csv")
    pd.DataFrame(
        suppression_rows,
        columns=[
            "candidate_city", "resolved_city", "country", "source_type", "institution_count",
            "raw_affiliation_evidence", "location_method", "included_in_final_maps", "exclusion_reason",
        ],
    ).to_csv(suppression_path, index=False)
    remember_generated(generated, suppression_path)
    qa_path = processed_path(f"{slug}_final_map_city_reliability_qa.csv")
    pd.DataFrame(
        qa_rows,
        columns=[
            "map_file", "city", "country", "institution_count", "publication_count", "source_type",
            "raw_affiliation_evidence_count", "location_method", "confidence", "qa_status", "warning_reason",
        ],
    ).to_csv(qa_path, index=False)
    remember_generated(generated, qa_path)


def log_final_map_source_check(slug: str, final_institution_points: list[dict[str, object]]) -> dict[str, object]:
    path = OUTPUTS_DIR / f"{slug}_institution_locations.csv"
    exists = path.exists()
    row_count = 0
    rows_with_lat_lon = 0
    rows_with_city = 0
    rows_with_country = 0
    rows_with_publication_count = 0
    schema_mismatch = False
    reasons: list[str] = []
    if exists:
        frame = safe_read_csv(path)
        row_count = len(frame)
        columns = set(frame.columns)
        lat_col = next((col for col in ("latitude", "lat") if col in columns), "")
        lon_col = next((col for col in ("longitude", "lon", "lng") if col in columns), "")
        city_cols = [col for col in ("institution_city", "affiliation_city", "city") if col in columns]
        country_cols = [
            col for col in (
                "institution_country_iso3", "institution_country",
                "affiliation_country_iso3", "affiliation_country", "country",
            )
            if col in columns
        ]
        publication_cols = [col for col in ("publication_count", "record_count") if col in columns]
        required_groups = {
            "lat/lon": bool(lat_col and lon_col),
            "city": bool(city_cols),
            "country": bool(country_cols),
            "publication_count": bool(publication_cols),
        }
        schema_mismatch = not all(required_groups.values())
        if schema_mismatch:
            reasons.extend(name for name, ok in required_groups.items() if not ok)
        if lat_col and lon_col:
            lat = pd.to_numeric(frame[lat_col], errors="coerce")
            lon = pd.to_numeric(frame[lon_col], errors="coerce")
            rows_with_lat_lon = int((lat.notna() & lon.notna()).sum())
        if city_cols:
            rows_with_city = int(frame[city_cols].fillna("").astype(str).apply(lambda row: any(value.strip() for value in row), axis=1).sum())
        if country_cols:
            rows_with_country = int(frame[country_cols].fillna("").astype(str).apply(lambda row: any(value.strip() for value in row), axis=1).sum())
        if publication_cols:
            pub_values = pd.concat([pd.to_numeric(frame[col], errors="coerce") for col in publication_cols], axis=1)
            rows_with_publication_count = int(pub_values.notna().any(axis=1).sum())
        if row_count == 0:
            reasons.append("all rows excluded")
        if rows_with_lat_lon == 0:
            reasons.append("no lat/lon")
        if rows_with_city == 0:
            reasons.append("no city")
        if rows_with_country == 0:
            reasons.append("no country")
        if rows_with_publication_count == 0:
            reasons.append("missing publication_count")
        if schema_mismatch:
            reasons.append("schema mismatch")
        if row_count and not final_institution_points:
            reasons.append("all rows excluded")
    final_source = "institution_locations" if exists and final_institution_points else "text_debug_only"
    print("FINAL MAP SOURCE CHECK")
    print(f"  institution_locations_path: {path}")
    print(f"  exists: {'yes' if exists else 'no'}")
    print(f"  row_count: {row_count}")
    print(f"  included_in_final_maps count: {len(final_institution_points)}")
    print(f"  rows with lat/lon: {rows_with_lat_lon}")
    print(f"  rows with institution_city: {rows_with_city}")
    print(f"  rows with country: {rows_with_country}")
    print(f"  rows with publication_count: {rows_with_publication_count}")
    print(f"  final maps will use: {final_source}")
    if exists and not final_institution_points:
        print(f"  unusable institution_locations reason: {'; '.join(dict.fromkeys(reasons)) or 'all rows excluded'}")
    return {
        "path": path,
        "exists": exists,
        "row_count": row_count,
        "rows_with_lat_lon": rows_with_lat_lon,
        "rows_with_city": rows_with_city,
        "rows_with_country": rows_with_country,
        "rows_with_publication_count": rows_with_publication_count,
        "included_in_final_maps": len(final_institution_points),
        "final_source": final_source,
        "reason": "; ".join(dict.fromkeys(reasons)),
    }


def write_country_coverage_recommendations(
    slug: str,
    summary: pd.DataFrame,
    country_meta: dict[int, dict[str, object]],
    map_metadata: list[dict[str, object]],
    institution_country_counts: Counter[int],
    generated: list[Path],
) -> Path:
    generated_detail = country_indices_with_generated_detail(map_metadata, country_meta)
    rows: list[dict[str, object]] = []
    priority_generated_counts = [
        int(row.total_geography_frequency)
        for row in summary.itertuples(index=False)
        if str(getattr(row, "country_key", "")) in PRIORITY_COUNTRY_MAPS and int(getattr(row, "country_index", -1)) in generated_detail
    ] if not summary.empty else []
    priority_floor = min(priority_generated_counts) if priority_generated_counts else 0
    high_without_detail: list[tuple[str, int]] = []
    for row in summary.itertuples(index=False):
        country_index = int(getattr(row, "country_index"))
        meta = country_meta.get(country_index, {})
        country = str(getattr(row, "country", "") or meta.get("country", ""))
        country_key = str(getattr(row, "country_key", "") or meta.get("country_key", ""))
        iso3 = str(getattr(row, "iso_a3", "") or meta.get("iso_a3", "")).upper()
        publication_count = int(getattr(row, "total_geography_frequency", 0) or 0)
        city_count = int(getattr(row, "place_count", 0) or 0)
        institution_count = int(institution_country_counts.get(country_index, 0))
        has_adm1 = bool(country_admin_boundary_file(iso3)) if iso3 else False
        has_adm2 = bool(geoboundaries_boundary_file(iso3, "ADM2")) if iso3 else False
        if iso3 == "USA":
            has_adm1 = has_adm1 or bool(census_boundary_shapefile("states"))
            has_adm2 = has_adm2 or bool(census_boundary_shapefile("counties"))
        detail_generated = country_index in generated_detail
        if detail_generated:
            reason_not_generated = ""
            recommendation = "Detailed map generated"
        elif publication_count < 3 and city_count < 2 and institution_count < 2 and not (priority_floor and publication_count > priority_floor):
            reason_not_generated = "below recommendation threshold"
            recommendation = "Low count; world map only is sufficient"
        elif has_adm1:
            reason_not_generated = "not currently selected for detailed country output"
            recommendation = "Add to priority country maps" if country_key not in PRIORITY_COUNTRY_MAPS else "Generate detailed country map"
            high_without_detail.append((country, publication_count))
        elif city_count or institution_count:
            reason_not_generated = "ADM1 boundary data missing"
            recommendation = "Boundary data missing; city map available"
            high_without_detail.append((country, publication_count))
        else:
            reason_not_generated = "ADM1 boundary data missing and insufficient point data"
            recommendation = "Generate city map only" if city_count >= 2 or institution_count >= 2 else "Low count; world map only is sufficient"
            if recommendation != "Low count; world map only is sufficient":
                high_without_detail.append((country, publication_count))
        rows.append(
            {
                "country": country,
                "iso3": iso3,
                "publication_geography_count": publication_count,
                "city_point_count": city_count,
                "institution_point_count": institution_count,
                "has_adm1_boundary_data": "yes" if has_adm1 else "no",
                "has_adm2_boundary_data": "yes" if has_adm2 else "no",
                "detailed_map_generated": "yes" if detail_generated else "no",
                "reason_not_generated": reason_not_generated,
                "recommendation": recommendation,
            }
        )
    path = processed_path(f"{slug}_map_country_coverage_recommendations.csv")
    pd.DataFrame(rows).sort_values(["publication_geography_count", "country"], ascending=[False, True]).to_csv(path, index=False)
    generated.append(path)
    if high_without_detail:
        summary_text = ", ".join(f"{country} ({count})" for country, count in sorted(high_without_detail, key=lambda item: (-item[1], item[0]))[:12])
        print(f"Countries with high detected publication geography but no detailed map: {summary_text}.")
    else:
        print("Countries with high detected publication geography but no detailed map: none.")
    return path


def region_match(summary: pd.DataFrame, region: str) -> bool:
    if summary.empty:
        return False
    iso3_values = {str(value).upper() for value in summary.get("iso_a3", pd.Series(dtype=str)).dropna().astype(str) if str(value).strip()}
    if region in REGION_ISO3 and iso3_values & REGION_ISO3[region]:
        return True
    countries = set(summary["country_key"].astype(str))
    continents = set(summary["continent"].astype(str).map(map_norm))
    subregions = set(summary["subregion"].astype(str).map(map_norm))
    region_wb = set(summary["region_wb"].astype(str).map(map_norm))
    if region == "europe":
        return "europe" in continents or any("europe" in item for item in subregions | region_wb)
    if region == "east_asia":
        return bool(countries & EAST_ASIA_COUNTRIES) or any("east asia" in item for item in subregions | region_wb)
    if region == "latin_america":
        return bool(subregions & LATIN_AMERICA_SUBREGIONS) or any("latin america" in item for item in region_wb) or "mexico" in countries
    if region == "africa":
        return "africa" in continents
    if region == "middle_east":
        return bool(countries & MIDDLE_EAST_COUNTRIES) or any("middle east" in item for item in subregions | region_wb)
    if region == "north_america":
        north_america = countries & NORTH_AMERICA_COUNTRIES
        return len(north_america) > 1 or bool(north_america & {"canada", "mexico"})
    if region == "south_asia":
        return bool(countries & SOUTH_ASIA_COUNTRIES) or any("south asia" in item for item in subregions | region_wb)
    if region == "oceania":
        return bool(countries & OCEANIA_COUNTRIES) or any(item in {"oceania", "australia and new zealand", "melanesia", "polynesia", "micronesia"} for item in subregions | region_wb)
    if region == "north_africa":
        return bool(countries & NORTH_AFRICA_COUNTRIES) or any("north africa" in item for item in subregions | region_wb)
    if region == "sub_saharan_africa":
        return "africa" in continents and bool(countries - SUB_SAHARAN_AFRICA_EXCLUDED)
    return False


def build_geography_map_plan(summary: pd.DataFrame, geo_df: pd.DataFrame) -> dict[str, object]:
    countries = set(summary["country_key"].astype(str)) if not summary.empty else set()
    term_keys = set()
    if not geo_df.empty:
        for _idx, row in geo_df.iterrows():
            term_keys.update(geography_term_keys(row))
    regions = {
        "europe": region_match(summary, "europe"),
        "east_asia": region_match(summary, "east_asia"),
        "latin_america": region_match(summary, "latin_america"),
        "africa": region_match(summary, "africa"),
        "middle_east": region_match(summary, "middle_east"),
        "north_america": region_match(summary, "north_america"),
        "south_asia": region_match(summary, "south_asia"),
        "oceania": region_match(summary, "oceania"),
        "sub_saharan_africa": region_match(summary, "sub_saharan_africa"),
        "north_africa": region_match(summary, "north_africa"),
    }
    us_needed = "united states" in countries or bool(term_keys & {"texas", "tx", "california", "new york"})
    texas_needed = "texas" in term_keys
    country_maps = []
    country_maps_skipped = []
    if not summary.empty:
        for row in summary.to_dict("records"):
            if row.get("map_needed_yes_no") == "yes":
                country_maps.append(row)
            else:
                country_maps_skipped.append(row)
    return {
        "world": True,
        "world_regions": bool(ENABLE_WORLD_REGIONS_OVERVIEW and not summary.empty),
        "regions": regions,
        "us": us_needed,
        "texas": texas_needed,
        "country_maps": country_maps,
        "country_maps_skipped": country_maps_skipped,
    }


def write_geography_map_plan(slug: str, plan: dict[str, object], summary: pd.DataFrame, generated: list[Path]) -> Path:
    region_plan = plan.get("regions", {})
    country_maps = plan.get("country_maps", [])
    country_maps_skipped = plan.get("country_maps_skipped", [])
    lines = [
        "Geography map plan",
        "",
        "Core maps:",
        "- world: planned; always generated when a country layer is available",
        f"- world_regions: {'planned' if plan.get('world_regions') else 'skipped'}; {'detected countries available' if plan.get('world_regions') else 'no detected countries'}",
        "",
        "Regional maps:",
    ]
    if isinstance(region_plan, dict):
        for name, planned in region_plan.items():
            lines.append(f"- {name}: {'planned' if planned else 'skipped'}; {'detected terms in region' if planned else 'no detected countries in region'}")
    lines.extend(["", "U.S. subnational maps:"])
    lines.append(f"- us: {'planned' if plan.get('us') else 'skipped'}; {'U.S. country/admin/place terms detected' if plan.get('us') else 'no U.S. geography detected'}")
    lines.append(f"- texas: {'planned' if plan.get('texas') else 'skipped'}; {'Texas terms detected' if plan.get('texas') else 'no Texas geography detected'}")
    lines.extend(["", "Country-specific maps:"])
    if country_maps:
        for row in country_maps:
            lines.append(f"- {row.get('country')}: planned; {row.get('reason')}")
    else:
        lines.append("- none: no country crossed the country-map thresholds")
    if country_maps_skipped:
        lines.extend(["", "Country-specific maps skipped:"])
        for row in country_maps_skipped:
            lines.append(f"- {row.get('country')}: skipped; {row.get('reason')}")
    lines.extend(["", f"Countries detected: {len(summary)}"])
    path = processed_path(f"{slug}_geography_map_plan.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    remember_generated(generated, path)
    return path


def draw_shape_layer(ax: plt.Axes, records: list[dict[str, object]], counts_by_index: dict[int, int]) -> None:
    from matplotlib.collections import PatchCollection
    from matplotlib.patches import Polygon

    patches = []
    colors = []
    max_count = max(counts_by_index.values()) if counts_by_index else 1
    cmap = plt.get_cmap(MAP_COLORMAP)
    fallback_edge_indexes: set[int] = set()
    for idx, record in enumerate(records):
        count = counts_by_index.get(idx, 0)
        if MAP_USE_LOG_SCALE and count:
            denom = math.log1p(max_count)
            scale_value = math.log1p(count) / denom if denom else 0
        else:
            scale_value = count / max_count if max_count else 0
        color = MAP_ZERO_COLOR if count <= 0 else cmap(0.18 + 0.78 * scale_value)
        for ring in record.get("_parts", []) or []:
            if len(ring) >= 3:
                patches.append(Polygon(ring, closed=True))
                colors.append(color)
                if record.get("fallback_source"):
                    fallback_edge_indexes.add(len(patches) - 1)
    if patches:
        linewidths = [0 if idx in fallback_edge_indexes else 0.26 for idx in range(len(patches))]
        ax.add_collection(PatchCollection(patches, facecolor=colors, edgecolor="#8a94a6", linewidths=linewidths, alpha=0.97, zorder=1))


def draw_basemap_layer(ax: plt.Axes, records: list[dict[str, object]]) -> int:
    from matplotlib.collections import PatchCollection
    from matplotlib.patches import Polygon

    patches = []
    fallback_edge_indexes: set[int] = set()
    for record in records:
        for ring in record.get("_parts", []) or []:
            if len(ring) >= 3:
                patches.append(Polygon(ring, closed=True))
                if record.get("fallback_source"):
                    fallback_edge_indexes.add(len(patches) - 1)
    if not patches:
        return 0
    ax.add_collection(
        PatchCollection(
            patches,
            facecolor="#f7f8fb",
            edgecolor="#9ca8bb",
            linewidths=[0 if idx in fallback_edge_indexes else 0.32 for idx in range(len(patches))],
            alpha=0.98,
            zorder=1,
        )
    )
    return len(patches)


def aggregate_points(points: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, float, float], dict[str, object]] = {}
    for point in points:
        lon = round(float(point["lon"]), 4)
        lat = round(float(point["lat"]), 4)
        label = str(point.get("label") or f"{lat:g}, {lon:g}")
        key = (map_norm(label), lon, lat)
        if key not in grouped:
            grouped[key] = {**point, "lon": lon, "lat": lat, "count": 0, "label": label}
        grouped[key]["count"] = int(grouped[key]["count"]) + int(point.get("count") or 1)
        institutions = point.get("institutions")
        if isinstance(institutions, Counter):
            grouped[key].setdefault("institutions", Counter())
            grouped[key]["institutions"].update(institutions)
        elif isinstance(institutions, dict):
            grouped[key].setdefault("institutions", Counter())
            grouped[key]["institutions"].update({str(name): int(count) for name, count in institutions.items()})
    return sorted(grouped.values(), key=lambda item: int(item.get("count") or 0), reverse=True)


def split_multi_value(value: object) -> list[str]:
    return [part.strip() for part in re.split(r";|\||,", str(value or "")) if part.strip()]


def institution_geocache_path() -> Path:
    return MAPS_ROOT / "institutions" / "institution_geocache.csv"


def institution_aliases_path() -> Path:
    return MAPS_ROOT / "institutions" / "institution_aliases.csv"


def institution_overrides_path() -> Path:
    return MAPS_ROOT / "institutions" / "institution_overrides.csv"


def institution_key(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"\buniv\b\.?", "university", text)
    text = re.sub(r"\b(the|at|of|dept|department|division|school|college)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_institution_aliases() -> dict[str, str]:
    aliases = {
        institution_key("Univ Texas"): "University of Texas at Austin",
        institution_key("University of Texas"): "University of Texas at Austin",
        institution_key("The University of Texas"): "University of Texas at Austin",
        institution_key("UT Austin"): "University of Texas at Austin",
        institution_key("University of Texas at Austin"): "University of Texas at Austin",
        institution_key("F. Edward Hebert"): "F. Edward Hebert School of Medicine",
        institution_key("F Edward Hebert"): "F. Edward Hebert School of Medicine",
        institution_key("Tripler Regional Med Center"): "Tripler Army Medical Center",
        institution_key("San Antonio Military Medical Center, Texas"): "San Antonio Military Medical Center",
    }
    for path, raw_column in ((institution_aliases_path(), "alias"), (institution_overrides_path(), "institution_raw")):
        if not path.exists():
            continue
        frame = safe_read_csv(path)
        for _idx, row in frame.iterrows():
            raw = str(row.get(raw_column) or "").strip()
            normalized = str(row.get("institution_normalized") or "").strip()
            if raw and normalized:
                aliases[institution_key(raw)] = normalized
    return aliases


def clean_institution_display_name(name: object, aliases: dict[str, str]) -> tuple[str, str, str, str, bool, str]:
    original = str(name or "").strip()
    normalized = normalize_institution_name(original)
    if not original:
        return original, normalized, "", "blank", False, "blank institution name"
    alias = aliases.get(institution_key(original)) or aliases.get(institution_key(normalized))
    display = alias or normalized or original
    method = "alias" if alias else "normalized"
    key = institution_key(display)
    generic_terms = {"university", "college", "hospital", "center", "centre", "school", "department", "dept"}
    if not key:
        return original, normalized, display, method, False, "blank normalized institution name"
    if len(key) <= 3 or key in generic_terms:
        return original, normalized, display, method, False, "generic or too-short institution name"
    if key in COUNTRY_NAME_TERMS or key in US_STATE_TERMS:
        return original, normalized, display, method, False, "country/state name is not an institution"
    return original, normalized, display, method, True, ""


def write_institution_name_cleanup_debug(slug: str, rows: list[dict[str, object]], generated: list[Path]) -> None:
    path = processed_path(f"{slug}_institution_name_cleanup_debug.csv")
    pd.DataFrame(
        rows,
        columns=[
            "original_institution_name", "normalized_institution", "display_institution_name",
            "cleanup_method", "accepted_for_display", "exclusion_reason",
        ],
    ).to_csv(path, index=False)
    remember_generated(generated, path)


def load_institution_geocache() -> tuple[dict[str, dict[str, object]], Path]:
    path = institution_geocache_path()
    if not path.exists():
        return {}, path
    df = safe_read_csv(path)
    cache: dict[str, dict[str, object]] = {}
    if df.empty:
        return cache, path
    for _idx, row in df.iterrows():
        lat = pd.to_numeric(pd.Series([row.get("latitude")]), errors="coerce").iloc[0]
        lon = pd.to_numeric(pd.Series([row.get("longitude")]), errors="coerce").iloc[0]
        name = str(row.get("institution_normalized") or row.get("institution_raw") or "").strip()
        if not name or pd.isna(lat) or pd.isna(lon):
            continue
        cache[normalize_institution_name(name)] = {
            "lat": float(lat),
            "lon": float(lon),
            "label": str(row.get("institution_raw") or name).strip(),
            "normalized": name,
            "city": str(row.get("city") or "").strip(),
            "admin1": str(row.get("admin1") or "").strip(),
            "country": str(row.get("country_iso3") or "").strip(),
            "source": str(row.get("source") or path.name).strip(),
            "confidence": str(row.get("confidence") or "").strip(),
        }
        cache[institution_key(name)] = cache[normalize_institution_name(name)]
    return cache, path


def current_core_dataset_for_slug(slug: str) -> Path | None:
    for candidate in (
        OUTPUTS_DIR / f"{slug}_year_limited_records.csv",
        OUTPUTS_DIR / f"{slug}_cleaned.csv",
        OUTPUTS_DIR / f"{slug}.csv",
    ):
        if candidate.exists():
            return candidate
    return None


def institution_points_for_slug(slug: str, generated: list[Path]) -> tuple[list[dict[str, object]], dict[str, object]]:
    cache, cache_path = load_institution_geocache()
    core_path = current_core_dataset_for_slug(slug)
    aliases = load_institution_aliases()
    candidates = 0
    unmatched: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    cleanup_rows: list[dict[str, object]] = []
    direct_points: dict[tuple[str, float, float], dict[str, object]] = {}
    institution_locations_path = OUTPUTS_DIR / f"{slug}_institution_locations.csv"
    if institution_locations_path.exists():
        locations_df = safe_read_csv(institution_locations_path)
        for _idx, row in locations_df.iterrows():
            included = str(row.get("included_in_final_maps") or "").strip().lower()
            if included and included != "yes":
                continue
            location_source = str(row.get("location_source") or "").strip()
            if location_source and location_source not in {
                "scopus_structured_affiliation",
                "scopus_raw_affiliation_parse",
                "openalex_raw_affiliation_parse",
                "ror",
                "institution_geocache",
                "parsed_affiliation_city_geocode",
            }:
                continue
            raw_institution = str(row.get("institution_name") or row.get("normalized_institution") or "").strip()
            original, normalized_name, display_name, cleanup_method, accepted, cleanup_reason = clean_institution_display_name(raw_institution, aliases)
            cleanup_rows.append(
                {
                    "original_institution_name": original,
                    "normalized_institution": normalized_name,
                    "display_institution_name": display_name,
                    "cleanup_method": cleanup_method,
                    "accepted_for_display": "yes" if accepted else "no",
                    "exclusion_reason": cleanup_reason,
                }
            )
            institution = display_name
            candidates += 1
            if not accepted:
                unmatched[raw_institution] += 1
                continue
            lat = pd.to_numeric(pd.Series([row.get("latitude")]), errors="coerce").iloc[0]
            lon = pd.to_numeric(pd.Series([row.get("longitude")]), errors="coerce").iloc[0]
            city = str(row.get("affiliation_city") or row.get("institution_city") or row.get("city") or "").strip()
            country = str(
                row.get("institution_country_iso3")
                or row.get("institution_country")
                or row.get("affiliation_country_iso3")
                or row.get("affiliation_country")
                or row.get("country")
                or ""
            ).strip()
            count = pd.to_numeric(pd.Series([row.get("publication_count", row.get("record_count", 1))]), errors="coerce").fillna(1).iloc[0]
            cache_key = normalize_institution_name(aliases.get(institution_key(institution), institution))
            alt_cache_key = institution_key(aliases.get(institution_key(institution), institution))
            cached = cache.get(cache_key) or cache.get(alt_cache_key)
            if (pd.isna(lat) or pd.isna(lon)) and cached:
                lat = pd.to_numeric(pd.Series([cached.get("lat")]), errors="coerce").iloc[0]
                lon = pd.to_numeric(pd.Series([cached.get("lon")]), errors="coerce").iloc[0]
                city = city or str(cached.get("city") or "")
                country = country or str(cached.get("country") or "")
            if pd.isna(lat) or pd.isna(lon):
                unmatched[institution] += 1
                continue
            key = (normalize_institution_name(institution), float(lon), float(lat))
            direct_points.setdefault(
                key,
                {
                    "lon": float(lon),
                    "lat": float(lat),
                    "count": 0,
                    "label": display_name,
                    "institution": institution,
                    "institution_name": display_name,
                    "country": country,
                    "city": city,
                    "admin1": str(row.get("affiliation_state_or_region") or "").strip(),
                    "record_ids": set(),
                    "enrichment_sources": Counter(),
                    "source_context": str(row.get("raw_affiliation_string") or ""),
                    "confidence_score": str(row.get("location_confidence") or ""),
                    "location_method": str(row.get("location_method") or ""),
                    "location_source": location_source,
                    "location_is_headquarters": str(row.get("location_is_headquarters") or "").strip(),
                    "location_is_affiliation_city": str(row.get("location_is_affiliation_city") or "").strip(),
                    "raw_affiliation_evidence": "yes" if str(row.get("raw_affiliation_string") or "").strip() else "no",
                    "source_type": "institution",
                },
            )
            direct_points[key]["count"] = int(direct_points[key].get("count", 0)) + max(1, int(count))
            record_ids = direct_points[key].setdefault("record_ids", set())
            if isinstance(record_ids, set) and str(row.get("record_id") or "").strip():
                record_ids.add(str(row.get("record_id")).strip())
            sources = direct_points[key].setdefault("enrichment_sources", Counter())
            if isinstance(sources, Counter):
                sources[str(row.get("source") or "institution_locations")] += 1
    city_links_path = OUTPUTS_DIR / f"{slug}_city_institution_links.csv"
    if not institution_locations_path.exists() and not direct_points and city_links_path.exists():
        links_df = safe_read_csv(city_links_path)
        for _idx, row in links_df.iterrows():
            raw_institution = str(row.get("institution") or row.get("normalized_institution") or "").strip()
            original, normalized_name, display_name, cleanup_method, accepted, cleanup_reason = clean_institution_display_name(raw_institution, aliases)
            cleanup_rows.append(
                {
                    "original_institution_name": original,
                    "normalized_institution": normalized_name,
                    "display_institution_name": display_name,
                    "cleanup_method": cleanup_method,
                    "accepted_for_display": "yes" if accepted else "no",
                    "exclusion_reason": cleanup_reason,
                }
            )
            institution = display_name
            candidates += 1
            if not accepted:
                unmatched[raw_institution] += 1
                continue
            lat = pd.to_numeric(pd.Series([row.get("latitude")]), errors="coerce").iloc[0]
            lon = pd.to_numeric(pd.Series([row.get("longitude")]), errors="coerce").iloc[0]
            city = str(row.get("city") or "").strip()
            country = str(row.get("country") or "").strip()
            count = pd.to_numeric(pd.Series([row.get("publication_count", row.get("record_count", 1))]), errors="coerce").fillna(1).iloc[0]
            cache_key = normalize_institution_name(aliases.get(institution_key(institution), institution))
            alt_cache_key = institution_key(aliases.get(institution_key(institution), institution))
            cached = cache.get(cache_key) or cache.get(alt_cache_key)
            if (pd.isna(lat) or pd.isna(lon)) and cached:
                lat = pd.to_numeric(pd.Series([cached.get("lat")]), errors="coerce").iloc[0]
                lon = pd.to_numeric(pd.Series([cached.get("lon")]), errors="coerce").iloc[0]
                city = city or str(cached.get("city") or "")
                country = country or str(cached.get("country") or "")
            if pd.isna(lat) or pd.isna(lon):
                unmatched[institution] += 1
                continue
            key = (normalize_institution_name(institution), float(lon), float(lat))
            direct_points.setdefault(
                key,
                {
                    "lon": float(lon),
                    "lat": float(lat),
                    "count": 0,
                    "label": display_name,
                    "institution": institution,
                    "institution_name": display_name,
                    "country": country,
                    "city": city,
                    "admin1": str(row.get("state_or_region") or "").strip(),
                    "record_ids": set(),
                    "enrichment_sources": Counter(),
                    "source_context": str(row.get("source_titles") or ""),
                    "confidence_score": str(row.get("match_confidence") or ""),
                },
            )
            direct_points[key]["count"] = int(direct_points[key].get("count", 0)) + max(1, int(count))
            record_ids = direct_points[key].setdefault("record_ids", set())
            if isinstance(record_ids, set):
                for record_id in str(row.get("source_record_ids") or "").split(";"):
                    if record_id.strip():
                        record_ids.add(record_id.strip())
            sources = direct_points[key].setdefault("enrichment_sources", Counter())
            if isinstance(sources, Counter):
                sources[str(row.get("geocode_source") or "city_institution_links")] += 1
    institutions_path = OUTPUTS_DIR / f"{slug}_institutions.csv"
    if not institution_locations_path.exists() and not direct_points and institutions_path.exists():
        inst_df = safe_read_csv(institutions_path)
        for _idx, row in inst_df.iterrows():
            raw_institution = str(row.get("institution") or row.get("normalized_institution") or "").strip()
            original, normalized_name, display_name, cleanup_method, accepted, cleanup_reason = clean_institution_display_name(raw_institution, aliases)
            cleanup_rows.append(
                {
                    "original_institution_name": original,
                    "normalized_institution": normalized_name,
                    "display_institution_name": display_name,
                    "cleanup_method": cleanup_method,
                    "accepted_for_display": "yes" if accepted else "no",
                    "exclusion_reason": cleanup_reason,
                }
            )
            institution = display_name
            candidates += 1
            if not accepted:
                unmatched[raw_institution] += 1
                continue
            lat = pd.to_numeric(pd.Series([row.get("latitude")]), errors="coerce").iloc[0]
            lon = pd.to_numeric(pd.Series([row.get("longitude")]), errors="coerce").iloc[0]
            city = str(row.get("city") or "").strip()
            country = str(row.get("country") or "").strip()
            if pd.isna(lat) or pd.isna(lon):
                unmatched[institution] += 1
                continue
            key = (normalize_institution_name(institution), float(lon), float(lat))
            direct_points.setdefault(
                key,
                {
                    "lon": float(lon),
                    "lat": float(lat),
                    "count": 0,
                    "label": institution,
                    "institution": institution,
                    "country": country,
                    "city": city,
                    "admin1": str(row.get("state_or_region") or "").strip(),
                    "record_ids": set(),
                    "enrichment_sources": Counter(),
                },
            )
            direct_points[key]["count"] = int(direct_points[key].get("count", 0)) + 1
            record_ids = direct_points[key].setdefault("record_ids", set())
            if isinstance(record_ids, set) and str(row.get("record_id") or "").strip():
                record_ids.add(str(row.get("record_id")))
            sources = direct_points[key].setdefault("enrichment_sources", Counter())
            if isinstance(sources, Counter):
                sources[str(row.get("geocode_source") or "institution_output")] += 1
    if not institution_locations_path.exists() and not direct_points and core_path and cache:
        df = safe_read_csv(core_path)
        if "institutions" in df.columns:
            for _idx, row in df.iterrows():
                for raw_institution in split_multi_value(row.get("institutions")):
                    original, normalized_name, display_name, cleanup_method, accepted, cleanup_reason = clean_institution_display_name(raw_institution, aliases)
                    cleanup_rows.append(
                        {
                            "original_institution_name": original,
                            "normalized_institution": normalized_name,
                            "display_institution_name": display_name,
                            "cleanup_method": cleanup_method,
                            "accepted_for_display": "yes" if accepted else "no",
                            "exclusion_reason": cleanup_reason,
                        }
                    )
                    institution = display_name
                    candidates += 1
                    if not accepted:
                        unmatched[raw_institution] += 1
                        continue
                    key = normalize_institution_name(aliases.get(institution_key(institution), institution))
                    alt_key = institution_key(aliases.get(institution_key(institution), institution))
                    if key in cache:
                        counts[key] += 1
                    elif alt_key in cache:
                        counts[alt_key] += 1
                    else:
                        unmatched[institution] += 1
    points = list(direct_points.values()) if direct_points else [
        {
            "lon": cache[key]["lon"],
            "lat": cache[key]["lat"],
            "count": count,
            "label": cache[key]["label"],
            "country": cache[key].get("country", ""),
            "city": cache[key].get("city", ""),
            "admin1": cache[key].get("admin1", ""),
        }
        for key, count in counts.items()
    ]
    for point in points:
        if isinstance(point.get("record_ids"), set):
            point["record_ids"] = "; ".join(sorted(point["record_ids"]))
        if isinstance(point.get("enrichment_sources"), Counter):
            point["enrichment_source_summary"] = "; ".join(f"{key}: {value}" for key, value in point["enrichment_sources"].most_common())
    unmatched_path = processed_path(f"{slug}_unmatched_institutions_for_map.csv")
    pd.DataFrame(
        [{"institution": name, "count": count} for name, count in unmatched.most_common()],
        columns=["institution", "count"],
    ).to_csv(unmatched_path, index=False)
    remember_generated(generated, unmatched_path)
    write_institution_name_cleanup_debug(slug, cleanup_rows, generated)
    return points, {
        "core_path": str(core_path or ""),
        "institution_locations_path": str(institution_locations_path if institution_locations_path.exists() else ""),
        "city_links_path": str(city_links_path if city_links_path.exists() else ""),
        "institutions_path": str(institutions_path if institutions_path.exists() else ""),
        "cache_path": str(cache_path),
        "candidate_locations": candidates,
        "mapped_points": len(points),
        "aggregated_locations": len(aggregate_points(points)),
        "unmatched_locations": sum(unmatched.values()),
    }


def draw_point_overlay(
    ax: plt.Axes,
    points: list[dict[str, object]],
    max_labels: int = 10,
    label_kind: str = "city",
    title: str = "",
    label_audit: dict[str, object] | None = None,
) -> None:
    if not points:
        if label_audit is not None:
            label_audit.update(
                {
                    "label_top_n": max_labels,
                    "labels_attempted_count": 0,
                    "labels_drawn_count": 0,
                    "labels_drawn": "",
                    "labels_skipped": "no points plotted",
                    "legend_explains_size": "no",
                    "legend_explains_color": "no",
                }
            )
        return
    values = [float(point["count"]) for point in points]
    max_value = max(values) if values else 1
    ax.scatter(
        [point["lon"] for point in points],
        [point["lat"] for point in points],
        s=[45 + 460 * math.sqrt(value / max_value) for value in values],
        c=values,
        cmap=MAP_COLORMAP,
        alpha=0.84,
        edgecolor="#5f1738",
        linewidth=0.55,
        zorder=3,
    )
    ranked_points = sorted(points, key=lambda item: (-float(item.get("count", 0) or 0), str(item.get("label", ""))))
    label_limit = min(max_labels, len(ranked_points)) if max_labels else 0
    attempted = ranked_points[:label_limit]
    drawn: list[str] = []
    skipped: list[str] = []
    texts = []
    if label_limit:
        xs = [float(point["lon"]) for point in points]
        ys = [float(point["lat"]) for point in points]
        x_span = max(max(xs) - min(xs), 1.0)
        y_span = max(max(ys) - min(ys), 1.0)
        x_offset = max(x_span * 0.012, 0.08)
        y_offset = max(y_span * 0.012, 0.08)
        for idx, point in enumerate(attempted):
            label = truncate_label(point.get("label", ""), 24)
            if not label:
                skipped.append(f"blank label ({int(float(point.get('count', 0) or 0))})")
                continue
            direction = 1 if idx % 2 == 0 else -1
            text = ax.text(
                float(point["lon"]) + x_offset,
                float(point["lat"]) + direction * y_offset,
                label,
                fontsize=8.2,
                color=INK,
                ha="left",
                va="center",
                bbox={"boxstyle": "round,pad=0.16", "facecolor": "white", "edgecolor": "none", "alpha": 0.78},
                path_effects=[path_effects.Stroke(linewidth=2.4, foreground="white"), path_effects.Normal()],
                zorder=4,
            )
            texts.append(text)
            drawn.append(f"{label} ({int(float(point.get('count', 0) or 0))})")
        try:
            from adjustText import adjust_text  # type: ignore

            adjust_text(
                texts,
                ax=ax,
                expand_points=(1.12, 1.18),
                expand_text=(1.08, 1.14),
                arrowprops={"arrowstyle": "-", "lw": 0.35, "color": MUTED, "alpha": 0.45},
            )
        except Exception as exc:
            if texts:
                skipped.append(f"collision adjustment unavailable: {exc.__class__.__name__}")
    elif max_labels <= 0:
        skipped.append("labels disabled")
    else:
        skipped.append("no candidate labels")
    if len(set(values)) > 1:
        legend_values = sorted({int(round(min(values))), int(round(float(pd.Series(values).median()))), int(round(max_value))})
    else:
        legend_values = [int(round(max_value))]
    legend_values = [value for value in legend_values if value > 0]
    handles = [
        ax.scatter(
            [],
            [],
            s=45 + 460 * math.sqrt(float(value) / max_value),
            color=plt.get_cmap(MAP_COLORMAP)(0.18 + 0.78 * (float(value) / max_value)),
            alpha=0.78,
            edgecolor="#5f1738",
            linewidth=0.55,
        )
        for value in legend_values
    ]
    labels = [str(int(value)) for value in legend_values]
    has_institution_context = any(bool(point.get("institutions")) for point in points)
    legend_title = (
        "City bubble size/color = detected publication count.\nCities may include multiple contributing institutions."
        if label_kind == "city" and has_institution_context
        else "Detected publication locations\nlarger/darker = more"
    )
    legend = ax.legend(
        handles,
        labels,
        title=legend_title,
        loc="lower left",
        frameon=True,
        facecolor="white",
        edgecolor="#cbd5e1",
        fontsize=8,
        title_fontsize=8.6,
    )
    legend._legend_box.align = "left"
    if label_audit is not None:
        label_audit.update(
            {
                "label_top_n": max_labels,
                "labels_attempted_count": len(attempted),
                "labels_drawn_count": len(drawn),
                "labels_drawn": ", ".join(drawn),
                "labels_skipped": "; ".join(skipped),
                "top_labeled_points": ", ".join(drawn),
                "legend_explains_size": "yes",
                "legend_explains_color": "yes",
                "legend_note": legend_title,
            }
        )
    if title:
        if drawn:
            print(f"{title}: {len(points)} points plotted; labels attempted: {len(attempted)}; labels drawn: {', '.join(drawn)}.")
        else:
            print(f"{title}: {len(points)} points plotted; no labels drawn; reason: {'; '.join(skipped) if skipped else 'no label candidates'}.")


def add_map_caption(fig: plt.Figure, text: str) -> None:
    fig.text(0.02, 0.018, text, fontsize=8.5, color="#475569", ha="left", va="bottom")


def draw_institution_side_panel(ax: plt.Axes, rows: list[dict[str, object]], meta: dict[str, object] | None = None) -> None:
    ax.axis("off")
    ax.text(0, 0.98, "Top Institutions", fontsize=17, weight="bold", color=INK, va="top")
    y = 0.9
    if meta:
        details = []
        if meta.get("total_country_publication_count") not in {"", None}:
            details.append(f"Total publications: {meta.get('total_country_publication_count')}")
        if meta.get("detected_cities_count") not in {"", None}:
            details.append(f"Detected cities: {meta.get('detected_cities_count')}")
        if meta.get("institutions_listed_count") not in {"", None}:
            details.append(f"Institutions listed: {meta.get('institutions_listed_count')}")
        for detail in details:
            ax.text(0, y, detail, fontsize=9.5, color="#475569", va="top")
            y -= 0.055
        y -= 0.02
    if not rows:
        ax.text(0, y, "No institution data available for this country.", fontsize=10.5, color="#475569", va="top", wrap=True)
        return
    for row in rows[:10]:
        name = textwrap.fill(str(row.get("institution_name") or row.get("normalized_institution") or ""), width=28)
        city = str(row.get("city") or "").strip()
        count = int(row.get("publication_count") or 0)
        rank = int(row.get("rank") or 0)
        label = f"{rank}. {name}"
        detail = f"{city} - {count} publication{'s' if count != 1 else ''}" if city else f"{count} publication{'s' if count != 1 else ''}"
        ax.text(0, y, label, fontsize=10.2, weight="bold", color=INK, va="top")
        y -= 0.045 * max(1, label.count("\n") + 1)
        ax.text(0.03, y, detail, fontsize=9.2, color="#475569", va="top")
        y -= 0.07
        if y < 0.08:
            break
    more = int(meta.get("more_institutions", 0) if meta else 0)
    if more > 0 and y > 0.04:
        ax.text(0, y, f"+ {more} more institutions", fontsize=9.5, color="#475569", va="top")


def save_layer_map(
    layer_records: list[dict[str, object]],
    counts_by_index: dict[int, int],
    points: list[dict[str, object]],
    title: str,
    output_stem: str,
    generated: list[Path],
    extent: tuple[float, float, float, float] | None = None,
    max_labels: int = 10,
    max_points: int | None = None,
    include_points: bool = False,
    labels_enabled: bool = False,
    side_panel_rows: list[dict[str, object]] | None = None,
    side_panel_meta: dict[str, object] | None = None,
) -> Path | None:
    if not layer_records:
        return None
    apply_static_style()
    if side_panel_rows is not None:
        fig, (panel_ax, ax) = plt.subplots(1, 2, figsize=(14.8, 7.4), dpi=STATIC_DPI, gridspec_kw={"width_ratios": [3, 7]})
        draw_institution_side_panel(panel_ax, side_panel_rows, side_panel_meta)
    else:
        fig, ax = plt.subplots(figsize=(12.8, 7.4), dpi=STATIC_DPI)
    ax.set_facecolor("#eef4f8")
    draw_shape_layer(ax, layer_records, counts_by_index)
    if max_points is not None and len(points) > max_points:
        points = sorted(points, key=lambda item: item["count"], reverse=True)[:max_points]
    if include_points:
        draw_point_overlay(ax, points, max_labels=max_labels if labels_enabled else 0)
    if counts_by_index:
        max_count = max(counts_by_index.values())
        if max_count > 0:
            if max_count == 1:
                norm = matplotlib.colors.BoundaryNorm([-0.5, 0.5, 1.5], plt.get_cmap(MAP_COLORMAP).N)
                ticks = [0, 1]
            elif max_count <= 3:
                norm = matplotlib.colors.BoundaryNorm([value - 0.5 for value in range(0, max_count + 2)], plt.get_cmap(MAP_COLORMAP).N)
                ticks = list(range(0, max_count + 1))
            else:
                norm = matplotlib.colors.Normalize(vmin=0, vmax=max_count)
                ticks = list(range(0, max_count + 1)) if max_count <= 8 else None
            scalar = matplotlib.cm.ScalarMappable(norm=norm, cmap=plt.get_cmap(MAP_COLORMAP))
            scalar.set_array([])
            cbar = fig.colorbar(scalar, ax=ax, shrink=0.54, pad=0.018, fraction=0.032, ticks=ticks)
            cbar.set_label("Publication geography count")
            cbar.ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    extent = extent or layer_extent(layer_records)
    if extent:
        xmin, xmax, ymin, ymax = extent
        xpad = max((xmax - xmin) * 0.05, 1)
        ypad = max((ymax - ymin) * 0.05, 1)
        ax.set_xlim(xmin - xpad, xmax + xpad)
        ax.set_ylim(ymin - ypad, ymax + ypad)
    ax.set_xlabel("Longitude", fontsize=10)
    ax.set_ylabel("Latitude", fontsize=10)
    ax.grid(True, color="white", linewidth=0.35, alpha=0.38)
    ax.set_title(title, loc="left", fontsize=19, pad=14, weight="bold")
    caption = (
        "Institution counts are based on enriched Scopus/OpenAlex affiliation metadata where available. "
        "Shading/points reflect publication geography, not prevalence or disease burden."
        if side_panel_rows is not None
        else MAP_CAPTION_CHOROPLETH
    )
    if len([value for value in counts_by_index.values() if value > 0]) == 1:
        caption = "Only one subnational region had detected publication geography in this dataset. Shading reflects publication geography, not prevalence or disease burden."
    add_map_caption(fig, caption)
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    save_static_formats(fig, output_stem, generated)
    plt.close(fig)
    return VISUALS_DIR / f"{output_stem}.png"


def save_point_map(
    points: list[dict[str, object]],
    title: str,
    output_stem: str,
    generated: list[Path],
    extent: tuple[float, float, float, float] | None = None,
    labels_enabled: bool = False,
    label_top_n: int = 5,
    label_kind: str = "city",
    max_points: int | None = None,
    basemap_records: list[dict[str, object]] | None = None,
    label_audit: dict[str, object] | None = None,
    side_panel_rows: list[dict[str, object]] | None = None,
    side_panel_meta: dict[str, object] | None = None,
    caption_text: str | None = None,
) -> Path | None:
    if not points:
        return None
    points = aggregate_points(points)
    if max_points is not None and len(points) > max_points:
        points = sorted(points, key=lambda item: item["count"], reverse=True)[:max_points]
    apply_static_style()
    if side_panel_rows is not None:
        fig, (panel_ax, ax) = plt.subplots(1, 2, figsize=(14.8, 7.4), dpi=STATIC_DPI, gridspec_kw={"width_ratios": [3, 7]})
        draw_institution_side_panel(panel_ax, side_panel_rows, side_panel_meta)
    else:
        fig, ax = plt.subplots(figsize=(12.8, 7.4), dpi=STATIC_DPI)
    ax.set_facecolor("#eef4f8")
    basemap_records = basemap_records or []
    if basemap_records:
        draw_basemap_layer(ax, basemap_records)
    label_limit = min(max(0, label_top_n), len(points)) if labels_enabled else 0
    draw_point_overlay(ax, points, max_labels=label_limit, label_kind=label_kind, title=title, label_audit=label_audit)
    basemap_extent = layer_extent(basemap_records) if basemap_records else None
    if extent:
        xmin, xmax, ymin, ymax = extent
    elif basemap_extent:
        xmin, xmax, ymin, ymax = basemap_extent
    else:
        xs = [float(point["lon"]) for point in points]
        ys = [float(point["lat"]) for point in points]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    xpad = max((xmax - xmin) * 0.06, 0.4)
    ypad = max((ymax - ymin) * 0.06, 0.4)
    ax.set_xlim(xmin - xpad, xmax + xpad)
    ax.set_ylim(ymin - ypad, ymax + ypad)
    ax.set_xlabel("Longitude", fontsize=10)
    ax.set_ylabel("Latitude", fontsize=10)
    ax.grid(True, color="white", linewidth=0.35, alpha=0.35)
    ax.set_title(title, loc="left", fontsize=19, pad=14, weight="bold")
    add_map_caption(
        fig,
        caption_text
        or (
            "Institution counts are based on enriched Scopus/OpenAlex affiliation metadata where available. Shading/points reflect publication geography, not prevalence or disease burden."
            if side_panel_rows is not None
            else MAP_CAPTION_POINTS
        ),
    )
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    save_static_formats(fig, output_stem, generated)
    plt.close(fig)
    return VISUALS_DIR / f"{output_stem}.png"


def add_unmapped(unmapped: list[dict[str, object]], row: pd.Series, attempted: str, reason: str) -> None:
    canonical = str(row.get("canonical_name") or row.get("term") or "").strip()
    unmapped.append(
        {
            "original_term": canonical,
            "normalized_term": title_case_place(map_norm(canonical)),
            "canonical_name": canonical,
            "count": int(row.get("count") or 0),
            "geo_type": row.get("geo_type", ""),
            "state": row.get("state", ""),
            "country": row.get("country", ""),
            "latitude": row.get("latitude", ""),
            "longitude": row.get("longitude", ""),
            "attempted_map_type": attempted,
            "reason_not_mapped": reason,
        }
    )


AMBIGUOUS_PLACE_TERMS = {"china", "italy", "post"}
CITY_FEATURE_TYPES = {
    "city",
    "town",
    "municipality",
    "populated place",
    "populated_place",
    "locality",
    "place",
    "census place",
    "census_place",
    "village",
}
NON_CITY_FEATURE_TYPES = {
    "country",
    "state",
    "province",
    "territory",
    "county",
    "county_subdivision",
    "region",
    "continent",
    "adm0",
    "adm1",
    "adm2",
    "fallback centroid",
    "fallback_centroid",
    "unresolved geography term",
}
US_STATE_TERMS = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "district of columbia", "florida", "georgia", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
    "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
}


def place_label(row: pd.Series) -> str:
    label = str(row.get("canonical_name") or row.get("term") or "").strip()
    state = map_norm(row.get("state", "") or row.get("admin1_name", ""))
    if map_norm(label) in AMBIGUOUS_PLACE_TERMS and state == "texas":
        return f"{label.title()}, TX"
    return label


def city_point_exclusion_reason(row: pd.Series) -> str:
    label_key = map_norm(row.get("canonical_name") or row.get("term") or "")
    geo_type = str(row.get("geo_type") or row.get("feature_type") or "").strip().lower()
    feature_class = str(row.get("feature_class") or "").strip().lower()
    confidence = str(row.get("confidence") or "").strip().lower()
    matched_field = str(row.get("matched_field") or "").strip().lower()
    context = str(row.get("context_snippet") or row.get("source_context") or "").lower()
    if label_key in US_STATE_TERMS:
        return "ADM1/state-level term excluded from city map"
    if label_key in COUNTRY_NAME_TERMS and label_key not in context:
        return "country-name place alias excluded without explicit local context"
    if label_key in WEAK_AMBIGUOUS_PLACE_TERMS:
        return "weak/ambiguous short GeoNames match excluded from city map"
    if len(label_key) < 4 and not (confidence == "high" and matched_field in {"affiliation", "address", "location"} and context):
        return "short place label requires high-confidence affiliation/address context"
    if geo_type in NON_CITY_FEATURE_TYPES or feature_class in {"adm0", "adm1", "adm2", "country", "state", "province", "county"}:
        return f"non-city feature type excluded from city map: {geo_type or feature_class}"
    if not geo_type:
        return "unresolved geography term excluded from city map"
    if geo_type not in CITY_FEATURE_TYPES:
        return f"feature type is not city/locality/place-level: {geo_type}"
    if geo_type in {"village", "census place", "census_place"} and re.search(r"\b(?:village|cdp|census designated place)\b", label_key):
        return "generic census/village phrase fragment excluded from city map"
    return ""


def top_institution_summary(point: dict[str, object], max_items: int = 3) -> tuple[int, str]:
    institutions = point.get("institutions")
    if isinstance(institutions, Counter):
        items = institutions.most_common(max_items)
    elif isinstance(institutions, dict):
        items = sorted(institutions.items(), key=lambda item: (-int(item[1]), str(item[0])))[:max_items]
    else:
        items = []
    return len(institutions) if isinstance(institutions, (Counter, dict)) else 0, "; ".join(f"{name} ({count})" for name, count in items)


def load_city_institution_lookup(slug: str) -> dict[tuple[str, str, str], Counter[str]]:
    path = OUTPUTS_DIR / f"{slug}_city_institution_links.csv"
    if not path.exists():
        return {}
    frame = safe_read_csv(path)
    if frame.empty or "city" not in frame.columns:
        return {}
    lookup: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for _idx, row in frame.iterrows():
        city = map_norm(row.get("city", ""))
        if not city:
            continue
        admin1 = map_norm(row.get("state_or_region", ""))
        country = map_norm(row.get("country", ""))
        institution = str(row.get("normalized_institution") or row.get("institution") or "").strip()
        count = int(pd.to_numeric(pd.Series([row.get("publication_count", row.get("record_count", 1))]), errors="coerce").fillna(1).iloc[0])
        if institution:
            lookup[(city, admin1, country)][institution] += max(1, count)
            lookup[(city, "", country)][institution] += max(1, count)
            lookup[(city, admin1, "")][institution] += max(1, count)
            lookup[(city, "", "")][institution] += max(1, count)
    return lookup


def attach_city_institutions(slug: str, points: list[dict[str, object]]) -> int:
    lookup = load_city_institution_lookup(slug)
    if not lookup:
        return 0
    matched = 0
    for point in points:
        keys = [
            (map_norm(point.get("label", "")), map_norm(point.get("admin1", "")), map_norm(point.get("country", ""))),
            (map_norm(point.get("label", "")), "", map_norm(point.get("country", ""))),
            (map_norm(point.get("label", "")), map_norm(point.get("admin1", "")), ""),
            (map_norm(point.get("label", "")), "", ""),
        ]
        institutions = next((lookup[key] for key in keys if key in lookup), None)
        if institutions:
            point["institutions"] = institutions
            matched += 1
    return matched


def write_city_map_debug(
    slug: str,
    output_stem: str,
    title: str,
    candidate_points: list[dict[str, object]],
    included_points: list[dict[str, object]],
    label_audit: dict[str, object],
    output_path: Path | None,
    generated: list[Path],
) -> Path:
    included_ids = {id(point) for point in included_points}
    rows: list[dict[str, object]] = []
    for point in candidate_points:
        institution_count, top_institutions = top_institution_summary(point)
        included = id(point) in included_ids
        rows.append(
            {
                "original_term": point.get("original_term", point.get("label", "")),
                "resolved_name": point.get("resolved_name", point.get("label", "")),
                "display_label": point.get("label", ""),
                "country": point.get("country", ""),
                "admin1": point.get("admin1", ""),
                "admin2": point.get("admin2", ""),
                "feature_type": point.get("feature_type", ""),
                "feature_class": point.get("feature_class", ""),
                "latitude": point.get("lat", ""),
                "longitude": point.get("lon", ""),
                "publication_geography_count": point.get("count", 0),
                "included_in_city_map": "yes" if included else "no",
                "exclusion_reason": "" if included else point.get("exclusion_reason", "not selected for this city-map extent"),
                "institution_count": institution_count,
                "top_institutions": top_institutions,
            }
        )
    debug_path = processed_path(f"{output_stem}_debug.csv")
    pd.DataFrame(
        rows,
        columns=[
            "original_term", "resolved_name", "display_label", "country", "admin1", "admin2",
            "feature_type", "feature_class", "latitude", "longitude", "publication_geography_count",
            "included_in_city_map", "exclusion_reason", "institution_count", "top_institutions",
        ],
    ).to_csv(debug_path, index=False)
    remember_generated(generated, debug_path)
    excluded = [row for row in rows if row["included_in_city_map"] == "no"]
    top_excluded = ", ".join(
        f"{row['display_label']} ({row['publication_geography_count']})"
        for row in sorted(excluded, key=lambda item: (-int(item["publication_geography_count"] or 0), str(item["display_label"])))[:5]
    )
    print(
        f"{title}: candidate points: {len(candidate_points)}; city/locality points included: {len(included_points)}; "
        f"non-city terms excluded: {len(excluded)}; top excluded non-city terms: {top_excluded or 'none'}; "
        f"labels drawn: {label_audit.get('labels_drawn', '') or 'none'}; output path: {output_path or 'none'}."
    )
    for row in excluded[:8]:
        reason = str(row.get("exclusion_reason") or "excluded from city map")
        print(f"{title}: {reason}: {row.get('display_label')} ({row.get('publication_geography_count')}).")
    return debug_path


def ambiguity_details(row: pd.Series) -> tuple[str, str, str]:
    term = map_norm(row.get("term", "") or row.get("canonical_name", ""))
    state = map_norm(row.get("state", "") or row.get("admin1_name", ""))
    country = map_norm(row.get("country", ""))
    matched_field = str(row.get("matched_field") or "").lower()
    context = str(row.get("context_snippet") or row.get("source_reference") or row.get("source") or "")
    if term not in AMBIGUOUS_PLACE_TERMS:
        return "no", "high", ""
    if state == "texas" or ", tx" in context.lower() or "texas" in context.lower():
        return "yes", "high", "ambiguous place name retained because Texas context/admin metadata is present"
    if matched_field in {"affiliation", "location", "address"} or country:
        return "yes", "medium", "ambiguous place name retained with geographic field/country context"
    return "yes", "low", "ambiguous place name; no explicit state/country context"


def generate_geography_heatmaps(slug: str, generated: list[Path]) -> tuple[int, int, list[Path]]:
    discovery = build_map_discovery()
    geo_df = geography_count_rows(slug)
    geography_source = str(geo_df.attrs.get("source_path", "")) if not geo_df.empty else "none"
    suppressed_count = int(geo_df.attrs.get("suppressed_count", 0)) if not geo_df.empty else 0
    unmapped: list[dict[str, object]] = []
    mapping_audit: list[dict[str, object]] = []
    ambiguous_matches: list[dict[str, object]] = []
    maps_generated: list[str] = []
    map_metadata: list[dict[str, object]] = []
    skipped: list[str] = []
    mapped_terms: set[int] = set()
    country_lookup = match_records(discovery.country_layer, ("boundaryName", "boundaryISO", "NAME", "NAME_LONG", "ADMIN", "ISO_A3", "ADM0_A3"))
    country_meta = build_country_metadata(discovery.country_layer)
    admin1_layer = discovery.census_state_layer or discovery.admin1_layer
    admin1_lookup = match_records(admin1_layer, ("NAME", "name", "name_en", "region", "postal", "iso_3166_2", "STUSPS"))
    place_layer = discovery.census_place_layer or discovery.place_layer
    place_lookup = match_records(place_layer, ("NAME", "NAMEASCII"))
    country_counts: Counter[int] = Counter()
    country_level_counts: Counter[int] = Counter()
    admin1_counts_by_country: Counter[int] = Counter()
    place_counts_by_country: Counter[int] = Counter()
    record_counts_by_country: Counter[int] = Counter()
    state_counts: Counter[int] = Counter()
    points: list[dict[str, object]] = []
    city_candidates: list[dict[str, object]] = []
    suspicious_city_matches: list[dict[str, object]] = []
    regional_city_debug_rows: list[dict[str, object]] = []
    regional_map_debug_rows: list[dict[str, object]] = []
    regional_point_assignment_rows: list[dict[str, object]] = []
    regional_basemap_debug_rows: list[dict[str, object]] = []
    regional_qa_rows: list[dict[str, object]] = []
    country_side_panel_debug_rows: list[dict[str, object]] = []

    def infer_country_index(row: pd.Series, keys: set[str]) -> int | None:
        for value in (row.get("country", ""), row.get("canonical_name", "") if str(row.get("geo_type", "")).lower() == "country" else ""):
            key = map_norm(value)
            if key in country_lookup:
                return country_lookup[key]
        for key in keys:
            if key in country_lookup:
                return country_lookup[key]
        return None

    def country_from_admin_record(record: dict[str, object]) -> int | None:
        iso_key = str(record_value(record, "shapeGroup", "boundaryISO", "ISO_A3", "ADM0_A3")).upper()
        for idx, metadata in country_meta.items():
            if iso_key and iso_key == str(metadata.get("iso_a3", "")).upper():
                return idx
        key = record_country_key(record)
        if key in country_lookup:
            return country_lookup[key]
        if str(record_value(record, "iso_3166_2")).upper().startswith("US-") or str(record_value(record, "STUSPS")).strip():
            return country_lookup.get("united states")
        return None

    def country_from_place_record(record: dict[str, object]) -> int | None:
        for field in ("ADM0NAME", "adm0name", "COUNTRY"):
            key = map_norm(record_value(record, field))
            if key in country_lookup:
                return country_lookup[key]
        return None

    for idx, row in geo_df.iterrows():
        keys = geography_term_keys(row)
        count = int(row.get("count") or 0)
        geo_type = str(row.get("geo_type") or "").lower()
        matched = False
        attempted = "country" if geo_type == "country" else "state" if geo_type in {"state", "admin1"} else "place"
        reason = ""
        matched_layer = ""
        matched_geometry_name = ""
        map_output_used = ""
        row_country_index = infer_country_index(row, keys)
        ambiguity_flag, confidence, ambiguity_reason = ambiguity_details(row)
        metadata_country_key = map_norm(row.get("country", ""))
        country_match_keys = keys if geo_type == "country" else set()
        for key in country_match_keys:
            if key in country_lookup:
                matched_country = country_lookup[key]
                country_counts[matched_country] += count
                country_level_counts[matched_country] += count
                record_counts_by_country[matched_country] += count
                mapped_terms.add(idx)
                matched = True
                row_country_index = matched_country
                matched_layer = "geoboundaries_admin"
                matched_geometry_name = str(country_meta.get(matched_country, {}).get("country", key))
                map_output_used = f"{slug}_geography_heatmap_world.png"
                break
        metadata_state_key = map_norm(row.get("state", "") or row.get("admin1_name", ""))
        admin_match_keys = keys if geo_type in {"state", "admin1", "county", "county_subdivision"} else {key for key in keys if key not in {metadata_country_key, metadata_state_key}}
        for key in admin_match_keys:
            if key in admin1_lookup:
                admin_idx = admin1_lookup[key]
                state_counts[admin_idx] += count
                admin_country = country_from_admin_record(admin1_layer.records[admin_idx]) if admin1_layer else row_country_index
                if admin_country is not None:
                    country_counts[admin_country] += count
                    admin1_counts_by_country[admin_country] += count
                    record_counts_by_country[admin_country] += count
                    row_country_index = admin_country
                mapped_terms.add(idx)
                matched = True
                matched_layer = "census_admin1" if admin1_layer == discovery.census_state_layer else "geoboundaries_admin1"
                matched_geometry_name = str(record_value(admin1_layer.records[admin_idx], "NAME", "name", "name_en", "region", "postal", "STUSPS")) if admin1_layer else key
                map_output_used = f"{slug}_geography_heatmap_us.png" if row_country_index is not None and country_meta.get(row_country_index, {}).get("country_key") == "united states" else f"{slug}_geography_heatmap_world.png"
                break
        point = point_from_row(row)
        if point:
            lon, lat = point
            if row_country_index is not None:
                country_counts[row_country_index] += count
                place_counts_by_country[row_country_index] += count
                record_counts_by_country[row_country_index] += count
            country_label = str(row.get("country") or country_meta.get(row_country_index if row_country_index is not None else -1, {}).get("country", ""))
            exclusion_reason = city_point_exclusion_reason(row)
            city_point = {
                "lon": lon,
                "lat": lat,
                "count": count,
                "label": place_label(row),
                "country_index": row_country_index,
                "country": country_label,
                "admin1": row.get("state", "") or row.get("admin1_name", ""),
                "admin2": row.get("admin2_name", "") or row.get("county", ""),
                "original_term": row.get("term", "") or row.get("canonical_name", ""),
                "resolved_name": row.get("canonical_name", "") or row.get("term", ""),
                "feature_type": geo_type,
                "feature_class": row.get("feature_class", row.get("feature_code", "")),
                "exclusion_reason": exclusion_reason,
                "source_context": row.get("context_snippet", "") or row.get("source_context", "") or row.get("source_reference", ""),
                "confidence_score": row.get("confidence", ""),
                "matched_field": row.get("matched_field", ""),
            }
            city_candidates.append(city_point)
            if exclusion_reason:
                suspicious_city_matches.append(
                    {
                        "original_term": city_point["original_term"],
                        "resolved_name": city_point["resolved_name"],
                        "country": city_point["country"],
                        "admin1": city_point["admin1"],
                        "feature_type": city_point["feature_type"],
                        "confidence_score": row.get("confidence", ""),
                        "reason_flagged": exclusion_reason,
                        "included_in_city_map": "no",
                        "exclusion_reason": exclusion_reason,
                        "source_context": row.get("context_snippet", ""),
                    }
                )
            if not exclusion_reason:
                points.append(city_point)
                suspicious_reason = suspicious_regional_reason(city_point)
                if suspicious_reason:
                    suspicious_city_matches.append(
                        {
                            "original_term": city_point["original_term"],
                            "resolved_name": city_point["resolved_name"],
                            "country": city_point["country"],
                            "admin1": city_point["admin1"],
                            "feature_type": city_point["feature_type"],
                            "confidence_score": row.get("confidence", ""),
                            "reason_flagged": suspicious_reason,
                            "included_in_city_map": "yes",
                            "exclusion_reason": "",
                            "source_context": row.get("context_snippet", ""),
                        }
                    )
            mapped_terms.add(idx)
            matched = True
            matched_layer = "detected_lat_lon"
            matched_geometry_name = place_label(row)
            map_output_used = f"{slug}_geography_heatmap_world.png"
        elif geo_type in {"place", "county", "county_subdivision"}:
            place_key = next((key for key in keys if key in place_lookup), "")
            if place_key and place_layer:
                place_record = place_layer.records[place_lookup[place_key]]
                place_point = place_record.get("_point")
                place_country = country_from_place_record(place_record) or row_country_index
                if place_point:
                    lon, lat = place_point
                    if place_country is not None:
                        country_counts[place_country] += count
                        place_counts_by_country[place_country] += count
                        record_counts_by_country[place_country] += count
                    country_label = str(row.get("country") or country_meta.get(place_country if place_country is not None else -1, {}).get("country", ""))
                    exclusion_reason = city_point_exclusion_reason(row)
                    city_point = {
                        "lon": lon,
                        "lat": lat,
                        "count": count,
                        "label": place_label(row),
                        "country_index": place_country,
                        "country": country_label,
                        "admin1": row.get("state", "") or row.get("admin1_name", ""),
                        "admin2": row.get("admin2_name", "") or row.get("county", ""),
                        "original_term": row.get("term", "") or row.get("canonical_name", ""),
                        "resolved_name": row.get("canonical_name", "") or row.get("term", ""),
                        "feature_type": geo_type or "place",
                        "feature_class": row.get("feature_class", row.get("feature_code", "")),
                        "exclusion_reason": exclusion_reason,
                        "source_context": row.get("context_snippet", "") or row.get("source_context", "") or row.get("source_reference", ""),
                        "confidence_score": row.get("confidence", ""),
                        "matched_field": row.get("matched_field", ""),
                    }
                    city_candidates.append(city_point)
                    if exclusion_reason:
                        suspicious_city_matches.append(
                            {
                                "original_term": city_point["original_term"],
                                "resolved_name": city_point["resolved_name"],
                                "country": city_point["country"],
                                "admin1": city_point["admin1"],
                                "feature_type": city_point["feature_type"],
                                "confidence_score": row.get("confidence", ""),
                                "reason_flagged": exclusion_reason,
                                "included_in_city_map": "no",
                                "exclusion_reason": exclusion_reason,
                                "source_context": row.get("context_snippet", ""),
                            }
                        )
                    if not exclusion_reason:
                        points.append(city_point)
                        suspicious_reason = suspicious_regional_reason(city_point)
                        if suspicious_reason:
                            suspicious_city_matches.append(
                                {
                                    "original_term": city_point["original_term"],
                                    "resolved_name": city_point["resolved_name"],
                                    "country": city_point["country"],
                                    "admin1": city_point["admin1"],
                                    "feature_type": city_point["feature_type"],
                                    "confidence_score": row.get("confidence", ""),
                                    "reason_flagged": suspicious_reason,
                                    "included_in_city_map": "yes",
                                    "exclusion_reason": "",
                                    "source_context": row.get("context_snippet", ""),
                                }
                            )
                    mapped_terms.add(idx)
                    matched = True
                    matched_layer = "local_place_gazetteer"
                    matched_geometry_name = str(record_value(place_record, "NAME", "NAMEASCII"))
                    map_output_used = f"{slug}_geography_heatmap_world.png"
        if not matched:
            reason = "no_matching_country" if attempted == "country" else "no_matching_state" if attempted == "state" else "missing_lat_lon"
            add_unmapped(unmapped, row, attempted, reason)
        canonical = str(row.get("canonical_name") or row.get("term") or "").strip()
        audit_row = {
            "canonical_name": canonical,
            "original_term": str(row.get("term") or canonical),
            "count": count,
            "geo_type": row.get("geo_type", ""),
            "map_level": row.get("map_level", ""),
            "country": row.get("country", country_meta.get(row_country_index if row_country_index is not None else -1, {}).get("country", "")),
            "state": row.get("state", ""),
            "admin1_name": row.get("admin1_name", ""),
            "place_name": canonical if geo_type in {"place", "county", "county_subdivision"} else "",
            "latitude": row.get("latitude", ""),
            "longitude": row.get("longitude", ""),
            "attempted_map_type": attempted,
            "matched_layer": matched_layer,
            "matched_geometry_name": matched_geometry_name,
            "mapped_yes_no": "yes" if matched else "no",
            "map_output_used": map_output_used,
            "reason_not_mapped": reason,
            "ambiguity_flag": ambiguity_flag,
            "confidence": confidence,
        }
        mapping_audit.append(audit_row)
        if ambiguity_flag == "yes":
            ambiguous_matches.append(
                {
                    "term": audit_row["original_term"],
                    "canonical_name": canonical,
                    "possible_matches": "country; Texas place; common/generic term",
                    "selected_match": matched_geometry_name or canonical,
                    "selected_country": audit_row["country"],
                    "selected_state": audit_row["state"] or audit_row["admin1_name"],
                    "latitude": audit_row["latitude"],
                    "longitude": audit_row["longitude"],
                    "matched_field": row.get("matched_field", ""),
                    "context_snippet": row.get("context_snippet", ""),
                    "confidence": confidence,
                    "ambiguity_reason": ambiguity_reason,
                    "kept_yes_no": "yes",
                    "suppression_reason": "",
                }
            )

    city_institution_matches = attach_city_institutions(slug, city_candidates)
    if city_institution_matches:
        print(f"City-institution context matched for {city_institution_matches} city map candidate points.")
    institution_points, institution_meta = institution_points_for_slug(slug, generated)
    final_institution_points = [point for point in institution_points if not final_city_exclusion_reason(point, 1)]
    institution_source_check = log_final_map_source_check(slug, final_institution_points)
    write_final_city_reliability_outputs(slug, institution_points, city_candidates, generated)
    suspicious_path = processed_path(f"{slug}_suspicious_city_matches_debug.csv")
    pd.DataFrame(
        suspicious_city_matches,
        columns=[
            "original_term", "resolved_name", "country", "admin1", "feature_type", "confidence_score",
            "reason_flagged", "included_in_city_map", "exclusion_reason", "source_context",
        ],
    ).to_csv(suspicious_path, index=False)
    remember_generated(generated, suspicious_path)

    summary = summarize_detected_countries(
        slug,
        country_meta,
        country_counts,
        country_level_counts,
        admin1_counts_by_country,
        place_counts_by_country,
        record_counts_by_country,
    )
    summary_path = processed_path(f"{slug}_geography_country_summary.csv")
    remember_generated(generated, summary_path)
    plan = build_geography_map_plan(summary, geo_df)
    write_geography_map_plan(slug, plan, summary, generated)

    def save_or_skip(
        name: str,
        path: Path | None,
        reason: str,
        map_type: str = "choropleth",
        records_mapped: int = 0,
        join_key: str = "",
        polygon_joins: int = 0,
        unmatched: int = 0,
        labels_enabled: bool = False,
        boundary_file: Path | str = "",
        points_plotted: int = 0,
        polygons_plotted: int = 0,
        basemap_drawn: bool = False,
        extra_metadata: dict[str, object] | None = None,
    ) -> None:
        if path and path.exists():
            unique_count_values = ""
            map_detail_level = "high"
            if map_type == "subnational choropleth":
                if polygon_joins <= 1:
                    map_detail_level = "single-region"
                elif polygon_joins <= 3:
                    map_detail_level = "low"
                elif polygon_joins <= 8:
                    map_detail_level = "medium"
                else:
                    map_detail_level = "high"
            metadata = {
                "name": name,
                "map_type": map_type,
                "geography_source_file": geography_source,
                "boundary_file_used": boundary_file,
                "records_mapped": records_mapped,
                "points_plotted": points_plotted,
                "polygons_plotted": polygons_plotted,
                "records_suppressed": suppressed_count,
                "join_key_used": join_key,
                "successful_polygon_joins": polygon_joins,
                "unmatched_regions_or_places": unmatched,
                "labels_enabled": "yes" if labels_enabled else "no",
                "basemap_drawn": "yes" if basemap_drawn else "no",
                "output_file_path": path,
                "nonzero_polygon_count": polygon_joins,
                "unique_count_values": unique_count_values or str(polygon_joins if polygon_joins else ""),
                "map_detail_level": map_detail_level,
            }
            if extra_metadata:
                metadata.update(extra_metadata)
            maps_generated.append(str(path))
            map_metadata.append(metadata)
        else:
            skipped.append(f"{name}: {reason}")

    def indexed_subset(records: list[dict[str, object]], predicate: Callable[[dict[str, object]], bool]) -> tuple[list[dict[str, object]], dict[int, int]]:
        source_index = {id(record): idx for idx, record in enumerate(records)}
        subset = [record for record in records if predicate(record)]
        counts = {local_idx: country_counts[source_index[id(record)]] for local_idx, record in enumerate(subset) if source_index[id(record)] in country_counts}
        return subset, counts

    def subset_points(extent: tuple[float, float, float, float], country_keys: set[str] | None = None) -> list[dict[str, object]]:
        selected = [point for point in points if point_in_extent(point, extent)]
        if country_keys is None:
            return selected
        return [
            point for point in selected
            if point.get("country_index") in country_meta and str(country_meta[int(point["country_index"])].get("country_key")) in country_keys
        ]

    def subset_city_candidates(extent: tuple[float, float, float, float], country_keys: set[str] | None = None) -> list[dict[str, object]]:
        selected = [point for point in city_candidates if point_in_extent(point, extent)]
        if country_keys is None:
            return selected
        return [
            point for point in selected
            if point.get("country_index") in country_meta and str(country_meta[int(point["country_index"])].get("country_key")) in country_keys
        ]

    country_key_to_meta = {str(meta.get("country_key", "")): meta for meta in country_meta.values()}
    iso3_to_meta = {str(meta.get("iso_a3", "")).upper(): meta for meta in country_meta.values() if meta.get("iso_a3")}

    def point_country_meta(point: dict[str, object]) -> dict[str, object]:
        idx = point.get("country_index")
        if isinstance(idx, int) and idx in country_meta:
            return country_meta[idx]
        if str(idx).isdigit() and int(str(idx)) in country_meta:
            return country_meta[int(str(idx))]
        country_text = str(point.get("country") or "").strip()
        iso = country_text.upper()
        if iso in iso3_to_meta:
            return iso3_to_meta[iso]
        return country_key_to_meta.get(map_norm(country_text), {})

    def point_expected_regions(point: dict[str, object]) -> set[str]:
        meta = point_country_meta(point)
        country_key = str(meta.get("country_key") or point.get("country") or "")
        return expected_regions_for_country(country_key, str(meta.get("iso_a3") or point.get("iso3") or ""), meta)

    def point_assignment_method(point: dict[str, object], region_key: str, extent: tuple[float, float, float, float]) -> str:
        expected = point_expected_regions(point)
        if region_key in expected:
            return "iso3_country_list"
        if point_in_extent(point, extent):
            return "coordinate_bbox"
        return "unresolved"

    def selected_points_for_region(points_to_filter: list[dict[str, object]], region_key: str, extent: tuple[float, float, float, float], final_map: bool) -> list[dict[str, object]]:
        selected: list[dict[str, object]] = []
        for point in points_to_filter:
            method = point_assignment_method(point, region_key, extent)
            if method == "iso3_country_list":
                selected.append(point)
            elif not final_map and method == "coordinate_bbox":
                selected.append(point)
        return selected

    def append_point_assignment_rows(
        region_key: str,
        title: str,
        extent: tuple[float, float, float, float],
        city_region_candidates: list[dict[str, object]],
        city_region_points: list[dict[str, object]],
        institution_region_points: list[dict[str, object]],
    ) -> int:
        plotted_ids = {id(point) for point in city_region_points}
        suspicious_count = 0
        all_candidates = [*city_region_candidates, *institution_region_points]
        for point in all_candidates:
            meta = point_country_meta(point)
            expected = point_expected_regions(point)
            method = point_assignment_method(point, region_key, extent)
            point_type = point_source_type(point)
            included = id(point) in plotted_ids
            if point_type == "institution":
                included = method == "iso3_country_list"
            suspicious_reason = suspicious_regional_reason(point)
            if suspicious_reason and (included or method != "unresolved"):
                suspicious_count += 1
            expected_text = "; ".join(sorted(expected)) if expected else ""
            regional_point_assignment_rows.append(
                {
                    "record_id": point.get("record_ids", point.get("record_id", "")),
                    "source_title": point.get("source_title", ""),
                    "point_name": point.get("original_term", point.get("institution", point.get("label", ""))),
                    "display_label": point.get("label", point.get("institution", "")),
                    "point_type": point_type,
                    "latitude": point.get("lat", ""),
                    "longitude": point.get("lon", ""),
                    "country": point.get("country", meta.get("country", "")),
                    "iso3": meta.get("iso_a3", point.get("iso3", "")),
                    "admin1": point.get("admin1", ""),
                    "city": point.get("city", point.get("label", "")),
                    "institution_name": point.get("institution", "") if point_type == "institution" else "",
                    "publication_count": point.get("count", ""),
                    "assigned_region": title if method != "unresolved" else "",
                    "expected_region_from_country": expected_text,
                    "region_assignment_method": method,
                    "included_in_regional_map": "yes" if included else "no",
                    "exclusion_reason": "" if included else suspicious_reason or ("institution point excluded from final regional map" if point_type == "institution" else str(point.get("exclusion_reason") or "")),
                    "source_context": point.get("source_context", ""),
                    "confidence_score": point.get("confidence_score", ""),
                }
            )
        return suspicious_count

    def append_regional_basemap_debug(region_key: str, title: str, records: list[dict[str, object]], predicate: Callable[[dict[str, object]], bool]) -> None:
        record_ids = {id(record) for record in records}
        found_iso3 = set()
        for record in discovery.country_layer.records if discovery.country_layer else []:
            name = basemap_country_name(record)
            iso3 = basemap_country_iso3(record)
            if iso3:
                found_iso3.add(iso3)
            expected = bool(predicate(record))
            included = id(record) in record_ids
            geometry_valid, geometry_empty = basemap_geometry_status(record)
            regional_basemap_debug_rows.append(
                {
                    "region": title,
                    "country_name": name,
                    "iso3": iso3,
                    "expected_in_region": "yes" if expected else "no",
                    "found_in_world_adm0": "yes",
                    "included_in_basemap": "yes" if included else "no",
                    "geometry_valid": "yes" if geometry_valid else "no",
                    "geometry_empty": "yes" if geometry_empty else "no",
                    "geometry_type": "Polygon/MultiPolygon" if record.get("_parts") else "Point" if record.get("_point") else "",
                    "polygon_count": basemap_polygon_count(record),
                    "reason_excluded": "" if included else "not part of regional ADM0 predicate" if not expected else "expected country not selected into regional basemap",
                }
            )
        expected_names = REGIONAL_MAPS_REQUIRED.get(region_key, {}).get("countries", set())
        for expected_name in sorted(str(item).upper() for item in expected_names):
            if expected_name in found_iso3:
                continue
            regional_basemap_debug_rows.append(
                {
                    "region": title,
                    "country_name": "",
                    "iso3": expected_name,
                    "expected_in_region": "yes",
                    "found_in_world_adm0": "no",
                    "included_in_basemap": "no",
                    "geometry_valid": "no",
                    "geometry_empty": "yes",
                    "geometry_type": "",
                    "polygon_count": 0,
                    "reason_excluded": "expected country not found in WORLD_ADM0",
                }
            )

    def log_regional_diagnostic(
        title: str,
        enabled: bool,
        countries_in_region: str,
        records: list[dict[str, object]],
        extent: tuple[float, float, float, float],
        points_before: int,
        region_points: list[dict[str, object]],
        region_candidates: list[dict[str, object]],
        institution_points_before: int,
        institution_points_after: int,
        generated_path: Path | None,
        final_institution_path: Path | None,
        text_debug_path: Path | None,
        labels_drawn: int,
        skip_reason: str,
        warnings: list[str],
    ) -> None:
        basemap_extent = layer_extent(records)
        print(f"REGIONAL MAP DIAGNOSTIC: {title}")
        print(f"  region enabled: {'yes' if enabled else 'no'}")
        print(f"  region country list: {countries_in_region or 'none'}")
        print(f"  ADM0 basemap path: {discovery.country_layer.path if discovery.country_layer else 'none'}")
        print(f"  ADM0 basemap loaded: {'yes' if records else 'no'}")
        print(f"  basemap polygon count: {sum(basemap_polygon_count(record) for record in records)}")
        print("  basemap CRS: EPSG:4326 assumed lon/lat")
        print(f"  points before region filtering: {points_before}")
        print(f"  points after region filtering: {len(region_points)}")
        print(f"  institution points before filtering: {institution_points_before}")
        print(f"  institution points after filtering: {institution_points_after}")
        print(f"  institution_location_rows_available: {institution_points_before}")
        print(f"  institution_location_rows_after_region_filter: {institution_points_after}")
        print(f"  city/text geography points before filtering: {len(city_candidates)}")
        print(f"  city/text geography points after filtering: {len(region_candidates)}")
        plotted_type_counts = Counter(point_source_type(point) for point in region_points)
        print(f"  institution-affiliation points plotted: {institution_points_after}")
        print(f"  enriched institution points plotted: {institution_points_after}")
        print(f"  institution_points_plotted: {institution_points_after}")
        print(f"  text-mentioned geography points plotted: {plotted_type_counts.get('text_geography', 0)}")
        print(f"  study-location phrase points plotted: {plotted_type_counts.get('city', 0)}")
        print(f"  plotted point count: {len(region_points)}")
        print(f"  labeled point count: {labels_drawn}")
        print(f"  map extent/bounds: {extent}")
        print(f"  basemap bounds: {basemap_extent or 'none'}")
        print(f"  generated: {'yes' if generated_path and generated_path.exists() else 'no'}")
        print(f"  output path: {generated_path or ''}")
        print(f"  final_map_generated: {'yes' if final_institution_path and final_institution_path.exists() else 'no'}")
        print(f"  final_map_output_path: {final_institution_path or ''}")
        print(f"  text_debug_map_generated: {'yes' if text_debug_path and text_debug_path.exists() else 'no'}")
        print(f"  text_debug_output_path: {text_debug_path or ''}")
        print(f"  skip reason: {skip_reason or 'none'}")
        print(f"  warnings: {' | '.join(warnings) if warnings else 'none'}")

    if discovery.country_layer:
        world_adm0_ok, world_adm0_errors = validate_adm0_world_layer("World Publication Geography", discovery.country_layer.records)
        save_or_skip(
            "world",
            save_layer_map(
                discovery.country_layer.records,
                dict(country_counts),
                points,
                "World Publication Geography: Raw Counts",
                f"{slug}_geography_heatmap_world",
                generated,
                (-180, 180, -60, 85),
                max_labels=MAX_MAP_LABELS["world"],
                max_points=80,
            ) if world_adm0_ok else None,
            " | ".join(world_adm0_errors) if not world_adm0_ok else "country layer unavailable",
            "country choropleth",
            len(country_counts),
            "country name/ISO",
            len([value for value in country_counts.values() if value > 0]),
            0,
            False,
            discovery.country_layer.path if discovery.country_layer else "",
            0,
            len(discovery.country_layer.records),
            True,
        )
        detected_counts = {idx: 1 for idx, value in country_counts.items() if value > 0}
        save_or_skip(
            "world_detected",
            save_layer_map(
                discovery.country_layer.records,
                detected_counts,
                [],
                "World Publication Geography: Detected / Not Detected",
                f"{slug}_geography_detected_world",
                generated,
                (-180, 180, -60, 85),
                max_labels=0,
                max_points=0,
            ) if world_adm0_ok else None,
            " | ".join(world_adm0_errors) if not world_adm0_ok else "country layer unavailable",
            "country choropleth",
            len(detected_counts),
            "country name/ISO",
            len(detected_counts),
            0,
            False,
            discovery.country_layer.path if discovery.country_layer else "",
            0,
            len(discovery.country_layer.records),
            True,
        )
        city_world_label_audit: dict[str, object] = {}
        write_world_city_basemap_debug(slug, discovery.country_layer.records, generated)
        world_basemap_ok, world_basemap_errors = validate_required_basemap_countries(
            "World city",
            discovery.country_layer.records,
            REQUIRED_WORLD_BASEMAP_COUNTRIES,
        )
        city_world_path = None
        if world_basemap_ok:
            city_world_path = save_point_map(
                points,
                "City Publication Geography",
                f"{slug}_geography_points_cities",
                generated,
                (-180, 180, -60, 85),
                labels_enabled=LABEL_CITY_POINTS,
                label_top_n=LABEL_TOP_CITIES,
                label_kind="city",
                max_points=220,
                basemap_records=discovery.country_layer.records,
                label_audit=city_world_label_audit,
            )
        else:
            skipped.extend(f"city_points_world: {error}" for error in world_basemap_errors)
        write_city_map_debug(
            slug,
            f"{slug}_geography_points_cities",
            "City Publication Geography",
            city_candidates,
            points,
            city_world_label_audit,
            city_world_path,
            generated,
        )
        save_or_skip(
            "city_points_world",
            city_world_path,
            "city/place point data unavailable",
            "city point map",
            len(points),
            "latitude/longitude",
            0,
            max(0, len(points) - len(aggregate_points(points))),
            LABEL_CITY_POINTS,
            discovery.country_layer.path if discovery.country_layer else "",
            len(aggregate_points(points)),
            len(discovery.country_layer.records),
            True,
            city_world_label_audit,
        )
        def region_iso_predicate(region_key: str) -> Callable[[dict[str, object]], bool]:
            iso_set = REGION_ISO3.get(region_key, set())
            return lambda record: basemap_country_iso3(record) in iso_set

        regional_specs = [
            ("europe", "Europe", (-25, 45, 34, 72), region_iso_predicate("europe")),
            ("east_asia", "East Asia", (72, 150, 15, 55), region_iso_predicate("east_asia")),
            ("south_asia", "South Asia", (58, 100, -2, 38), region_iso_predicate("south_asia")),
            ("oceania", "Oceania", (105, 180, -50, 5), region_iso_predicate("oceania")),
            ("latin_america", "Latin America", (-118, -30, -58, 35), region_iso_predicate("latin_america")),
            ("sub_saharan_africa", "Sub-Saharan Africa", (-20, 55, -36, 18), region_iso_predicate("sub_saharan_africa")),
            ("north_africa", "North Africa", (-20, 40, 15, 38), region_iso_predicate("north_africa")),
            ("middle_east", "Middle East", (25, 65, 10, 43), region_iso_predicate("middle_east")),
            ("north_america", "North America", (-172, -50, 7, 84), region_iso_predicate("north_america")),
        ]
        regions = plan.get("regions", {}) if isinstance(plan.get("regions"), dict) else {}
        for region_key, title, extent, predicate in regional_specs:
            records, counts = indexed_subset(discovery.country_layer.records, predicate)
            countries_in_region = "; ".join(sorted(filter(None, (basemap_country_name(record) for record in records))))
            if region_key in REGIONAL_MAPS_REQUIRED:
                append_regional_basemap_debug(region_key, title, records, predicate)
            enabled = bool(regions.get(region_key))
            if not enabled:
                skipped.append(f"{region_key}: no detected countries in region")
                region_candidates = selected_points_for_region(city_candidates, region_key, extent, final_map=False)
                region_points = selected_points_for_region(points, region_key, extent, final_map=False)
                institution_region_points = selected_points_for_region(final_institution_points, region_key, extent, final_map=True)
                regional_city_debug_rows.append(
                    {
                        "region": region_key,
                        "countries_in_region": countries_in_region,
                        "candidate_city_points": len(region_candidates),
                        "plotted_city_points": len(region_points),
                        "basemap_loaded": "yes" if records else "no",
                        "basemap_country_count": len(records),
                        "generated": "no",
                        "output_path": "",
                        "skip_reason": "no detected countries in region",
                    }
                )
                suspicious_count = append_point_assignment_rows(region_key, title, extent, region_candidates, region_points, institution_region_points)
                warnings = []
                if region_points and not records:
                    warnings.append(f"WARNING: {title} points plotted but ADM0 basemap was not drawn.")
                log_regional_diagnostic(
                    title,
                    enabled,
                    countries_in_region,
                    records,
                    extent,
                    len(points),
                    region_points,
                    region_candidates,
                    len(final_institution_points),
                    len(institution_region_points),
                    None,
                    None,
                    None,
                    0,
                    "no detected countries in region",
                    warnings,
                )
                if region_key in REGIONAL_MAPS_REQUIRED:
                    countries_expected = REGIONAL_MAPS_REQUIRED.get(region_key, {}).get("countries", set())
                    regional_map_debug_rows.append(
                        {
                            "region": title,
                            "countries_expected": "; ".join(sorted(str(item).upper() for item in countries_expected)) if countries_expected else "region metadata predicate",
                            "countries_loaded": countries_in_region,
                            "basemap_loaded": "yes" if records else "no",
                            "basemap_polygon_count": sum(basemap_polygon_count(record) for record in records),
                            "city_points_available": len(region_candidates),
                            "institution_points_available": len(institution_region_points),
                            "institution_location_rows_available": len(final_institution_points),
                            "institution_location_rows_after_region_filter": len(institution_region_points),
                            "institution_points_plotted": 0,
                            "final_map_generated": "no",
                            "final_map_output_path": "",
                            "text_debug_map_generated": "no",
                            "text_debug_output_path": "",
                            "plotted_points": len(region_points),
                            "labels_drawn": 0,
                            "generated": "no",
                            "output_path": "",
                            "skip_reason": "no detected countries in region",
                            "warning": " | ".join(warnings),
                        }
                    )
                    regional_qa_rows.append(
                        {
                            "region": title,
                            "generated": "no",
                            "visual_file": "",
                            "basemap_ok": "yes" if records else "no",
                            "point_count": len(region_points),
                            "institution_point_count": len(institution_region_points),
                            "text_geo_point_count": len(region_candidates),
                            "suspicious_point_count": suspicious_count,
                            "labels_drawn": 0,
                            "skip_reason": "no detected countries in region",
                            "warning_summary": " | ".join(warnings),
                            "qa_status": "fail" if region_points or institution_region_points else "warning",
                        }
                    )
                continue
            region_points = selected_points_for_region(points, region_key, extent, final_map=False)
            region_candidates = selected_points_for_region(city_candidates, region_key, extent, final_map=False)
            institution_region_points = selected_points_for_region(final_institution_points, region_key, extent, final_map=True)
            if region_key == "south_asia":
                south_asia_ok, south_asia_errors = validate_required_basemap_countries("South Asia city", records, SOUTH_ASIA_REQUIRED_ISO3)
                if not south_asia_ok:
                    skipped.extend(f"south_asia: {error}" for error in south_asia_errors)
                    suspicious_count = append_point_assignment_rows(region_key, title, extent, region_candidates, region_points, institution_region_points)
                    warnings = ["South Asia required ADM0 validation failed", *south_asia_errors]
                    log_regional_diagnostic(
                        title,
                        enabled,
                        countries_in_region,
                        records,
                        extent,
                        len(points),
                        region_points,
                        region_candidates,
                        len(final_institution_points),
                        len(institution_region_points),
                        None,
                        None,
                        None,
                        0,
                        " | ".join(south_asia_errors),
                        warnings,
                    )
                    regional_city_debug_rows.append(
                        {
                            "region": region_key,
                            "countries_in_region": countries_in_region,
                            "candidate_city_points": len(region_candidates),
                            "plotted_city_points": len(region_points),
                            "basemap_loaded": "yes" if records else "no",
                            "basemap_country_count": len(records),
                            "generated": "no",
                            "output_path": "",
                            "skip_reason": " | ".join(south_asia_errors),
                        }
                    )
                    regional_map_debug_rows.append(
                        {
                            "region": title,
                            "countries_expected": "; ".join(sorted(SOUTH_ASIA_REQUIRED_ISO3.values())),
                            "countries_loaded": countries_in_region,
                            "basemap_loaded": "yes" if records else "no",
                            "basemap_polygon_count": sum(basemap_polygon_count(record) for record in records),
                            "city_points_available": len(region_candidates),
                            "institution_points_available": len([point for point in final_institution_points if point_in_extent(point, extent)]),
                            "institution_location_rows_available": len(final_institution_points),
                            "institution_location_rows_after_region_filter": len(institution_region_points),
                            "institution_points_plotted": 0,
                            "final_map_generated": "no",
                            "final_map_output_path": "",
                            "text_debug_map_generated": "no",
                            "text_debug_output_path": "",
                            "plotted_points": 0,
                            "labels_drawn": 0,
                            "generated": "no",
                            "output_path": "",
                            "skip_reason": " | ".join(south_asia_errors),
                            "warning": "South Asia required ADM0 validation failed",
                        }
                    )
                    regional_qa_rows.append(
                        {
                            "region": title,
                            "generated": "no",
                            "visual_file": "",
                            "basemap_ok": "no",
                            "point_count": len(region_points),
                            "institution_point_count": len(institution_region_points),
                            "text_geo_point_count": len(region_candidates),
                            "suspicious_point_count": suspicious_count,
                            "labels_drawn": 0,
                            "skip_reason": " | ".join(south_asia_errors),
                            "warning_summary": " | ".join(warnings),
                            "qa_status": "fail",
                        }
                    )
                    continue
            region_heatmap_path = save_layer_map(
                records,
                counts,
                subset_points(extent),
                f"{title} Publication Geography",
                f"{slug}_geography_heatmap_{region_key}",
                generated,
                extent,
                max_labels=MAX_MAP_LABELS["region"],
                max_points=120,
            )
            save_or_skip(
                region_key,
                region_heatmap_path,
                f"no {title} country records in country layer",
                "country choropleth",
                len(counts),
                "country name/region",
                len([value for value in counts.values() if value > 0]),
                0,
                False,
                discovery.country_layer.path if discovery.country_layer else "",
                0,
                len(records),
                True,
            )
            basemap_polygon_total = sum(basemap_polygon_count(record) for record in records)
            basemap_ok = bool(records and basemap_polygon_total > 0)
            region_institution_path = None
            region_text_debug_path = None
            region_label_audit: dict[str, object] = {}
            text_label_audit: dict[str, object] = {}
            if basemap_ok and institution_region_points:
                region_side_panel_rows, region_side_panel_meta = institution_side_panel_rows(
                    title,
                    institution_region_points,
                    sum(int(point.get("count") or 0) for point in institution_region_points),
                )
                region_institution_path = save_point_map(
                    institution_region_points,
                    f"{title} Institution Publication Geography",
                    f"{slug}_geography_points_region_{region_key}_institutions",
                    generated,
                    extent,
                    labels_enabled=LABEL_INSTITUTION_POINTS,
                    label_top_n=LABEL_TOP_INSTITUTIONS,
                    label_kind="institution",
                    max_points=160,
                    basemap_records=records,
                    label_audit=region_label_audit,
                    side_panel_rows=region_side_panel_rows,
                    side_panel_meta=region_side_panel_meta,
                    caption_text="Points reflect publication-affiliated institution locations from Scopus/OpenAlex enrichment where available, not prevalence or disease burden.",
                )
                save_or_skip(
                    f"region_{region_key}_institution_points",
                    region_institution_path,
                    f"{title} institution point map unavailable",
                    "institution point map",
                    len(institution_region_points),
                    "institution latitude/longitude",
                    0,
                    0,
                    LABEL_INSTITUTION_POINTS,
                    discovery.country_layer.path if discovery.country_layer else "",
                    len(aggregate_points(institution_region_points)),
                    len(records),
                    True,
                    region_label_audit,
                )
            elif institution_region_points and not basemap_ok:
                skipped.append(f"region_{region_key}_institution_points: {title} ADM0 basemap unavailable; map not generated")
            if basemap_ok and region_points:
                region_text_debug_path = save_point_map(
                    region_points,
                    f"{title} Text-Mentioned Geography, Debug",
                    f"{slug}_geography_points_region_{region_key}_text_mentions_debug",
                    generated,
                    extent,
                    labels_enabled=LABEL_CITY_POINTS,
                    label_top_n=LABEL_TOP_CITIES,
                    label_kind="city",
                    max_points=160,
                    basemap_records=records,
                    label_audit=text_label_audit,
                    caption_text="Points reflect geographic terms detected in publication text, not affiliation locations, prevalence, or disease burden.",
                )
                write_city_map_debug(
                    slug,
                    f"{slug}_geography_points_region_{region_key}_text_mentions_debug",
                    f"{title} Text-Mentioned Geography, Debug",
                    region_candidates,
                    region_points,
                    text_label_audit,
                    region_text_debug_path,
                    generated,
                )
                save_or_skip(
                    f"region_{region_key}_text_mentions_debug",
                    region_text_debug_path,
                    f"{title} text-mentioned geography debug map unavailable",
                    "text geography debug point map",
                    len(region_candidates),
                    "latitude/longitude",
                    0,
                    len([point for point in region_candidates if point not in region_points]),
                    LABEL_CITY_POINTS,
                    discovery.country_layer.path if discovery.country_layer else "",
                    len(aggregate_points(region_points)),
                    len(records),
                    True,
                    text_label_audit,
                )
            regional_city_debug_rows.append(
                {
                    "region": region_key,
                    "countries_in_region": countries_in_region,
                    "candidate_city_points": len(region_candidates),
                    "plotted_city_points": len(region_points),
                    "basemap_loaded": "yes" if records else "no",
                    "basemap_country_count": len(records),
                    "generated": "yes" if region_text_debug_path and region_text_debug_path.exists() else "no",
                    "output_path": str(region_text_debug_path or ""),
                    "skip_reason": "" if region_text_debug_path and region_text_debug_path.exists() else "no regional text geography points" if not region_points else "regional text geography debug map unavailable",
                }
            )
            if region_key in REGIONAL_MAPS_REQUIRED:
                countries_expected = REGIONAL_MAPS_REQUIRED.get(region_key, {}).get("countries", set())
                generated_path = region_institution_path
                suspicious_count = append_point_assignment_rows(region_key, title, extent, region_candidates, region_points, institution_region_points)
                basemap_extent = layer_extent(records)
                warning_items: list[str] = []
                if (region_points or institution_region_points) and not basemap_ok:
                    warning_items.append(f"WARNING: {title} points plotted but ADM0 basemap was not drawn.")
                    print(warning_items[-1])
                if records and not extents_overlap(basemap_extent, extent):
                    warning_items.append(f"WARNING: {title} basemap loaded but outside current axis extent.")
                    print(warning_items[-1])
                if suspicious_count:
                    warning_items.append(f"suspicious regional labels/points flagged: {suspicious_count}")
                if len(region_candidates) and len(region_points) == 0:
                    warning_items.append("all regional city/text geography candidates were filtered out")
                if region_points and not any(point_expected_regions(point) and region_key in point_expected_regions(point) for point in region_points):
                    warning_items.append("regional plotted points assigned primarily by coordinate bbox rather than country/ISO3")
                coordinate_bbox_excluded = sum(1 for point in [*region_candidates, *final_institution_points] if point_assignment_method(point, region_key, extent) == "coordinate_bbox")
                if final_institution_points and not institution_region_points:
                    warning_items.append("institution data exists but no institution points assigned to this region")
                final_map_source = "institution" if region_institution_path and region_institution_path.exists() else "text_debug_only" if region_text_debug_path and region_text_debug_path.exists() else "none"
                final_map_point_count = len(institution_region_points) if final_map_source == "institution" else 0
                if generated_path and generated_path.exists():
                    skip_reason = ""
                elif not institution_region_points:
                    skip_reason = "no regional institution points from institution_locations.csv"
                elif not basemap_ok:
                    skip_reason = "regional ADM0 basemap unavailable"
                else:
                    skip_reason = "regional institution map unavailable"
                log_regional_diagnostic(
                    title,
                    enabled,
                    countries_in_region,
                    records,
                    extent,
                    len(points),
                    region_points,
                    region_candidates,
                    len(final_institution_points),
                    len(institution_region_points),
                    generated_path,
                    region_institution_path,
                    region_text_debug_path,
                    int((region_label_audit if region_institution_path else text_label_audit).get("labels_drawn_count", 0)),
                    skip_reason,
                    warning_items,
                )
                regional_map_debug_rows.append(
                    {
                        "region": title,
                        "countries_expected": "; ".join(sorted(str(item).upper() for item in countries_expected)) if countries_expected else "region metadata predicate",
                        "countries_loaded": countries_in_region,
                        "basemap_loaded": "yes" if records else "no",
                        "basemap_polygon_count": basemap_polygon_total,
                        "city_points_available": len(region_candidates),
                        "institution_points_available": len(institution_region_points),
                        "institution_location_rows_available": len(final_institution_points),
                        "institution_location_rows_after_region_filter": len(institution_region_points),
                        "institution_points_plotted": final_map_point_count,
                        "final_map_generated": "yes" if region_institution_path and region_institution_path.exists() else "no",
                        "final_map_output_path": str(region_institution_path or ""),
                        "text_debug_map_generated": "yes" if region_text_debug_path and region_text_debug_path.exists() else "no",
                        "text_debug_output_path": str(region_text_debug_path or ""),
                        "plotted_points": final_map_point_count,
                        "labels_drawn": int((region_label_audit if region_institution_path else text_label_audit).get("labels_drawn_count", 0)),
                        "generated": "yes" if generated_path and generated_path.exists() else "no",
                        "output_path": str(generated_path or ""),
                        "skip_reason": skip_reason,
                        "warning": " | ".join(warning_items),
                    }
                )
                if generated_path and generated_path.exists() and basemap_ok and final_map_point_count and not warning_items:
                    qa_status = "pass"
                elif not generated_path or not generated_path.exists() or not basemap_ok:
                    qa_status = "fail"
                else:
                    qa_status = "warning"
                regional_qa_rows.append(
                    {
                        "region": title,
                        "generated": "yes" if generated_path and generated_path.exists() else "no",
                        "visual_file": str(generated_path or ""),
                        "basemap_ok": "yes" if basemap_ok else "no",
                        "point_count": len(region_points),
                        "institution_point_count": len(institution_region_points),
                        "text_geo_point_count": len(region_candidates),
                        "suspicious_point_count": suspicious_count,
                        "labels_drawn": int((region_label_audit if region_institution_path else text_label_audit).get("labels_drawn_count", 0)),
                        "skip_reason": skip_reason,
                        "warning_summary": " | ".join(warning_items),
                        "qa_status": qa_status,
                        "institution_map_generated": "yes" if region_institution_path and region_institution_path.exists() else "no",
                        "text_mention_map_generated": "yes" if region_text_debug_path and region_text_debug_path.exists() else "no",
                        "final_map_source": final_map_source,
                        "coordinate_bbox_points_excluded": coordinate_bbox_excluded,
                        "final_map_point_count": final_map_point_count,
                        "final_map_institution_count": len(institution_region_points) if final_map_source == "institution" else 0,
                        "final_map_text_geo_count": 0,
                    }
                )
        if plan.get("world_regions"):
            region_field = next((field for field in ("CONTINENT", "REGION_UN", "SUBREGION", "REGION_WB") if field in discovery.country_layer.fields), "")
            if region_field:
                region_counts: Counter[str] = Counter()
                for idx, record in enumerate(discovery.country_layer.records):
                    if idx in country_counts:
                        region_counts[str(record_value(record, region_field))] += country_counts[idx]
                region_record_counts = {idx: int(region_counts[str(record_value(record, region_field))]) for idx, record in enumerate(discovery.country_layer.records) if str(record_value(record, region_field))}
                save_or_skip(
                    "world_regions",
                    save_layer_map(
                        discovery.country_layer.records,
                        region_record_counts,
                        [],
                        "World Regions Publication Geography",
                        f"{slug}_geography_overview_world_regions",
                        generated,
                        (-180, 180, -60, 85),
                        max_labels=MAX_MAP_LABELS["world"],
                        max_points=0,
                    ),
                    "region grouping failed",
                    "country choropleth",
                    len(region_record_counts),
                    region_field,
                    len([value for value in region_record_counts.values() if value > 0]),
                    0,
                    False,
                    discovery.country_layer.path if discovery.country_layer else "",
                    0,
                    len(discovery.country_layer.records),
                    True,
                )
            else:
                skipped.append("world_regions: no region field available for grouping")
        else:
            skipped.append("world_regions: no detected countries")
    else:
        skipped.extend(["world: country layer unavailable", "regional maps: country layer unavailable", "world_regions: country layer unavailable"])

    if admin1_layer and plan.get("us"):
        us_records = lower48_us_admin1_records(admin1_layer)
        admin_original = {id(record): idx for idx, record in enumerate(admin1_layer.records)}
        us_counts = {local_idx: state_counts[admin_original[id(record)]] for local_idx, record in enumerate(us_records) if admin_original[id(record)] in state_counts}
        us_points = subset_points((-130, -65, 24, 50), {"united states"})
        save_or_skip(
            "us",
            save_layer_map(us_records, us_counts, us_points, "United States Publication Geography by State", f"{slug}_geography_heatmap_us", generated, (-126, -66, 24, 50), max_labels=MAX_MAP_LABELS["us"], max_points=160),
            "state/admin-1 layer unavailable",
            "subnational choropleth",
            len(us_counts),
            "state/admin1 name",
            len([value for value in us_counts.values() if value > 0]),
            max(0, len(state_counts) - len(us_counts)),
            False,
            admin1_layer.path if admin1_layer else "",
            0,
            len(us_records),
            True,
        )
    elif not plan.get("us"):
        skipped.append("us: no U.S. geography detected")
    else:
        skipped.append("us: state/admin-1 layer unavailable")

    if admin1_layer and plan.get("texas"):
        texas_source = discovery.census_county_layer or admin1_layer
        texas_extent = (-107, -93, 25, 37)
        texas_records = [
            record for record in texas_source.records
            if (
                state_fips(record) == "48"
                or map_norm(record_value(record, "NAME", "name", "region", "STUSPS", "state")) == "texas"
                or str(record_value(record, "iso_3166_2")).upper() == "US-TX"
            )
            and record_intersects_extent(record, texas_extent)
        ]
        texas_points = subset_points(texas_extent, {"united states"})
        texas_city_candidates = subset_city_candidates(texas_extent, {"united states"})
        county_terms_present = any(str(row.get("geo_type") or "").lower() == "county" and map_norm(row.get("state", "") or row.get("admin1_name", "")) == "texas" for _idx, row in geo_df.iterrows())
        texas_label_audit: dict[str, object] = {}
        texas_path = None
        if texas_points:
            texas_path = save_point_map(
                texas_points,
                "Texas City Publication Geography",
                f"{slug}_geography_points_texas_cities",
                generated,
                texas_extent,
                labels_enabled=LABEL_CITY_POINTS,
                label_top_n=LABEL_TOP_CITIES,
                label_kind="city",
                max_points=160,
                basemap_records=texas_records,
                label_audit=texas_label_audit,
            )
        if texas_city_candidates:
            write_city_map_debug(
                slug,
                f"{slug}_geography_points_texas_cities",
                "Texas City Publication Geography",
                texas_city_candidates,
                texas_points,
                texas_label_audit,
                texas_path,
                generated,
            )
        if texas_points:
            save_or_skip(
                "texas_city_points",
                texas_path,
                "Texas point data unavailable",
                "city point map",
                len(texas_city_candidates),
                "latitude/longitude",
                0,
                len([point for point in texas_city_candidates if point not in texas_points]),
                LABEL_CITY_POINTS,
                texas_source.path,
                len(aggregate_points(texas_points)),
                len(texas_records),
                bool(texas_records),
                texas_label_audit,
            )
        elif county_terms_present and discovery.census_county_layer:
            skipped.append("texas_county: county terms detected but county-level polygon counts are not available")
        elif texas_city_candidates:
            skipped.append("texas: only non-city geography candidates detected for city point map")
        else:
            skipped.append("texas: no county-level data or city points available")
    elif not plan.get("texas"):
        skipped.append("texas: no Texas geography detected")
    else:
        skipped.append("texas: state/admin-1 layer unavailable")

    if discovery.country_layer and isinstance(plan.get("country_maps"), list):
        country_layer_original = {id(record): idx for idx, record in enumerate(discovery.country_layer.records)}
        admin_records_by_country: dict[str, list[dict[str, object]]] = defaultdict(list)
        iso_to_country_key = {str(meta.get("iso_a3", "")).upper(): str(meta.get("country_key", "")) for meta in country_meta.values() if meta.get("iso_a3")}
        if admin1_layer:
            for record in admin1_layer.records:
                record_iso = str(record_value(record, "shapeGroup", "boundaryISO", "ISO_A3", "ADM0_A3")).upper()
                key = iso_to_country_key.get(record_iso) or record_country_key(record)
                if key:
                    admin_records_by_country[key].append(record)
        for country_row in plan["country_maps"]:
            country_key = str(country_row.get("country_key") or "")
            country_index = int(country_row.get("country_index"))
            country_name = str(country_row.get("country") or country_key).strip()
            country_iso3 = str(country_meta.get(country_index, {}).get("iso_a3") or "").upper()
            output_key = re.sub(r"[^a-z0-9]+", "_", country_key).strip("_") or str(country_index)
            country_text_debug_stem = f"{slug}_geography_points_country_{output_key}_text_mentions_debug"
            local_admin_layer = find_country_admin_layer(country_iso3, discovery.invalid_layers, discovery.fallback_choices) if country_iso3 else None
            provider_selection = getattr(local_admin_layer, "provider_selection", None) if local_admin_layer else None
            provider_name = str(getattr(provider_selection, "provider", "") or "")
            provider_level = str(getattr(provider_selection, "admin_level", "") or "")
            country_output_key = output_key
            if country_iso3 == "GBR" and provider_name == "ONS.gov.uk":
                country_output_key = f"{output_key}_ons_lad"
            elif country_iso3 == "GBR" and provider_level:
                country_output_key = f"{output_key}_{provider_level.lower()}"
            country_records = local_admin_layer.records if local_admin_layer else admin_records_by_country.get(country_key) or [discovery.country_layer.records[country_index]]
            if country_key == "united states" and admin1_layer:
                country_records = us_admin1_records(admin1_layer) or country_records
            local_counts: dict[int, int] = {}
            if country_records and country_records[0] in discovery.country_layer.records:
                local_counts = {local_idx: country_counts[country_layer_original[id(record)]] for local_idx, record in enumerate(country_records) if country_layer_original[id(record)] in country_counts}
            elif local_admin_layer:
                local_lookup = match_records(local_admin_layer, ("shapeName", "shapeISO", "name", "NAME", "admin1", "boundaryName"))
                for _idx, row in geo_df.iterrows():
                    row_country = map_norm(row.get("country", ""))
                    if row_country and row_country != country_key:
                        continue
                    for key in geography_term_keys(row) | {map_norm(row.get("admin1_name", "")), map_norm(row.get("state", ""))}:
                        if key in local_lookup:
                            local_counts[local_lookup[key]] = local_counts.get(local_lookup[key], 0) + int(row.get("count") or 1)
                            break
                for local_idx, record in enumerate(local_admin_layer.records):
                    local_keys = record_name_keys(record, ("shapeName", "shapeISO", "name", "NAME", "admin1"))
                    for key in local_keys:
                        if key in admin1_lookup:
                            source_idx = admin1_lookup[key]
                            if source_idx in state_counts:
                                local_counts[local_idx] = state_counts[source_idx]
                                break
            elif admin1_layer:
                admin_original = {id(record): idx for idx, record in enumerate(admin1_layer.records)}
                local_counts = {local_idx: state_counts[admin_original[id(record)]] for local_idx, record in enumerate(country_records) if admin_original[id(record)] in state_counts}
            extent = layer_extent(country_records)
            country_points = [
                point for point in points
                if point.get("country_index") == country_index and (extent is None or point_in_extent(point, extent))
            ]
            country_institution_points = [
                point for point in final_institution_points
                if (
                    map_norm(point.get("country", "")) in {country_key, map_norm(country_iso3)}
                    or (country_iso3 == "GBR" and map_norm(point.get("country", "")) in {"uk", "united kingdom", "gbr"})
                )
                and (extent is None or point_in_extent(point, extent))
            ]
            side_panel_rows, side_panel_meta, side_panel_debug = country_institution_side_panel_rows(
                country_name,
                country_key,
                country_iso3,
                country_institution_points,
                int(country_row.get("total_records") or country_row.get("total_geography_frequency") or 0),
                len(aggregate_points(country_points)),
            )
            if local_admin_layer and (country_points or country_institution_points):
                name_field = layer_field(local_admin_layer, ONS_NAME_FIELDS) or next((field for field in ("shapeName", "NAME", "name", "admin1", "boundaryName") if field in local_admin_layer.fields), "")
                code_field = layer_field(local_admin_layer, ONS_CODE_FIELDS) or next((field for field in ("shapeISO", "iso_3166_2", "GID_1", "CODE", "code") if field in local_admin_layer.fields), "")
                boundary_level = provider_level or "ADM1"
                spatial_counts, join_debug_rows = spatial_subnational_counts(
                    country_points + country_institution_points,
                    country_records,
                    name_field,
                    code_field,
                    boundary_level,
                )
                write_subnational_join_debug(slug, country_key, join_debug_rows, generated)
                if spatial_counts:
                    local_counts = spatial_counts
                    print(
                        f"{country_name} subnational join: boundary={local_admin_layer.path}; "
                        f"level={boundary_level}; spatially joined={sum(1 for row in join_debug_rows if row['included_in_choropleth'] == 'yes')}; "
                        f"unresolved={sum(1 for row in join_debug_rows if row['included_in_choropleth'] == 'no')}."
                    )
                elif country_key == "austria":
                    skipped.append("Austria choropleth skipped: no city/institution points joined spatially to Austria boundary polygons")
                    local_counts = {}
            country_city_candidates = [
                point for point in city_candidates
                if point.get("country_index") == country_index and (extent is None or point_in_extent(point, extent))
            ]
            uk_join_rows: list[dict[str, object]] = []
            uk_unmatched_rows: list[dict[str, object]] = []
            uk_debug_notes: list[str] = []
            if country_iso3 == "GBR" and local_admin_layer:
                uk_debug_notes.append(str(getattr(provider_selection, "reason", "")))
                write_uk_boundary_provider_debug(slug, provider_selection, local_admin_layer, generated, uk_debug_notes)
                if provider_name == "ONS.gov.uk":
                    name_field = layer_field(local_admin_layer, ONS_NAME_FIELDS)
                    code_field = layer_field(local_admin_layer, ONS_CODE_FIELDS)
                    local_counts, uk_join_rows, uk_unmatched_rows = join_points_to_polygons(
                        country_points + country_institution_points,
                        country_records,
                        name_field,
                        code_field,
                    )
                    write_uk_ons_lad_join_debug(slug, uk_join_rows, uk_unmatched_rows, generated)
                    top_areas = sorted(
                        (
                            (
                                str(record_value(country_records[idx], name_field) if name_field else idx),
                                int(count),
                            )
                            for idx, count in local_counts.items()
                        ),
                        key=lambda item: (-item[1], item[0]),
                    )[:8]
                    print("UK detailed map provider: ONS.gov.uk Local Authority Districts.")
                    print("Fallback not used.")
                    print(
                        "UK ONS LAD join: "
                        f"polygons loaded={len(country_records)}; joined={len(uk_join_rows)}; unmatched={len(uk_unmatched_rows)}; "
                        f"top areas={top_areas}."
                    )
                elif provider_level == "ADM2":
                    print("ONS.gov.uk boundaries unavailable; falling back to geoBoundaries GBR ADM2.")
                elif provider_level == "ADM1":
                    print("ONS.gov.uk boundaries unavailable; falling back to geoBoundaries GBR ADM1.")
            if local_counts:
                title = (
                    uk_boundary_title(country_name, provider_selection, local_admin_layer)
                    if country_iso3 == "GBR" and local_admin_layer
                    else country_publication_title(country_name)
                )
                country_path = save_layer_map(
                    country_records,
                    local_counts,
                    [],
                    title,
                    f"{slug}_geography_heatmap_country_{country_output_key}",
                    generated,
                    extent,
                    max_labels=MAX_MAP_LABELS["country"],
                    max_points=0,
                    side_panel_rows=side_panel_rows if side_panel_rows or country_institution_points else None,
                    side_panel_meta=side_panel_meta,
                )
                for row in side_panel_debug:
                    row["map_filename"] = f"{slug}_geography_heatmap_country_{country_output_key}.png"
                country_side_panel_debug_rows.extend(side_panel_debug)
                save_or_skip(
                    f"country_{output_key}",
                    country_path,
                    f"{country_name} geometry unavailable",
                    "subnational choropleth",
                    len(local_counts),
                    "ADM1 name/shapeName",
                    len([value for value in local_counts.values() if value > 0]),
                    0,
                    False,
                    local_admin_layer.path if local_admin_layer else discovery.country_layer.path if discovery.country_layer else "",
                    0,
                    len(country_records),
                    True,
                    {
                        "boundary_provider_selected": provider_name,
                        "boundary_type_detected": uk_boundary_type_from_layer(local_admin_layer, provider_selection) if country_iso3 == "GBR" and local_admin_layer else "",
                        "join_method": "point_in_polygon" if country_iso3 == "GBR" and provider_name == "ONS.gov.uk" else "name/code join",
                        "city_or_institution_points_joined": len(uk_join_rows) if country_iso3 == "GBR" else "",
                        "unmatched_city_or_institution_points": len(uk_unmatched_rows) if country_iso3 == "GBR" else "",
                    } if country_iso3 == "GBR" else None,
                )
            else:
                country_label_audit: dict[str, object] = {}
                country_path = save_point_map(
                    country_points,
                    f"{country_name} Text-Mentioned Geography, Debug",
                    country_text_debug_stem,
                    generated,
                    extent,
                    labels_enabled=LABEL_CITY_POINTS,
                    label_top_n=LABEL_TOP_CITIES,
                    label_kind="city",
                    max_points=160,
                    basemap_records=country_records,
                    label_audit=country_label_audit,
                    side_panel_rows=None,
                    side_panel_meta=None,
                )
                for row in side_panel_debug:
                    row["map_filename"] = f"{country_text_debug_stem}.png"
                country_side_panel_debug_rows.extend(side_panel_debug)
                if country_city_candidates:
                    write_city_map_debug(
                        slug,
                        country_text_debug_stem,
                        f"{country_name} Text-Mentioned Geography, Debug",
                        country_city_candidates,
                        country_points,
                        country_label_audit,
                        country_path,
                        generated,
                    )
                save_or_skip(
                    f"country_{output_key}_points",
                    country_path,
                    f"{country_name} ADM1 join unavailable and no point coordinates found",
                    "city point map",
                    len(country_points),
                    "latitude/longitude",
                    0,
                    len(country_points),
                    LABEL_CITY_POINTS,
                    local_admin_layer.path if local_admin_layer else discovery.country_layer.path if discovery.country_layer else "",
                    len(aggregate_points(country_points)),
                    len(country_records),
                    bool(country_records),
                    country_label_audit,
                )
                if country_key == "germany":
                    skipped.append("Germany map generated as point map with visible Germany basemap.")
            if country_key == "spain":
                skipped.append("Spain map generated as ADM1 choropleth" if local_counts else "Spain map generated as point map only; no ADM1 join available.")
            if local_counts and country_points:
                country_companion_label_audit: dict[str, object] = {}
                country_companion_path = save_point_map(
                    country_points,
                    f"{country_name} Text-Mentioned Geography, Debug",
                    country_text_debug_stem,
                    generated,
                    extent,
                    labels_enabled=LABEL_CITY_POINTS,
                    label_top_n=LABEL_TOP_CITIES,
                    label_kind="city",
                    max_points=160,
                    basemap_records=country_records,
                    label_audit=country_companion_label_audit,
                    side_panel_rows=None,
                    side_panel_meta=None,
                )
                if country_city_candidates:
                    write_city_map_debug(
                        slug,
                        country_text_debug_stem,
                        f"{country_name} Text-Mentioned Geography, Debug",
                        country_city_candidates,
                        country_points,
                        country_companion_label_audit,
                        country_companion_path,
                        generated,
                    )
                save_or_skip(
                    f"country_{output_key}_points",
                    country_companion_path,
                    f"{country_name} point map unavailable",
                    "city point map",
                    len(country_points),
                    "latitude/longitude",
                    0,
                    0,
                    LABEL_CITY_POINTS,
                    local_admin_layer.path if local_admin_layer else discovery.country_layer.path if discovery.country_layer else "",
                    len(aggregate_points(country_points)),
                    len(country_records),
                    bool(country_records),
                    country_companion_label_audit,
                )
                if country_key == "germany":
                    skipped.append("Germany map generated as ADM1 choropleth and separate point map with basemap.")
            if country_institution_points:
                uk_inst_label_audit: dict[str, object] = {}
                uk_inst_path = save_point_map(
                    country_institution_points,
                    f"{country_name} Institution Publication Geography",
                    f"{slug}_geography_points_country_{output_key}_institutions",
                    generated,
                    extent,
                    labels_enabled=LABEL_INSTITUTION_POINTS,
                    label_top_n=LABEL_TOP_INSTITUTIONS,
                    label_kind="institution",
                    max_points=160,
                    basemap_records=country_records,
                    label_audit=uk_inst_label_audit,
                    side_panel_rows=side_panel_rows if side_panel_rows or country_institution_points else None,
                    side_panel_meta=side_panel_meta,
                    caption_text="Points reflect publication-affiliated institution locations from Scopus/OpenAlex enrichment where available, not prevalence or disease burden.",
                )
                save_or_skip(
                    f"country_{output_key}_institution_points",
                    uk_inst_path,
                    f"{country_name} institution point map unavailable",
                    "institution point map",
                    len(country_institution_points),
                    "institution geocache latitude/longitude",
                    0,
                    0,
                    LABEL_INSTITUTION_POINTS,
                    local_admin_layer.path if local_admin_layer else discovery.country_layer.path if discovery.country_layer else "",
                    len(aggregate_points(country_institution_points)),
                    len(country_records),
                    bool(country_records),
                    uk_inst_label_audit,
                )

    if final_institution_points and discovery.country_layer:
        institution_label_audit: dict[str, object] = {}
        world_side_panel_rows, world_side_panel_meta = institution_side_panel_rows(
            "World",
            final_institution_points,
            sum(int(point.get("count") or 0) for point in final_institution_points),
        )
        save_or_skip(
            "institution_points_world",
            save_point_map(
                final_institution_points,
                "Institution Publication Geography",
                f"{slug}_geography_points_world_institutions",
                generated,
                (-180, 180, -60, 85),
                labels_enabled=LABEL_INSTITUTION_POINTS,
                label_top_n=LABEL_TOP_INSTITUTIONS,
                label_kind="institution",
                max_points=220,
                basemap_records=discovery.country_layer.records,
                label_audit=institution_label_audit,
                side_panel_rows=world_side_panel_rows,
                side_panel_meta=world_side_panel_meta,
                caption_text="Points reflect publication-affiliated institution locations from Scopus/OpenAlex enrichment where available, not prevalence or disease burden.",
            ),
            "institution geocoding unavailable",
            "institution point map",
            int(institution_meta.get("candidate_locations", 0)),
            "institution geocache latitude/longitude",
            0,
            int(institution_meta.get("unmatched_locations", 0)),
            LABEL_INSTITUTION_POINTS,
            institution_meta.get("cache_path", ""),
            int(institution_meta.get("aggregated_locations", 0)),
            len(discovery.country_layer.records),
            True,
            {
                **institution_label_audit,
                "geocoding_source_cache_used": institution_meta.get("cache_path", ""),
                "candidate_locations": institution_meta.get("candidate_locations", 0),
                "mapped_points": len(final_institution_points),
                "aggregated_institutions": len(aggregate_points(final_institution_points)),
                "unmatched_institutions": institution_meta.get("unmatched_locations", 0),
                "institution_source_file_used": institution_meta.get("core_path", ""),
            },
        )
    else:
        skipped.append(
            "institution_points_world: institution geocoding unavailable or no matched institutions "
            f"(cache: {institution_meta.get('cache_path', institution_geocache_path())}; "
            f"candidates: {institution_meta.get('candidate_locations', 0)}; "
            f"unmatched: {institution_meta.get('unmatched_locations', 0)})"
        )

    write_country_coverage_recommendations(
        slug,
        summary,
        country_meta,
        map_metadata,
        institution_counts_by_country_index(final_institution_points, country_meta),
        generated,
    )

    unmapped_path = processed_path(f"{slug}_unmapped_geography_terms.csv")
    pd.DataFrame(unmapped, columns=["original_term", "normalized_term", "canonical_name", "count", "geo_type", "state", "country", "latitude", "longitude", "attempted_map_type", "reason_not_mapped"]).to_csv(unmapped_path, index=False)
    remember_generated(generated, unmapped_path)
    regional_map_debug_path = processed_path(f"{slug}_regional_map_generation_debug.csv")
    pd.DataFrame(
        regional_map_debug_rows,
        columns=[
            "region", "countries_expected", "countries_loaded", "basemap_loaded", "basemap_polygon_count",
            "city_points_available", "institution_points_available",
            "institution_location_rows_available", "institution_location_rows_after_region_filter",
            "institution_points_plotted", "final_map_generated", "final_map_output_path",
            "text_debug_map_generated", "text_debug_output_path",
            "plotted_points", "labels_drawn",
            "generated", "output_path", "skip_reason", "warning",
        ],
    ).to_csv(regional_map_debug_path, index=False)
    remember_generated(generated, regional_map_debug_path)
    regional_point_assignment_path = processed_path(f"{slug}_regional_point_assignment_debug.csv")
    pd.DataFrame(
        regional_point_assignment_rows,
        columns=[
            "record_id", "source_title", "point_name", "display_label", "point_type", "latitude",
            "longitude", "country", "iso3", "admin1", "city", "institution_name", "publication_count",
            "assigned_region", "expected_region_from_country", "region_assignment_method",
            "included_in_regional_map", "exclusion_reason", "source_context", "confidence_score",
        ],
    ).to_csv(regional_point_assignment_path, index=False)
    remember_generated(generated, regional_point_assignment_path)
    regional_basemap_debug_path = processed_path(f"{slug}_regional_basemap_debug.csv")
    pd.DataFrame(
        regional_basemap_debug_rows,
        columns=[
            "region", "country_name", "iso3", "expected_in_region", "found_in_world_adm0",
            "included_in_basemap", "geometry_valid", "geometry_empty", "geometry_type",
            "polygon_count", "reason_excluded",
        ],
    ).to_csv(regional_basemap_debug_path, index=False)
    remember_generated(generated, regional_basemap_debug_path)
    regional_qa_path = processed_path(f"{slug}_regional_map_qa_summary.csv")
    pd.DataFrame(
        regional_qa_rows,
        columns=[
            "region", "generated", "visual_file", "basemap_ok", "point_count",
            "institution_point_count", "text_geo_point_count", "suspicious_point_count",
            "labels_drawn", "skip_reason", "warning_summary", "qa_status",
            "institution_map_generated", "text_mention_map_generated", "final_map_source",
            "coordinate_bbox_points_excluded", "final_map_point_count", "final_map_institution_count",
            "final_map_text_geo_count",
        ],
    ).to_csv(regional_qa_path, index=False)
    remember_generated(generated, regional_qa_path)
    print(f"Regional map diagnostics written to: {regional_map_debug_path}")
    print(f"Regional point assignment debug written to: {regional_point_assignment_path}")
    print(f"Regional basemap debug written to: {regional_basemap_debug_path}")
    print(f"Regional map QA summary written to: {regional_qa_path}")
    print("To inspect regional map issues, search the log for: REGIONAL MAP DIAGNOSTIC")
    print("To inspect missing maps, search for: skip reason")
    print(f"To inspect suspicious labels, open: {regional_point_assignment_path.name}")
    print(
        "Regional map log helper command:\n"
        'cd "/Users/roger.smith/Library/Application Support/DansBibGUI/logs"\n'
        'latest_log=$(ls -t pipeline_*.log | head -1)\n'
        'echo "Latest log: $latest_log"\n'
        'grep -nE "REGIONAL MAP DIAGNOSTIC|regional map|basemap|Europe|Sub-Saharan|East Asia|Latin America|North America|Middle East|skip reason|WARNING: .*points plotted|ADM0|qa_status|suspicious" "$latest_log" | tail -n 300'
    )
    side_panel_debug_path = processed_path(f"{slug}_country_map_institution_side_panel_debug.csv")
    pd.DataFrame(
        country_side_panel_debug_rows,
        columns=[
            "country", "map_filename", "institution_name", "normalized_institution", "city",
            "publication_count", "rank", "included_in_side_panel", "exclusion_reason",
            "enrichment_source_summary",
        ],
    ).to_csv(side_panel_debug_path, index=False)
    remember_generated(generated, side_panel_debug_path)
    regional_debug_path = processed_path(f"{slug}_regional_city_map_generation_debug.csv")
    pd.DataFrame(
        regional_city_debug_rows,
        columns=[
            "region", "countries_in_region", "candidate_city_points", "plotted_city_points",
            "basemap_loaded", "basemap_country_count", "generated", "output_path", "skip_reason",
        ],
    ).to_csv(regional_debug_path, index=False)
    remember_generated(generated, regional_debug_path)
    mapping_audit_path = processed_path(f"{slug}_geography_mapping_audit.csv")
    pd.DataFrame(
        mapping_audit,
        columns=[
            "canonical_name", "original_term", "count", "geo_type", "map_level", "country", "state", "admin1_name",
            "place_name", "latitude", "longitude", "attempted_map_type", "matched_layer", "matched_geometry_name",
            "mapped_yes_no", "map_output_used", "reason_not_mapped", "ambiguity_flag", "confidence",
        ],
    ).to_csv(mapping_audit_path, index=False)
    remember_generated(generated, mapping_audit_path)
    ambiguous_path = processed_path(f"{slug}_ambiguous_geography_matches.csv")
    pd.DataFrame(
        ambiguous_matches,
        columns=[
            "term", "canonical_name", "possible_matches", "selected_match", "selected_country", "selected_state",
            "latitude", "longitude", "matched_field", "context_snippet", "confidence", "ambiguity_reason",
            "kept_yes_no", "suppression_reason",
        ],
    ).to_csv(ambiguous_path, index=False)
    remember_generated(generated, ambiguous_path)
    remember_generated(generated, report_map_availability(discovery, slug, maps_generated, skipped))
    write_geography_map_validation(slug, discovery, maps_generated, skipped, len(mapped_terms), len(unmapped), generated, map_metadata)
    return len(mapped_terms), len(unmapped), []


def dedupe_terms(terms: Iterable[str]) -> list[str]:
    output = []
    seen = set()
    for term in terms:
        term = normalize_keyword(term)
        if not term or term in seen:
            continue
        seen.add(term)
        output.append(term)
    return output


def basic_keyword_allowed(keyword: str) -> bool:
    if not keyword or len(keyword) < 3:
        return False
    words = set(re.findall(r"[a-z][a-z0-9-]+", keyword.lower()))
    if not words:
        return False
    if words <= STOPWORDS:
        return False
    return True


def keyword_network_exclusion_reason(keyword: str, category: str = "", high_confidence: bool = False) -> str:
    normalized = normalize_keyword(str(keyword or ""))
    if not normalized or len(normalized) < 3:
        return "blank or too short"
    words = re.findall(r"[a-z][a-z0-9-]+", normalized)
    word_set = set(words)
    if not words:
        return "no alphabetic tokens"
    if normalized in KEYWORD_NETWORK_JUNK_TERMS:
        return "generic/junk term"
    if word_set <= STOPWORDS or word_set <= DOMAIN_GENERIC_TERMS or word_set <= KEYWORD_NETWORK_JUNK_WORDS:
        return "generic low-value term"
    if len(words) == 1 and normalized not in MEANINGFUL_SINGLE_TERMS and (normalized in COMMON_ENGLISH_WORDS or normalized in FILTERED_DOMAIN_TERMS or normalized in KEYWORD_NETWORK_JUNK_WORDS):
        return "generic one-word term"
    if any(word in KEYWORD_NETWORK_JUNK_WORDS for word in word_set):
        return "contains generic low-value word"
    if words[-1] in {"city", "town", "village"} and normalized not in GEOGRAPHY_KEYWORD_NAMES:
        return "unverified city/town/village fragment"
    if words[-1] in LOW_VALUE_PLACE_SUFFIXES and category.lower() not in {"geography", "geographic"}:
        return "unverified place-fragment term"
    if words[0] in {"model", "healthy", "standard", "burden", "lead", "meta", "point", "international"} and words[-1] in {"city", "town", "village"}:
        return "geographic false positive"
    if category.lower() in {"geography", "geographic"} and not high_confidence:
        return "geographic term not high-confidence for final network"
    return ""


def write_suppressed_keyword_debug(stem: str, rows: list[dict[str, object]], generated: list[Path]) -> Path | None:
    if not rows:
        return None
    path = processed_path(f"{stem}_suppressed_keyword_network_terms_debug.csv")
    pd.DataFrame(rows).sort_values(["reason", "term"]).to_csv(path, index=False)
    generated.append(path)
    return path


def csv_record_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return len(safe_read_csv(path))
    except Exception:
        return 0


def keyword_label_column(counts: pd.DataFrame) -> str:
    if "drug_name" in counts.columns:
        return "drug_name"
    if "procedure_name" in counts.columns:
        return "procedure_name"
    return "keyword"


def plot_keyword_bar(counts: pd.DataFrame, mode: str, stem: str, generated: list[Path]) -> None:
    if counts.empty:
        print(f"Skipping {mode} keyword bar chart; no keywords available.")
        return
    label_col = keyword_label_column(counts)
    count_col = "count" if "count" in counts.columns else "frequency"
    chart_df = counts.nlargest(dynamic_top_n(len(counts), 32), count_col).reset_index(drop=True)
    chart_df["rank"] = range(1, len(chart_df) + 1)
    if mode == "drugs":
        title = "Drug Intervention Frequencies"
        color_scale = "Plasma_r"
    elif mode == "procedural":
        title = "Procedural Intervention Frequencies"
        color_scale = "Teal_r"
    else:
        title = f"{mode.title()} Keyword Frequencies"
        color_scale = "Viridis_r"
    fig = px.bar(
        chart_df.sort_values(count_col, ascending=True),
        x=count_col,
        y=label_col,
        orientation="h",
        color="rank",
        color_continuous_scale=color_scale,
        text=count_col,
    )
    fig.update_traces(texttemplate="%{text:,}", textposition="outside", marker_line_width=0)
    fig.update_layout(coloraxis_showscale=False)
    configure_plotly_layout(fig, title, "Count", "")
    fig.update_yaxes(automargin=True)
    save_figure(fig, f"{stem}_keyword_frequencies_{mode}", generated)
    save_static_bar(
        chart_df,
        f"{stem}_keyword_frequencies_{mode}",
        title,
        label_col,
        count_col,
        generated,
        highlight_n=5,
        subtitle="Top five highlighted; color gradient follows rank",
    )


def save_keyword_outputs(stem: str, keyword_counts: pd.DataFrame, edges: pd.DataFrame, generated: list[Path]) -> None:
    counts_path = processed_path(f"{stem}_cleaned_keyword_counts.csv")
    edges_path = processed_path(f"{stem}_keyword_network_edges.csv")
    keyword_counts.to_csv(counts_path, index=False)
    edges.to_csv(edges_path, index=False)
    generated.extend([counts_path, edges_path])

    if keyword_counts.empty:
        print("Skipping keyword frequency chart; no keywords survived filtering.")
        return
    count_col = "count" if "count" in keyword_counts.columns else "frequency"
    chart_df = keyword_counts.nlargest(dynamic_top_n(len(keyword_counts), 28), count_col).reset_index(drop=True)
    chart_df["rank"] = range(1, len(chart_df) + 1)
    chart_df["is_drug"] = chart_df["keyword"].str.lower().isin(DRUG_TERMS)
    fig = px.bar(
        chart_df.sort_values(count_col, ascending=True),
        x=count_col,
        y="keyword",
        orientation="h",
        color=count_col,
        color_continuous_scale="Viridis",
        text=count_col,
    )
    fig.update_traces(texttemplate="%{text:,}", textposition="outside", marker_line_width=0)
    fig.update_layout(coloraxis_showscale=False)
    configure_plotly_layout(fig, "Cleaned Keyword Frequencies", "Frequency", "")
    fig.update_yaxes(automargin=True)
    save_figure(fig, "keyword_frequencies", generated)
    save_keyword_frequency_static(chart_df, generated)


def dynamic_top_n(total: int, cap: int) -> int:
    if total <= 0:
        return 0
    return min(cap, max(12, int(math.sqrt(total) * 4)))


def save_keyword_frequency_static(chart_df: pd.DataFrame, generated: list[Path]) -> None:
    apply_static_style()
    count_col = "count" if "count" in chart_df.columns else "frequency"
    plot_df = chart_df.sort_values(count_col, ascending=False).reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(13.5, max(8, 0.52 * len(plot_df) + 2)), dpi=STATIC_DPI)
    values = plot_df[count_col].astype(float).to_numpy()
    ranks = plot_df["rank"].astype(float).to_numpy()
    norm = plt.Normalize(ranks.min(), ranks.max())
    colors = [plt.cm.viridis_r(norm(rank)) for rank in ranks]
    for idx in range(min(5, len(colors))):
        colors[idx] = EMPHASIS
    for idx, keyword in enumerate(plot_df["keyword"].str.lower()):
        if keyword in DRUG_TERMS:
            colors[idx] = DRUG_COLOR

    bars = ax.barh(plot_df["keyword"], plot_df[count_col], color=colors, height=0.62)
    ax.invert_yaxis()
    ax.set_title("Cleaned Keyword Frequencies", loc="left", pad=24)
    add_subtitle(ax, "Query terms and generic biomedical filler removed; drug terms highlighted in magenta")
    ax.set_xlabel("Frequency")
    ax.set_ylabel("")
    ax.grid(False)
    ax.xaxis.grid(True, color=GRID, linewidth=0.7, alpha=0.5)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", labelsize=12, length=0)
    max_value = max(values) if len(values) else 1
    for idx, (bar, value) in enumerate(zip(bars, values, strict=False)):
        ax.text(
            bar.get_width() + max_value * 0.012,
            bar.get_y() + bar.get_height() / 2,
            f"{int(value):,}",
            va="center",
            ha="left",
            fontsize=11,
            color=INK,
            weight="bold" if idx < 5 else "normal",
        )
    for idx, label in enumerate(ax.get_yticklabels()):
        if idx < 5 or label.get_text().lower() in DRUG_TERMS:
            label.set_weight("bold")
        if label.get_text().lower() in DRUG_TERMS:
            label.set_color(DRUG_COLOR)
    fig.tight_layout()
    save_static_formats(fig, "keyword_frequencies", generated)
    plt.close(fig)


def load_best_network(file_path: Path | None, fallback_name: str, detected_networks: list[Path]) -> pd.DataFrame:
    if file_path is not None and file_path.exists():
        return read_edge_csv(file_path)
    fallback = next((path for path in detected_networks if fallback_name in path.name.lower()), None)
    if fallback:
        return read_vos_network(fallback)
    return pd.DataFrame(columns=["source", "target", "weight"])


def validate_visualization_outputs(
    generated: list[Path],
    mapped_geography_terms: int,
    unmapped_geography_terms: int,
    missing_geometry_files: list[Path] | None = None,
    slug: str = "",
) -> Path:
    visualization_exts = {".png", ".svg", ".html", ".pdf"}
    generated_visuals = [path for path in generated if path.suffix.lower() in visualization_exts]
    non_png = [path for path in generated_visuals if path.suffix.lower() != ".png"]
    unlabeled = [path for path in generated if "unlabeled" in path.name.lower()]
    stems = {path.with_suffix("").name for path in generated_visuals}
    duplicate_pairs = []
    for stem in stems:
        if stem.endswith("_top_labeled") and f"{stem[:-12]}_full_unlabeled" in stems:
            duplicate_pairs.append(stem[:-12])
    filtered_keyword_network = next((path for path in generated_visuals if path.name.endswith("_keyword_network.png")), VISUALS_DIR / f"{slug}_keyword_network.png")
    topic_keyword_network = next((path for path in generated_visuals if path.name.endswith("_keyword_network_topic_categories.png")), VISUALS_DIR / f"{slug}_keyword_network_topic_categories.png")
    hindex_generated = any(path.name == "author_network_hindex.png" for path in generated_visuals)
    required = {
        "author_network_hindex": VISUALS_DIR / "author_network_hindex.png",
        "filtered_keyword_network": filtered_keyword_network,
        "topic_category_keyword_network": topic_keyword_network,
        "geography_heatmap_world": VISUALS_DIR / f"{slug}_geography_heatmap_world.png",
        "geography_points_world_institutions": VISUALS_DIR / f"{slug}_geography_points_world_institutions.png",
        "geography_heatmap_us": VISUALS_DIR / f"{slug}_geography_heatmap_us.png",
        "geography_points_texas_cities": VISUALS_DIR / f"{slug}_geography_points_texas_cities.png",
        "geography_heatmap_europe": VISUALS_DIR / f"{slug}_geography_heatmap_europe.png",
        "geography_heatmap_east_asia": VISUALS_DIR / f"{slug}_geography_heatmap_east_asia.png",
    }
    ranking_csvs = {
        "country_publication_rankings": OUTPUTS_DIR / f"{slug}_country_publication_rankings.csv",
        "country_citation_rankings": OUTPUTS_DIR / f"{slug}_country_citation_rankings.csv",
        "institution_publication_rankings": OUTPUTS_DIR / f"{slug}_institution_publication_rankings.csv",
        "institution_citation_rankings": OUTPUTS_DIR / f"{slug}_institution_citation_rankings.csv",
    }
    ranking_charts = {
        "top_countries_by_publications": VISUALS_DIR / f"{slug}_top_countries_by_publications.png",
        "top_countries_by_citations": VISUALS_DIR / f"{slug}_top_countries_by_citations.png",
        "top_institutions_by_publications": VISUALS_DIR / f"{slug}_top_institutions_by_publications.png",
        "top_institutions_by_citations": VISUALS_DIR / f"{slug}_top_institutions_by_citations.png",
    }
    institution_locations_path = OUTPUTS_DIR / f"{slug}_institution_locations.csv"
    institution_map_paths = [path for path in VISUALS_DIR.glob(f"{slug}_geography_points_*_institutions.png")] if VISUALS_DIR.exists() else []
    warnings: list[str] = []
    if institution_locations_path.exists() and not institution_map_paths:
        warnings.append("WARNING: institution_locations.csv exists but final institution maps were not generated.")
    if any(not path.exists() for path in ranking_csvs.values()):
        warnings.append("WARNING: ranking CSV generation failed or was not wired into validation.")
    if any(not path.exists() for path in ranking_charts.values()):
        warnings.append("WARNING: ranking chart generation failed or was not wired into visualization.")
    missing_geometry_files = missing_geometry_files or []
    lines = [
        "Visualization validation report",
        "",
        f"PNG visualizations created: {len([path for path in generated_visuals if path.suffix.lower() == '.png'])}",
        f"Non-PNG visualization files created: {len(non_png)}",
        f"Unlabeled files created: {len(unlabeled)}",
        f"Duplicate labeled/unlabeled pairs: {len(duplicate_pairs)}",
        f"H-index author visualization created: {'yes' if hindex_generated else 'no'}",
        "H-index visualization skipped: h-index/citation data unavailable." if not hindex_generated else "H-index visualization generated from available h-index data.",
        f"Geography terms mapped: {mapped_geography_terms}",
        f"Geography terms unmapped: {unmapped_geography_terms}",
        f"Missing geometry source files: {len(missing_geometry_files)}",
        "",
        "Required visualization categories:",
    ]
    for label, path in required.items():
        lines.append(f"- {label}: {'present' if path.exists() else 'missing'}")
    lines.extend(["", "Ranking CSV outputs:"])
    for label, path in ranking_csvs.items():
        lines.append(f"- {label}: {'present' if path.exists() else 'missing'} ({path.name})")
    lines.extend(["", "Ranking chart outputs:"])
    for label, path in ranking_charts.items():
        lines.append(f"- {label}: {'present' if path.exists() else 'missing'} ({path.name})")
    lines.extend(["", "Final vs debug geography maps:"])
    lines.append(f"- final institution maps: {len(institution_map_paths)}")
    text_debug_maps = sorted(VISUALS_DIR.glob(f"{slug}_geography_points_*_text_mentions_debug.png")) if VISUALS_DIR.exists() else []
    lines.append(f"- text-debug maps: {len(text_debug_maps)}")
    if warnings:
        lines.extend(["", "Warnings:", *warnings])
    if non_png:
        lines.extend(["", "Non-PNG visualization files:", *[f"- {path.name}" for path in non_png]])
    if unlabeled:
        lines.extend(["", "Unlabeled files:", *[f"- {path.name}" for path in unlabeled]])
    if missing_geometry_files:
        lines.extend(["", "Missing geometry source files:", *[f"- {path}" for path in missing_geometry_files]])
    VALIDATION_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("")
    print("\n".join(lines))
    return VALIDATION_REPORT


def validate_visuals_folder(slug: str, generated: list[Path]) -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    offending: list[str] = []
    unlabeled_pngs: list[str] = []
    if VISUALS_DIR.exists():
        for path in sorted(VISUALS_DIR.iterdir()):
            if path.is_dir():
                continue
            if path.suffix.lower() != ".png":
                offending.append(path.name)
                destination = PROCESSED_DIR / path.name
                try:
                    if destination.exists():
                        destination = PROCESSED_DIR / f"{path.stem}_from_visuals{path.suffix}"
                    shutil.move(str(path), str(destination))
                    moved.append(f"{path.name} -> {destination.name}")
                    for idx, generated_path in enumerate(generated):
                        if generated_path == path:
                            generated[idx] = destination
                except Exception as exc:
                    moved.append(f"{path.name}: move failed ({exc.__class__.__name__})")
            elif "unlabeled" in path.name.lower():
                unlabeled_pngs.append(path.name)
    remaining_non_png = [path.name for path in sorted(VISUALS_DIR.iterdir()) if path.is_file() and path.suffix.lower() != ".png"] if VISUALS_DIR.exists() else []
    passed = not remaining_non_png and not unlabeled_pngs
    path = processed_path(f"{slug}_visuals_folder_validation.txt")
    lines = [
        "Visuals folder validation",
        "",
        f"visuals_png_only: {'yes' if not remaining_non_png else 'no'}",
        f"unlabeled_pngs_present: {'yes' if unlabeled_pngs else 'no'}",
        f"validation_result: {'pass' if passed else 'fail'}",
        "non-PNG files found before move:",
        *(f"- {name}" for name in offending),
        *(["- none"] if not offending else []),
        "non-PNG files remaining:",
        *(f"- {name}" for name in remaining_non_png),
        *(["- none"] if not remaining_non_png else []),
        "non-PNG files moved to processed:",
        *(f"- {item}" for item in moved),
        *(["- none"] if not moved else []),
        "unlabeled PNGs:",
        *(f"- {name}" for name in unlabeled_pngs),
        *(["- none"] if not unlabeled_pngs else []),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    remember_generated(generated, path)
    if not passed:
        print(f"WARNING: visuals folder validation failed; see {path}")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create bibliometric visuals and three keyword pipeline lenses.")
    parser.add_argument("--core", type=Path, help="Core dataset CSV with title, keywords, and optional abstract columns.")
    parser.add_argument("--slug", default="", help="Project/output slug used to find GeoCensus.py extraction outputs.")
    parser.add_argument("--query", default="", help="Query string used to remove query-bias terms from filtered keywords.")
    parser.add_argument("--skip-rxnorm", action="store_true", help="Deprecated; drug extraction now runs in GeoCensus.py using local caches.")
    parser.add_argument("--drug-visual-min-frequency", type=int, default=1, help="Minimum drug count for drug network visuals only; CSV outputs keep drugs with count >= 1.")
    parser.add_argument("--drug-visual-min-degree", type=int, default=1, help="Minimum drug node degree for drug network visuals only.")
    parser.add_argument("--include-world-regions-map", action="store_true", help="Generate optional world-regions overview map.")
    parser.add_argument("--include-point-maps", action="store_true", help="Generate separate optional point/bubble geography maps.")
    parser.add_argument("--include-labeled-point-maps", action="store_true", help="Enable labels on optional geography point maps.")
    parser.add_argument("--label-top-cities", type=int, default=5, help="Label the top N city/place point bubbles by publication geography count.")
    parser.add_argument("--no-city-labels", action="store_true", help="Disable labels on city/place point maps.")
    parser.add_argument("--label-top-institutions", type=int, default=5, help="Label the top N institution point bubbles by publication geography count.")
    parser.add_argument("--no-institution-labels", action="store_true", help="Disable labels on institution point maps.")
    parser.add_argument("--debug", action="store_true", help="Print sample tokens, detected drugs, and trimmed RxNorm responses.")
    return parser.parse_args()


def main() -> None:
    global DEBUG, ENABLE_WORLD_REGIONS_OVERVIEW, INCLUDE_POINT_MAPS, INCLUDE_LABELED_POINT_MAPS, LABEL_CITY_POINTS, LABEL_INSTITUTION_POINTS, LABEL_TOP_CITIES, LABEL_TOP_INSTITUTIONS, VALIDATION_REPORT
    args = parse_args()
    DEBUG = args.debug
    ENABLE_WORLD_REGIONS_OVERVIEW = bool(args.include_world_regions_map)
    INCLUDE_POINT_MAPS = bool(args.include_point_maps or args.include_labeled_point_maps)
    LABEL_CITY_POINTS = not bool(args.no_city_labels)
    LABEL_INSTITUTION_POINTS = not bool(args.no_institution_labels)
    LABEL_TOP_CITIES = max(1, int(args.label_top_cities or 5))
    LABEL_TOP_INSTITUTIONS = max(1, int(args.label_top_institutions or 5))
    INCLUDE_LABELED_POINT_MAPS = bool(LABEL_CITY_POINTS or LABEL_INSTITUTION_POINTS or args.include_labeled_point_maps)
    VALIDATION_REPORT = processed_path(f"{args.slug or 'visualization'}_visualization_validation_report.txt")
    VISUALS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    VOS_DIR.mkdir(parents=True, exist_ok=True)
    core_arg = args.core
    if core_arg is None and args.slug:
        for candidate in (
            OUTPUTS_DIR / f"{args.slug}_year_limited_records.csv",
            OUTPUTS_DIR / f"{args.slug}.csv",
            OUTPUTS_DIR / f"{args.slug}_cleaned.csv",
        ):
            if candidate.exists():
                core_arg = candidate
                break
    if core_arg is None:
        raise FileNotFoundError(
            "Visualization generation requires --core or a slug with a current matching core dataset. "
            "Refusing to auto-select an unrelated CSV from data/outputs."
        )
    files = detect_files(args.slug, core_arg)
    if not isinstance(files.get("core"), Path) or not Path(files["core"]).exists():  # type: ignore[arg-type]
        raise FileNotFoundError(f"Core dataset not found for visualization run: {core_arg}")
    generated: list[Path] = []
    mapped_geography_terms = 0
    unmapped_geography_terms = 0
    missing_geometry_files: list[Path] = []
    validation_slug = args.slug
    output_slug = args.slug or (files["core"].with_suffix("").name.replace("_year_limited_records", "").replace("_cleaned", "") if isinstance(files.get("core"), Path) else "bibliometrics")

    print("Detected inputs:")
    print(f"  active_slug: {args.slug or active_slug_from_core(Path(files['core']))}")  # type: ignore[arg-type]
    print(f"  visuals_dir: {VISUALS_DIR}")
    print(f"  processed_dir: {PROCESSED_DIR}")
    print(f"  vos_dir: {VOS_DIR}")
    for key, value in files.items():
        if isinstance(value, list):
            print(f"  {key}: {len(value)} files")
        else:
            print(f"  {key}: {value.name if value else 'not found'}")

    if files["year_counts"]:
        run_optional_visual("publication trends", lambda: publication_trends(files["year_counts"], generated))  # type: ignore[arg-type]
    elif files["core"]:
        core_df = safe_read_csv(files["core"])  # type: ignore[arg-type]
        if "year" in core_df.columns:
            tmp = core_df.groupby("year", as_index=False).size().rename(columns={"size": "publications"})
            tmp_path = processed_path("_publication_years_from_core.csv")
            tmp.to_csv(tmp_path, index=False)
            run_optional_visual("publication trends", lambda: publication_trends(tmp_path, generated))
            tmp_path.unlink(missing_ok=True)

    if files["country_year_counts"]:
        run_optional_visual("country trends", lambda: country_trends(files["country_year_counts"], generated))  # type: ignore[arg-type]

    if files["top_authors"]:
        run_optional_visual(
            "top authors",
            lambda: horizontal_bar(
                files["top_authors"],  # type: ignore[arg-type]
                "author",
                "total_citations" if "total_citations" in safe_read_csv(files["top_authors"]).columns else "publications",  # type: ignore[arg-type]
                f"Top {TOP_AUTHORS} Authors",
                "top_authors",
                generated,
                TOP_AUTHORS,
            ),
        )

    if files["top_papers"]:
        run_optional_visual(
            "top papers",
            lambda: horizontal_bar(
                files["top_papers"],  # type: ignore[arg-type]
                "title",
                "citations",
                f"Top {TOP_PAPERS} Papers by Citations",
                f"{output_slug}_top_cited_papers_chart",
                generated,
                TOP_PAPERS,
            ),
        )

    ranking_visuals = [
        (
            "top countries by publication count",
            "country_publication_rankings",
            "country",
            "publication_count",
            "Top Countries by Publication Count",
            f"{output_slug}_top_countries_by_publications",
            20,
        ),
        (
            "top countries by citation count",
            "country_citation_rankings",
            "country",
            "total_citations",
            "Top Countries by Citation Count",
            f"{output_slug}_top_countries_by_citations",
            20,
        ),
        (
            "top institutions by publication count",
            "institution_publication_rankings",
            "institution",
            "publication_count",
            "Top Institutions by Publication Count",
            f"{output_slug}_top_institutions_by_publications",
            20,
        ),
        (
            "top institutions by citation count",
            "institution_citation_rankings",
            "institution",
            "total_citations",
            "Top Institutions by Citation Count",
            f"{output_slug}_top_institutions_by_citations",
            20,
        ),
    ]
    for visual_name, file_key, label_col, value_col, title, stem, top_n in ranking_visuals:
        if files.get(file_key):
            run_optional_visual(
                visual_name,
                lambda path=files[file_key], label=label_col, value=value_col, chart_title=title, chart_stem=stem, limit=top_n: horizontal_bar(  # type: ignore[index]
                    path,  # type: ignore[arg-type]
                    label,
                    value,
                    chart_title,
                    chart_stem,
                    generated,
                    limit,
                ),
            )

    networks = files["networks"] if isinstance(files["networks"], list) else []
    author_edges = load_best_network(files["author_edges"], "author", networks)  # type: ignore[arg-type]
    institution_edges = load_best_network(files["institution_edges"], "institution", networks)  # type: ignore[arg-type]
    institution_weights: dict[str, float] = {}
    if files.get("institution_year_counts"):
        inst_years = safe_read_csv(files["institution_year_counts"])  # type: ignore[arg-type]
        if {"institution", "publications"} <= set(inst_years.columns):
            inst_years["publications"] = pd.to_numeric(inst_years["publications"], errors="coerce").fillna(0)
            institution_weights = (
                inst_years.assign(institution=inst_years["institution"].map(normalize_institution_name))
                .groupby("institution")["publications"]
                .sum()
                .to_dict()
            )

    def build_author_visuals() -> None:
        author_graph = graph_from_edges(author_edges, max_edges=420)
        author_metrics = load_author_metrics(files.get("top_authors") if isinstance(files.get("top_authors"), Path) else None)
        apply_author_metrics(author_graph, author_metrics)
        has_hindex = any(bool(values.get("h_index_known", False)) for values in author_metrics.values())
        if ENABLE_HINDEX_AUTHOR_VIS and not has_hindex:
            print("H-index visualization skipped: h-index/citation data unavailable.")
        plot_network(
            author_graph,
            "Author Collaboration Network",
            "author_network_hindex" if ENABLE_HINDEX_AUTHOR_VIS and has_hindex else "author_network",
            generated,
            top_nodes=80,
            label_top_n=18,
            node_kind="Author",
            size_metric="publication_count",
            border_metric="h_index" if ENABLE_HINDEX_AUTHOR_VIS and has_hindex else "",
        )
        if ENABLE_HINDEX_AUTHOR_VIS and has_hindex:
            save_author_hindex_audit(output_slug, author_graph, generated)

    def build_institution_visuals() -> None:
        institution_graph = graph_from_edges(
            institution_edges,
            max_edges=320,
            node_weights=institution_weights,
            min_edge_weight=1,
            node_normalizer=normalize_institution_name,
        )
        plot_network(
            institution_graph,
            "Institution Collaboration Network, top institutions by co-authorship",
            "institution_network",
            generated,
            top_nodes=50,
            label_top_n=10,
            min_edge_weight=2,
            keep_largest_component=True,
            node_kind="Institution",
        )
        save_institution_rankings(institution_graph, generated)

    run_optional_visual("author collaboration network", build_author_visuals)
    run_optional_visual("institution collaboration network", build_institution_visuals)

    if files["core"]:
        query = args.query or infer_query(files["core"])  # type: ignore[arg-type]
        def extract_keyword_outputs() -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
            with timed_stage("Extracting general keywords"):
                return extract_visual_keyword_pipelines(files["core"], query, generated)  # type: ignore[arg-type]

        keyword_outputs = run_optional_visual("keyword extraction", extract_keyword_outputs)
        if not keyword_outputs:
            empty_counts = pd.DataFrame(columns=["keyword", "count"])
            empty_edges = pd.DataFrame(columns=["source", "target", "weight"])
            keyword_outputs = {"general": (empty_counts, empty_edges), "filtered": (empty_counts, empty_edges)}
        stem = files["core"].with_suffix("").name  # type: ignore[union-attr]
        slug = args.slug or stem.replace("_year_limited_records", "").replace("_cleaned", "")
        validation_slug = slug
        geocensus_drug_counts, geocensus_drug_edges = load_geocensus_drug_outputs(slug, stem)
        print("Saving visuals...")
        with timed_stage("Generating visuals"):
            for mode, (counts, edges) in tqdm(keyword_outputs.items(), desc="Generating visuals"):
                run_optional_visual(f"{mode} keyword bar chart", lambda counts=counts, mode=mode: plot_keyword_bar(counts, mode, stem, generated))
            if not geocensus_drug_counts.empty:
                weights = dict(zip(geocensus_drug_counts["drug_name"], geocensus_drug_counts["count"], strict=False))
                drug_graph = graph_from_edges(geocensus_drug_edges, max_edges=650, node_weights=weights)
                run_optional_visual(
                    "drug network",
                    lambda: plot_drug_network(
                        drug_graph,
                        geocensus_drug_counts,
                        "Drug Intervention Network by RxNorm Type",
                        f"{slug}_network_drugs",
                        generated,
                        min_frequency=max(1, args.drug_visual_min_frequency),
                        min_degree=max(1, args.drug_visual_min_degree),
                    ),
                )
            else:
                print("Skipping drug visuals; run GeoCensus.py first to create drug extraction outputs.")
            run_optional_visual("reference term counts", lambda: plot_reference_term_counts(slug, generated))
            if ENABLE_GEO_MAPS:
                geography_result = run_optional_visual("geography heatmaps", lambda: generate_geography_heatmaps(slug, generated))
                if geography_result:
                    mapped_geography_terms, unmapped_geography_terms, missing_geometry_files = geography_result
            all_keyword_outputs = run_optional_visual("all-keyword extraction", lambda: extract_all_keyword_network(files["core"], generated, include_debug_visual=DEBUG))  # type: ignore[arg-type]
            if all_keyword_outputs:
                all_keyword_counts, all_keyword_edges = all_keyword_outputs
                run_optional_visual(
                    "topic category keyword network",
                    lambda: plot_topic_category_keyword_network(
                        all_keyword_counts,
                        all_keyword_edges,
                        stem,
                        generated,
                        input_keyword_table=processed_path(f"{stem}_keyword_network_all_keywords_nodes.csv"),
                    ),
                )
            keyword_counts, keyword_edges = keyword_outputs["filtered"]
            run_optional_visual("filtered keyword outputs", lambda: save_keyword_outputs(stem, keyword_counts, keyword_edges, generated))
            keyword_weights = dict(zip(keyword_counts["keyword"], keyword_counts["count"], strict=False))
            suppressed_count = csv_record_count(processed_path(f"{stem}_suppressed_keyword_network_terms_debug.csv"))
            run_optional_visual(
                "filtered keyword network",
                lambda: plot_network(
                    graph_from_edges(keyword_edges, max_edges=650, node_weights=keyword_weights),
                    "Filtered Keyword Co-occurrence Network",
                    f"{stem}_keyword_network",
                    generated,
                    top_nodes=FINAL_KEYWORD_TOP_NODES,
                    label_top_n=FINAL_KEYWORD_LABEL_TOP_N,
                    min_edge_weight=FINAL_KEYWORD_MIN_EDGE_WEIGHT,
                    keep_largest_component=True,
                    node_metric="frequency",
                    network_type="Filtered Keyword Co-occurrence Network",
                    input_keyword_table=str(processed_path(f"{stem}_keywords_filtered.csv")),
                    terms_suppressed=suppressed_count,
                ),
            )
    else:
        raise FileNotFoundError(
            "No core dataset CSV found in data/outputs/. A core dataset must contain title, authors, and year columns "
            "and must not contain derived-only columns such as h_index, year_count, edge, or rank. "
            "Provide --core PATH to skip auto-detection."
        )

    validate_visuals_folder(validation_slug, generated)
    remember_generated(generated, validate_visualization_outputs(generated, mapped_geography_terms, unmapped_geography_terms, missing_geometry_files, validation_slug))

    print("\nGenerated outputs:")
    if generated:
        for path in generated:
            try:
                display_path = path.relative_to(ROOT)
            except ValueError:
                display_path = path
            print(f"  {display_path}")
    else:
        print("  None")


if __name__ == "__main__":
    main()
