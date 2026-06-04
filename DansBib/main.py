"""Entry point for the bibliometric analysis pipeline."""

from __future__ import annotations

import argparse
import glob
import itertools
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

QueryInput = str | dict[str, str]

from apis.openalex_api import search_openalex
from apis.pubmed_api import search_pubmed
from apis.scopus_api import search_scopus, check_pybliometrics_setup_once
from apis.wos_api import search_wos
from pipeline_capabilities import (
    CONCEPT_PROFILES,
    LIVE_API_DATABASES,
    RIS_SOURCE_LABELS,
    RisInput,
    normalize_ris_source,
)
from processing.analysis import (
    prepare_analysis_dataset,
    run_citation_analysis,
    run_collaboration_analysis,
    run_h_index_analysis,
    run_institutional_temporal_analysis,
    run_publication_year_analysis,
)
from processing.deduplicate import deduplicate_records
from processing.filters import apply_filters, log_filter_effects, merge_filters, parse_query_flags
from processing.geography import apply_country_extraction
from processing.metrics import compute_metrics
from processing.normalize import normalize_doi, normalize_records, parse_year
from processing.reference_cache import (
    build_reference_cache,
    extract_demographic_terms,
    extract_geographic_terms,
)
from processing.ris_parser import load_ris_files
from utils.config import DEFAULT_DATABASES, RECOGNIZED_DATABASES, get_scaling_mode, get_scopus_api_key, get_wos_api_key
from utils.runtime_paths import configure_matplotlib_cache

LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
SCHEMA_COLUMNS = ["title", "doi", "authors", "year", "citations", "source"]
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
configure_matplotlib_cache(PROJECT_DIR)
DEFAULT_RIS_RAW_DIR = os.getenv("DANSBIB_RIS_RAW_DIR", os.path.join(PROJECT_DIR, "data", "raw"))
SCOPUS_STATUS_URL = "https://api.elsevier.com/content/search/scopus"
WOS_STATUS_URL = "https://api.clarivate.com/api/wos"


def _display_db_name(db_key: str) -> str:
    """Return a user-friendly database name for terminal status output."""
    names = {
        "openalex": "OpenAlex",
        "pubmed": "PubMed",
        "scopus": "Scopus",
        "wos": "Web of Science",
        "covidence": "Covidence",
    }
    return names.get(db_key.lower(), db_key)


@contextmanager
def _api_spinner(db_key: str):
    """Show a lightweight spinner while an API request is in progress."""
    stop_event = threading.Event()
    display_name = _display_db_name(db_key)
    frames = itertools.cycle(["|", "/", "-", "\\"])
    stream = sys.stderr

    def _spin() -> None:
        while not stop_event.wait(0.5):
            frame = next(frames)
            stream.write(f"\rQuerying {display_name}... {frame}")
            stream.flush()

    stream.write(f"Querying {display_name}... ⏳\n")
    stream.flush()
    spinner_thread = threading.Thread(target=_spin, daemon=True)
    spinner_thread.start()
    try:
        yield
    finally:
        stop_event.set()
        spinner_thread.join(timeout=1)
        stream.write("\r" + " " * 60 + "\r")
        stream.flush()


def clean_filename(query: str) -> str:
    """Return a filesystem-safe, lowercase, underscore-separated filename for a query."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", query.lower()).strip("_")


def _call_api_safe(
    func: Callable[..., pd.DataFrame],
    query: str,
    db_key: str,
    filters: dict[str, bool | None] | None = None,
) -> pd.DataFrame:
    """Call an API function and ensure a DataFrame is returned. Exceptions are logged and an empty DataFrame returned."""
    try:
        df = func(query, filters=filters)
        if not isinstance(df, pd.DataFrame):
            LOGGER.warning("%s did not return a DataFrame; coercing to empty DataFrame.", db_key)
            return pd.DataFrame(columns=SCHEMA_COLUMNS)
        return df
    except Exception as exc:  # pragma: no cover - depends on network and API state
        LOGGER.warning("%s query failed: %s", db_key, exc)
        return pd.DataFrame(columns=SCHEMA_COLUMNS)


def extract_year(date_value: object) -> int | None:
    """
    Extract a 4-digit year (1800-2099) from any string/date-like value.
    Returns int or None.
    """
    if date_value in (None, ""):
        return None
    match = re.search(r"\b(18|19|20)\d{2}\b", str(date_value))
    return int(match.group()) if match else None


def apply_year_fix(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Apply robust year extraction to the normalized year column."""
    if dataframe.empty or "year" not in dataframe.columns:
        return dataframe
    fixed = dataframe.copy()
    fixed["year"] = fixed["year"].apply(extract_year)
    return fixed


def debug_country_output(dataframe: pd.DataFrame, output_dir: str = "data/outputs", prefix: str = "") -> None:
    """Write country parsing diagnostics for review."""
    if dataframe.empty or "country" not in dataframe.columns:
        return

    os.makedirs(output_dir, exist_ok=True)
    unique_countries = sorted(str(country) for country in dataframe["country"].dropna().unique())

    print("\n=== UNIQUE COUNTRY VALUES ===")
    for country in unique_countries:
        print(country)

    pd.DataFrame(unique_countries, columns=["country"]).to_csv(
        os.path.join(output_dir, f"{prefix}country_list_debug.csv"),
        index=False,
    )

    bad = dataframe[dataframe["country"].isna()].copy()
    print("\n=== SAMPLE UNPARSED AFFILIATIONS ===")
    if "affiliations" in bad.columns:
        print(bad["affiliations"].head(20))
    else:
        print(pd.Series(dtype=str))

    bad.to_csv(os.path.join(output_dir, f"{prefix}country_unparsed_debug.csv"), index=False)

    if "affiliations" in dataframe.columns:
        affiliations = dataframe["affiliations"].astype(str)
        debug_columns = [column for column in ["affiliations", "country"] if column in dataframe.columns]

        print("\n=== CHECKING EDGE CASE FIXES ===")
        print("\nNeijiang cases:")
        print(dataframe[affiliations.str.contains("neijiang", case=False, na=False)][debug_columns].head())

        print("\nTurkey false positive cases:")
        print(dataframe[affiliations.str.contains("turkey|\\.tr", case=False, na=False, regex=True)][debug_columns].head())

        print("\nIsle of Man cases:")
        print(dataframe[affiliations.str.contains("isle of man", case=False, na=False)][debug_columns].head())


def investigate_year_distribution(dataframe: pd.DataFrame, output_dir: str = "data/outputs", prefix: str = "") -> None:
    """Log and save diagnostics for suspicious recent publication-year spikes."""
    if dataframe.empty or "year" not in dataframe.columns:
        return

    os.makedirs(output_dir, exist_ok=True)
    working = dataframe.copy()
    years = working["year"].map(parse_year)
    year_counts = years.dropna().astype(int).value_counts().sort_index()
    LOGGER.info("Year distribution tail: %s", year_counts.tail(10).to_dict())

    recent = working[years.fillna(0).astype(int) >= 2024].copy()
    LOGGER.info("Records in 2024+: %d", len(recent))

    early_flags = []
    for col in ["document_type", "publication_status"]:
        if col in recent.columns:
            early_flags.append(recent[col].astype(str).str.contains("early|ahead", case=False, na=False))

    if early_flags:
        early_count = pd.concat(early_flags, axis=1).any(axis=1).sum()
        LOGGER.info("Early-access-like records in 2024+: %d", int(early_count))

    recent.to_csv(os.path.join(output_dir, f"{prefix}debug_recent_years.csv"), index=False)


