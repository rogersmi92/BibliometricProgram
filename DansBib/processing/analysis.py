"""Analysis helpers for bibliometric influence, contribution, and collaboration outputs."""

from __future__ import annotations

import itertools
import logging
import os
import re
from collections import Counter, defaultdict
from typing import Iterable

import pandas as pd

from processing.normalize import normalize_doi, normalize_title, parse_citations, parse_year, standardize_record

LOGGER = logging.getLogger(__name__)
CORE_COLUMNS = ["title", "doi", "authors", "year", "citations", "source"]
EXTRA_COLUMNS = ["institutions", "countries", "affiliations", "citation_available"]


def _clean_filename(query: str) -> str:
    """Return a filesystem-safe filename stem."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", query.lower()).strip("_") or "results"


def _split_multi_value(value: object) -> list[str]:
    """Split a semicolon-separated field into clean tokens.

    Author names use commas internally ("Last, Initials"), so comma is not a
    safe generic delimiter for bibliometric metadata.
    """
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    parts = text.split(";") if ";" in text else [text]
    return [part.strip() for part in parts if part.strip()]


def _canonical_author_name(value: object) -> str:
    """Return a stable author label while preserving surname-comma-initials style."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text or "," not in text:
        return text

    surname, given = (part.strip() for part in text.split(",", 1))
    if not surname or not given:
        return text

    compact_given = re.sub(r"[\s.]+", "", given)
    looks_like_initials = "." in given or compact_given.isupper() or len(compact_given) <= 3
    if compact_given and looks_like_initials and re.fullmatch(r"[A-Za-z-]+", compact_given) and len(compact_given) <= 6:
        return f"{surname}, {compact_given.upper()}"
    return f"{surname}, {given}"


def _combine_unique(values: Iterable[object]) -> str:
    """Combine multi-valued fields into a stable semicolon-separated string."""
    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in _split_multi_value(value):
            normalized = part.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_values.append(part)
    return "; ".join(unique_values)


def _first_non_empty(values: Iterable[object]) -> str:
    """Return the first non-empty text value."""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _merge_analysis_group(group: pd.DataFrame) -> dict[str, object]:
    """Merge duplicate analysis records while preserving extra metadata."""
    year_series = group["year"].dropna()
    return {
        "title": _first_non_empty(group["title"]),
        "doi": normalize_doi(_first_non_empty(group["doi"])),
        "authors": _combine_unique(group["authors"]),
        "year": int(year_series.iloc[0]) if not year_series.empty else None,
        "citations": int(group["citations"].fillna(0).max()),
        "source": ", ".join(sorted({part for value in group["source"] for part in _split_multi_value(value)})),
        "institutions": _combine_unique(group["institutions"]),
        "countries": _combine_unique(group["countries"]),
        "affiliations": _combine_unique(group["affiliations"]),
        "citation_available": bool(group.get("citation_available", pd.Series(dtype=bool)).fillna(False).astype(bool).any()),
    }


def _merge_by_key(dataframe: pd.DataFrame, key_column: str) -> pd.DataFrame:
    """Merge records sharing the same non-empty key."""
    with_key = dataframe[dataframe[key_column] != ""].copy()
    without_key = dataframe[dataframe[key_column] == ""].copy()
    merged_records = [_merge_analysis_group(group) for _, group in with_key.groupby(key_column, sort=False, dropna=False)]
    if not without_key.empty:
        passthrough_columns = CORE_COLUMNS + EXTRA_COLUMNS
        merged_records.extend(without_key[passthrough_columns].to_dict(orient="records"))
    return pd.DataFrame(merged_records, columns=CORE_COLUMNS + EXTRA_COLUMNS)


