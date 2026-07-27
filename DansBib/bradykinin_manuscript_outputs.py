#!/usr/bin/env python3
"""Generate the Bradykinin-mediated angioedema manuscript output bundle.

The generator is deliberately project-scoped. It refuses to fall back to an
all-records file or another project's slug when the Bradykinin dataset is not
present.
"""

from __future__ import annotations

import argparse
import itertools
import math
import re
import textwrap
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from utils.runtime_paths import configure_matplotlib_cache

ROOT = Path(__file__).resolve().parent
configure_matplotlib_cache(ROOT)

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd

from processing.metrics import calculate_h_index

OUTPUTS_DIR = ROOT / "data" / "outputs"
EXPECTED_RECORDS = 1481
EXPECTED_STUDY_YEARS = 20
FLOW_COUNTS = {
    "Records imported": 2817,
    "Scopus": 1384,
    "PubMed": 1061,
    "Web of Science": 207,
    "Embase": 165,
    "Duplicates identified manually": 0,
    "Duplicates identified by Covidence": 1336,
    "Final included records": 1481,
}
DPI = 320
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

COUNTRY_ALIASES = {
    "usa": "United States",
    "u s a": "United States",
    "us": "United States",
    "united states of america": "United States",
    "england": "United Kingdom",
    "uk": "United Kingdom",
    "u k": "United Kingdom",
    "great britain": "United Kingdom",
    "turkiye": "Turkey",
    "türkiye": "Turkey",
}

KEYWORD_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "case",
    "cases",
    "clinical",
    "data",
    "due",
    "effect",
    "effects",
    "for",
    "from",
    "in",
    "is",
    "method",
    "methods",
    "of",
    "on",
    "or",
    "paper",
    "patient",
    "patients",
    "report",
    "reports",
    "research",
    "result",
    "results",
    "review",
    "study",
    "studies",
    "the",
    "this",
    "to",
    "treatment",
    "using",
    "with",
    "without",
}

MEANINGFUL_ABBREVIATIONS = {"hae", "acei-ae", "c1-inh", "ffp"}


class ValidationError(RuntimeError):
    """Raised when the requested project corpus is missing or inconsistent."""


@dataclass(frozen=True)
class DatasetContext:
    slug: str
    records_path: Path
    records: pd.DataFrame
    min_year: int
    max_year: int
    partial_final_year: bool
    complete_year_max: int


def normalize_slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")


def discover_bradykinin_slug(outputs_dir: Path | None = None) -> str:
    """Discover an existing Bradykinin slug from manifests or slugged filenames."""
    outputs_dir = outputs_dir or OUTPUTS_DIR
    if not outputs_dir.exists():
        raise ValidationError(f"Outputs directory does not exist: {outputs_dir}")
    manifest_hits: list[str] = []
    for path in sorted(outputs_dir.glob("*manifest*")) + sorted(outputs_dir.glob("*README*")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"bradykinin|angioedema", text, flags=re.I):
            match = re.search(r"Project slug:\s*([A-Za-z0-9_-]+)", text)
            if match:
                manifest_hits.append(match.group(1))
    if manifest_hits:
        return normalize_slug(manifest_hits[0])

    stems = [path.name for path in outputs_dir.glob("*_year_limited_records.csv")]
    slug_hits = [
        name.replace("_year_limited_records.csv", "")
        for name in stems
        if re.search(r"brady|bradykinin|angio|angioedema", name, flags=re.I)
    ]
    if slug_hits:
        return normalize_slug(slug_hits[0])
    raise ValidationError(
        "Could not identify an existing Bradykinin project slug from manifests or slug-prefixed files. "
        "Pass --slug after the Bradykinin final year-limited dataset is present."
    )


def split_values(value: object) -> list[str]:
    if pd.isna(value):
        return []
    return [part.strip() for part in re.split(r";|\||\n", str(value)) if part.strip()]


