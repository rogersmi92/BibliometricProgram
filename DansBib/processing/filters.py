"""Query flag parsing and cross-source record filtering."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pandas as pd

LOGGER = logging.getLogger(__name__)

FILTER_COLUMNS = [
    "document_type",
    "publication_type",
    "publication_status",
    "open_access",
    "is_oa",
    "oa_status",
]


def empty_filters() -> dict[str, bool | None]:
    """Return the supported filter keys with unset values."""
    return {
        "review": None,
        "early_access": None,
        "open_access": None,
    }


def parse_query_flags(raw_query: Any) -> tuple[str, dict[str, bool | None]]:
    """Extract query flags and return the remaining search terms plus filters."""
    filters = empty_filters()
    clean_terms = []

    for token in str(raw_query or "").split():
        t = token.lower()

        if t == "-revart":
            filters["review"] = True
        elif t == "-xrevart":
            filters["review"] = False
        elif t == "-early":
            filters["early_access"] = True
        elif t == "-xearly":
            filters["early_access"] = False
        elif t == "-oa":
            filters["open_access"] = True
        elif t == "-xoa":
            filters["open_access"] = False
        else:
            clean_terms.append(token)

    return " ".join(clean_terms), filters


def merge_filters(filters: Mapping[str, Any] | None) -> dict[str, bool | None]:
    """Merge caller-supplied filters with the supported filter defaults."""
    merged = empty_filters()
    if filters:
        for key in merged:
            if key in filters:
                merged[key] = filters[key]
    return merged


def has_active_filters(filters: Mapping[str, Any] | None) -> bool:
    """Return True when any supported filter is set."""
    return any(value is not None for value in merge_filters(filters).values())


def _coerce_series(dataframe: pd.DataFrame, column: str) -> pd.Series:
    if column in dataframe.columns:
        return dataframe[column]
    return pd.Series([""] * len(dataframe), index=dataframe.index)


def _str_contains(dataframe: pd.DataFrame, column: str, term: str) -> pd.Series:
    return _coerce_series(dataframe, column).astype(str).str.contains(term, case=False, na=False)


def _truthy_open_access_mask(dataframe: pd.DataFrame) -> pd.Series:
    oa_cols = ["open_access", "is_oa", "oa_status"]
    truthy_values = {"true", "1", "yes", "y", "gold", "green", "bronze", "hybrid", "diamond", "open"}
    mask = pd.Series([False] * len(dataframe), index=dataframe.index)

    for column in oa_cols:
        if column not in dataframe.columns:
            continue
        normalized = dataframe[column].astype(str).str.strip().str.lower()
        mask = mask | normalized.isin(truthy_values)

    return mask


def apply_filters(dataframe: pd.DataFrame, filters: Mapping[str, Any] | None) -> pd.DataFrame:
    """Apply review, early-access, and open-access fallback filters to a DataFrame."""
    active_filters = merge_filters(filters)
    if dataframe is None or dataframe.empty or not has_active_filters(active_filters):
        return dataframe

    filtered = dataframe.copy()

    if active_filters["review"] is not None:
        mask = (
            _str_contains(filtered, "document_type", "review")
            | _str_contains(filtered, "publication_type", "review")
        )
        filtered = filtered[mask] if active_filters["review"] else filtered[~mask]

    if active_filters["early_access"] is not None:
        mask = (
            _coerce_series(filtered, "is_early_access").astype(str).str.lower().isin({"true", "1", "yes"})
            | _str_contains(filtered, "document_type", "early")
            | _str_contains(filtered, "document_type", "epub")
            | _str_contains(filtered, "publication_status", "ahead of print")
            | _str_contains(filtered, "publication_status", "early")
            | _str_contains(filtered, "publication_status", "epub")
        )
        filtered = filtered[mask] if active_filters["early_access"] else filtered[~mask]

    if active_filters["open_access"] is not None:
        mask = _truthy_open_access_mask(filtered)
        if not any(column in filtered.columns for column in ["open_access", "is_oa", "oa_status"]):
            LOGGER.info("Open-access fallback filter skipped: no OA metadata columns available.")
        filtered = filtered[mask] if active_filters["open_access"] else filtered[~mask]

    return filtered


def log_filter_effects(before_df: pd.DataFrame, after_df: pd.DataFrame, filters: Mapping[str, Any] | None) -> None:
    """Log the effect of active filters."""
    if not has_active_filters(filters):
        return
    LOGGER.info("Applied filters: %s", merge_filters(filters))
    LOGGER.info("Records before filters: %d", len(before_df))
    LOGGER.info("Records after filters: %d", len(after_df))
