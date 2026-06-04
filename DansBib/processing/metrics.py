"""Bibliometric metrics utilities."""

from __future__ import annotations

import pandas as pd


def calculate_h_index(citations: pd.Series) -> int:
    """Calculate the h-index from a Series of citation counts."""
    sorted_citations = sorted((int(value) for value in citations.fillna(0)), reverse=True)
    h_index = 0
    for position, count in enumerate(sorted_citations, start=1):
        if count >= position:
            h_index = position
        else:
            break
    return h_index


def compute_metrics(dataframe: pd.DataFrame) -> dict[str, int]:
    """Return total publications, total citations, and h-index."""
    if dataframe.empty:
        return {"total_publications": 0, "total_citations": 0, "h_index": 0}
    citations = dataframe["citations"].fillna(0).astype(int)
    return {
        "total_publications": int(len(dataframe)),
        "total_citations": int(citations.sum()),
        "h_index": calculate_h_index(citations),
    }