def prepare_analysis_dataset(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Create a deduplicated enriched dataset for downstream analyses."""
    if dataframe.empty:
        return pd.DataFrame(columns=CORE_COLUMNS + EXTRA_COLUMNS)

    records = []
    for record in dataframe.to_dict(orient="records"):
        base = standardize_record(record)
        base.update(
            {
                "institutions": _combine_unique([record.get("institutions")]),
                "countries": _combine_unique([record.get("countries")]),
                "affiliations": _combine_unique([record.get("affiliations")]),
                "citation_available": bool(record.get("citation_available") or False),
            }
        )
        records.append(base)

    prepared = pd.DataFrame(records, columns=CORE_COLUMNS + EXTRA_COLUMNS)
    prepared["doi_normalized"] = prepared["doi"].map(normalize_doi)
    prepared = _merge_by_key(prepared, "doi_normalized")
    prepared["title_normalized"] = prepared["title"].map(normalize_title)
    prepared = _merge_by_key(prepared.assign(title_normalized=prepared["title"].map(normalize_title)), "title_normalized")
    return prepared.drop(columns=["title_normalized"], errors="ignore").reset_index(drop=True)


def run_citation_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str, citation_available: bool = True) -> None:
    """Generate citation-focused outputs and logs."""
    if dataframe.empty:
        LOGGER.info("Citation analysis skipped: no records available.")
        return

    stem = _clean_filename(query)
    citations = dataframe["citations"].fillna(0).astype(int)

    author_rows = []
    for _, row in dataframe.iterrows():
        for author in _split_multi_value(row.get("authors")):
            canonical_author = _canonical_author_name(author)
            if canonical_author:
                author_rows.append({"author": canonical_author, "citations": int(row.get("citations") or 0), "papers": 1})
    if author_rows:
        authors_df = pd.DataFrame(author_rows)
        top_authors = (
            authors_df.groupby("author", as_index=False)
            .agg(total_citations=("citations", "sum"), publications=("papers", "sum"))
        )
        if citation_available:
            top_authors = top_authors.sort_values(["total_citations", "publications", "author"], ascending=[False, False, True])
        else:
            top_authors = top_authors.drop(columns=["total_citations"]).sort_values(["publications", "author"], ascending=[False, True])
    else:
        top_authors = pd.DataFrame(columns=["author", "publications"] if not citation_available else ["author", "total_citations", "publications"])
    top_authors.to_csv(os.path.join(outputs_dir, f"{stem}_top_authors.csv"), index=False)

    if citation_available:
        top_papers = dataframe.sort_values(["citations", "year"], ascending=[False, False]).head(20).copy()
        top_papers.to_csv(os.path.join(outputs_dir, f"{stem}_top_papers.csv"), index=False)
    else:
        LOGGER.warning("Citation counts unavailable in source export. Citation-based rankings were not generated.")
        LOGGER.info("Top authors output: %s", os.path.join(outputs_dir, f"{stem}_top_authors.csv"))
        return

    avg_citations = float(citations.mean()) if not citations.empty else 0.0
    quantile_cutoff = float(citations.quantile(0.95)) if len(citations) > 1 else float(citations.max())
    outliers = dataframe[citations >= quantile_cutoff].copy() if not dataframe.empty else dataframe.copy()
    histogram = pd.cut(
        citations,
        bins=[-1, 0, 1, 5, 10, 25, 50, 100, 250, 500, max(int(citations.max()), 5000)],
        include_lowest=True,
    ).value_counts(sort=False)

    LOGGER.info("Average citations per paper: %.2f", avg_citations)
    LOGGER.info("Citation distribution: %s", {str(index): int(value) for index, value in histogram.items()})
    LOGGER.info("Highly cited outliers (top 5%% threshold %.2f): %d", quantile_cutoff, len(outliers))
    if citation_available:
        LOGGER.info("Top cited papers output: %s", os.path.join(outputs_dir, f"{stem}_top_papers.csv"))
    LOGGER.info("Top authors output: %s", os.path.join(outputs_dir, f"{stem}_top_authors.csv"))


def run_h_index_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str = "results") -> None:
    """Write h-index summary outputs for the deduplicated result set."""
    stem = _clean_filename(query)
    output_path = os.path.join(outputs_dir, f"{stem}_h_index.csv")
    if dataframe.empty:
        pd.DataFrame([{"h_index": 0, "total_publications": 0, "total_citations": 0}]).to_csv(output_path, index=False)
        LOGGER.info("H-index analysis skipped: no records available.")
        return

    citations = sorted((int(value) for value in dataframe["citations"].fillna(0)), reverse=True)
    h_index = 0
    for position, count in enumerate(citations, start=1):
        if count >= position:
            h_index = position
        else:
            break

    summary = pd.DataFrame(
        [
            {
                "h_index": h_index,
                "total_publications": int(len(dataframe)),
                "total_citations": int(sum(citations)),
            }
        ]
    )
    summary.to_csv(output_path, index=False)
    LOGGER.info("H-index analysis output: %s", output_path)


def run_publication_year_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str = "results") -> None:
    """Write publication counts by year for the deduplicated result set."""
    stem = _clean_filename(query)
    output_path = os.path.join(outputs_dir, f"{stem}_publication_years.csv")
    if dataframe.empty:
        pd.DataFrame(columns=["year", "publications"]).to_csv(output_path, index=False)
        LOGGER.info("Publication year analysis skipped: no records available.")
        return

    years = dataframe["year"].map(parse_year).dropna().astype(int)
    year_counts = (
        years.value_counts()
        .sort_index()
        .rename_axis("year")
        .reset_index(name="publications")
    )
    year_counts.to_csv(output_path, index=False)
    LOGGER.info("Publication year analysis output: %s", output_path)


def run_institutional_temporal_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str) -> None:
    """Generate institution/country trend outputs and logs."""
    if dataframe.empty:
        LOGGER.info("Institutional analysis skipped: no records available.")
        return

    stem = _clean_filename(query)
    records = []
    country_records = []
    for _, row in dataframe.iterrows():
        year = parse_year(row.get("year"))
        if year is None:
            continue
        for institution in _split_multi_value(row.get("institutions")):
            records.append({"year": year, "institution": institution})
        for country in _split_multi_value(row.get("countries")):
            country_records.append({"year": year, "country": country})

    institutions_df = pd.DataFrame(records, columns=["year", "institution"])
    countries_df = pd.DataFrame(country_records, columns=["year", "country"])

    institution_counts = (
        institutions_df.groupby(["year", "institution"], as_index=False)
        .size()
        .rename(columns={"size": "publications"})
        if not institutions_df.empty
        else pd.DataFrame(columns=["year", "institution", "publications"])
    )
    country_counts = (
        countries_df.groupby(["year", "country"], as_index=False)
        .size()
        .rename(columns={"size": "publications"})
        if not countries_df.empty
        else pd.DataFrame(columns=["year", "country", "publications"])
    )

    institution_counts.to_csv(os.path.join(outputs_dir, f"{stem}_institution_year_counts.csv"), index=False)
    country_counts.to_csv(os.path.join(outputs_dir, f"{stem}_country_year_counts.csv"), index=False)

    top_institutions = (
        institution_counts.groupby("institution", as_index=False)["publications"].sum().sort_values("publications", ascending=False).head(10)
        if not institution_counts.empty
        else pd.DataFrame(columns=["institution", "publications"])
    )

    growth_rows = []
    if not institution_counts.empty:
        for institution, group in institution_counts.groupby("institution", sort=False):
            ordered = group.sort_values("year")
            growth_rows.append(
                {
                    "institution": institution,
                    "growth": int(ordered["publications"].iloc[-1] - ordered["publications"].iloc[0]),
                    "start_year": int(ordered["year"].iloc[0]),
                    "end_year": int(ordered["year"].iloc[-1]),
                }
            )
    growth_df = pd.DataFrame(growth_rows).sort_values(["growth", "institution"], ascending=[False, True]).head(10) if growth_rows else pd.DataFrame(columns=["institution", "growth", "start_year", "end_year"])

    LOGGER.info("Top institutions overall: %s", top_institutions.to_dict(orient="records"))
    LOGGER.info("Top institution growth: %s", growth_df.to_dict(orient="records"))


def _normalize_entity_name(value: str) -> str:
    """Normalize an author/institution label for network analysis."""
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _build_edge_list(dataframe: pd.DataFrame, column: str, entity_label: str) -> pd.DataFrame:
    """Build a co-occurrence edge list for the provided column."""
    edge_counter: Counter[tuple[str, str]] = Counter()
    for _, row in dataframe.iterrows():
        entities = []
        seen = set()
        for value in _split_multi_value(row.get(column)):
            normalized = _normalize_entity_name(value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            entities.append(normalized)
        for source, target in itertools.combinations(sorted(entities), 2):
            if source == target:
                continue
            edge_counter[(source, target)] += 1

    edge_rows = [{"source": source, "target": target, "weight": weight} for (source, target), weight in edge_counter.items()]
    edge_df = pd.DataFrame(edge_rows, columns=["source", "target", "weight"]).sort_values(["weight", "source", "target"], ascending=[False, True, True]) if edge_rows else pd.DataFrame(columns=["source", "target", "weight"])
    LOGGER.info("%s collaboration edges: %d", entity_label, len(edge_df))
    return edge_df


def _build_matrix(edge_df: pd.DataFrame, top_n: int = 100) -> pd.DataFrame:
    """Build a symmetric co-occurrence matrix from an edge list."""
    if edge_df.empty:
        return pd.DataFrame()

    totals: defaultdict[str, int] = defaultdict(int)
    for _, row in edge_df.iterrows():
        totals[row["source"]] += int(row["weight"])
        totals[row["target"]] += int(row["weight"])

    top_entities = {entity for entity, _ in sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:top_n]}
    filtered = edge_df[edge_df["source"].isin(top_entities) & edge_df["target"].isin(top_entities)].copy()
    if filtered.empty:
        return pd.DataFrame()

    entities = sorted(top_entities)
    matrix = pd.DataFrame(0, index=entities, columns=entities, dtype=int)
    for _, row in filtered.iterrows():
        matrix.at[row["source"], row["target"]] = int(row["weight"])
        matrix.at[row["target"], row["source"]] = int(row["weight"])
    return matrix


def run_collaboration_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str) -> None:
    """Generate author and institution collaboration outputs."""
    if dataframe.empty:
        LOGGER.info("Collaboration analysis skipped: no records available.")
        return

    stem = _clean_filename(query)
    author_edges = _build_edge_list(dataframe, "authors", "Author")
    institution_edges = _build_edge_list(dataframe, "institutions", "Institution")

    author_edges.to_csv(os.path.join(outputs_dir, f"{stem}_author_edges.csv"), index=False)
    institution_edges.to_csv(os.path.join(outputs_dir, f"{stem}_institution_edges.csv"), index=False)

    author_matrix = _build_matrix(author_edges)
    institution_matrix = _build_matrix(institution_edges)
    author_matrix.to_csv(os.path.join(outputs_dir, f"{stem}_author_matrix.csv"))
    institution_matrix.to_csv(os.path.join(outputs_dir, f"{stem}_institution_matrix.csv"))
