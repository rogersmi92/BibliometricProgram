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
from contextlib import contextmanager
from dataclasses import dataclass
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote_plus

_MPLCONFIGDIR = Path(__file__).resolve().parent / "data" / "visuals" / ".mplconfig"
_MPLCONFIGDIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPLCONFIGDIR))

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


ROOT = Path(__file__).resolve().parent
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
MAP_COLORMAP = "turbo"
MAP_ZERO_COLOR_INCLUDED = True
MAP_NODATA_COLOR = "#eeeeee"
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


def require_core_columns(df: pd.DataFrame, core_path: Path) -> None:
    columns = {col.lower() for col in df.columns}
    keyword_columns = {"keywords", "keyword", "author_keywords", "index_keywords", "mesh_terms", "mesh"}
    missing = []
    if "title" not in columns:
        missing.append("title")
    if not columns & keyword_columns:
        missing.append("keywords")
    if missing:
        raise ValueError(
            f"Required column(s) missing from {core_path}: {', '.join(missing)}. "
            "Expected at least title and one keyword column; abstract is optional."
        )


def clean_file_candidates(paths: Iterable[Path]) -> list[Path]:
    kept = []
    for path in paths:
        stem = path.stem.lower()
        if stem in SKIP_EXACT_STEMS or any(stem.endswith(suffix) for suffix in SKIP_STEM_SUFFIXES):
            continue
        kept.append(path)
    return sorted(kept)


def detect_files() -> dict[str, Path | list[Path] | None]:
    output_csvs = clean_file_candidates(OUTPUTS_DIR.glob("*.csv")) if OUTPUTS_DIR.exists() else []
    vos_networks = clean_file_candidates(VOS_DIR.glob("*_network.txt")) if VOS_DIR.exists() else []
    core = detect_core_dataset(output_csvs)

    return {
        "core": core,
        "networks": vos_networks,
        "year_counts": find_publication_year_file(output_csvs),
        "country_year_counts": find_first(output_csvs, ("_country_year_counts.csv",)),
        "institution_year_counts": find_first(output_csvs, ("_institution_year_counts.csv",)),
        "top_authors": find_first(output_csvs, ("_top_authors.csv",)),
        "top_papers": find_first(output_csvs, ("_top_papers.csv",)),
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
            graph.nodes[node]["publication_count"] = float(graph.nodes[node].get("frequency", 0) or 0)
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
) -> None:
    path = processed_path(f"{config.stem}_qc_stats.txt")
    lines = [
        config.title,
        filter_subtitle(config),
        "",
        f"Graph before filtering: {stats.get('before_nodes', original_graph.number_of_nodes())} nodes, {stats.get('before_edges', original_graph.number_of_edges())} edges",
        f"Connected components before filtering: {stats.get('before_components', connected_component_count(original_graph))}",
        f"Graph after filtering: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges",
        f"Connected components after filtering: {connected_component_count(graph)}",
        f"Labels drawn: {min(config.label_top_n, graph.number_of_nodes())}",
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
    print(f"{config.title}: {stats.get('before_nodes')} nodes/{stats.get('before_edges')} edges before filtering; {graph.number_of_nodes()} nodes/{graph.number_of_edges()} edges after filtering.")
    print(f"{config.title}: {connected_component_count(graph)} connected component(s); drawing {min(config.label_top_n, graph.number_of_nodes())} labels.")
    if not node_df.empty:
        print("Top 20 nodes by weighted degree:")
        for row in node_df.head(20).itertuples(index=False):
            print(f"  {row.node}: freq={row.frequency:g}, degree={row.degree}, weighted={row.weighted_degree:g}")


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

    save_network_stats(graph, original_graph, config, stats, node_df, generated)
    label_nodes = node_df.head(config.label_top_n)["node"].tolist()
    subtitle = config.subtitle or filter_subtitle(config)
    pos = network_layout(graph, seed=42)
    draw_static_network(graph, pos, node_df, f"{config.title}", subtitle, config.stem, generated, label_nodes, config)
    lcc = largest_component(graph)
    if lcc.number_of_nodes() != graph.number_of_nodes():
        lcc_clusters = community_colors(lcc)
        lcc_df = node_table(lcc, lcc_clusters)
        lcc_pos = network_layout(lcc, seed=42)
        draw_static_network(lcc, lcc_pos, lcc_df, f"{config.title} (Largest Component)", subtitle, f"{config.stem}_largest_component", generated, lcc_df.head(config.label_top_n)["node"].tolist(), config)
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
) -> None:
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
        subtitle=filter_subtitle(
            NetworkPlotConfig(
                title=title,
                stem=stem,
                top_n_nodes=top_nodes,
                min_edge_weight=min_edge_weight,
                min_node_frequency=min_node_frequency,
                keep_largest_component=keep_largest_component,
            )
        ),
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