def flag_early_access(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Add a central early-access flag based on publication metadata."""
    if dataframe.empty:
        return dataframe

    flagged = dataframe.copy()
    flagged["is_early_access"] = False

    for col in ["document_type", "publication_status"]:
        if col in flagged.columns:
            flagged["is_early_access"] = flagged["is_early_access"] | flagged[col].astype(str).str.contains(
                r"early|ahead of print|epub",
                case=False,
                na=False,
            )

    return flagged


def yearly_counts(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Return publication counts by parsed year."""
    if dataframe.empty or "year" not in dataframe.columns:
        return pd.DataFrame(columns=["year", "count"])

    years = dataframe["year"].map(parse_year).dropna().astype(int)
    return (
        years.value_counts()
        .sort_index()
        .rename_axis("year")
        .reset_index(name="count")
    )


def build_year_trends(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Build all/no-EA/EA-only yearly publication counts with EA share."""
    if dataframe.empty:
        return pd.DataFrame(columns=["year", "all", "no_ea", "ea_only", "ea_share"])

    flagged = dataframe.copy() if "is_early_access" in dataframe.columns else flag_early_access(dataframe)

    all_years = yearly_counts(flagged).rename(columns={"count": "all"})
    no_ea_years = yearly_counts(flagged[~flagged["is_early_access"]]).rename(columns={"count": "no_ea"})
    ea_years = yearly_counts(flagged[flagged["is_early_access"]]).rename(columns={"count": "ea_only"})

    trends = (
        all_years.merge(no_ea_years, on="year", how="outer")
        .merge(ea_years, on="year", how="outer")
        .fillna(0)
        .sort_values("year")
        .reset_index(drop=True)
    )
    for col in ["all", "no_ea", "ea_only"]:
        trends[col] = trends[col].astype(int)
    trends["ea_share"] = 0.0
    nonzero = trends["all"] > 0
    trends.loc[nonzero, "ea_share"] = trends.loc[nonzero, "ea_only"] / trends.loc[nonzero, "all"]
    return trends


def plot_year_trends(trends: pd.DataFrame) -> None:
    """Show a quick year-trend comparison plot when matplotlib is available."""
    import matplotlib.pyplot as plt

    plt.figure()
    plt.plot(trends["year"], trends["all"], label="All (incl. EA)")
    plt.plot(trends["year"], trends["no_ea"], label="No EA (corrected)")
    plt.plot(trends["year"], trends["ea_only"], label="EA only", linestyle="--")
    plt.xlabel("Year")
    plt.ylabel("Count")
    plt.title("Publications by Year (with vs without Early Access)")
    plt.legend()
    plt.tight_layout()
    plt.show()


def save_year_trends(trends: pd.DataFrame, out_dir: str = "data/outputs", prefix: str = "") -> None:
    """Save year trend comparison data for reporting."""
    os.makedirs(out_dir, exist_ok=True)
    trends.to_csv(os.path.join(out_dir, f"{prefix}year_trends_ea_comparison.csv"), index=False)


def _records_with_ris_source(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Return RIS-imported records when identifiable, otherwise all imported records."""
    if dataframe.empty or "ris_file" not in dataframe.columns:
        return dataframe.copy()
    ris_mask = dataframe["ris_file"].fillna("").astype(str).str.strip().ne("")
    return dataframe.loc[ris_mask].copy() if ris_mask.any() else dataframe.copy()


def _year_limited_records(dataframe: pd.DataFrame, start_year: int | None, end_year: int | None) -> pd.DataFrame:
    """Apply only the requested publication-year range."""
    if dataframe.empty or "year" not in dataframe.columns or (start_year is None and end_year is None):
        return dataframe.copy()
    years = pd.to_numeric(dataframe["year"], errors="coerce")
    mask = years.notna()
    if start_year is not None:
        mask &= years.ge(start_year)
    if end_year is not None:
        mask &= years.le(end_year)
    return dataframe.loc[mask].copy()


def _publication_year_counts(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Return annual publication counts with the reporting column name."""
    counts = yearly_counts(dataframe).rename(columns={"count": "publications"})
    if counts.empty:
        return pd.DataFrame(columns=["year", "publications"])
    return counts


def _concept_term_qa_year_counts(dataframe: pd.DataFrame, concept_matches: pd.DataFrame) -> pd.DataFrame:
    """Return annual counts for all records and each concept-block match."""
    if dataframe.empty or "year" not in dataframe.columns:
        columns = ["year", "all_records"] + [column.replace("matches_", "", 1) for column in concept_matches.columns]
        return pd.DataFrame(columns=columns)

    working = dataframe.copy()
    working["_year"] = pd.to_numeric(working["year"], errors="coerce")
    working = working[working["_year"].notna()].copy()
    if working.empty:
        columns = ["year", "all_records"] + [column.replace("matches_", "", 1) for column in concept_matches.columns]
        return pd.DataFrame(columns=columns)

    aligned_matches = concept_matches.reindex(working.index).fillna(False)
    rows = []
    for year, group in working.groupby(working["_year"].astype(int), sort=True):
        row: dict[str, int] = {"year": int(year), "all_records": int(len(group))}
        for column in aligned_matches.columns:
            block_name = column.replace("matches_", "", 1)
            row[block_name] = int(aligned_matches.loc[group.index, column].astype(bool).sum())
        rows.append(row)
    return pd.DataFrame(rows).sort_values("year").reset_index(drop=True)


def _save_publication_trend_png(
    counts: pd.DataFrame,
    path: str,
    title: str,
    subtitle: str,
    y_columns: list[str] | None = None,
) -> None:
    """Save a static publication trend PNG for reports."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        LOGGER.warning("Matplotlib unavailable; skipped publication trend PNG: %s", path)
        return

    plot_df = counts.copy()
    if "year" not in plot_df.columns:
        return
    plot_df["year"] = pd.to_numeric(plot_df["year"], errors="coerce")
    plot_df = plot_df.dropna(subset=["year"]).sort_values("year")
    if plot_df.empty:
        return

    y_columns = y_columns or [column for column in ("publications", "all_records") if column in plot_df.columns]
    y_columns = [column for column in y_columns if column in plot_df.columns]
    if not y_columns:
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig, ax = plt.subplots(figsize=(13, 7), dpi=220)
    colors = ["#2563eb", "#f97316", "#10b981", "#a855f7"]
    for idx, column in enumerate(y_columns):
        values = pd.to_numeric(plot_df[column], errors="coerce").fillna(0)
        label = column.replace("_", " ").title()
        ax.plot(plot_df["year"], values, marker="o", linewidth=2.4, label=label, color=colors[idx % len(colors)])

    ax.set_title(title, loc="left", fontsize=20, fontweight="bold", pad=20)
    ax.text(0, 1.01, subtitle, transform=ax.transAxes, ha="left", va="bottom", fontsize=10.5, color="#4b5563")
    ax.set_xlabel("Publication Year")
    ax.set_ylabel("Records")
    ax.grid(axis="y", color="#dbe2ea", linewidth=0.8)
    ax.grid(axis="x", visible=False)
    ax.spines[["top", "right"]].set_visible(False)
    if len(y_columns) > 1:
        ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def save_publication_trend_outputs(
    all_records: pd.DataFrame,
    target_year_records: pd.DataFrame,
    concept_matches: pd.DataFrame,
    outputs_dir: str,
    visuals_dir: str,
    slug: str,
    start_year: int | None,
    end_year: int | None,
) -> list[str]:
    """Save the three requested publication-trend count files and figures."""
    os.makedirs(outputs_dir, exist_ok=True)
    os.makedirs(visuals_dir, exist_ok=True)
    generated: list[str] = []

    trend_specs = [
        (
            "all_ris_records",
            _publication_year_counts(all_records),
            "Publication Trends: All RIS Records",
            "Annual publication counts from all records imported from the source RIS file",
            ["publications"],
        ),
        (
            "target_year_range",
            _publication_year_counts(target_year_records),
            "Publication Trends: Target Year Range",
            f"Annual publication counts after applying the {start_year or ''}-{end_year or ''} publication year limit",
            ["publications"],
        ),
    ]

    qa_counts = _concept_term_qa_year_counts(target_year_records, concept_matches)
    trend_specs.append(
        (
            "concept_term_qa",
            qa_counts,
            "Publication Trends: Concept-Term QA",
            "Annual counts showing records with identifiable telehealth, cancer, and geography terms; records are not excluded",
            [column for column in ["all_records", "telehealth", "cancer", "geography"] if column in qa_counts.columns],
        )
    )

    for label, counts, title, subtitle, y_columns in trend_specs:
        counts_path = os.path.join(outputs_dir, f"{slug}_year_counts_{label}.csv")
        figure_path = os.path.join(visuals_dir, f"{slug}_publication_trends_{label}.png")
        counts.to_csv(counts_path, index=False)
        _save_publication_trend_png(counts, figure_path, title, subtitle, y_columns)
        generated.append(counts_path)
        if os.path.exists(figure_path):
            generated.append(figure_path)

    return generated


def save_clean_csv(dataframe: pd.DataFrame, path: str) -> None:
    """Write a CSV with stable UTF-8 string serialization and no NaN cells."""
    clean = dataframe.copy().fillna("")
    for col in clean.columns:
        clean[col] = clean[col].astype(str)
    clean.to_csv(path, index=False, encoding="utf-8")


def enforce_vosviewer_format(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Convert multi-value fields into delimiters expected by VOSviewer imports."""
    formatted = dataframe.copy()
    if "authors" in formatted.columns:
        formatted["authors"] = formatted["authors"].apply(
            lambda value: ";".join(value) if isinstance(value, list) else ";".join(_split_export_values(value, ";"))
        )
    if "keywords" in formatted.columns:
        formatted["keywords"] = formatted["keywords"].apply(
            lambda value: ",".join(value) if isinstance(value, list) else ",".join(_split_export_values(value, ","))
        )
    return formatted


def _split_export_values(value: object, preferred_separator: str) -> list[str]:
    """Split list-like strings without changing single text values unnecessarily."""
    text = str(value or "").strip()
    if not text:
        return []
    if preferred_separator in text:
        return [part.strip() for part in text.split(preferred_separator) if part.strip()]
    if preferred_separator == ";":
        return [text]
    alternate_separator = "," if preferred_separator == ";" else ";"
    if alternate_separator in text:
        return [part.strip() for part in text.split(alternate_separator) if part.strip()]
    return [text]


def _log_doi_diagnostics(dataframe: pd.DataFrame) -> int:
    """Log sample and overlap diagnostics for normalized DOIs and return cross-source merge count."""
    if dataframe.empty:
        LOGGER.info("Unique normalized DOIs: 0")
        LOGGER.info("Cross-source merges: 0")
        LOGGER.info("OpenAlex ∩ PubMed: 0")
        LOGGER.info("OpenAlex ∩ Scopus: 0")
        LOGGER.info("PubMed ∩ Scopus: 0")
        LOGGER.info("Covidence DOI count: 0")
        return 0

    diagnostics_df = dataframe.copy()
    source_dois: dict[str, set[str]] = {}

    for source_name in ("openalex", "pubmed", "scopus", "wos", "covidence"):
        source_df = diagnostics_df[diagnostics_df["source"].astype(str).str.lower() == source_name].copy()
        raw_sample = [str(value or "").strip() for value in source_df["doi"].head(10).tolist()]
        normalized_sample = [normalize_doi(value) for value in raw_sample]
        LOGGER.info("%s sample DOIs: %s", _display_db_name(source_name), raw_sample)
        LOGGER.info("%s normalized DOIs: %s", _display_db_name(source_name), normalized_sample)
        source_dois[source_name] = {normalize_doi(doi) for doi in source_df["doi"].tolist() if normalize_doi(doi)}

    diagnostics_df["doi"] = diagnostics_df["doi"].map(normalize_doi)
    grouped = diagnostics_df[diagnostics_df["doi"] != ""].groupby("doi", sort=False)
    cross_source_merges = 0
    for _, group in grouped:
        unique_sources = {str(source or "").strip() for source in group["source"] if str(source or "").strip()}
        if len(unique_sources) > 1:
            cross_source_merges += 1

    unique_dois = diagnostics_df.loc[diagnostics_df["doi"] != "", "doi"].nunique()
    LOGGER.info("Unique normalized DOIs: %d", int(unique_dois))
    LOGGER.info("Cross-source merges: %d", cross_source_merges)
    LOGGER.info("OpenAlex ∩ PubMed: %d", len(source_dois["openalex"] & source_dois["pubmed"]))
    LOGGER.info("OpenAlex ∩ Scopus: %d", len(source_dois["openalex"] & source_dois["scopus"]))
    LOGGER.info("PubMed ∩ Scopus: %d", len(source_dois["pubmed"] & source_dois["scopus"]))
    LOGGER.info("Covidence DOI count: %d", len(source_dois["covidence"]))
    return cross_source_merges


def get_default_ris_files() -> list[str]:
    """Return RIS files from the configured project raw folder."""
    return sorted(set(glob.glob(os.path.join(DEFAULT_RIS_RAW_DIR, "*.ris"))))


def build_ris_inputs(ris_files: list[str] | None, ris_file_sources: list[str] | None = None) -> list[RisInput]:
    """Pair RIS paths with optional per-file source/origin labels."""
    if not ris_files:
        return []
    sources = ris_file_sources or []
    if sources and len(sources) != len(ris_files):
        raise ValueError("--ris-file-sources count must match --ris-files count")
    return [
        RisInput(path=Path(path), source=normalize_ris_source(sources[index] if index < len(sources) else "unknown"))
        for index, path in enumerate(ris_files)
    ]


def write_pipeline_logs(raw: pd.DataFrame, normalized: pd.DataFrame, deduped: pd.DataFrame, outputs_dir: str, prefix: str = "") -> None:
    """Write reproducibility diagnostics for validation, deduplication, and missing metadata."""
    wos_raw = raw[raw.get("source", pd.Series(dtype=str)).astype(str).str.lower().eq("wos")].copy() if not raw.empty else pd.DataFrame()
    wos_validation = pd.DataFrame(
        {
            "total_wos_records": [int(len(wos_raw))],
            "records_with_wos_uid": [int(wos_raw.get("wos_uid", pd.Series(dtype=str)).astype(str).str.strip().ne("").sum()) if not wos_raw.empty else 0],
            "records_with_doi": [int(wos_raw.get("doi", pd.Series(dtype=str)).astype(str).str.strip().ne("").sum()) if not wos_raw.empty else 0],
        }
    )
    save_clean_csv(wos_validation, os.path.join(outputs_dir, f"{prefix}wos_validation_log.csv"))

    dedup_log = pd.DataFrame(
        [
            {
                "records_before_deduplication": int(len(normalized)),
                "records_after_deduplication": int(len(deduped)),
                "duplicates_removed": int(len(normalized) - len(deduped)),
            }
        ]
    )
    save_clean_csv(dedup_log, os.path.join(outputs_dir, f"{prefix}deduplication_log.csv"))

    covidence_raw = (
        raw.get("source_db", pd.Series(dtype=str)).astype(str).str.lower().eq("covidence")
        if not raw.empty
        else pd.Series(dtype=bool)
    )
    covidence_normalized = (
        normalized.get("source_db", pd.Series(dtype=str)).astype(str).str.lower().eq("covidence")
        if not normalized.empty
        else pd.Series(dtype=bool)
    )
    covidence_deduped = (
        deduped.get("source", pd.Series(dtype=str)).astype(str).str.lower().str.contains(r"\bcovidence\b", regex=True)
        if not deduped.empty
        else pd.Series(dtype=bool)
    )
    covidence_log = pd.DataFrame(
        [
            {
                "source_type": "curated_subset",
                "records_ingested": int(covidence_raw.sum()) if not covidence_raw.empty else 0,
                "records_before_deduplication": int(covidence_normalized.sum()) if not covidence_normalized.empty else 0,
                "records_after_deduplication": int(covidence_deduped.sum()) if not covidence_deduped.empty else 0,
                "duplicates_removed": max(
                    (int(covidence_normalized.sum()) if not covidence_normalized.empty else 0)
                    - (int(covidence_deduped.sum()) if not covidence_deduped.empty else 0),
                    0,
                ),
            }
        ]
    )
    save_clean_csv(covidence_log, os.path.join(outputs_dir, f"{prefix}covidence_ingestion_log.csv"))

    missing_rows = []
    for column in SCHEMA_COLUMNS:
        if column in deduped.columns:
            missing_rows.append({"field": column, "missing_count": int(deduped[column].isna().sum() + deduped[column].astype(str).str.strip().eq("").sum())})
    save_clean_csv(pd.DataFrame(missing_rows), os.path.join(outputs_dir, f"{prefix}missing_metadata_log.csv"))


def prompt_for_databases() -> tuple[list[str] | None, bool]:
    """Prompt interactively for live API database selection and optional RIS skipping."""
    options = {"1": "openalex", "2": "pubmed", "3": "scopus", "4": "wos"}

    print("\nSelect databases:")
    print("1. OpenAlex\n2. PubMed\n3. Scopus\n4. Web of Science\n5. No RIS files")
    try:
        raw = input("Enter selection (e.g. 123, 35 for Scopus no RIS, blank for defaults): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None, False

    if not raw:
        return None, False

    no_ris = "5" in raw
    selected = list(dict.fromkeys(options[c] for c in raw if c in options))
    return (selected if selected else None), no_ris


def _log_year_distribution(dataframe: pd.DataFrame, source_name: str) -> None:
    """Log year distribution and summary stats for a single source."""
    if source_name not in {"openalex", "pubmed", "scopus"}:
        return

    years = (
        dataframe["year"]
        .map(parse_year)
        .dropna()
        .astype(int)
    )
    display_name = _display_db_name(source_name)

    if years.empty:
        LOGGER.info("%s years: unavailable", display_name)
        return

    year_counts = years.value_counts().sort_index()
    median_year = int(years.median())
    min_year = int(years.min())
    max_year = int(years.max())

    LOGGER.info("%s year counts: %s", display_name, year_counts.to_dict())
    LOGGER.info("%s years: %d–%d", display_name, min_year, max_year)
    LOGGER.info("%s median year: %d", display_name, median_year)
    LOGGER.info("%s min year: %d", display_name, min_year)
    LOGGER.info("%s max year: %d", display_name, max_year)


def _probe_endpoint(url: str, headers: dict[str, str] | None = None, params: dict[str, str] | None = None) -> dict[str, object]:
    """Probe an API endpoint with a short timeout and capture availability details."""
    try:
        response = requests.get(url, headers=headers, params=params, timeout=(3, 5))
        if response.status_code == 403:
            return {"available": False, "status_code": 403}
        return {"available": True, "status_code": response.status_code}
    except (requests.ConnectionError, requests.Timeout):
        return {"available": False, "status_code": None}
    except requests.RequestException as exc:
        status_code = getattr(exc.response, "status_code", None)
        if status_code == 403:
            return {"available": False, "status_code": 403}
        return {"available": False, "status_code": status_code}


def check_network_status() -> dict[str, dict[str, object]]:
    """Check whether Scopus and Web of Science appear reachable before querying them."""
    scopus_headers = {}
    scopus_api_key = get_scopus_api_key()
    if scopus_api_key:
        scopus_headers["X-ELS-APIKey"] = scopus_api_key

    wos_headers = {}
    wos_api_key = get_wos_api_key()
    if wos_api_key:
        wos_headers["X-ApiKey"] = wos_api_key

    status = {
        "scopus": _probe_endpoint(
            SCOPUS_STATUS_URL,
            headers=scopus_headers or None,
            params={"query": "TITLE(test)", "count": "1"},
        ),
        "wos": _probe_endpoint(
            WOS_STATUS_URL,
            headers=wos_headers or None,
            params={"databaseId": "WOS", "usrQuery": "TS=test", "count": "1", "firstRecord": "1"},
        ),
    }

    print("Network status:")
    print(f"Scopus: {'available' if status['scopus']['available'] else 'unavailable'}")
    print(f"Web of Science: {'available' if status['wos']['available'] else 'unavailable'}")

    if not status["scopus"]["available"]:
        LOGGER.info("Scopus unavailable: likely not on institutional network")
    if not status["wos"]["available"]:
        LOGGER.info("Web of Science unavailable: access denied or network restricted")

    return status


# New: friendly query cleaning and optional synonym expansion
SYNONYMS = {
    "hus": "hemolytic uremic syndrome",
    "ckd": "chronic kidney disease",
}

QA_SEARCH_COLUMNS = (
    "title",
    "abstract",
    "keywords",
    "author_keywords",
    "index_keywords",
    "keywords_plus",
    "subject",
    "subjects",
    "category",
    "categories",
    "subject_category",
    "wos_categories",
    "research_areas",
)


def clean_query(raw: str) -> str:
    """
    Clean and mildly normalize user input for queries.

    Rules applied:
    - Strip leading/trailing whitespace.
    - Collapse multiple spaces to one.
    - Remove excessive punctuation but keep parentheses.
    - Preserve original capitalization (do not force full lowercase).
    - If short (1-3 words) and contains a known abbreviation, expand it:
        "HUS" -> "HUS OR hemolytic uremic syndrome"
    """
    if raw is None:
        return ""

    s = raw.strip()
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)

    # Remove excessive punctuation but keep parentheses.
    # Replace any character that is not a word char, whitespace, or parentheses with a single space.
    # This keeps letters, numbers, underscore, and parentheses; removes things like "!!!", "??", ",", ";" etc.
    s = re.sub(r"[^\w\s\(\)]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # If result is empty after cleaning, return empty string (caller will re-prompt)
    if not s:
        return ""

    # Synonym expansion for short queries (1-3 words)
    words = s.split()
    if 1 <= len(words) <= 3:
        lowered = [w.lower().strip("()") for w in words]
        for i, w in enumerate(lowered):
            if w in SYNONYMS:
                expansion = SYNONYMS[w]
                # Append expansion if not already present (case-insensitive check)
                if expansion.lower() not in s.lower():
                    # Keep user's original token(s) first, then expansion
                    s = f"{s} OR {expansion}"
                break

    return s


def _query_label(query: QueryInput) -> str:
    """Return a stable display/output label for a string query or per-source query map."""
    if isinstance(query, dict):
        for source_name in LIVE_API_DATABASES:
            source_query = str(query.get(source_name, "")).strip()
            if source_query:
                return source_query
        return "bibliometric_query"
    return query


def _query_for_database(query: QueryInput, db_key: str) -> str:
    """Return the query text that should be sent to a specific database."""
    if isinstance(query, dict):
        return str(query.get(db_key) or _query_label(query))
    return query


def _compile_concept_pattern(terms: list[str]) -> re.Pattern[str]:
    """Compile phrase terms with word boundaries and flexible inner spacing."""
    parts = []
    for term in terms:
        escaped = re.escape(term).replace(r"\ ", r"[\s\-]+")
        parts.append(rf"(?<!\w){escaped}(?!\w)")
    return re.compile("|".join(parts), flags=re.IGNORECASE)


def _qa_search_text(dataframe: pd.DataFrame) -> pd.Series:
    """Return searchable title/abstract/keyword/category text for concept QA."""
    if dataframe.empty:
        return pd.Series(index=dataframe.index, dtype=str)
    available_columns = [column for column in QA_SEARCH_COLUMNS if column in dataframe.columns]
    if not available_columns:
        return pd.Series([""] * len(dataframe), index=dataframe.index, dtype=str)
    return (
        dataframe[available_columns]
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
    )


def _match_concept_blocks(dataframe: pd.DataFrame, concept_blocks: dict[str, list[str]]) -> pd.DataFrame:
    """Add one boolean match column for each configured concept block."""
    matched = pd.DataFrame(index=dataframe.index)
    search_text = _qa_search_text(dataframe)
    for block_name, terms in concept_blocks.items():
        pattern = _compile_concept_pattern(terms)
        matched[f"matches_{block_name}"] = search_text.str.contains(pattern, na=False)
    return matched


def _citation_available(dataframe: pd.DataFrame) -> bool:
    """Return True when imported records include an actual citation-count field."""
    if dataframe.empty:
        return False
    if "citation_available" in dataframe.columns:
        return dataframe["citation_available"].fillna(False).astype(bool).any()
    if "citations" not in dataframe.columns:
        return False
    citations = pd.to_numeric(dataframe["citations"], errors="coerce")
    return bool(citations.notna().any() and citations.gt(0).any())


def _citation_coverage(dataframe: pd.DataFrame) -> dict[str, int | bool]:
    citations = pd.to_numeric(dataframe.get("citations", pd.Series(dtype=object)), errors="coerce")
    if "citation_available" in dataframe.columns:
        available_mask = dataframe["citation_available"].fillna(False).astype(bool)
    else:
        available_mask = citations.notna()
    return {
        "citation_available": bool(available_mask.any()),
        "citation_field_nonblank_records": int(available_mask.sum()),
        "citation_nonzero_records": int(citations.fillna(0).gt(0).sum()),
    }


def _save_qa_summary(
    outputs_dir: str,
    slug: str,
    raw: pd.DataFrame,
    cleaned: pd.DataFrame,
    excluded: pd.DataFrame,
    concept_matches: pd.DataFrame,
    start_year: int | None,
    end_year: int | None,
    concept_profile: str | None,
    citation_available: bool,
    enforce_concept_blocks: bool,
) -> tuple[str, str]:
    """Write text and CSV QA summaries for the current run."""
    years = pd.to_numeric(raw.get("year", pd.Series(dtype=object)), errors="coerce")
    cleaned_years = pd.to_numeric(cleaned.get("year", pd.Series(dtype=object)), errors="coerce")
    missing_year = int(years.isna().sum())
    if start_year is not None or end_year is not None:
        outside_year = pd.Series(False, index=years.index)
        if start_year is not None:
            outside_year |= years.lt(start_year)
        if end_year is not None:
            outside_year |= years.gt(end_year)
        outside_year &= years.notna()
        outside_year_count = int(outside_year.sum())
    else:
        outside_year_count = 0

    coverage = _citation_coverage(raw)
    rows: list[dict[str, object]] = [
        {"metric": "project_slug", "value": slug},
        {"metric": "concept_profile", "value": concept_profile or ""},
        {"metric": "requested_start_year", "value": start_year if start_year is not None else ""},
        {"metric": "requested_end_year", "value": end_year if end_year is not None else ""},
        {"metric": "source_import_records", "value": int(len(raw))},
        {"metric": "year_limited_records", "value": int(len(cleaned))},
        {"metric": "total_excluded_records", "value": int(len(excluded))},
        {"metric": "minimum_publication_year_raw", "value": int(years.min()) if years.notna().any() else ""},
        {"metric": "maximum_publication_year_raw", "value": int(years.max()) if years.notna().any() else ""},
        {"metric": "minimum_publication_year_cleaned", "value": int(cleaned_years.min()) if cleaned_years.notna().any() else ""},
        {"metric": "maximum_publication_year_cleaned", "value": int(cleaned_years.max()) if cleaned_years.notna().any() else ""},
        {"metric": "records_missing_year", "value": missing_year},
        {"metric": "records_outside_requested_year_range", "value": outside_year_count},
        {"metric": "ris_file_pre_filtered_by_database_search", "value": True},
        {"metric": "concept_term_checks_are_qa_only", "value": not enforce_concept_blocks},
        {"metric": "concept_block_exclusions_applied", "value": enforce_concept_blocks},
        {"metric": "citation_available", "value": citation_available},
        {"metric": "citation_field_nonblank_records", "value": coverage["citation_field_nonblank_records"]},
        {"metric": "citation_nonzero_records", "value": coverage["citation_nonzero_records"]},
    ]

    for column in concept_matches.columns:
        block_name = column.replace("matches_", "", 1)
        count = int(concept_matches[column].sum())
        pct = (count / len(raw) * 100.0) if len(raw) else 0.0
        rows.append({"metric": f"{block_name}_concept_match_count", "value": count})
        rows.append({"metric": f"{block_name}_concept_match_percent", "value": f"{pct:.2f}"})

    year_counts = years.dropna().astype(int).value_counts().sort_index()
    for year, count in year_counts.items():
        rows.append({"metric": f"records_in_year_{year}", "value": int(count)})

    summary_csv = os.path.join(outputs_dir, f"{slug}_qa_summary.csv")
    summary_txt = os.path.join(outputs_dir, f"{slug}_qa_summary.txt")
    pd.DataFrame(rows).to_csv(summary_csv, index=False)

    with open(summary_txt, "w", encoding="utf-8") as handle:
        handle.write(f"Project slug: {slug}\n")
        handle.write("Dataset handling note: The RIS file is treated as pre-filtered by the database search strategy.\n")
        handle.write("Concept-term checks describe and audit the dataset; they do not exclude records unless --enforce-concept-blocks is passed.\n")
        handle.write(f"Concept profile: {concept_profile or 'none'}\n")
        handle.write(f"Requested year range: {start_year or ''}-{end_year or ''}\n")
        handle.write(f"Source import records: {len(raw)}\n")
        handle.write(f"Year-limited records: {len(cleaned)}\n")
        handle.write(f"Excluded records: {len(excluded)}\n")
        handle.write(f"Concept-block exclusions applied: {'yes' if enforce_concept_blocks else 'no'}\n")
        handle.write(f"Minimum publication year: {int(years.min()) if years.notna().any() else 'NA'}\n")
        handle.write(f"Maximum publication year: {int(years.max()) if years.notna().any() else 'NA'}\n")
        handle.write(f"Records missing year: {missing_year}\n")
        handle.write(f"Records outside requested year range: {outside_year_count}\n")
        for column in concept_matches.columns:
            block_name = column.replace("matches_", "", 1)
            count = int(concept_matches[column].sum())
            pct = (count / len(raw) * 100.0) if len(raw) else 0.0
            handle.write(f"{block_name} concept matches: {count} ({pct:.2f}%)\n")
        handle.write(f"Citation available: {citation_available}\n")
        handle.write(f"Citation field nonblank records: {coverage['citation_field_nonblank_records']}\n")
        handle.write(f"Citation nonzero records: {coverage['citation_nonzero_records']}\n")
        handle.write("Records by publication year:\n")
        for year, count in year_counts.items():
            handle.write(f"  {year}: {int(count)}\n")

    return summary_txt, summary_csv


def _qa_filter_records(
    dataframe: pd.DataFrame,
    start_year: int | None,
    end_year: int | None,
    concept_profile: str | None,
    enforce_concept_blocks: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return year-limited records, excluded records with reasons, and concept-match flags."""
    working = dataframe.copy()
    years = pd.to_numeric(working.get("year", pd.Series(dtype=object)), errors="coerce")
    concept_blocks = CONCEPT_PROFILES.get(concept_profile or "", {})
    concept_matches = _match_concept_blocks(working, concept_blocks) if concept_blocks else pd.DataFrame(index=working.index)

    reasons: dict[int, list[str]] = {idx: [] for idx in working.index}
    year_range_requested = start_year is not None or end_year is not None
    for idx, year in years.items():
        if year_range_requested and pd.isna(year):
            reasons[idx].append("missing_year")
        elif pd.notna(year) and ((start_year is not None and int(year) < start_year) or (end_year is not None and int(year) > end_year)):
            reasons[idx].append("outside_year_range")

    if enforce_concept_blocks:
        for column in concept_matches.columns:
            block_name = column.replace("matches_", "", 1)
            reason = f"missing_required_{block_name}_terms"
            for idx, matched in concept_matches[column].items():
                if not bool(matched):
                    reasons[idx].append(reason)

    excluded_mask = pd.Series({idx: bool(items) for idx, items in reasons.items()})
    excluded = working.loc[excluded_mask].copy()
    if not excluded.empty:
        excluded["exclusion_reason"] = [";".join(reasons[idx]) for idx in excluded.index]
    else:
        excluded["exclusion_reason"] = []

    cleaned = working.loc[~excluded_mask].copy()
    return cleaned.reset_index(drop=True), excluded.reset_index(drop=True), concept_matches


def _validate_cleaned_dataset(
    cleaned: pd.DataFrame,
    excluded: pd.DataFrame,
    start_year: int | None,
    end_year: int | None,
    concept_profile: str | None,
) -> list[str]:
    failures: list[str] = []
    if cleaned.empty:
        failures.append("record count after QA filtering is zero")
    if start_year is not None or end_year is not None:
        years = pd.to_numeric(cleaned.get("year", pd.Series(dtype=object)), errors="coerce")
        if years.isna().any():
            failures.append("cleaned dataset still contains missing publication years")
        if start_year is not None and years.notna().any() and years.lt(start_year).any():
            failures.append(f"cleaned dataset contains records before {start_year}")
        if end_year is not None and years.notna().any() and years.gt(end_year).any():
            failures.append(f"cleaned dataset contains records after {end_year}")
    if concept_profile and concept_profile not in CONCEPT_PROFILES:
        failures.append(f"unknown concept profile: {concept_profile}")
    if "exclusion_reason" not in excluded.columns:
        failures.append("excluded records were not annotated with exclusion_reason")
    return failures


def _remove_stale_final_outputs(outputs_dir: str, slug: str) -> None:
    """Remove prior slug-specific final analysis files so failed QA cannot leave stale networks."""
    final_suffixes = (
        "_top_authors.csv",
        "_top_papers.csv",
        "_author_edges.csv",
        "_author_matrix.csv",
        "_institution_edges.csv",
        "_institution_matrix.csv",
        "_h_index.csv",
        "_publication_years.csv",
        "_year_counts_all_ris_records.csv",
        "_year_counts_target_year_range.csv",
        "_year_counts_concept_term_qa.csv",
        "_institution_year_counts.csv",
        "_country_year_counts.csv",
        "_vosviewer.csv",
    )
    for suffix in final_suffixes:
        path = os.path.join(outputs_dir, f"{slug}{suffix}")
        if os.path.exists(path):
            os.remove(path)


def _print_run_summary(
    *,
    slug: str,
    source_files: list[str] | None,
    all_ris_records: int,
    records_missing_year: int,
    records_before_start_year: int,
    records_after_end_year: int,
    target_year_records: int,
    excluded_records: int,
    concept_matches: pd.DataFrame,
    citation_available: bool,
    concept_exclusions_applied: bool,
    publication_trend_files: list[str],
    network_files: list[str],
    networks_generated: bool,
) -> None:
    """Print the concise terminal summary requested for each run."""
    print("\n=== Bibliometric Run Summary ===")
    print(f"Project slug: {slug}")
    print(f"Source RIS: {', '.join(source_files or []) if source_files else 'API/no RIS source'}")
    print(f"All RIS records: {all_ris_records}")
    print(f"Records missing year: {records_missing_year}")
    print(f"Records before start year: {records_before_start_year}")
    print(f"Records after end year: {records_after_end_year}")
    print(f"Records within target year range: {target_year_records}")
    if concept_matches.empty:
        print("Telehealth term matches, QA only: not configured")
        print("Cancer term matches, QA only: not configured")
        print("Geography term matches, QA only: not configured")
    else:
        total = len(concept_matches)
        for block_name in ("telehealth", "cancer", "geography"):
            column = f"matches_{block_name}"
            if column in concept_matches.columns:
                count = int(concept_matches[column].sum())
                pct = (count / total * 100.0) if total else 0.0
                print(f"{block_name.title()} term matches, QA only: {count}/{total} ({pct:.2f}%)")
            else:
                print(f"{block_name.title()} term matches, QA only: not configured")
    print(f"Concept-block exclusions applied: {'yes' if concept_exclusions_applied else 'no'}")
    print(f"Citation availability: {'yes' if citation_available else 'no'}")
    print("Publication trend files generated:")
    for path in publication_trend_files:
        print(f"  {path}")
    print("Network files generated:")
    if network_files:
        for path in network_files:
            print(f"  {path}")
    else:
        print(f"  {'yes, see data/outputs' if networks_generated else 'no'}")


def _source_counts(dataframe: pd.DataFrame) -> dict[str, int]:
    """Return record counts by source-like column for README provenance."""
    if dataframe.empty:
        return {}
    source_column = "source" if "source" in dataframe.columns else "source_db"
    if source_column not in dataframe.columns:
        return {}
    counts = dataframe[source_column].fillna("").astype(str).str.strip()
    counts = counts[counts.ne("")]
    return {str(source): int(count) for source, count in counts.value_counts().sort_index().items()}


def _raw_file_entries(requested_ris_files: list[str] | None, raw: pd.DataFrame) -> list[tuple[str, str]]:
    """Return raw RIS filenames and absolute paths requested or observed in ingested rows."""
    paths: list[str] = []
    if requested_ris_files:
        paths.extend(requested_ris_files)
    if not raw.empty and "ris_file" in raw.columns:
        paths.extend(str(path) for path in raw["ris_file"].dropna().unique() if str(path).strip())

    entries = []
    seen = set()
    for path in paths:
        absolute = os.path.abspath(os.path.expanduser(str(path)))
        if absolute in seen:
            continue
        seen.add(absolute)
        entries.append((os.path.basename(absolute), absolute))
    return entries


def _database_query_text(db_key: str, query: QueryInput, filters: dict[str, bool | None]) -> str:
    """Return the query text or search parameter used for a database."""
    db_query = _query_for_database(query, db_key)
    if db_key == "pubmed":
        from apis.pubmed_api import build_pubmed_query

        return build_pubmed_query(db_query, filters)
    if db_key == "scopus":
        from apis.scopus_api import build_scopus_query

        return build_scopus_query(db_query, filters)
    if db_key == "wos":
        from apis.wos_api import build_wos_query

        return build_wos_query(db_query, filters)
    if db_key == "openalex":
        from apis.openalex_api import _openalex_filter_param

        filter_param = _openalex_filter_param(filters)
        return f"search={db_query}; filter={filter_param}" if filter_param else db_query
    return db_query


def write_query_readme(
    outputs_dir: str,
    label: str,
    query: QueryInput,
    original_query: str | None,
    selected_databases: list[str],
    attempted_databases: list[str],
    filters: dict[str, bool | None],
    requested_ris_files: list[str] | None,
    raw: pd.DataFrame,
    deduped: pd.DataFrame,
    output_path: str,
    vosviewer_path: str,
) -> str:
    """Write a per-query README describing query provenance and raw inputs."""
    readme_path = os.path.join(outputs_dir, f"{clean_filename(label)}_README.txt")
    raw_entries = _raw_file_entries(requested_ris_files, raw)
    raw_counts = _source_counts(raw)
    output_counts = _source_counts(deduped)

    lines = [
        f"# Query README: {label}",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Query",
        "",
    ]
    if original_query and original_query != label:
        lines.append(f"- Original input query: `{original_query}`")
    lines.extend(
        [
            f"- Cleaned query used by the pipeline: `{label}`",
            "",
            "## Database Queries",
            "",
            f"- Selected databases: {', '.join(selected_databases) if selected_databases else 'None'}",
            f"- Databases queried or ingested: {', '.join(attempted_databases) if attempted_databases else 'None'}",
            "",
        ]
    )

    for db_key in selected_databases:
        normalized_db = db_key.lower()
        if normalized_db in LIVE_API_DATABASES:
            query_text = _database_query_text(normalized_db, query, filters)
            lines.append(f"- {_display_db_name(normalized_db)}: `{query_text}`")
        else:
            lines.append(f"- {db_key}: not recognized by this pipeline.")

    lines.extend(["", "## Raw Files Used", ""])
    if raw_entries:
        for filename, absolute in raw_entries:
            lines.append(f"- {filename} ({absolute})")
    else:
        lines.append("- None")

    lines.extend(["", "## Record Sources", ""])
    lines.append("- Raw records by source: " + (", ".join(f"{source}={count}" for source, count in raw_counts.items()) if raw_counts else "None"))
    lines.append("- Final output records by source: " + (", ".join(f"{source}={count}" for source, count in output_counts.items()) if output_counts else "None"))
    lines.extend(
        [
            "",
            "## Output Files",
            "",
            f"- Main results: {os.path.abspath(output_path)}",
            f"- VOSviewer results: {os.path.abspath(vosviewer_path)}",
        ]
    )

    with open(readme_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return readme_path


def run_pipeline(
    query: QueryInput,
    databases: list[str] | None = None,
    ris_files: list[str] | None = None,
    ris_file_sources: list[str] | None = None,
    filters: dict[str, bool | None] | None = None,
    original_query: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    concept_profile: str | None = None,
    qa_only: bool = False,
    slug: str | None = None,
    enforce_concept_blocks: bool = False,
    extract_geography: bool = False,
    extract_demographics: bool = False,
    rebuild_geo_cache: bool = False,
    rebuild_demographic_cache: bool = False,
) -> tuple[pd.DataFrame, dict[str, int | str | dict[str, str]]]:
    """
    Query selected databases, normalize, deduplicate, compute metrics, and save results.

    databases: list of live API sources among {"openalex", "pubmed", "scopus", "wos"}.
               If None, DEFAULT_DATABASES from utils.config is used.
    """
    if databases is None:
        databases = DEFAULT_DATABASES
    ris_inputs = build_ris_inputs(ris_files, ris_file_sources)

    active_filters = merge_filters(filters)
    if isinstance(query, str):
        query_without_flags, parsed_filters = parse_query_flags(query)
        if query_without_flags != query:
            query = query_without_flags
        for key, value in parsed_filters.items():
            if value is not None:
                active_filters[key] = value

    label = _query_label(query)
    project_slug = slug or clean_filename(label)
    if project_slug == "telehealth_cancer_treatment":
        start_year = 2016 if start_year is None else start_year
        end_year = 2026 if end_year is None else end_year
        concept_profile = concept_profile or project_slug
    if concept_profile and concept_profile not in CONCEPT_PROFILES:
        raise ValueError(f"Unknown concept profile: {concept_profile}")

    outputs_dir = os.path.join("data", "outputs")
    visuals_dir = os.path.join("data", "visuals")
    os.makedirs(outputs_dir, exist_ok=True)
    os.makedirs(visuals_dir, exist_ok=True)
    _remove_stale_final_outputs(outputs_dir, project_slug)

    LOGGER.info("Running query: %s", label)
    LOGGER.info("Project slug: %s", project_slug)
    LOGGER.info("Query filters: %s", active_filters)
    LOGGER.info("Databases: %s", databases)
    LOGGER.info("RIS files loaded: %s", [str(item.path) for item in ris_inputs])
    LOGGER.info("Scaling mode: %s", get_scaling_mode())

    rebuilt_caches: dict[str, int] = {}
    if rebuild_geo_cache:
        rebuilt_caches.update(build_reference_cache("geography"))
    if rebuild_demographic_cache:
        rebuilt_caches.update(build_reference_cache("demographics"))
    for cache_name, count in rebuilt_caches.items():
        LOGGER.info("%s reference cache rebuilt: %d entries", cache_name.title(), count)
    needs_network_probe = any(db.lower() in {"scopus", "wos"} for db in databases)
    network_status = (
        check_network_status()
        if needs_network_probe
        else {"scopus": {"available": True}, "wos": {"available": True}}
    )

    db_map = {
        "openalex": search_openalex,
        "pubmed": search_pubmed,
        "scopus": search_scopus,
        "wos": search_wos,
    }

    collected_frames: list[pd.DataFrame] = []
    attempted_databases: list[str] = []

    for db in databases:
        db_key = db.lower()
        if db_key not in db_map:
            recognized = ", ".join(RECOGNIZED_DATABASES)
            LOGGER.warning("Unknown database '%s' - skipping. Recognized sources: %s", db, recognized)
            continue

        if db_key == "scopus" and not network_status["scopus"]["available"]:
            LOGGER.info("Skipping Scopus due to network status")
            continue

        if db_key == "wos" and network_status["wos"].get("status_code") == 403:
            LOGGER.warning("Skipping Web of Science: access forbidden (likely network restriction)")
            continue

        func = db_map[db_key]
        db_query = _query_for_database(query, db_key)
        attempted_databases.append(db_key)
        LOGGER.info("%s query started.", db_key)
        source_started_at = time.perf_counter()
        with _api_spinner(db_key):
            df = _call_api_safe(func, db_query, db_key, active_filters)
        LOGGER.info("%s query completed in %.1f seconds.", db_key, time.perf_counter() - source_started_at)
        print(f"{_display_db_name(db_key)} completed", file=sys.stderr, flush=True)

        if df.empty:
            LOGGER.info("%s returned 0 records.", db_key)
            continue

        # Ensure source column exists and annotate
        df = df.copy()
        df["source"] = db_key
        LOGGER.info("%s returned %d records.", db_key, len(df))
        _log_year_distribution(df, db_key)
        collected_frames.append(df)

    if ris_inputs:
        ris_df = load_ris_files(ris_inputs)
        if not ris_df.empty:
            attempted_databases.extend(str(source) for source in ris_df.get("source", pd.Series(dtype=str)).dropna().unique())
            collected_frames.append(ris_df)

    if collected_frames:
        master = pd.concat(collected_frames, ignore_index=True, sort=False)
    else:
        master = pd.DataFrame(columns=SCHEMA_COLUMNS)

    raw_total_before_filters = len(master)

    cross_source_merges = _log_doi_diagnostics(master)

    # Normalize records (bring to common schema)
    master_norm = apply_year_fix(normalize_records(master))
    master_norm = apply_country_extraction(master_norm)
    debug_country_output(master_norm, outputs_dir, prefix=f"{project_slug}_")
    master_norm = flag_early_access(master_norm)
    investigate_year_distribution(master_norm, outputs_dir, prefix=f"{project_slug}_")
    save_year_trends(build_year_trends(master_norm), outputs_dir, prefix=f"{project_slug}_")
    raw_output_path = os.path.join(outputs_dir, f"{project_slug}_raw.csv")
    save_clean_csv(master_norm, raw_output_path)

    master_norm_before_filters = master_norm.copy()
    master_norm = apply_filters(master_norm, active_filters)
    log_filter_effects(master_norm_before_filters, master_norm, active_filters)
    _log_doi_diagnostics(master_norm)

    all_ris_records_df = _records_with_ris_source(master_norm)
    target_year_range_df = _year_limited_records(all_ris_records_df, start_year, end_year)

    qa_cleaned, excluded, concept_matches = _qa_filter_records(
        master_norm,
        start_year=start_year,
        end_year=end_year,
        concept_profile=concept_profile,
        enforce_concept_blocks=enforce_concept_blocks,
    )
    target_concept_matches = concept_matches.reindex(target_year_range_df.index) if not concept_matches.empty else concept_matches
    publication_trend_files = save_publication_trend_outputs(
        all_records=all_ris_records_df,
        target_year_records=target_year_range_df,
        concept_matches=target_concept_matches,
        outputs_dir=outputs_dir,
        visuals_dir=visuals_dir,
        slug=project_slug,
        start_year=start_year,
        end_year=end_year,
    )
    excluded_path = os.path.join(outputs_dir, f"{project_slug}_excluded_records.csv")
    save_clean_csv(excluded, excluded_path)
    citation_available = _citation_available(qa_cleaned)

    # Deduplicate only the QA-cleaned records.
    covidence_before_dedup = (
        int(qa_cleaned.get("source_db", pd.Series(dtype=str)).astype(str).str.lower().eq("covidence").sum())
        if not qa_cleaned.empty
        else 0
    )
    deduped = deduplicate_records(qa_cleaned)
    deduped = apply_year_fix(deduped)
    total_before = len(qa_cleaned)
    total_after = len(deduped)
    duplicates_removed = total_before - total_after
    covidence_after_dedup = (
        int(deduped.get("source", pd.Series(dtype=str)).astype(str).str.lower().str.contains(r"\bcovidence\b", regex=True).sum())
        if not deduped.empty
        else 0
    )
    covidence_duplicates_removed = max(covidence_before_dedup - covidence_after_dedup, 0)
    LOGGER.info("Raw records before filters: %d", raw_total_before_filters)
    LOGGER.info("Records after year/concept policy before deduplication: %d", total_before)
    LOGGER.info("Records after deduplication: %d", total_after)
    LOGGER.info("Duplicates removed: %d", duplicates_removed)
    if covidence_before_dedup:
        LOGGER.info("Covidence records before deduplication: %d", covidence_before_dedup)
        LOGGER.info("Covidence duplicates removed: %d", covidence_duplicates_removed)
    LOGGER.info("Cross-source merges: %d", cross_source_merges)
    LOGGER.info("Analysis record count after deduplication: %d", len(deduped))

    output_path = os.path.join(outputs_dir, f"{project_slug}_year_limited_records.csv")
    vosviewer_path = os.path.join(outputs_dir, f"{project_slug}_vosviewer.csv")
    save_clean_csv(deduped, output_path)
    summary_txt, summary_csv = _save_qa_summary(
        outputs_dir=outputs_dir,
        slug=project_slug,
        raw=master_norm,
        cleaned=deduped,
        excluded=excluded,
        concept_matches=concept_matches,
        start_year=start_year,
        end_year=end_year,
        concept_profile=concept_profile,
        citation_available=citation_available,
        enforce_concept_blocks=enforce_concept_blocks,
    )
    write_pipeline_logs(master, qa_cleaned, deduped, outputs_dir, prefix=f"{project_slug}_")

    qa_failures = _validate_cleaned_dataset(
        cleaned=deduped,
        excluded=excluded,
        start_year=start_year,
        end_year=end_year,
        concept_profile=concept_profile,
    )
    networks_generated = False
    network_files: list[str] = []
    if qa_failures:
        LOGGER.error("QA failed; final network and citation outputs were not generated.")
        for failure in qa_failures:
            LOGGER.error("QA failure: %s", failure)
    elif qa_only:
        LOGGER.info("QA-only mode enabled; final network and visualization outputs were not generated.")
    else:
        analysis_df = apply_country_extraction(prepare_analysis_dataset(deduped))
        run_citation_analysis(analysis_df, outputs_dir, project_slug, citation_available=citation_available)
        run_institutional_temporal_analysis(analysis_df, outputs_dir, project_slug)
        run_collaboration_analysis(analysis_df, outputs_dir, project_slug)
        network_files.extend(
            [
                os.path.join(outputs_dir, f"{project_slug}_author_edges.csv"),
                os.path.join(outputs_dir, f"{project_slug}_institution_edges.csv"),
                os.path.join(outputs_dir, f"{project_slug}_author_matrix.csv"),
                os.path.join(outputs_dir, f"{project_slug}_institution_matrix.csv"),
                os.path.join(outputs_dir, f"{project_slug}_institution_year_counts.csv"),
                os.path.join(outputs_dir, f"{project_slug}_country_year_counts.csv"),
            ]
        )
        if citation_available:
            run_h_index_analysis(deduped, outputs_dir, project_slug)
        else:
            LOGGER.warning("Citation counts unavailable in source export. H-index analysis was not generated.")
        run_publication_year_analysis(deduped, outputs_dir, project_slug)

        vosviewer_df = enforce_vosviewer_format(deduped)
        save_clean_csv(vosviewer_df, vosviewer_path)
        network_files.append(vosviewer_path)
        networks_generated = True

    extraction_files: list[str] = []
    if extract_geography:
        terms_path, counts_path = extract_geographic_terms(deduped, project_slug)
        extraction_files.extend([str(terms_path), str(counts_path)])
        LOGGER.info("Geographic term extraction saved: %s, %s", terms_path, counts_path)
    if extract_demographics:
        terms_path, counts_path = extract_demographic_terms(deduped, project_slug)
        extraction_files.extend([str(terms_path), str(counts_path)])
        LOGGER.info("Demographic term extraction saved: %s, %s", terms_path, counts_path)

    all_years = pd.to_numeric(all_ris_records_df.get("year", pd.Series(dtype=object)), errors="coerce")
    records_missing_year = int(all_years.isna().sum())
    records_before_start_year = int((all_years.notna() & all_years.lt(start_year)).sum()) if start_year is not None else 0
    records_after_end_year = int((all_years.notna() & all_years.gt(end_year)).sum()) if end_year is not None else 0

    _print_run_summary(
        slug=project_slug,
        source_files=[os.path.abspath(str(item.path)) for item in ris_inputs],
        all_ris_records=len(all_ris_records_df),
        records_missing_year=records_missing_year,
        records_before_start_year=records_before_start_year,
        records_after_end_year=records_after_end_year,
        target_year_records=len(target_year_range_df),
        excluded_records=len(excluded),
        concept_matches=target_concept_matches,
        citation_available=citation_available,
        concept_exclusions_applied=enforce_concept_blocks,
        publication_trend_files=publication_trend_files,
        network_files=[path for path in network_files if os.path.exists(path)],
        networks_generated=networks_generated,
    )

    if qa_failures:
        print("\nQA failed. Final networks were not generated:")
        for failure in qa_failures:
            print(f"- {failure}")
    if not citation_available:
        print("Citation counts unavailable in source export. Citation-based rankings were not generated.")

    # Compute metrics after QA/deduplication.
    metrics = compute_metrics(deduped)
    readme_path = write_query_readme(
        outputs_dir=outputs_dir,
        label=project_slug,
        query=query,
        original_query=original_query,
        selected_databases=databases,
        attempted_databases=list(dict.fromkeys(attempted_databases)),
        filters=active_filters,
        requested_ris_files=[str(item.path) for item in ris_inputs],
        raw=master,
        deduped=deduped,
        output_path=output_path,
        vosviewer_path=vosviewer_path,
    )
    LOGGER.info("Results saved to: %s", output_path)
    LOGGER.info("QA summary saved to: %s", summary_txt)
    LOGGER.info("QA summary CSV saved to: %s", summary_csv)
    LOGGER.info("Query README saved to: %s", readme_path)

    metrics["duplicates_removed"] = duplicates_removed
    metrics["qa_failures"] = len(qa_failures)
    metrics["networks_generated"] = "yes" if networks_generated else "no"
    metrics["output_paths"] = {
        "raw_results": os.path.abspath(raw_output_path),
        "year_limited_records": os.path.abspath(output_path),
        "excluded_records": os.path.abspath(excluded_path),
        "qa_summary": os.path.abspath(summary_txt),
        "qa_summary_csv": os.path.abspath(summary_csv),
        "publication_trend_files": ";".join(os.path.abspath(path) for path in publication_trend_files),
        "reference_extraction_files": ";".join(os.path.abspath(path) for path in extraction_files),
        "vosviewer_results": os.path.abspath(vosviewer_path) if networks_generated else "",
        "query_readme": os.path.abspath(readme_path),
        "output_folder": os.path.abspath(outputs_dir),
    }
    return deduped, metrics


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description="Run a bibliometric query across multiple APIs.")
    # Make query optional so we can prompt interactively when omitted or invalid
    parser.add_argument("query", nargs="?", help="Query string to send to the configured scholarly APIs.", default=None)
    parser.add_argument(
        "--databases",
        help="Comma-separated list of live API sources (openalex,pubmed,scopus,wos). If omitted, defaults are used.",
        default=None,
    )
    parser.add_argument(
        "--ris-files",
        help="Comma-separated list of RIS files to include. If omitted, data/raw RIS files are auto-loaded unless --no-ris is set.",
        default=None,
    )
    parser.add_argument(
        "--ris-file-sources",
        help=(
            "Comma-separated source/origin labels matching --ris-files. "
            f"Allowed values: {','.join(RIS_SOURCE_LABELS)}. Defaults to unknown/auto-detect per file."
        ),
        default=None,
    )
    parser.add_argument(
        "--no-ris",
        action="store_true",
        help="Do not load RIS files, even if default RIS exports are available.",
    )
    parser.add_argument("--slug", help="Project/output slug. Defaults to a cleaned version of the query.", default=None)
    parser.add_argument("--start-year", type=int, help="Earliest publication year to include after import.", default=None)
    parser.add_argument("--end-year", type=int, help="Latest publication year to include after import.", default=None)
    parser.add_argument(
        "--concept-profile",
        choices=sorted(CONCEPT_PROFILES),
        help="Named concept-block QA profile to audit after import.",
        default=None,
    )
    parser.add_argument(
        "--qa-only",
        action="store_true",
        help="Import data, save source/year-limited/QA files, and stop before networks or visuals.",
    )
    parser.add_argument(
        "--enforce-concept-blocks",
        action="store_true",
        help="Exclude records missing configured concept-block terms. Off by default; concept checks are QA-only unless this is passed.",
    )
    parser.add_argument(
        "--rebuild-geo-cache",
        action="store_true",
        help="Rebuild data/cache/geographic_cache.json from local Census and GeoNames reference files before extraction.",
    )
    parser.add_argument(
        "--rebuild-demographic-cache",
        action="store_true",
        help="Rebuild data/cache/demographic_cache.json from the local MeSH descriptor XML before extraction.",
    )
    parser.add_argument(
        "--extract-geography",
        action="store_true",
        help="Extract descriptive geographic terms from imported records using the local geography cache only.",
    )
    parser.add_argument(
        "--extract-demographics",
        action="store_true",
        help="Extract descriptive demographic terms from imported records using the local demographic cache only.",
    )
    return parser


def build_reference_cache_argument_parser() -> argparse.ArgumentParser:
    """Create the CLI parser for local reference-cache rebuilds."""
    parser = argparse.ArgumentParser(description="Build local geography and demographic reference caches.")
    parser.add_argument(
        "--type",
        choices=("geography", "demographics", "all"),
        required=True,
        help="Reference cache to rebuild.",
    )
    return parser


def main() -> None:
    """Run the bibliometric pipeline from the command line."""
    if len(sys.argv) > 1 and sys.argv[1] == "build-reference-cache":
        parser = build_reference_cache_argument_parser()
        args = parser.parse_args(sys.argv[2:])
        counts = build_reference_cache(args.type)
        print("Reference cache build complete:")
        for cache_name, count in counts.items():
            print(f"{cache_name}: {count} entries")
        return

    # One-time pybliometrics setup hint (will log instruction if missing)
    try:
        check_pybliometrics_setup_once()
    except Exception:
        # Ensure any unexpected error here does not stop the pipeline; we only log setup hints.
        logging.debug("Unexpected error during pybliometrics setup check.", exc_info=False)

    parser = build_argument_parser()
    args = parser.parse_args()

    # Handle database selection
    if args.databases is not None:
        databases = [d.strip().lower() for d in args.databases.split(",") if d.strip()]
        no_ris_from_prompt = False
    else:
        databases, no_ris_from_prompt = prompt_for_databases()

    no_ris = args.no_ris or no_ris_from_prompt
    if no_ris:
        ris_files = []
    elif args.ris_files:
        ris_files = [path.strip() for path in args.ris_files.split(",") if path.strip()]
    else:
        ris_files = get_default_ris_files()
        if len(ris_files) > 1:
            LOGGER.error("Multiple RIS files were found in data/raw. Refusing to auto-combine projects:")
            for path in ris_files:
                LOGGER.error("  %s", path)
            LOGGER.error("Pass the intended file with --ris-files to prevent mixed-project contamination.")
            return
    ris_file_sources = [source.strip() for source in args.ris_file_sources.split(",") if source.strip()] if args.ris_file_sources else None
    if ris_file_sources and len(ris_file_sources) != len(ris_files):
        LOGGER.error("--ris-file-sources count must match --ris-files count.")
        return
    LOGGER.info("RIS files loaded: %s", ris_files)

    # Handle query input: allow CLI, or interactive prompt if omitted/empty; ensure cleaned and non-empty.
    raw_query = args.query
    parsed_query, filters = parse_query_flags(raw_query) if raw_query is not None else ("", merge_filters(None))
    cleaned = clean_query(parsed_query) if raw_query is not None else ""

    while not cleaned:
        try:
            raw_query = input("Enter query (e.g. hemolytic uremic syndrome): ")
        except (EOFError, KeyboardInterrupt):
            LOGGER.error("No query provided; exiting.")
            return
        parsed_query, filters = parse_query_flags(raw_query)
        cleaned = clean_query(parsed_query)

    # Print cleaned query before running
    LOGGER.info("Running cleaned query: %s", cleaned)

    dataframe, metrics = run_pipeline(
        query=cleaned,
        databases=databases,
        ris_files=ris_files,
        ris_file_sources=ris_file_sources,
        filters=filters,
        original_query=raw_query,
        start_year=args.start_year,
        end_year=args.end_year,
        concept_profile=args.concept_profile,
        qa_only=args.qa_only,
        slug=args.slug,
        enforce_concept_blocks=args.enforce_concept_blocks,
        extract_geography=args.extract_geography,
        extract_demographics=args.extract_demographics,
        rebuild_geo_cache=args.rebuild_geo_cache,
        rebuild_demographic_cache=args.rebuild_demographic_cache,
    )
    print(dataframe)
    print(metrics)


if __name__ == "__main__":
    main()