def normalize_country(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    key = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    key = re.sub(r"[^a-z0-9]+", " ", key).strip()
    return COUNTRY_ALIASES.get(key, text.title() if text.isupper() or text.islower() else text)


def normalize_author_id(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def display_author_name(canonical: object) -> str:
    text = normalize_author_id(canonical)
    if not text:
        return ""
    if "," in text:
        last, initials = [part.strip() for part in text.split(",", 1)]
    else:
        parts = text.split()
        last, initials = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (text, "")

    def cap_piece(piece: str) -> str:
        particles = {"de", "del", "der", "di", "du", "la", "le", "van", "von"}
        return "-".join(
            "'" .join(
                token if token in particles else token[:1].upper() + token[1:]
                for token in subpiece.split("'")
            )
            for subpiece in piece.split("-")
        )

    last_display = " ".join(cap_piece(part) for part in last.split())
    initials_display = " ".join(
        token.upper() if len(token) <= 3 else token[:1].upper() + token[1:]
        for token in re.split(r"\s+", initials.replace(".", " ").strip())
        if token
    )
    return f"{last_display}, {initials_display}" if initials_display else last_display


def validate_flow_counts() -> list[str]:
    warnings = []
    source_sum = FLOW_COUNTS["Scopus"] + FLOW_COUNTS["PubMed"] + FLOW_COUNTS["Web of Science"] + FLOW_COUNTS["Embase"]
    if source_sum != FLOW_COUNTS["Records imported"]:
        raise ValidationError(f"Flow source counts sum to {source_sum}, expected {FLOW_COUNTS['Records imported']}.")
    included = FLOW_COUNTS["Records imported"] - FLOW_COUNTS["Duplicates identified by Covidence"]
    if included != FLOW_COUNTS["Final included records"]:
        raise ValidationError(f"Flow import-minus-duplicates equals {included}, expected {FLOW_COUNTS['Final included records']}.")
    warnings.append("Methods names PubMed, Scopus, and Web of Science, while the flow diagram also lists Embase.")
    return warnings


def load_validated_dataset(slug: str | None = None, outputs_dir: Path | None = None) -> DatasetContext:
    outputs_dir = outputs_dir or OUTPUTS_DIR
    slug = normalize_slug(slug) if slug else discover_bradykinin_slug(outputs_dir)
    if not re.search(r"brady|angio", slug, flags=re.I):
        raise ValidationError(f"Project slug '{slug}' does not look Bradykinin/angioedema-specific.")
    records_path = outputs_dir / f"{slug}_year_limited_records.csv"
    if not records_path.exists():
        raise ValidationError(f"Expected final year-limited dataset not found: {records_path}")
    records = pd.read_csv(records_path)
    if len(records) != EXPECTED_RECORDS:
        raise ValidationError(f"{records_path.name} contains {len(records):,} records; expected {EXPECTED_RECORDS:,}.")
    years = pd.to_numeric(records.get("year"), errors="coerce").dropna().astype(int)
    if years.empty:
        raise ValidationError("Validated dataset has no usable publication years.")
    min_year, max_year = int(years.min()), int(years.max())
    if max_year - min_year + 1 != EXPECTED_STUDY_YEARS:
        raise ValidationError(
            f"Dataset spans {min_year}-{max_year} ({max_year - min_year + 1} years); expected a 20-year study period."
        )
    partial_final_year = max_year == 2026
    complete_year_max = max_year - 1 if partial_final_year else max_year
    return DatasetContext(slug, records_path, records, min_year, max_year, partial_final_year, complete_year_max)


def save_figure(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, bbox_inches="tight", pad_inches=0.35, facecolor="white")
    plt.close(fig)


def annual_trends(ctx: DatasetContext) -> tuple[Path, Path, pd.DataFrame]:
    years = pd.to_numeric(ctx.records["year"], errors="coerce").dropna().astype(int)
    index = pd.Index(range(ctx.min_year, ctx.max_year + 1), name="year")
    counts = years.value_counts().reindex(index, fill_value=0).sort_index().reset_index(name="annual_publication_count")
    counts["rolling_average"] = counts["annual_publication_count"].rolling(3, min_periods=1).mean().round(2)
    counts["partial_year"] = counts["year"].eq(ctx.max_year) & ctx.partial_final_year
    csv_path = OUTPUTS_DIR / f"{ctx.slug}_figure2_annual_publication_trends.csv"
    counts.to_csv(csv_path, index=False)

    complete = counts[~counts["partial_year"]]
    peak = complete.sort_values(["annual_publication_count", "year"], ascending=[False, False]).iloc[0]
    fig, ax = plt.subplots(figsize=(11, 6.5), dpi=DPI)
    ax.bar(counts["year"], counts["annual_publication_count"], color="#2563eb", alpha=0.82, label="Annual publications")
    ax.plot(counts["year"], counts["rolling_average"], color="#ef4444", linewidth=2.4, marker="o", label="3-year rolling average")
    ax.annotate(
        f"Peak complete year: {int(peak['year'])}\n{int(peak['annual_publication_count'])} publications",
        xy=(peak["year"], peak["annual_publication_count"]),
        xytext=(peak["year"], max(counts["annual_publication_count"]) * 1.08),
        arrowprops={"arrowstyle": "->", "color": "#111827"},
        fontsize=9,
    )
    if ctx.partial_final_year:
        partial = counts[counts["partial_year"]].iloc[0]
        ax.bar([partial["year"]], [partial["annual_publication_count"]], color="#94a3b8", alpha=0.9, label="Partial final year")
        ax.text(partial["year"], partial["annual_publication_count"], "partial", ha="center", va="bottom", fontsize=8)
    ax.set_title("Annual Publication Trends", loc="left", fontsize=16, weight="bold")
    ax.set_xlabel("Year")
    ax.set_ylabel("Publications")
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.25)
    png_path = OUTPUTS_DIR / f"{ctx.slug}_figure2_annual_publication_trends.png"
    save_figure(fig, png_path)
    return png_path, csv_path, counts


def country_productivity(ctx: DatasetContext) -> tuple[Path, Path, Path, pd.DataFrame, pd.DataFrame]:
    rows = []
    for idx, row in ctx.records.iterrows():
        year = pd.to_numeric(pd.Series([row.get("year")]), errors="coerce").iloc[0]
        if pd.isna(year):
            continue
        for country in split_values(row.get("countries") or row.get("country")):
            normalized = normalize_country(country)
            if normalized:
                rows.append({"record_id": idx, "year": int(year), "country": normalized})
    frame = pd.DataFrame(rows, columns=["record_id", "year", "country"]).drop_duplicates()
    yearly = frame.groupby(["year", "country"], as_index=False)["record_id"].nunique().rename(columns={"record_id": "publications"})
    totals = (
        frame.groupby("country", as_index=False)["record_id"]
        .nunique()
        .rename(columns={"record_id": "total_publications"})
        .sort_values(["total_publications", "country"], ascending=[False, True])
    )
    totals["rank"] = range(1, len(totals) + 1)
    top_n = min(10, len(totals))
    top_countries = list(totals.head(top_n)["country"])
    plotted = yearly[yearly["country"].isin(top_countries)].copy()
    full_index = pd.MultiIndex.from_product([range(ctx.min_year, ctx.max_year + 1), top_countries], names=["year", "country"])
    plotted = plotted.set_index(["year", "country"]).reindex(full_index, fill_value=0).reset_index()
    plotted["country"] = pd.Categorical(plotted["country"], categories=top_countries, ordered=True)
    plotted = plotted.sort_values(["country", "year"])
    csv_path = OUTPUTS_DIR / f"{ctx.slug}_figure3_country_trends_top10.csv"
    ranking_path = OUTPUTS_DIR / f"{ctx.slug}_figure3_country_rankings.csv"
    plotted.to_csv(csv_path, index=False)
    totals.to_csv(ranking_path, index=False)

    fig, ax = plt.subplots(figsize=(12.5, 7.2), dpi=DPI)
    for i, country in enumerate(top_countries):
        subset = plotted[plotted["country"].astype(str).eq(country)]
        ax.plot(subset["year"], subset["publications"], marker="o", linewidth=2, label=country, color=COLOR_SEQUENCE[i % len(COLOR_SEQUENCE)])
    ax.set_title("Geographic Productivity: Top 10 Countries", loc="left", fontsize=16, weight="bold")
    ax.set_xlabel("Year")
    ax.set_ylabel("Publications")
    ax.legend(frameon=False, bbox_to_anchor=(1.02, 1), loc="upper left", title="Country")
    ax.grid(axis="y", alpha=0.25)
    png_path = OUTPUTS_DIR / f"{ctx.slug}_figure3_country_trends_top10.png"
    save_figure(fig, png_path)
    return png_path, csv_path, ranking_path, plotted, totals


def build_author_edges(records: pd.DataFrame) -> pd.DataFrame:
    counter: Counter[tuple[str, str]] = Counter()
    for _, row in records.iterrows():
        authors = sorted({normalize_author_id(author) for author in split_values(row.get("authors")) if normalize_author_id(author)})
        for source, target in itertools.combinations(authors, 2):
            counter[(source, target)] += 1
    return pd.DataFrame(
        [{"source": s, "target": t, "weight": w} for (s, t), w in counter.items()],
        columns=["source", "target", "weight"],
    ).sort_values(["weight", "source", "target"], ascending=[False, True, True])


def author_network(ctx: DatasetContext) -> tuple[Path, Path, Path, pd.DataFrame]:
    edge_path = OUTPUTS_DIR / f"{ctx.slug}_author_edges.csv"
    edges = pd.read_csv(edge_path) if edge_path.exists() else build_author_edges(ctx.records)
    graph = nx.Graph()
    for row in edges.itertuples(index=False):
        graph.add_edge(str(row.source), str(row.target), weight=float(row.weight))
    pub_counts: Counter[str] = Counter()
    for _, row in ctx.records.iterrows():
        for author in {normalize_author_id(author) for author in split_values(row.get("authors"))}:
            if author:
                pub_counts[author] += 1
    for author, count in pub_counts.items():
        graph.add_node(author, publication_count=count)
    graph.remove_nodes_from(list(nx.isolates(graph)))
    if graph.number_of_nodes() > 90:
        keep = [node for node, _ in sorted(graph.degree(weight="weight"), key=lambda item: item[1], reverse=True)[:90]]
        graph = graph.subgraph(keep).copy()
    communities = nx.algorithms.community.greedy_modularity_communities(graph, weight="weight") if graph.number_of_edges() else []
    community = {node: idx for idx, members in enumerate(communities) for node in members}
    pos = nx.spring_layout(graph, seed=42, k=1.25 / math.sqrt(max(graph.number_of_nodes(), 1)), iterations=250, weight="weight")
    nodes = []
    for node in sorted(graph.nodes):
        nodes.append(
            {
                "canonical_identifier": node,
                "display_label": display_author_name(node),
                "publication_count": int(pub_counts.get(node, 0)),
                "degree": int(graph.degree(node)),
                "weighted_degree": float(graph.degree(node, weight="weight")),
                "h_index_available": False,
            }
        )
    node_df = pd.DataFrame(nodes).sort_values(["weighted_degree", "publication_count", "canonical_identifier"], ascending=[False, False, True])
    final_edges = nx.to_pandas_edgelist(graph).sort_values(["weight", "source", "target"], ascending=[False, True, True])
    node_path = OUTPUTS_DIR / f"{ctx.slug}_figure4_author_nodes.csv"
    final_edge_path = OUTPUTS_DIR / f"{ctx.slug}_figure4_author_edges.csv"
    node_df.to_csv(node_path, index=False)
    final_edges.to_csv(final_edge_path, index=False)

    label_nodes = set(node_df.head(min(24, len(node_df)))["canonical_identifier"])
    fig, ax = plt.subplots(figsize=(15, 11), dpi=DPI)
    colors = [community.get(node, 0) for node in graph.nodes]
    sizes = [80 + 35 * math.sqrt(float(graph.nodes[node].get("publication_count", 1))) for node in graph.nodes]
    widths = [0.4 + min(3.2, float(data.get("weight", 1)) * 0.45) for _, _, data in graph.edges(data=True)]
    nx.draw_networkx_edges(graph, pos, ax=ax, width=widths, alpha=0.25, edge_color="#64748b")
    nx.draw_networkx_nodes(graph, pos, ax=ax, node_size=sizes, node_color=colors, cmap=plt.cm.tab20, edgecolors="white", linewidths=0.8)
    for node in label_nodes:
        x, y = pos[node]
        ax.text(
            x,
            y,
            display_author_name(node),
            fontsize=7.5,
            ha="center",
            va="center",
            path_effects=[path_effects.withStroke(linewidth=2.4, foreground="white")],
        )
    ax.set_title("Author Collaboration Network", loc="left", fontsize=16, weight="bold")
    ax.text(0.01, 0.01, f"Displayed nodes: {graph.number_of_nodes()}; labels: {len(label_nodes)}", transform=ax.transAxes, fontsize=9)
    ax.margins(0.18)
    ax.axis("off")
    png_path = OUTPUTS_DIR / f"{ctx.slug}_figure4_author_collaboration_network.png"
    save_figure(fig, png_path)
    return png_path, node_path, final_edge_path, node_df


def normalize_keyword(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"^[^a-z0-9]+|[^a-z0-9-]+$", "", text)


def keyword_exclusion_reason(term: str) -> str:
    if not term:
        return "empty"
    if re.search(r"\b(and|or|not)\b\s*\[", term) or re.search(r"\[[a-z]{2,}\]", term):
        return "query syntax or database field tag"
    words = set(re.findall(r"[a-z0-9-]+", term))
    if term not in MEANINGFUL_ABBREVIATIONS and (words <= KEYWORD_STOPWORDS or len(term) < 3):
        return "generic filler word"
    if term in KEYWORD_STOPWORDS:
        return "generic filler word"
    return ""


def extract_keywords(records: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[list[str]]]:
    keyword_cols = [c for c in records.columns if str(c).lower() in {"keywords", "keyword", "author_keywords", "index_keywords", "keywords_plus", "mesh_terms", "mesh"}]
    counts: Counter[str] = Counter()
    variants: defaultdict[str, Counter[str]] = defaultdict(Counter)
    audit: dict[str, str] = {}
    per_record: list[list[str]] = []
    for _, row in records.iterrows():
        terms = []
        for col in keyword_cols:
            for raw in re.split(r";|\||,", str(row.get(col) or "")):
                normalized = normalize_keyword(raw)
                reason = keyword_exclusion_reason(normalized)
                if reason:
                    if normalized:
                        audit[normalized] = reason
                    continue
                counts[normalized] += 1
                variants[normalized][str(raw).strip() or normalized] += 1
                terms.append(normalized)
        per_record.append(sorted(set(terms)))
    count_rows = []
    for rank, (term, freq) in enumerate(sorted(counts.items(), key=lambda item: (-item[1], item[0])), start=1):
        count_rows.append(
            {
                "keyword": next(iter(variants[term])),
                "normalized_keyword": term,
                "frequency": int(freq),
                "rank": rank,
                "exclusion_status": "included",
            }
        )
    removed = pd.DataFrame(
        [{"term": term, "normalized_keyword": term, "reason": reason} for term, reason in sorted(audit.items())],
        columns=["term", "normalized_keyword", "reason"],
    )
    return pd.DataFrame(count_rows), removed, per_record


def keyword_frequencies(ctx: DatasetContext) -> tuple[Path, Path, Path, pd.DataFrame, list[list[str]]]:
    counts, audit, per_record = extract_keywords(ctx.records)
    csv_path = OUTPUTS_DIR / f"{ctx.slug}_figure5_keyword_frequencies.csv"
    audit_path = OUTPUTS_DIR / f"{ctx.slug}_figure5_keyword_filter_audit.csv"
    counts.to_csv(csv_path, index=False)
    audit.to_csv(audit_path, index=False)
    plot_df = counts.head(25).sort_values("frequency")
    fig, ax = plt.subplots(figsize=(10.5, max(6, 0.32 * len(plot_df) + 2)), dpi=DPI)
    ax.barh(plot_df["normalized_keyword"], plot_df["frequency"], color="#0f766e")
    ax.set_title("Keyword Frequencies", loc="left", fontsize=16, weight="bold")
    ax.text(0, 1.02, "Most frequent research terms after removal of generic filler words", transform=ax.transAxes, fontsize=10, color="#475569")
    ax.set_xlabel("Frequency")
    ax.set_ylabel("")
    ax.grid(axis="x", alpha=0.25)
    png_path = OUTPUTS_DIR / f"{ctx.slug}_figure5_keyword_frequencies.png"
    save_figure(fig, png_path)
    return png_path, csv_path, audit_path, counts, per_record


def keyword_network(ctx: DatasetContext, counts: pd.DataFrame, per_record: list[list[str]]) -> tuple[Path, Path, Path, Path, str]:
    top_n = min(45, len(counts))
    included = set(counts.head(top_n)["normalized_keyword"])
    edge_counter: Counter[tuple[str, str]] = Counter()
    for terms in per_record:
        filtered = sorted(set(terms) & included)
        for source, target in itertools.combinations(filtered, 2):
            edge_counter[(source, target)] += 1
    edges = pd.DataFrame(
        [{"source": s, "target": t, "weight": w} for (s, t), w in edge_counter.items() if w >= 2],
        columns=["source", "target", "weight"],
    )
    graph = nx.Graph()
    frequencies = dict(zip(counts["normalized_keyword"], counts["frequency"], strict=False))
    for row in edges.itertuples(index=False):
        graph.add_edge(str(row.source), str(row.target), weight=float(row.weight))
    for node in list(graph.nodes):
        graph.nodes[node]["frequency"] = int(frequencies.get(node, 1))
    if graph.number_of_nodes() and not nx.is_connected(graph):
        graph = graph.subgraph(max(nx.connected_components(graph), key=len)).copy()
    final_nodes = sorted(graph.nodes, key=lambda n: (-frequencies.get(n, 0), n))
    final_edges = nx.to_pandas_edgelist(graph)
    node_df = pd.DataFrame(
        [
            {
                "keyword": node,
                "frequency": int(frequencies.get(node, 0)),
                "degree": int(graph.degree(node)),
                "weighted_degree": float(graph.degree(node, weight="weight")),
                "labeled": "yes",
            }
            for node in final_nodes
        ]
    )
    node_path = OUTPUTS_DIR / f"{ctx.slug}_figure6_keyword_nodes.csv"
    edge_path = OUTPUTS_DIR / f"{ctx.slug}_figure6_keyword_edges.csv"
    audit_path = OUTPUTS_DIR / f"{ctx.slug}_figure6_keyword_label_audit.csv"
    node_df.to_csv(node_path, index=False)
    final_edges.to_csv(edge_path, index=False)
    node_df[["keyword", "labeled"]].to_csv(audit_path, index=False)

    fig, ax = plt.subplots(figsize=(15.5, 11.5), dpi=DPI)
    if graph.number_of_nodes():
        pos = nx.spring_layout(graph, seed=9, k=1.45 / math.sqrt(max(graph.number_of_nodes(), 1)), iterations=350, weight="weight")
        communities = nx.algorithms.community.greedy_modularity_communities(graph, weight="weight") if graph.number_of_edges() else []
        community = {node: idx for idx, members in enumerate(communities) for node in members}
        sizes = [80 + 30 * math.sqrt(float(graph.nodes[n].get("frequency", 1))) for n in graph.nodes]
        widths = [0.35 + min(3.5, float(d.get("weight", 1)) * 0.35) for _, _, d in graph.edges(data=True)]
        nx.draw_networkx_edges(graph, pos, ax=ax, width=widths, alpha=0.22, edge_color="#64748b")
        nx.draw_networkx_nodes(graph, pos, ax=ax, node_size=sizes, node_color=[community.get(n, 0) for n in graph.nodes], cmap=plt.cm.tab20, edgecolors="white")
        for node in graph.nodes:
            x, y = pos[node]
            ax.text(
                x,
                y,
                "\n".join(textwrap.wrap(node, width=18)),
                fontsize=7,
                ha="center",
                va="center",
                path_effects=[path_effects.withStroke(linewidth=2.5, foreground="white")],
            )
    ax.set_title("Keyword Co-occurrence Network", loc="left", fontsize=16, weight="bold")
    ax.margins(0.22)
    ax.axis("off")
    png_path = OUTPUTS_DIR / f"{ctx.slug}_figure6_keyword_cooccurrence_network.png"
    save_figure(fig, png_path)
    summary = (
        f"Nodes: {graph.number_of_nodes()}; edges: {graph.number_of_edges()}; "
        f"filtering threshold: top {top_n} keywords by frequency and edge weight >= 2; "
        "connected-component policy: largest connected component retained."
    )
    return png_path, node_path, edge_path, audit_path, summary


def citation_series(records: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(records.get("citations"), errors="coerce")


def markdown_table(rows: list[dict[str, object]], columns: list[str]) -> str:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell(row.get(column, "")) for column in columns) + " |")
    return "\n".join(lines)