def extract_all_keyword_network(core_path: Path, generated: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
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
    for _, row in df.iterrows():
        terms: list[str] = []
        for column in keyword_cols:
            for raw in re.split(r";|\||,", str(row.get(column) or "")):
                raw = raw.strip()
                normalized = normalize_keyword_for_network(raw, str(column))
                if not normalized or not basic_keyword_allowed(normalized):
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
            if not normalized:
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
    if not counts.empty:
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
                title="All Keyword Co-occurrence Network",
                stem="keyword_network_all_keywords",
                node_kind="Keyword",
                min_node_frequency=1,
                min_edge_weight=1,
                top_n_nodes=None,
                label_top_n=min(30, len(counts)),
                drop_isolates=False,
                min_component_size=1,
                keep_largest_component=False,
                node_metric="frequency",
                size_metric="variant_count",
                subtitle="all cleaned normalized keywords; node size reflects variant/alias rollups",
            ),
            generated,
        )
    return counts, edges


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

    for _, row in tqdm(df.iterrows(), total=len(df), desc="Extracting general keywords"):
        general_terms = tokenize_all(row)
        filtered_terms = dedupe_terms(term for term in general_terms if not term_contains_query(term, query_terms))
        for mode, selected_terms in (("general", general_terms), ("filtered", filtered_terms)):
            selected_terms = selected_terms[:MAX_KEYWORDS_PER_RECORD]
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
    return outputs


