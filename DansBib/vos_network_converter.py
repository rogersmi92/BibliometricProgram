"""Derive VOSviewer network and overlay files from the cleaned core dataset."""

from __future__ import annotations

import itertools
import argparse
import statistics
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = PROJECT_DIR / "data" / "outputs"
VOS_DIR = PROJECT_DIR / "data" / "VOS"
REQUIRED_CORE_COLUMNS = ("title", "authors", "year")
DERIVED_ONLY_COLUMNS = (
    "h_index",
    "year_count",
    "edge",
    "rank",
)
SKIP_STEM_SUFFIXES = (
    "_raw",
    "_excluded_records",
    "_qa_summary",
    "_top_authors",
    "_top_papers",
    "_author_edges",
    "_author_matrix",
    "_institution_edges",
    "_institution_matrix",
    "_publication_years",
    "_country_year_counts",
    "_institution_year_counts",
    "_h_index",
    "_vosviewer",
)


def normalize_label(value: object) -> str:
    """Normalize labels consistently while preserving internal punctuation."""
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return ""
    return " ".join(text.split()).casefold()


def parse_year(value: object) -> float | None:
    year = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(year):
        return None
    return float(year)


def split_tokens(value: object, separators: tuple[str, ...]) -> list[str]:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return []

    normalized = text
    primary = separators[0]
    for separator in separators[1:]:
        normalized = normalized.replace(separator, primary)

    tokens: list[str] = []
    seen: set[str] = set()
    for raw_token in normalized.split(primary):
        token = normalize_label(raw_token)
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def split_authors(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return []
    return split_tokens(text, (";",))


def split_keywords(value: object) -> list[str]:
    return split_tokens(value, (",", ";"))


def split_countries(row: pd.Series) -> list[str]:
    values = split_tokens(row.get("country"), (";", ",")) if "country" in row.index else []
    if not values and "countries" in row.index:
        values = split_tokens(row.get("countries"), (";", ","))

    deduped: list[str] = []
    seen: set[str] = set()
    for country in values:
        if country and country not in seen:
            seen.add(country)
            deduped.append(country)
    return deduped


def pairwise(tokens: list[str]) -> set[tuple[str, str]]:
    pairs = set()
    for node1, node2 in itertools.combinations(tokens, 2):
        if node1 != node2:
            pairs.add(tuple(sorted((node1, node2))))
    return pairs


def build_network(df: pd.DataFrame, token_getter) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for _, row in df.iterrows():
        tokens = [token.strip() for token in token_getter(row) if token.strip()]
        if len(tokens) < 2:
            continue
        edges.update(pairwise(tokens))
    return edges


def write_network(edges: set[tuple[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for node1, node2 in sorted(edges):
            handle.write(f"{node1};{node2}\n")


def build_overlay(df: pd.DataFrame, token_getter) -> pd.DataFrame:
    years_by_label: dict[str, list[float]] = {}
    for _, row in df.iterrows():
        year = parse_year(row.get("year"))
        if year is None:
            continue
        for token in token_getter(row):
            years_by_label.setdefault(token, []).append(year)

    rows = [
        {"label": label, "year": int(round(statistics.median(years)))}
        for label, years in years_by_label.items()
        if years
    ]
    return pd.DataFrame(rows, columns=["label", "year"]).sort_values("label")


def write_overlay(overlay: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("label;year\n")
        for _, row in overlay.iterrows():
            handle.write(f"{row['label']};{int(row['year'])}\n")


def detect_core_dataset(outputs_dir: Path = OUTPUTS_DIR) -> Path:
    if not outputs_dir.exists():
        raise FileNotFoundError(f"Outputs directory not found: {outputs_dir}")

    candidates: list[tuple[Path, int]] = []
    inspected: list[str] = []
    for path in sorted(outputs_dir.glob("*.csv")):
        if path.stem.endswith(SKIP_STEM_SUFFIXES) and not path.stem.endswith("_cleaned"):
            inspected.append(f"{path.name}: derived/non-core filename")
            continue
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

        row_count = count_csv_rows(path)
        candidates.append((path, row_count))

    if len(candidates) == 1:
        return candidates[0][0]
    if len(candidates) > 1:
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

    print("Inspected CSV files:")
    for detail in inspected:
        print(f"  {detail}")
    raise FileNotFoundError(
        "No core dataset CSV found in data/outputs/. A core dataset must contain title, authors, and year columns "
        "and must not contain derived-only columns such as h_index, year_count, edge, or rank."
    )


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        row_count = sum(1 for _ in handle)
    return max(0, row_count - 1)


def derive_citation_network(df: pd.DataFrame, stem: str) -> Path | None:
    if "references" not in df.columns:
        return None

    edges: set[tuple[str, str]] = set()
    for _, row in df.iterrows():
        paper = normalize_label(row.get("title") or row.get("doi"))
        if not paper:
            continue
        for reference in split_tokens(row.get("references"), (";", "|", "\n")):
            if reference and reference != paper:
                edges.add((paper, reference))

    out_path = VOS_DIR / f"{stem}_citation_network.txt"
    write_network(edges, out_path)
    return out_path


def derive_vos_outputs(dataset_path: Path) -> list[Path]:
    if not dataset_path.exists():
        raise FileNotFoundError(f"Core dataset not found: {dataset_path}")

    print(f"Using core dataset: {dataset_path}")
    df = pd.read_csv(dataset_path)
    stem = dataset_path.stem
    VOS_DIR.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    if "authors" in df.columns:
        author_network = VOS_DIR / f"{stem}_author_network.txt"
        author_edges = build_network(df, lambda row: split_authors(row.get("authors")))
        write_network(author_edges, author_network)
        print(f"Author network edges generated: {len(author_edges)}")
        created.append(author_network)

        author_overlay = VOS_DIR / f"{stem}_author_overlay.txt"
        write_overlay(build_overlay(df, lambda row: split_authors(row.get("authors"))), author_overlay)
        created.append(author_overlay)
    else:
        print("Skipping author outputs: no authors column.")

    if "keywords" in df.columns:
        keyword_network = VOS_DIR / f"{stem}_keyword_network.txt"
        write_network(build_network(df, lambda row: split_keywords(row.get("keywords"))), keyword_network)
        created.append(keyword_network)

        keyword_overlay = VOS_DIR / f"{stem}_keyword_overlay.txt"
        write_overlay(build_overlay(df, lambda row: split_keywords(row.get("keywords"))), keyword_overlay)
        created.append(keyword_overlay)
    else:
        print("Skipping keyword outputs: no keywords column.")

    if any(column in df.columns for column in ("country", "countries")):
        country_network = VOS_DIR / f"{stem}_country_network.txt"
        write_network(build_network(df, split_countries), country_network)
        created.append(country_network)

        country_overlay = VOS_DIR / f"{stem}_country_overlay.txt"
        write_overlay(build_overlay(df, split_countries), country_overlay)
        created.append(country_overlay)
    else:
        print("Skipping country outputs: no country/countries column.")

    citation_network = derive_citation_network(df, stem)
    if citation_network:
        created.append(citation_network)
    else:
        print("Skipping citation network: no references column.")

    print(f"\nDerived {len(created)} VOS file(s) from {dataset_path.name}:")
    for path in created:
        print(f"  {path}")

    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive VOSviewer network and overlay files from a core dataset.")
    parser.add_argument("--core", type=Path, help="Path to the core dataset CSV. If omitted, auto-detects in data/outputs/.")
    args = parser.parse_args()

    dataset_path = args.core if args.core else detect_core_dataset()
    derive_vos_outputs(dataset_path)


if __name__ == "__main__":
    main()