def table1(ctx: DatasetContext, country_rankings: pd.DataFrame) -> tuple[Path, Path, dict[str, str]]:
    records = ctx.records
    authors = [normalize_author_id(author) for value in records.get("authors", pd.Series(dtype=str)) for author in split_values(value)]
    journals = [str(value).strip() for value in records.get("source", pd.Series(dtype=str)) if str(value).strip()]
    countries = [normalize_country(country) for value in records.get("countries", pd.Series(dtype=str)) for country in split_values(value)]
    citations = citation_series(records)
    citation_available = citations.notna()
    usable_citations = citations.dropna().astype(int)
    if usable_citations.empty:
        total_citations = mean_citations = median_iqr = corpus_h = "Not available"
    else:
        total_citations = str(int(usable_citations.sum()))
        mean_citations = f"{usable_citations.mean():.2f}"
        median_iqr = f"{usable_citations.median():.0f} ({usable_citations.quantile(0.25):.0f}-{usable_citations.quantile(0.75):.0f})"
        corpus_h = str(calculate_h_index(usable_citations))
    years = pd.to_numeric(records["year"], errors="coerce").dropna().astype(int)
    complete = records[pd.to_numeric(records["year"], errors="coerce").le(ctx.complete_year_max)]
    annual = pd.to_numeric(complete["year"], errors="coerce").dropna().astype(int).value_counts()
    nonzero = annual[annual > 0].sort_index()
    if len(nonzero) >= 2:
        start_year, end_year = int(nonzero.index[0]), int(nonzero.index[-1])
        cagr = ((float(nonzero.iloc[-1]) / float(nonzero.iloc[0])) ** (1 / (end_year - start_year)) - 1) * 100 if end_year > start_year else 0.0
        growth = f"{cagr:.2f}"
    else:
        growth = "Not available"
    author_counts = Counter(author for author in authors if author)
    journal_counts = Counter(journals)
    top_author_count = max(author_counts.values()) if author_counts else 0
    top_journal_count = max(journal_counts.values()) if journal_counts else 0
    values = {
        "Total publications": str(len(records)),
        "Study period": f"{ctx.min_year}-{ctx.max_year}",
        "Total authors": str(len(set(author for author in authors if author))),
        "Unique journals": str(len(set(journals))),
        "Countries represented": str(len(set(country for country in countries if country))),
        "Total citations": total_citations,
        "Mean citations per publication": mean_citations,
        "Median citations with IQR": median_iqr,
        "Corpus h-index": corpus_h,
        "Average authors per publication": f"{len([a for a in authors if a]) / max(records.get('authors', pd.Series(dtype=str)).map(lambda v: bool(split_values(v))).sum(), 1):.2f}",
        "Annual publication growth rate (%)": growth,
        "Most productive journal": "; ".join([f"{j} ({top_journal_count})" for j, c in sorted(journal_counts.items()) if c == top_journal_count]) or "Not available",
        "Most productive country": "; ".join(country_rankings.head(1).apply(lambda r: f"{r['country']} ({int(r['total_publications'])})", axis=1)) if not country_rankings.empty else "Not available",
        "Most productive author": "; ".join([f"{display_author_name(a)} ({top_author_count})" for a, c in sorted(author_counts.items()) if c == top_author_count]) or "Not available",
        "Citation-data coverage": f"{int(citation_available.sum())}/{len(records)} records with usable citation counts",
    }
    csv_path = OUTPUTS_DIR / f"{ctx.slug}_table1_bibliometric_characteristics.csv"
    md_path = OUTPUTS_DIR / f"{ctx.slug}_table1_bibliometric_characteristics.md"
    csv_rows = [{"metric": k, "value": v} for k, v in values.items()]
    md_rows = [{"Metric": k, "Value": v} for k, v in values.items()]
    pd.DataFrame(csv_rows).to_csv(csv_path, index=False)
    md_path.write_text(
        markdown_table(md_rows, ["Metric", "Value"])
        + "\n\nMetric definitions follow the manuscript request and use the final validated dataset.\n",
        encoding="utf-8",
    )
    return csv_path, md_path, values


