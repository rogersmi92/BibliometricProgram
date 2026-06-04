"""Normalization helpers for bibliometric records."""

from __future__ import annotations

import pandas as pd
import re
import string
from typing import Any, Dict, List

try:
    from processing.filters import FILTER_COLUMNS
except ModuleNotFoundError:  # pragma: no cover - package import path
    from DansBib.processing.filters import FILTER_COLUMNS


def normalize_doi(doi: Any) -> str:
    """Normalize a DOI for case-insensitive comparisons."""
    if doi is None:
        return ""
    value = str(doi).strip()
    if not value:
        return ""
    normalized = value.lower().strip()
    if normalized in {"none", "null", "nan", "n/a", "na"}:
        return ""
    normalized = re.sub(r"^\s*doi:\s*", "", normalized)
    for prefix in (
        "https://doi.org/",
        "http://dx.doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    normalized = normalized.strip().rstrip("/")
    return normalized if normalized else ""


def normalize_title(title: Any) -> str:
    """Normalize a title by lowercasing and stripping punctuation."""
    if title is None:
        return ""
    lowered = str(title).lower().strip()
    translator = str.maketrans("", "", string.punctuation)
    without_punctuation = lowered.translate(translator)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def parse_year(value: Any) -> int | None:
    """Extract a four-digit publication year when available."""
    if value is None:
        return None
    match = re.search(r"\b(18|19|20)\d{2}\b", str(value))
    return int(match.group(0)) if match else None


def parse_citations(value: Any) -> int:
    """Convert a citation count to an integer, defaulting to zero."""
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def standardize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw API record into the shared project schema."""
    standardized = {
        "title": str(record.get("title") or "").strip(),
        "doi": normalize_doi(record.get("doi")),
        "authors": str(record.get("authors") or "").strip(),
        "year": parse_year(record.get("year")),
        "citations": parse_citations(record.get("citations")),
        "source": str(record.get("source") or "").strip(),
    }
    for column in FILTER_COLUMNS:
        if column in record:
            standardized[column] = record.get(column)
    return standardized


def normalize_records(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize a DataFrame of raw records into the standardized schema.

    - If the input DataFrame is empty, return it unchanged.
    - Convert rows to dicts, apply standardize_record to each, and rebuild a DataFrame.
    - Ensure final columns include the core schema, followed by any enriched
      metadata and filter metadata columns present in the raw records.
    """
    if df is None:
        return pd.DataFrame(columns=["title", "doi", "authors", "year", "citations", "source"])

    if df.empty:
        return df

    records: List[Dict[str, Any]] = df.to_dict(orient="records")
    cleaned_records: List[Dict[str, Any]] = [standardize_record(rec) for rec in records]

    new_df = pd.DataFrame(cleaned_records)

    # Ensure expected columns exist and in a stable order.
    expected_cols = ["title", "doi", "authors", "year", "citations", "source"]
    for col in expected_cols:
        if col not in new_df.columns:
            new_df[col] = None

    metadata_cols = [col for col in FILTER_COLUMNS if col in new_df.columns]
    enriched_cols = [col for col in ["abstract", "keywords", "institutions", "countries", "affiliations", "source_db", "ris_file", "citation_available"] if col in df.columns]
    for col in enriched_cols:
        new_df[col] = df[col].values

    return new_df[expected_cols + enriched_cols + metadata_cols]