def load_geocensus_drug_outputs(slug: str, stem: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load drug extraction outputs generated by GeoCensus.py, if available."""
    count_candidates = [
        OUTPUTS_DIR / f"{slug}_drug_term_counts_cleaned.csv",
        OUTPUTS_DIR / f"{slug}_drug_term_counts.csv",
        PROCESSED_DIR / f"{slug}_interventions_drugs.csv",
        PROCESSED_DIR / f"{stem}_interventions_drugs.csv",
        VISUALS_DIR / f"{slug}_interventions_drugs.csv",
        VISUALS_DIR / f"{stem}_interventions_drugs.csv",
    ]
    edge_candidates = [
        PROCESSED_DIR / f"{slug}_network_drugs.txt",
        PROCESSED_DIR / f"{stem}_network_drugs.txt",
        VISUALS_DIR / f"{slug}_network_drugs.txt",
        VOS_DIR / f"{slug}_network_drugs.txt",
        VISUALS_DIR / f"{stem}_network_drugs.txt",
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
        df = safe_read_csv(path)
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
    "world": REFERENCE_DIR / "geography" / "boundaries" / "world.geojson",
    "us": REFERENCE_DIR / "geography" / "boundaries" / "us_states.geojson",
    "texas": REFERENCE_DIR / "geography" / "boundaries" / "texas_counties.geojson",
    "world_regions": REFERENCE_DIR / "geography" / "boundaries" / "world_regions.geojson",
}
MAPS_ROOT = REFERENCE_DIR / "Maps"
CULTURAL_FOLDER_ORDER = ("50m_cultural", "10m_cultural", "110m_cultural")
IGNORED_MAP_FOLDERS = ("10m_physical", "50m_physical", "110m_physical", "50m_raster")
COUNTRY_LAYER_PATTERNS = (
    "ne_50m_admin_0_countries.shp",
    "ne_10m_admin_0_countries.shp",
    "ne_110m_admin_0_countries.shp",
)
ADMIN1_LAYER_PATTERNS = (
    "ne_50m_admin_1_states_provinces_lakes.shp",
    "ne_50m_admin_1_states_provinces.shp",
    "ne_10m_admin_1_states_provinces.shp",
    "ne_110m_admin_1_states_provinces.shp",
)
PLACE_LAYER_PATTERNS = (
    "ne_50m_populated_places.shp",
    "ne_10m_populated_places.shp",
    "ne_110m_populated_places.shp",
)
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
    ignored_folders: list[Path]
    cultural_folders: list[Path]


def map_norm(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return COUNTRY_ALIASES.get(text, US_STATE_ABBREVIATIONS.get(text, text))


def discover_map_files() -> dict[str, list[Path]]:
    discovered = {folder: [] for folder in CULTURAL_FOLDER_ORDER}
    if not MAPS_ROOT.exists():
        return discovered
    for folder in CULTURAL_FOLDER_ORDER:
        path = MAPS_ROOT / folder
        if path.exists():
            discovered[folder] = sorted(path.glob("*.shp"))
    return discovered


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


def validate_map_layer(path: Path, expected_columns: tuple[str, ...] = ()) -> tuple[bool, str, ShapeLayer | None]:
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


def find_best_cultural_layer(patterns: tuple[str, ...], expected_columns: tuple[str, ...], invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    for folder in CULTURAL_FOLDER_ORDER:
        for pattern in patterns:
            candidate = MAPS_ROOT / folder / pattern
            if not candidate.exists():
                continue
            valid, reason, layer = validate_map_layer(candidate, expected_columns)
            if valid and layer:
                if folder != "50m_cultural":
                    fallbacks.append(f"Used {folder} fallback for {pattern}")
                return layer
            invalid.append(f"{candidate}: {reason}")
    return None


def find_country_layer(invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    return find_best_cultural_layer(COUNTRY_LAYER_PATTERNS, ("NAME", "NAME_LONG", "ADMIN", "SOVEREIGNT", "ISO_A3", "ADM0_A3"), invalid, fallbacks)


def find_admin1_layer(invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    return find_best_cultural_layer(ADMIN1_LAYER_PATTERNS, ("name", "name_en", "region", "postal", "iso_3166_2", "admin"), invalid, fallbacks)


def find_populated_places_layer(invalid: list[str], fallbacks: list[str]) -> ShapeLayer | None:
    return find_best_cultural_layer(PLACE_LAYER_PATTERNS, ("NAME", "NAMEASCII", "ADM0NAME", "ADM1NAME"), invalid, fallbacks)


def find_census_layer(kind: str, invalid: list[str]) -> ShapeLayer | None:
    census_root = MAPS_ROOT / "census"
    if not census_root.exists():
        return None
    for path in sorted(census_root.rglob("*.shp")):
        if kind not in path.name.lower() and kind not in str(path.parent).lower():
            continue
        valid, reason, layer = validate_map_layer(path, ("NAME", "STUSPS", "STATEFP", "COUNTYFP", "PLACEFP"))
        if valid and layer:
            return layer
        invalid.append(f"{path}: {reason}")
    return None


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
        "Cultural folders found:",
        *[f"- {path}" for path in discovery.cultural_folders],
        "Physical/raster folders ignored:",
        *[f"- {path}" for path in discovery.ignored_folders],
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
) -> Path:
    path = processed_path(f"{slug}_geography_map_validation.txt")
    generated_names = {Path(item).name for item in maps_generated}
    generated_paths = [Path(item) for item in maps_generated]

    def yes_no(name: str) -> str:
        return "yes" if f"{slug}_{name}.png" in generated_names else "no"

    regional_generated = sorted(name for name in generated_names if f"{slug}_geography_heatmap_" in name and any(region in name for region in ("europe", "east_asia", "latin_america", "africa", "middle_east", "north_america")))
    country_generated = sorted(name for name in generated_names if name.startswith(f"{slug}_geography_heatmap_country_"))
    regional_skipped = [reason for reason in skipped if any(region in reason for region in ("europe", "east_asia", "latin_america", "africa", "middle_east", "north_america"))]
    country_skipped = [reason for reason in skipped if reason.startswith("country_") or "country-level only" in reason or "geometry unavailable" in reason]

    lines = [
        "Geography map validation",
        "",
        f"world map generated: {yes_no('geography_heatmap_world')}",
        f"world_map_generated: {yes_no('geography_heatmap_world')}",
        f"Europe map generated: {yes_no('geography_heatmap_europe')}",
        f"East Asia map generated: {yes_no('geography_heatmap_east_asia')}",
        f"Latin America map generated: {yes_no('geography_heatmap_latin_america')}",
        f"Africa map generated: {yes_no('geography_heatmap_africa')}",
        f"Middle East map generated: {yes_no('geography_heatmap_middle_east')}",
        f"North America map generated: {yes_no('geography_heatmap_north_america')}",
        f"U.S. map generated: {yes_no('geography_heatmap_us')}",
        f"Texas map generated: {yes_no('geography_heatmap_texas')}",
        f"world regions map generated: {'yes' if f'{slug}_geography_overview_world_regions.png' in generated_names else 'no'}",
        f"world_regions_overview_generated: {'yes' if f'{slug}_geography_overview_world_regions.png' in generated_names else 'no'}",
        "world_regions_overview_required: no",
        f"country-specific maps generated: {sum(1 for item in generated_names if item.startswith(f'{slug}_geography_heatmap_country_'))}",
        f"regional_maps_generated: {', '.join(regional_generated) if regional_generated else 'none'}",
        f"regional_maps_skipped: {' | '.join(regional_skipped) if regional_skipped else 'none'}",
        f"country_maps_generated: {', '.join(country_generated) if country_generated else 'none'}",
        f"country_maps_skipped: {' | '.join(country_skipped) if country_skipped else 'none'}",
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
        f"Natural Earth fallback used: {'yes' if not (discovery.census_state_layer and discovery.census_county_layer and discovery.census_place_layer) else 'no'}",
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
        "skip reasons:",
        *[f"- {reason}" for reason in skipped],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    remember_generated(generated, path)
    return path


def build_map_discovery() -> MapDiscovery:
    invalid: list[str] = []
    fallbacks: list[str] = []
    cultural_folders = [MAPS_ROOT / folder for folder in CULTURAL_FOLDER_ORDER if (MAPS_ROOT / folder).exists()]
    ignored = [MAPS_ROOT / folder for folder in IGNORED_MAP_FOLDERS if (MAPS_ROOT / folder).exists()]
    expected = [
        MAPS_ROOT / "50m_cultural" / "ne_50m_admin_0_countries.shp",
        MAPS_ROOT / "50m_cultural" / "ne_50m_admin_1_states_provinces_lakes.shp",
        MAPS_ROOT / "50m_cultural" / "ne_50m_populated_places.shp",
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
        ignored_folders=ignored,
        cultural_folders=cultural_folders,
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
        PROCESSED_DIR / f"{slug}_geography_mapping_audit.csv",
        OUTPUTS_DIR / f"{slug}_geographic_term_counts.csv",
    ]
    frames = []
    for priority, path in enumerate(candidates):
        if path.exists():
            frame = safe_read_csv(path)
            if not frame.empty:
                frame["_source_priority"] = priority
                frames.append(frame)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True, sort=False)
    if df.empty:
        return df
    count_col = geography_count_column(df)
    df["count"] = pd.to_numeric(df[count_col], errors="coerce").fillna(0).astype(int)
    df["count"] = df["count"].where(df["count"] > 0, 1)
    dedupe_cols = [column for column in ("canonical_name", "term", "country", "state", "admin1_name", "latitude", "longitude", "geo_type") if column in df.columns]
    if dedupe_cols:
        df = df.sort_values("_source_priority").drop_duplicates(dedupe_cols, keep="first")
    df = df.drop(columns=["_source_priority"], errors="ignore")
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
    name = str(record_value(record, "NAME_LONG", "NAME", "ADMIN", "SOVEREIGNT")).strip()
    return {
        "country_key": map_norm(name),
        "country_index": index,
        "country": name,
        "iso_a2": str(record_value(record, "ISO_A2", "WB_A2")).strip(),
        "iso_a3": str(record_value(record, "ISO_A3", "ADM0_A3", "WB_A3")).strip(),
        "continent": str(record_value(record, "CONTINENT")).strip(),
        "region_un": str(record_value(record, "REGION_UN")).strip(),
        "subregion": str(record_value(record, "SUBREGION")).strip(),
        "region_wb": str(record_value(record, "REGION_WB")).strip(),
    }


def build_country_metadata(layer: ShapeLayer | None) -> dict[int, dict[str, object]]:
    if layer is None:
        return {}
    return {idx: country_metadata(record, idx) for idx, record in enumerate(layer.records)}


def record_country_key(record: dict[str, object]) -> str:
    return map_norm(record_value(record, "ADMIN", "ADM0NAME", "adm0_name", "geonunit", "SOVEREIGNT", "COUNTRY"))


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


def point_in_extent(point: dict[str, object], extent: tuple[float, float, float, float]) -> bool:
    lon = float(point["lon"])
    lat = float(point["lat"])
    xmin, xmax, ymin, ymax = extent
    return xmin <= lon <= xmax and ymin <= lat <= ymax


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
        map_needed = subnational_count > 0
        reasons = []
        if subnational_count > 0:
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
                "total_geography_frequency": int(count),
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


def region_match(summary: pd.DataFrame, region: str) -> bool:
    if summary.empty:
        return False
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
    }
    us_needed = "united states" in countries or bool(term_keys & {"texas", "tx", "california", "new york"})
    texas_needed = "texas" in term_keys
    country_maps = []
    if not summary.empty:
        for row in summary.to_dict("records"):
            if row.get("map_needed_yes_no") == "yes":
                country_maps.append(row)
    return {
        "world": True,
        "world_regions": bool(ENABLE_WORLD_REGIONS_OVERVIEW and not summary.empty),
        "regions": regions,
        "us": us_needed,
        "texas": texas_needed,
        "country_maps": country_maps,
    }


def write_geography_map_plan(slug: str, plan: dict[str, object], summary: pd.DataFrame, generated: list[Path]) -> Path:
    region_plan = plan.get("regions", {})
    country_maps = plan.get("country_maps", [])
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
    for idx, record in enumerate(records):
        count = counts_by_index.get(idx, 0)
        if MAP_USE_LOG_SCALE and count:
            denom = math.log1p(max_count)
            scale_value = math.log1p(count) / denom if denom else 0
        else:
            scale_value = count / max_count if max_count else 0
        color = cmap(scale_value if MAP_ZERO_COLOR_INCLUDED else 0.20 + 0.80 * scale_value)
        for ring in record.get("_parts", []) or []:
            if len(ring) >= 3:
                patches.append(Polygon(ring, closed=True))
                colors.append(color)
    if patches:
        ax.add_collection(PatchCollection(patches, facecolor=colors, edgecolor="#64748b", linewidths=0.35, alpha=0.96, zorder=1))


def draw_point_overlay(ax: plt.Axes, points: list[dict[str, object]], max_labels: int = 10) -> None:
    if not points:
        return
    values = [float(point["count"]) for point in points]
    max_value = max(values) if values else 1
    ax.scatter(
        [point["lon"] for point in points],
        [point["lat"] for point in points],
        s=[45 + 460 * math.sqrt(value / max_value) for value in values],
        c=values,
        cmap="YlOrRd",
        alpha=0.82,
        edgecolor="#7f1d1d",
        linewidth=0.6,
        zorder=3,
    )
    for point in sorted(points, key=lambda item: item["count"], reverse=True)[:max_labels]:
        ax.text(float(point["lon"]) + 0.4, float(point["lat"]) + 0.35, truncate_label(point["label"], 22), fontsize=8, color=INK, zorder=4)
    if len(set(values)) > 1:
        legend_values = sorted({min(values), max_value, float(pd.Series(values).median())})
    else:
        legend_values = [max_value]
    handles = [
        ax.scatter([], [], s=45 + 460 * math.sqrt(value / max_value), color=plt.get_cmap(MAP_COLORMAP)(0.78), alpha=0.72, edgecolor="#7f1d1d", linewidth=0.6)
        for value in legend_values
    ]
    labels = [f"{value:g}" for value in legend_values]
    ax.legend(handles, labels, title="Point frequency", loc="lower left", frameon=True, facecolor="white", edgecolor="#cbd5e1", fontsize=8, title_fontsize=9)


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
) -> Path | None:
    if not layer_records:
        return None
    apply_static_style()
    fig, ax = plt.subplots(figsize=(13.5, 7.8), dpi=STATIC_DPI)
    ax.set_facecolor("#eef6fb")
    draw_shape_layer(ax, layer_records, counts_by_index)
    if max_points is not None and len(points) > max_points:
        points = sorted(points, key=lambda item: item["count"], reverse=True)[:max_points]
    draw_point_overlay(ax, points, max_labels=max_labels)
    if counts_by_index:
        max_count = max(counts_by_index.values())
        if max_count > 0:
            norm = matplotlib.colors.Normalize(vmin=0, vmax=max_count)
            scalar = matplotlib.cm.ScalarMappable(norm=norm, cmap=plt.get_cmap(MAP_COLORMAP))
            scalar.set_array([])
            cbar = fig.colorbar(scalar, ax=ax, shrink=0.68, pad=0.02)
            cbar.set_label("Detected geography frequency")
    extent = extent or layer_extent(layer_records)
    if extent:
        xmin, xmax, ymin, ymax = extent
        xpad = max((xmax - xmin) * 0.05, 1)
        ypad = max((ymax - ymin) * 0.05, 1)
        ax.set_xlim(xmin - xpad, xmax + xpad)
        ax.set_ylim(ymin - ypad, ymax + ypad)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(True, color="white", linewidth=0.6)
    ax.set_title(title, loc="left", fontsize=24, pad=18, weight="bold")
    fig.tight_layout()
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


def place_label(row: pd.Series) -> str:
    label = str(row.get("canonical_name") or row.get("term") or "").strip()
    state = map_norm(row.get("state", "") or row.get("admin1_name", ""))
    if map_norm(label) in AMBIGUOUS_PLACE_TERMS and state == "texas":
        return f"{label.title()}, TX"
    return label


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
    unmapped: list[dict[str, object]] = []
    mapping_audit: list[dict[str, object]] = []
    ambiguous_matches: list[dict[str, object]] = []
    maps_generated: list[str] = []
    skipped: list[str] = []
    mapped_terms: set[int] = set()
    country_lookup = match_records(discovery.country_layer, ("NAME", "NAME_LONG", "ADMIN", "ISO_A3", "ADM0_A3"))
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
                matched_layer = "natural_earth_admin0"
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
                matched_layer = "census_admin1" if admin1_layer == discovery.census_state_layer else "natural_earth_admin1"
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
            points.append(
                {
                    "lon": lon,
                    "lat": lat,
                    "count": count,
                    "label": place_label(row),
                    "country_index": row_country_index,
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
                    points.append(
                        {
                            "lon": lon,
                            "lat": lat,
                            "count": count,
                            "label": place_label(row),
                            "country_index": place_country,
                        }
                    )
                    mapped_terms.add(idx)
                    matched = True
                    matched_layer = "natural_earth_populated_places"
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

    def save_or_skip(name: str, path: Path | None, reason: str) -> None:
        if path and path.exists():
            maps_generated.append(str(path))
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

    if discovery.country_layer:
        save_or_skip(
            "world",
            save_layer_map(
                discovery.country_layer.records,
                dict(country_counts),
                points,
                "Geography Heatmap: World",
                f"{slug}_geography_heatmap_world",
                generated,
                (-180, 180, -60, 85),
                max_labels=MAX_MAP_LABELS["world"],
                max_points=80,
            ),
            "country layer unavailable",
        )
        europe_records = [
            record for record in discovery.country_layer.records
            if map_norm(record_value(record, "CONTINENT", "REGION_UN")) == "europe"
            or "europe" in map_norm(record_value(record, "SUBREGION", "REGION_WB"))
        ]
        regional_specs = [
            ("europe", "Europe", (-25, 45, 34, 72), lambda record: map_norm(record_value(record, "CONTINENT", "REGION_UN")) == "europe" or "europe" in map_norm(record_value(record, "SUBREGION", "REGION_WB"))),
            ("east_asia", "East Asia", (95, 150, 15, 55), lambda record: map_norm(record_value(record, "NAME", "NAME_LONG", "ADMIN", "SOVEREIGNT")) in EAST_ASIA_COUNTRIES or "east asia" in map_norm(record_value(record, "SUBREGION", "REGION_UN"))),
            ("latin_america", "Latin America", (-120, -30, -60, 35), lambda record: map_norm(record_value(record, "SUBREGION")) in LATIN_AMERICA_SUBREGIONS or "latin america" in map_norm(record_value(record, "REGION_WB")) or map_norm(record_value(record, "NAME", "ADMIN")) == "mexico"),
            ("africa", "Africa", (-20, 55, -37, 38), lambda record: map_norm(record_value(record, "CONTINENT")) == "africa"),
            ("middle_east", "Middle East", (25, 65, 10, 43), lambda record: map_norm(record_value(record, "NAME", "NAME_LONG", "ADMIN", "SOVEREIGNT")) in MIDDLE_EAST_COUNTRIES or "middle east" in map_norm(record_value(record, "SUBREGION", "REGION_WB"))),
            ("north_america", "North America", (-170, -50, 5, 85), lambda record: map_norm(record_value(record, "NAME", "NAME_LONG", "ADMIN", "SOVEREIGNT")) in NORTH_AMERICA_COUNTRIES),
        ]
        regions = plan.get("regions", {}) if isinstance(plan.get("regions"), dict) else {}
        for region_key, title, extent, predicate in regional_specs:
            if not regions.get(region_key):
                skipped.append(f"{region_key}: no detected countries in region")
                continue
            records, counts = indexed_subset(discovery.country_layer.records, predicate)
            save_or_skip(
                region_key,
                save_layer_map(
                    records,
                    counts,
                    subset_points(extent),
                    f"Geography Heatmap: {title}",
                    f"{slug}_geography_heatmap_{region_key}",
                    generated,
                    extent,
                    max_labels=MAX_MAP_LABELS["region"],
                    max_points=120,
                ),
                f"no {title} country records in country layer",
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
                        "Geography Heatmap: World Regions",
                        f"{slug}_geography_overview_world_regions",
                        generated,
                        (-180, 180, -60, 85),
                        max_labels=MAX_MAP_LABELS["world"],
                        max_points=0,
                    ),
                    "region grouping failed",
                )
            else:
                skipped.append("world_regions: no region field available for grouping")
        else:
            skipped.append("world_regions: no detected countries")
    else:
        skipped.extend(["world: country layer unavailable", "regional maps: country layer unavailable", "world_regions: country layer unavailable"])

    if admin1_layer and plan.get("us"):
        us_records = us_admin1_records(admin1_layer)
        admin_original = {id(record): idx for idx, record in enumerate(admin1_layer.records)}
        us_counts = {local_idx: state_counts[admin_original[id(record)]] for local_idx, record in enumerate(us_records) if admin_original[id(record)] in state_counts}
        us_points = subset_points((-130, -65, 24, 50), {"united states"})
        save_or_skip(
            "us",
            save_layer_map(us_records, us_counts, us_points, "Geography Heatmap: United States", f"{slug}_geography_heatmap_us", generated, (-126, -66, 24, 50), max_labels=MAX_MAP_LABELS["us"], max_points=160),
            "state/admin-1 layer unavailable",
        )
    elif not plan.get("us"):
        skipped.append("us: no U.S. geography detected")
    else:
        skipped.append("us: state/admin-1 layer unavailable")

    if admin1_layer and plan.get("texas"):
        texas_source = discovery.census_county_layer or admin1_layer
        texas_records = [
            record for record in texas_source.records
            if map_norm(record_value(record, "NAME", "name", "region", "STUSPS", "state")) == "texas"
            or str(record_value(record, "iso_3166_2")).upper() == "US-TX"
            or str(record_value(record, "STATEFP")).zfill(2) == "48"
        ]
        texas_points = subset_points((-107, -93, 25, 37), {"united states"})
        texas_note = "Texas county polygons unavailable; generated Texas outline/point map instead." if not discovery.census_county_layer else ""
        save_or_skip("texas", save_layer_map(texas_records, {}, texas_points, "Geography Heatmap: Texas", f"{slug}_geography_heatmap_texas", generated, (-107, -93, 25, 37), max_labels=MAX_MAP_LABELS["texas"], max_points=120), texas_note or "Texas geometry unavailable")
        if texas_note:
            skipped.append(texas_note)
    elif not plan.get("texas"):
        skipped.append("texas: no Texas geography detected")
    else:
        skipped.append("texas: state/admin-1 layer unavailable")

    if discovery.country_layer and isinstance(plan.get("country_maps"), list):
        country_layer_original = {id(record): idx for idx, record in enumerate(discovery.country_layer.records)}
        admin_records_by_country: dict[str, list[dict[str, object]]] = defaultdict(list)
        if admin1_layer:
            for record in admin1_layer.records:
                key = record_country_key(record)
                if key:
                    admin_records_by_country[key].append(record)
        for country_row in plan["country_maps"]:
            country_key = str(country_row.get("country_key") or "")
            country_index = int(country_row.get("country_index"))
            country_name = str(country_row.get("country") or country_key).strip()
            output_key = re.sub(r"[^a-z0-9]+", "_", country_key).strip("_") or str(country_index)
            country_records = admin_records_by_country.get(country_key) or [discovery.country_layer.records[country_index]]
            if country_key == "united states" and admin1_layer:
                country_records = us_admin1_records(admin1_layer) or country_records
            local_counts: dict[int, int] = {}
            if country_records and country_records[0] in discovery.country_layer.records:
                local_counts = {local_idx: country_counts[country_layer_original[id(record)]] for local_idx, record in enumerate(country_records) if country_layer_original[id(record)] in country_counts}
            elif admin1_layer:
                admin_original = {id(record): idx for idx, record in enumerate(admin1_layer.records)}
                local_counts = {local_idx: state_counts[admin_original[id(record)]] for local_idx, record in enumerate(country_records) if admin_original[id(record)] in state_counts}
            extent = layer_extent(country_records)
            country_points = [
                point for point in points
                if point.get("country_index") == country_index and (extent is None or point_in_extent(point, extent))
            ]
            save_or_skip(
                f"country_{output_key}",
                save_layer_map(
                    country_records,
                    local_counts,
                    country_points,
                    f"Geography Heatmap: {country_name}",
                    f"{slug}_geography_heatmap_country_{output_key}",
                    generated,
                    extent,
                    max_labels=MAX_MAP_LABELS["country"],
                    max_points=160,
                ),
                f"{country_name} geometry unavailable",
            )

    unmapped_path = processed_path(f"{slug}_unmapped_geography_terms.csv")
    pd.DataFrame(unmapped, columns=["original_term", "normalized_term", "canonical_name", "count", "geo_type", "state", "country", "latitude", "longitude", "attempted_map_type", "reason_not_mapped"]).to_csv(unmapped_path, index=False)
    remember_generated(generated, unmapped_path)
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
    write_geography_map_validation(slug, discovery, maps_generated, skipped, len(mapped_terms), len(unmapped), generated)
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
    required = {
        "author_network_hindex": VISUALS_DIR / "author_network_hindex.png",
        "keyword_network_all_keywords": VISUALS_DIR / "keyword_network_all_keywords.png",
        "geography_heatmap_world": VISUALS_DIR / f"{slug}_geography_heatmap_world.png",
        "geography_heatmap_us": VISUALS_DIR / f"{slug}_geography_heatmap_us.png",
        "geography_heatmap_texas": VISUALS_DIR / f"{slug}_geography_heatmap_texas.png",
        "geography_heatmap_europe": VISUALS_DIR / f"{slug}_geography_heatmap_europe.png",
        "geography_heatmap_east_asia": VISUALS_DIR / f"{slug}_geography_heatmap_east_asia.png",
    }
    missing_geometry_files = missing_geometry_files or []
    lines = [
        "Visualization validation report",
        "",
        f"PNG visualizations created: {len([path for path in generated_visuals if path.suffix.lower() == '.png'])}",
        f"Non-PNG visualization files created: {len(non_png)}",
        f"Unlabeled files created: {len(unlabeled)}",
        f"Duplicate labeled/unlabeled pairs: {len(duplicate_pairs)}",
        f"H-index author visualization created: {'yes' if required['author_network_hindex'].exists() else 'no'}",
        f"Geography terms mapped: {mapped_geography_terms}",
        f"Geography terms unmapped: {unmapped_geography_terms}",
        f"Missing geometry source files: {len(missing_geometry_files)}",
        "",
        "Required visualization categories:",
    ]
    for label, path in required.items():
        lines.append(f"- {label}: {'present' if path.exists() else 'missing'}")
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
    parser.add_argument("--debug", action="store_true", help="Print sample tokens, detected drugs, and trimmed RxNorm responses.")
    return parser.parse_args()


def main() -> None:
    global DEBUG, ENABLE_WORLD_REGIONS_OVERVIEW, VALIDATION_REPORT
    args = parse_args()
    DEBUG = args.debug
    ENABLE_WORLD_REGIONS_OVERVIEW = bool(args.include_world_regions_map)
    VALIDATION_REPORT = processed_path(f"{args.slug or 'visualization'}_visualization_validation_report.txt")
    VISUALS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    VOS_DIR.mkdir(parents=True, exist_ok=True)
    files = detect_files()
    if args.core:
        files["core"] = args.core
    elif args.slug:
        for candidate in (
            OUTPUTS_DIR / f"{args.slug}_year_limited_records.csv",
            OUTPUTS_DIR / f"{args.slug}.csv",
            OUTPUTS_DIR / f"{args.slug}_cleaned.csv",
        ):
            if candidate.exists():
                files["core"] = candidate
                break
    generated: list[Path] = []
    mapped_geography_terms = 0
    unmapped_geography_terms = 0
    missing_geometry_files: list[Path] = []
    validation_slug = args.slug

    print("Detected inputs:")
    for key, value in files.items():
        if isinstance(value, list):
            print(f"  {key}: {len(value)} files")
        else:
            print(f"  {key}: {value.name if value else 'not found'}")

    if files["year_counts"]:
        publication_trends(files["year_counts"], generated)  # type: ignore[arg-type]
    elif files["core"]:
        core_df = safe_read_csv(files["core"])  # type: ignore[arg-type]
        if "year" in core_df.columns:
            tmp = core_df.groupby("year", as_index=False).size().rename(columns={"size": "publications"})
            tmp_path = processed_path("_publication_years_from_core.csv")
            tmp.to_csv(tmp_path, index=False)
            publication_trends(tmp_path, generated)
            tmp_path.unlink(missing_ok=True)

    if files["country_year_counts"]:
        country_trends(files["country_year_counts"], generated)  # type: ignore[arg-type]

    if files["top_authors"]:
        horizontal_bar(
            files["top_authors"],  # type: ignore[arg-type]
            "author",
            "total_citations" if "total_citations" in safe_read_csv(files["top_authors"]).columns else "publications",  # type: ignore[arg-type]
            f"Top {TOP_AUTHORS} Authors",
            "top_authors",
            generated,
            TOP_AUTHORS,
        )

    if files["top_papers"]:
        horizontal_bar(
            files["top_papers"],  # type: ignore[arg-type]
            "title",
            "citations",
            f"Top {TOP_PAPERS} Papers by Citations",
            "top_papers",
            generated,
            TOP_PAPERS,
        )

    networks = files["networks"] if isinstance(files["networks"], list) else []
    output_slug = args.slug or (files["core"].with_suffix("").name.replace("_year_limited_records", "").replace("_cleaned", "") if isinstance(files.get("core"), Path) else "bibliometrics")
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

    author_graph = graph_from_edges(author_edges, max_edges=420)
    if ENABLE_HINDEX_AUTHOR_VIS:
        apply_author_metrics(author_graph, load_author_metrics(files.get("top_authors") if isinstance(files.get("top_authors"), Path) else None))
    plot_network(
        author_graph,
        "Author Collaboration Network",
        "author_network_hindex" if ENABLE_HINDEX_AUTHOR_VIS else "author_network",
        generated,
        top_nodes=80,
        label_top_n=18,
        node_kind="Author",
        size_metric="publication_count",
        border_metric="h_index" if ENABLE_HINDEX_AUTHOR_VIS else "",
    )
    if ENABLE_HINDEX_AUTHOR_VIS:
        save_author_hindex_audit(output_slug, author_graph, generated)
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

    if files["core"]:
        query = args.query or infer_query(files["core"])  # type: ignore[arg-type]
        with timed_stage("Extracting general keywords"):
            keyword_outputs = extract_visual_keyword_pipelines(files["core"], query, generated)  # type: ignore[arg-type]
        stem = files["core"].with_suffix("").name  # type: ignore[union-attr]
        slug = args.slug or stem.replace("_year_limited_records", "").replace("_cleaned", "")
        validation_slug = slug
        geocensus_drug_counts, geocensus_drug_edges = load_geocensus_drug_outputs(slug, stem)
        print("Saving visuals...")
        with timed_stage("Generating visuals"):
            for mode, (counts, edges) in tqdm(keyword_outputs.items(), desc="Generating visuals"):
                plot_keyword_bar(counts, mode, stem, generated)
                if mode in {"general", "filtered"}:
                    weights = dict(zip(counts["keyword"], counts["count"], strict=False)) if not counts.empty else {}
                    plot_network(
                        graph_from_edges(edges, max_edges=650, node_weights=weights),
                        f"Filtered {mode.title()} Keyword Co-occurrence Network",
                        f"{stem}_keyword_network_{mode}",
                        generated,
                        top_nodes=50,
                        label_top_n=16,
                        min_edge_weight=2,
                        keep_largest_component=True,
                        node_metric="frequency",
                    )
            if not geocensus_drug_counts.empty:
                weights = dict(zip(geocensus_drug_counts["drug_name"], geocensus_drug_counts["count"], strict=False))
                drug_graph = graph_from_edges(geocensus_drug_edges, max_edges=650, node_weights=weights)
                plot_drug_network(
                    drug_graph,
                    geocensus_drug_counts,
                    "Drug Intervention Network by RxNorm Type",
                    f"{slug}_network_drugs",
                    generated,
                    min_frequency=max(1, args.drug_visual_min_frequency),
                    min_degree=max(1, args.drug_visual_min_degree),
                )
            else:
                print("Skipping drug visuals; run GeoCensus.py first to create drug extraction outputs.")
            plot_reference_term_counts(slug, generated)
            if ENABLE_GEO_MAPS:
                mapped_geography_terms, unmapped_geography_terms, missing_geometry_files = generate_geography_heatmaps(slug, generated)
            extract_all_keyword_network(files["core"], generated)  # type: ignore[arg-type]
            keyword_counts, keyword_edges = keyword_outputs["filtered"]
            save_keyword_outputs(stem, keyword_counts, keyword_edges, generated)
            keyword_weights = dict(zip(keyword_counts["keyword"], keyword_counts["count"], strict=False))
            plot_network(
                graph_from_edges(keyword_edges, max_edges=650, node_weights=keyword_weights),
                "Filtered Keyword Co-occurrence Network",
                f"{stem}_keyword_network",
                generated,
                top_nodes=50,
                label_top_n=16,
                min_edge_weight=2,
                keep_largest_component=True,
                node_metric="frequency",
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