def infer_study_type_and_contribution(row: pd.Series) -> tuple[str, str, str, str]:
    text = f"{row.get('title', '')} {row.get('abstract', '')}".lower()
    if not text.strip():
        return "Needs manual review", "Needs manual review", "low", "Needs manual review"
    if "random" in text or "trial" in text:
        study_type = "Clinical trial"
    elif "review" in text or "meta-analysis" in text:
        study_type = "Review"
    elif "case report" in text or "case series" in text:
        study_type = "Case report/series"
    elif "cohort" in text or "registry" in text:
        study_type = "Observational cohort/registry"
    else:
        study_type = "Needs manual review"
    contribution = "Needs manual review"
    if "treatment" in text or "therapy" in text or "inhibitor" in text:
        contribution = "Therapeutic evidence or management focus"
    elif "genetic" in text or "mutation" in text:
        contribution = "Genetic or pathophysiology focus"
    confidence = "medium" if study_type != "Needs manual review" or contribution != "Needs manual review" else "low"
    status = "Needs manual review" if "Needs manual review" in {study_type, contribution} else "Auto-classified"
    return study_type, contribution, confidence, status


def table2(ctx: DatasetContext) -> tuple[Path, Path, Path, pd.DataFrame]:
    records = ctx.records.copy()
    records["citations_numeric"] = citation_series(records)
    records = records.dropna(subset=["citations_numeric"])
    records["year_numeric"] = pd.to_numeric(records.get("year"), errors="coerce").fillna(0).astype(int)
    records["title_sort"] = records.get("title", pd.Series(dtype=str)).astype(str).str.lower()
    top = records.sort_values(["citations_numeric", "year_numeric", "title_sort"], ascending=[False, False, True]).head(10).copy()
    rows, review_rows = [], []
    for rank, (_, row) in enumerate(top.iterrows(), start=1):
        first_author = display_author_name(split_values(row.get("authors"))[0]) if split_values(row.get("authors")) else "Not available"
        study_type, contribution, confidence, status = infer_study_type_and_contribution(row)
        rows.append(
            {
                "Rank": rank,
                "First Author": first_author,
                "Year": int(row.get("year_numeric", 0)) or "",
                "Journal": row.get("source", ""),
                "Citations": int(row.get("citations_numeric", 0)),
                "Study Type": study_type,
                "Primary Contribution": contribution,
            }
        )
        abstract = str(row.get("abstract") or "")
        review_rows.append(
            {
                "rank": rank,
                "doi": row.get("doi", ""),
                "full_title": row.get("title", ""),
                "abstract_evidence": textwrap.shorten(abstract, width=260, placeholder="..."),
                "inferred_study_type": study_type,
                "inferred_contribution": contribution,
                "confidence": confidence,
                "review_status": status,
            }
        )
    table = pd.DataFrame(rows)
    review = pd.DataFrame(review_rows)
    csv_path = OUTPUTS_DIR / f"{ctx.slug}_table2_top_publications.csv"
    review_path = OUTPUTS_DIR / f"{ctx.slug}_table2_top_publications_review.csv"
    md_path = OUTPUTS_DIR / f"{ctx.slug}_table2_top_publications.md"
    table.to_csv(csv_path, index=False)
    review.to_csv(review_path, index=False)
    md_path.write_text(markdown_table(table.to_dict(orient="records"), list(table.columns)) + "\n", encoding="utf-8")
    return csv_path, review_path, md_path, review


