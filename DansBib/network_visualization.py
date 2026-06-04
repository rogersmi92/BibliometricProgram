#!/usr/bin/env python3
"""Create clean NetworkX + Plotly network visualizations from edge lists.

Inputs are semicolon edge lists with one edge per line:

    node1;node2

Optional node attributes can be supplied as CSV/TSV/semicolon-delimited files
with a node/name/label column and any of: type, year, frequency.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import warnings
from collections import Counter
from pathlib import Path
from typing import Iterable

import networkx as nx
import pandas as pd
import plotly.graph_objects as go


ROOT = Path(__file__).resolve().parent
VISUALS_DIR = ROOT / "data" / "visuals"
DEFAULT_PNG = VISUALS_DIR / "network.png"
EXPORT_ONLY_PNG = True
VOS_DIR = ROOT / "data" / "VOS"
OUTPUTS_DIR = ROOT / "data" / "outputs"
DEFAULT_AUTO_TOP_N = 300
_MPLCONFIGDIR = VISUALS_DIR / ".mplconfig"
_MPLCONFIGDIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPLCONFIGDIR))

TYPE_COLORS = {
    "drug": "#2563eb",
    "procedural": "#f97316",
    "procedure": "#f97316",
    "both": "#9333ea",
    "unknown": "#6b7280",
}
TYPE_LABELS = {
    "drug": "Drug",
    "procedural": "Procedural",
    "procedure": "Procedural",
    "both": "Both",
    "unknown": "Unknown",
}
DISPLAY_TYPES = ("drug", "procedural", "both", "unknown")
DEFAULT_EDGE_PATTERNS = (
    VOS_DIR / "*_network_combined.txt",
    VOS_DIR / "*_network_*.txt",
    VOS_DIR / "*_network_combined.csv",
    VOS_DIR / "*_network_*.csv",
    OUTPUTS_DIR / "*_author_edges.csv",
    OUTPUTS_DIR / "*_institution_edges.csv",
    OUTPUTS_DIR / "*_keyword_edges.csv",
)


def clean_node(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def normalize_type(value: object) -> str:
    text = str(value or "").strip().casefold()
    if text in {"drug", "drugs", "medication", "medications"}:
        return "drug"
    if text in {"procedural", "procedure", "procedures", "intervention", "interventions"}:
        return "procedural"
    if text in {"both", "drug/procedural", "drug and procedural", "mixed"}:
        return "both"
    return "unknown"


def parse_float(value: object) -> float | None:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number):
        return None
    return float(number)


def sniff_delimiter(path: Path) -> str:
    sample = path.read_text(encoding="utf-8-sig", errors="replace")[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except csv.Error:
        return ";" if path.suffix.lower() in {".txt", ".tsv"} else ","


def read_edge_list(paths: Iterable[Path]) -> tuple[list[tuple[str, str]], Counter[str]]:
    edges: list[tuple[str, str]] = []
    frequencies: Counter[str] = Counter()
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"Edge list not found: {path}")
        delimiter = sniff_delimiter(path)
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            for row in reader:
                if len(row) < 2:
                    continue
                node1, node2 = clean_node(row[0]), clean_node(row[1])
                if not node1 or not node2 or node1 == node2:
                    continue
                if node1.casefold() in {"node1", "source", "from"} and node2.casefold() in {"node2", "target", "to"}:
                    continue
                edges.append((node1, node2))
                frequencies.update([node1, node2])
    return edges, frequencies


def discover_default_edge_paths() -> list[Path]:
    """Find current generated edge-list files when no --edges argument is supplied."""
    discovered: list[Path] = []
    seen: set[Path] = set()
    for pattern in DEFAULT_EDGE_PATTERNS:
        for path in sorted(pattern.parent.glob(pattern.name)):
            if path.name.startswith(".") or path in seen:
                continue
            seen.add(path)
            discovered.append(path)
    return discovered


def describe_edge_search_paths() -> str:
    """Return a readable list of default edge-list locations."""
    return ", ".join(str(pattern.relative_to(ROOT)) for pattern in DEFAULT_EDGE_PATTERNS)


def read_node_attributes(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Node attributes file not found: {path}")

    delimiter = sniff_delimiter(path)
    df = pd.read_csv(path, sep=delimiter)
    columns_by_lower = {str(column).strip().casefold(): column for column in df.columns}
    node_column = next(
        (columns_by_lower[name] for name in ("node", "label", "name", "id", "term") if name in columns_by_lower),
        df.columns[0],
    )
    type_column = next((columns_by_lower[name] for name in ("type", "node_type") if name in columns_by_lower), None)
    year_column = next((columns_by_lower[name] for name in ("year", "avg_year", "median_year") if name in columns_by_lower), None)
    frequency_column = next(
        (columns_by_lower[name] for name in ("frequency", "freq", "count", "weight") if name in columns_by_lower),
        None,
    )

    attributes: dict[str, dict[str, object]] = {}
    for row in df.itertuples(index=False):
        row_data = dict(zip(df.columns, row, strict=False))
        node = clean_node(row_data.get(node_column))
        if not node:
            continue
        attributes[node] = {
            "type": normalize_type(row_data.get(type_column)) if type_column else "unknown",
            "year": parse_float(row_data.get(year_column)) if year_column else None,
            "frequency": parse_float(row_data.get(frequency_column)) if frequency_column else None,
        }
    return attributes


def read_intervention_attributes(path: Path | None) -> tuple[list[tuple[str, str]], dict[str, dict[str, object]]]:
    """Read existing drug_name/procedure_name/count CSVs as weighted edge metadata."""
    if path is None:
        return [], {}
    if not path.exists():
        raise FileNotFoundError(f"Intervention CSV not found: {path}")
    df = pd.read_csv(path)
    required = {"drug_name", "procedure_name"}
    if not required <= set(df.columns):
        return [], {}

    edges: list[tuple[str, str]] = []
    attrs: dict[str, dict[str, object]] = {}
    counts: Counter[str] = Counter()
    for row in df.itertuples(index=False):
        row_data = dict(zip(df.columns, row, strict=False))
        drug = clean_node(row_data.get("drug_name"))
        procedure = clean_node(row_data.get("procedure_name"))
        if not drug or not procedure or drug == procedure:
            continue
        count = parse_float(row_data.get("count")) or 1.0
        edges.append((drug, procedure))
        counts[drug] += count
        counts[procedure] += count
        attrs.setdefault(drug, {"type": "drug", "year": None, "frequency": None})
        attrs.setdefault(procedure, {"type": "procedural", "year": None, "frequency": None})

    for node, count in counts.items():
        attrs[node]["frequency"] = float(count)
    return edges, attrs


def discover_intervention_file(edge_paths: Iterable[Path]) -> Path | None:
    for edge_path in edge_paths:
        candidates = [
            edge_path.with_name(edge_path.name.replace("_network_", "_interventions_")).with_suffix(".csv"),
            edge_path.with_name(edge_path.name.replace("network", "interventions")).with_suffix(".csv"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return None


def build_graph(
    edges: Iterable[tuple[str, str]],
    node_attributes: dict[str, dict[str, object]] | None = None,
    fallback_frequencies: Counter[str] | None = None,
) -> nx.Graph:
    graph = nx.Graph()
    for node1, node2 in edges:
        if graph.has_edge(node1, node2):
            graph[node1][node2]["weight"] += 1
        else:
            graph.add_edge(node1, node2, weight=1)

    fallback_frequencies = fallback_frequencies or Counter()
    node_attributes = node_attributes or {}
    for node in graph.nodes:
        attrs = node_attributes.get(node, {})
        graph.nodes[node]["type"] = normalize_type(attrs.get("type"))
        graph.nodes[node]["year"] = attrs.get("year")
        graph.nodes[node]["frequency"] = attrs.get("frequency") or float(fallback_frequencies.get(node, 0) or graph.degree(node))
    return graph


def filter_graph(graph: nx.Graph, min_degree: int = 2, top_n: int | None = None) -> nx.Graph:
    if top_n is not None and top_n > 0:
        ranked_nodes = sorted(graph.degree, key=lambda item: item[1], reverse=True)[:top_n]
        return graph.subgraph([node for node, _degree in ranked_nodes]).copy()
    return graph.subgraph([node for node, degree in graph.degree if degree >= min_degree]).copy()


def compute_spring_layout(graph: nx.Graph) -> dict[object, object]:
    """Compute a deterministic spring layout, falling back when SciPy is absent."""
    try:
        return nx.spring_layout(graph, k=0.4, iterations=50, seed=42, weight="weight")
    except ModuleNotFoundError as exc:
        if exc.name != "scipy":
            raise

    import numpy as np
    from networkx.drawing.layout import _fruchterman_reingold, rescale_layout

    print("SciPy is not installed; using NetworkX's dense layout fallback.")
    rng = np.random.RandomState(42)
    adjacency = nx.to_numpy_array(graph, weight="weight")
    positions = _fruchterman_reingold(adjacency, 0.4, None, None, 50, 1e-4, 2, rng)
    positions = rescale_layout(positions, scale=1)
    return dict(zip(graph, positions, strict=False))


def node_size(value: float, max_value: float) -> float:
    if max_value <= 0:
        return 14
    return 9 + 34 * math.sqrt(max(value, 1) / max_value)


def create_figure(
    graph: nx.Graph,
    title: str = "Network Visualization",
    color_by_year: bool = False,
) -> go.Figure:
    if graph.number_of_nodes() == 0:
        raise ValueError("No nodes remain after filtering.")

    pos = compute_spring_layout(graph)
    degrees = dict(graph.degree)
    frequencies = {node: float(graph.nodes[node].get("frequency") or degrees[node]) for node in graph.nodes}
    max_frequency = max(frequencies.values()) if frequencies else 1

    edge_x: list[float | None] = []
    edge_y: list[float | None] = []
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
            line={"width": 0.65, "color": "rgba(55, 65, 81, 0.30)"},
            hoverinfo="skip",
            showlegend=False,
        )
    )

    if color_by_year:
        years = [graph.nodes[node].get("year") for node in graph.nodes]
        year_values = [year if isinstance(year, (int, float)) else None for year in years]
        marker_color: list[float | None] | list[str] = year_values
        colorbar = {"title": "Year"}
        colorscale = "Viridis"
        showscale = True
        legend_types: Iterable[str] = ()
    else:
        marker_color = [TYPE_COLORS.get(graph.nodes[node].get("type"), TYPE_COLORS["unknown"]) for node in graph.nodes]
        colorbar = None
        colorscale = None
        showscale = False
        legend_types = DISPLAY_TYPES

    hover_text = []
    for node in graph.nodes:
        attrs = graph.nodes[node]
        node_type = TYPE_LABELS.get(str(attrs.get("type")), "Unknown")
        year = attrs.get("year")
        year_text = int(year) if isinstance(year, (int, float)) and not pd.isna(year) else "Unknown"
        hover_text.append(
            f"<b>{node}</b><br>"
            f"Type: {node_type}<br>"
            f"Frequency: {frequencies[node]:.0f}<br>"
            f"Year: {year_text}<br>"
            f"Degree: {degrees[node]}"
        )

    fig.add_trace(
        go.Scatter(
            x=[pos[node][0] for node in graph.nodes],
            y=[pos[node][1] for node in graph.nodes],
            mode="markers",
            text=hover_text,
            hoverinfo="text",
            marker={
                "size": [node_size(frequencies[node], max_frequency) for node in graph.nodes],
                "color": marker_color,
                "colorscale": colorscale,
                "colorbar": colorbar,
                "showscale": showscale,
                "line": {"width": 1.1, "color": "white"},
                "opacity": 0.94,
            },
            showlegend=False,
        )
    )

    for node_type in legend_types:
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker={"size": 12, "color": TYPE_COLORS[node_type], "line": {"width": 1.1, "color": "white"}},
                name=TYPE_LABELS[node_type],
                hoverinfo="skip",
            )
        )

    fig.update_layout(
        title={"text": title, "x": 0.02, "xanchor": "left"},
        template="plotly_white",
        font={"family": "Inter, DejaVu Sans, Arial, sans-serif", "size": 14, "color": "#111827"},
        height=900,
        margin={"l": 20, "r": 20, "t": 72, "b": 20},
        plot_bgcolor="#f8fafc",
        paper_bgcolor="#f8fafc",
        legend={"title": "Node type", "orientation": "h", "yanchor": "bottom", "y": 1.01, "xanchor": "left", "x": 0},
        hoverlabel={"bgcolor": "white", "bordercolor": "#cbd5e1", "font_size": 13},
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def save_static_png_fallback(graph: nx.Graph, output_png: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    pos = nx.spring_layout(graph, k=0.4, iterations=50, seed=42, weight="weight")
    degrees = dict(graph.degree)
    frequencies = {node: float(graph.nodes[node].get("frequency") or degrees[node]) for node in graph.nodes}
    max_frequency = max(frequencies.values()) if frequencies else 1
    weights = [graph.edges[edge].get("weight", 1) for edge in graph.edges]
    max_weight = max(weights) if weights else 1

    fig, ax = plt.subplots(figsize=(14, 10), dpi=220)
    fig.patch.set_facecolor("#f8fafc")
    ax.set_facecolor("#f8fafc")
    nx.draw_networkx_edges(
        graph,
        pos,
        ax=ax,
        width=[0.25 + 1.1 * (weight / max_weight) for weight in weights],
        alpha=0.30,
        edge_color="#374151",
    )

    for node_type in DISPLAY_TYPES:
        nodes = [node for node in graph.nodes if graph.nodes[node].get("type") == node_type]
        if not nodes:
            continue
        nx.draw_networkx_nodes(
            graph,
            pos,
            nodelist=nodes,
            node_size=[55 + 760 * math.sqrt(max(frequencies[node], 1) / max_frequency) for node in nodes],
            node_color=TYPE_COLORS[node_type],
            linewidths=1.1,
            edgecolors="white",
            alpha=0.94,
            ax=ax,
        )

    label_nodes = sorted(frequencies, key=frequencies.get, reverse=True)[:18]
    nx.draw_networkx_labels(
        graph,
        pos,
        labels={node: node if len(node) <= 28 else f"{node[:25]}..." for node in label_nodes},
        font_size=7,
        font_color="#111827",
        ax=ax,
    )

    handles = [
        Line2D([0], [0], marker="o", color="none", label=TYPE_LABELS[node_type], markerfacecolor=TYPE_COLORS[node_type], markersize=9)
        for node_type in DISPLAY_TYPES
        if any(graph.nodes[node].get("type") == node_type for node in graph.nodes)
    ]
    if handles:
        ax.legend(handles=handles, loc="upper left", frameon=False, ncols=min(len(handles), 4))
    ax.set_title(title, loc="left", fontsize=18, weight="bold", color="#111827", pad=18)
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(output_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def save_network_visualization(
    edge_paths: Iterable[Path],
    attributes_path: Path | None = None,
    intervention_attributes_path: Path | None = None,
    output_png: Path = DEFAULT_PNG,
    min_degree: int = 2,
    top_n: int | None = None,
    title: str = "Drug and Procedural Relationship Network",
    color_by_year: bool = False,
) -> tuple[Path, nx.Graph]:
    edge_paths = list(edge_paths)
    if not edge_paths and intervention_attributes_path is None:
        raise ValueError("Provide at least one edge list or an intervention CSV.")

    edges, fallback_frequencies = read_edge_list(edge_paths) if edge_paths else ([], Counter())
    if intervention_attributes_path is None:
        intervention_attributes_path = discover_intervention_file(edge_paths)
    intervention_edges, intervention_attrs = read_intervention_attributes(intervention_attributes_path)
    attrs = {**intervention_attrs, **read_node_attributes(attributes_path)}

    all_edges = edges + intervention_edges
    for node1, node2 in intervention_edges:
        fallback_frequencies.update([node1, node2])

    graph = build_graph(all_edges, attrs, fallback_frequencies)
    graph = filter_graph(graph, min_degree=min_degree, top_n=top_n)
    fig = create_figure(graph, title=title, color_by_year=color_by_year)

    output_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            fig.write_image(output_png, scale=2)
    except Exception as exc:
        print(f"Plotly PNG export failed ({exc.__class__.__name__}); using Matplotlib fallback.")
        save_static_png_fallback(graph, output_png, title)
    return output_png, graph


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create NetworkX + Plotly network visualizations.")
    parser.add_argument(
        "--edges",
        nargs="*",
        type=Path,
        default=None,
        help="Edge list file(s), one source/target edge per row. If omitted, generated VOS/output edge files are auto-detected.",
    )
    parser.add_argument("--attributes", type=Path, help="Optional node attributes file with node/type/year/frequency columns.")
    parser.add_argument(
        "--interventions",
        type=Path,
        default=None,
        help="Optional existing drug_name/procedure_name/count CSV used to infer node types and frequencies.",
    )
    parser.add_argument("--html", type=Path, default=None, help="Deprecated; HTML exports are disabled by default.")
    parser.add_argument("--png", type=Path, default=None, help="Output static PNG path.")
    parser.add_argument(
        "--overwrite-generic-output",
        action="store_true",
        help="Write generic network_interactive.html/network.png outputs instead of slug-specific files.",
    )
    parser.add_argument("--min-degree", type=int, default=2, help="Remove nodes with degree lower than this value.")
    parser.add_argument("--top-n", type=int, help="Keep only the top N nodes by degree.")
    parser.add_argument("--title", default="Drug and Procedural Relationship Network")
    parser.add_argument("--color-by-year", action="store_true", help="Use a year gradient instead of node type colors.")
    return parser.parse_args()


def infer_output_stem(edge_paths: Iterable[Path]) -> str:
    """Infer a slug-specific visualization stem from the first edge list."""
    for path in edge_paths:
        stem = path.stem
        for suffix in (
            "_author_edges",
            "_institution_edges",
            "_keyword_edges",
            "_author_network",
            "_keyword_network",
            "_country_network",
            "_network_combined",
        ):
            if stem.endswith(suffix):
                return stem[: -len(suffix)] or stem
        return stem
    return "network"


def main() -> None:
    args = parse_args()
    auto_edges = args.edges is None
    edge_paths = discover_default_edge_paths() if auto_edges else args.edges
    if not edge_paths and args.interventions is None:
        raise FileNotFoundError(
            "No edge lists found. Run the bibliometric pipeline/visualization export first, "
            f"or pass --edges explicitly. Looked for: {describe_edge_search_paths()}"
        )
    top_n = args.top_n
    if auto_edges and top_n is None:
        top_n = DEFAULT_AUTO_TOP_N

    if args.overwrite_generic_output:
        output_png = args.png or DEFAULT_PNG
    else:
        output_stem = infer_output_stem(edge_paths)
        output_png = args.png or (VISUALS_DIR / f"{output_stem}_network.png")

    png, graph = save_network_visualization(
        edge_paths=edge_paths,
        attributes_path=args.attributes,
        intervention_attributes_path=args.interventions,
        output_png=output_png,
        min_degree=args.min_degree,
        top_n=top_n,
        title=args.title,
        color_by_year=args.color_by_year,
    )
    if edge_paths:
        print("Using edge list(s):")
        for path in edge_paths:
            print(f"  {path}")
    if auto_edges and args.top_n is None:
        print(f"Auto-detected run limited to top {DEFAULT_AUTO_TOP_N} nodes by degree. Use --top-n to change this.")
    print(f"Saved static network: {png}")
    print(f"Visualized {graph.number_of_nodes():,} nodes and {graph.number_of_edges():,} edges.")


if __name__ == "__main__":
    main()
