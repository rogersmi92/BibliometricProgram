"""Deduplication logic for merged bibliometric results."""

from __future__ import annotations

import logging

import pandas as pd

from processing.normalize import normalize_doi, normalize_title

LOGGER = logging.getLogger(__name__)


def _first_non_empty(values: pd.Series) -> str:
    """Return the first non-empty string value from a Series."""
    for value in values:
        if pd.isna(value):
            continue
        text = str(value or "").strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return text
    return ""


def _first_non_null(values: pd.Series) -> int | None:
    """Return the first non-null value from a Series."""
    non_null = values.dropna()
    if non_null.empty:
        return None
    return non_null.iloc[0]


def _combine_sources(values: pd.Series) -> str:
    """Return unique sources as a sorted comma-separated string."""
    unique_sources = {
        source.strip()
        for value in values
        for source in str(value or "").split(",")
        if source.strip()
    }
    return ", ".join(sorted(unique_sources))


def _split_multi_value(value: object) -> list[str]:
    if pd.isna(value):
        return []
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return []
    separator = ";" if ";" in text else ","
    return [part.strip() for part in text.split(separator) if part.strip()]


def _combine_unique(values: pd.Series) -> str:
    """Return unique semicolon-separated metadata values in encounter order."""
    combined: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in _split_multi_value(value):
            key = part.casefold()
            if key in seen:
                continue
            seen.add(key)
            combined.append(part)
    return "; ".join(combined)


def _merge_group(group: pd.DataFrame) -> dict[str, object]:
    """Merge a duplicate group into a single normalized record."""
    merged = {
        "title": _first_non_empty(group["title"]),
        "doi": normalize_doi(_first_non_empty(group["doi"])),
        "authors": _first_non_empty(group["authors"]),
        "year": _first_non_null(group["year"]),
        "citations": int(group["citations"].fillna(0).max()),
        "source": _combine_sources(group["source"]),
    }

    for column in group.columns:
        if column in merged or column.endswith("_normalized"):
            continue
        if column in {"institutions", "countries", "affiliations"}:
            merged[column] = _combine_unique(group[column])
        elif column == "country":
            merged[column] = _first_non_empty(group[column])
        elif column == "citation_available":
            merged[column] = bool(group[column].fillna(False).astype(bool).any())
        else:
            merged[column] = _first_non_empty(group[column])

    return merged


def _merge_by_key(dataframe: pd.DataFrame, key_column: str) -> pd.DataFrame:
    """Merge duplicate records sharing the same non-empty key."""
    if dataframe.empty:
        return dataframe.copy()

    with_key = dataframe[dataframe[key_column] != ""].copy()
    without_key = dataframe[dataframe[key_column] == ""].copy()
    merged_records = [_merge_group(group) for _, group in with_key.groupby(key_column, sort=False, dropna=False)]

    if not without_key.empty:
        merged_records.extend(without_key[dataframe.columns.difference([key_column], sort=False)].to_dict(orient="records"))

    output_columns = [column for column in dataframe.columns if column != key_column]
    return pd.DataFrame(merged_records, columns=output_columns)


def _count_cross_source_doi_merges(dataframe: pd.DataFrame) -> tuple[int, int]:
    """Return cross-source merge count and number of unique non-empty normalized DOIs."""
    with_doi = dataframe[dataframe["doi_normalized"] != ""].copy()
    if with_doi.empty:
        return (0, 0)

    cross_source_merges = 0
    for _, group in with_doi.groupby("doi_normalized", sort=False, dropna=False):
        unique_sources = {
            source.strip()
            for value in group["source"]
            for source in str(value or "").split(",")
            if source.strip()
        }
        if len(group) > 1 and len(unique_sources) > 1:
            cross_source_merges += 1

    return (cross_source_merges, int(with_doi["doi_normalized"].nunique()))


def deduplicate_records(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate records by DOI first, then normalized title, merging duplicate metadata."""
    if dataframe.empty:
        return dataframe.copy()

    df = dataframe.copy()
    df["doi_normalized"] = df["doi"].map(normalize_doi)
    cross_source_merges, unique_dois = _count_cross_source_doi_merges(df)
    combined = _merge_by_key(df, "doi_normalized")
    combined["title_normalized"] = combined["title"].map(normalize_title)
    combined = _merge_by_key(combined, "title_normalized")
    combined["doi_normalized"] = combined["doi"].map(normalize_doi)
    combined["title_normalized"] = combined["title"].map(normalize_title)
    combined = combined.drop_duplicates(subset=["doi_normalized"], keep="first")
    combined = combined.drop_duplicates(subset=["title_normalized"], keep="first")
    LOGGER.info("Cross-source DOI merges: %d", cross_source_merges)
    LOGGER.info("Unique normalized DOIs: %d", unique_dois)
    return combined.drop(columns=["doi_normalized", "title_normalized"]).reset_index(drop=True)