def write_summary(ctx: DatasetContext, files: dict[str, Path], table1_values: dict[str, str], table2_review: pd.DataFrame, warnings: list[str], keyword_network_summary: str) -> Path:
    path = OUTPUTS_DIR / f"{ctx.slug}_manuscript_results_summary.md"
    manual = table2_review[table2_review["review_status"].eq("Needs manual review")] if not table2_review.empty else pd.DataFrame()
    lines = [
        "# Manuscript Results Summary",
        "",
        f"Project slug: `{ctx.slug}`",
        f"Validation: {len(ctx.records):,} final deduplicated, year-limited records from `{ctx.records_path.name}`.",
        f"Study period: {ctx.min_year}-{ctx.max_year}.",
        f"Final year partial: {'yes' if ctx.partial_final_year else 'no'}.",
        "",
        "## Figure Files",
    ]
    lines.extend(f"- {name}: `{file.name}`" for name, file in files.items() if file.suffix.lower() == ".png")
    lines.extend(
        [
            "",
            "## Draft Legends",
            "- Figure 2. Annual publication counts by year with a three-year rolling average. The peak annotation excludes a partial current year when present.",
            "- Figure 3. Annual publication counts for the ten countries with the highest total publication counts across the validated study period. Legend order follows total output.",
            "- Figure 4. Author collaboration network using Bradykinin author edges only. Node size reflects publication count, edge thickness reflects shared-publication weight, and color reflects detected community.",
            "- Figure 5. Keyword frequencies after removing generic filler words, query fragments, and database field syntax.",
            f"- Figure 6. Keyword co-occurrence network. Node size reflects keyword frequency, edge thickness reflects co-occurrence strength, colors reflect detected communities, and all displayed nodes are labeled. {keyword_network_summary}",
            "",
            "## Table 1",
        ]
    )
    lines.extend(f"- {k}: {v}" for k, v in table1_values.items())
    lines.extend(["", "## Table 2 Preview"])
    if not table2_review.empty:
        lines.extend(f"- Rank {int(r.rank)}: {r.full_title} ({r.review_status})" for r in table2_review.itertuples(index=False))
    lines.extend(["", "## Warnings And Data Issues"])
    lines.extend(f"- {warning}" for warning in warnings)
    lines.append(f"- Citation-data coverage: {table1_values.get('Citation-data coverage', 'Not available')}.")
    if not manual.empty:
        lines.append(f"- Table 2 rows needing manual review: {', '.join(str(int(v)) for v in manual['rank'])}.")
    lines.append("- Keyword filters: generic filler stopwords include `due` and `can`; query syntax and database field tags are excluded.")
    lines.append("- Network filters: Figure 6 uses the top 45 keywords by frequency and retains edges with weight >= 2 in the largest connected component.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def generate_bundle(slug: str | None = None) -> dict[str, Path]:
    warnings = validate_flow_counts()
    ctx = load_validated_dataset(slug)
    files: dict[str, Path] = {}
    files["figure2_png"], files["figure2_csv"], annual = annual_trends(ctx)
    files["figure3_png"], files["figure3_csv"], files["figure3_rankings"], countries, country_rankings = country_productivity(ctx)
    files["figure4_png"], files["figure4_nodes"], files["figure4_edges"], author_nodes = author_network(ctx)
    files["figure5_png"], files["figure5_csv"], files["figure5_audit"], keyword_counts, per_record = keyword_frequencies(ctx)
    (
        files["figure6_png"],
        files["figure6_nodes"],
        files["figure6_edges"],
        files["figure6_label_audit"],
        keyword_network_summary,
    ) = keyword_network(ctx, keyword_counts, per_record)
    files["table1_csv"], files["table1_md"], table1_values = table1(ctx, country_rankings)
    files["table2_csv"], files["table2_review"], files["table2_md"], table2_review = table2(ctx)
    files["summary"] = write_summary(ctx, files, table1_values, table2_review, warnings, keyword_network_summary)
    missing = [path for path in files.values() if not path.exists() or path.stat().st_size == 0]
    if missing:
        raise ValidationError(f"Generated bundle has missing or empty required files: {missing}")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default=None, help="Existing Bradykinin project slug. Auto-detected when omitted.")
    args = parser.parse_args()
    try:
        files = generate_bundle(args.slug)
    except ValidationError as exc:
        raise SystemExit(f"VALIDATION FAILED: {exc}") from exc
    print("Generated Bradykinin manuscript bundle:")
    for path in files.values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
