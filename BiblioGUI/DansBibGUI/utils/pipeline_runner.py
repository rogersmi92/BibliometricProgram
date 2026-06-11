from __future__ import annotations

import ast
import csv
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from DansBib.pipeline_capabilities import (
    CONCEPT_PROFILES as PIPELINE_CONCEPT_PROFILES,
    LIVE_API_DATABASES,
    RIS_SOURCE_LABELS,
    SCALING_MODES,
    SUPPORTED_ANALYSIS_OPTIONS,
    SUPPORTED_MAP_OPTIONS,
    RisInput,
    normalize_ris_source,
)
from DansBib.utils.map_providers import build_world_adm0_geojson, report_missing_map_packages, world_adm0_geojson_path, world_adm0_index_path, world_adm0_report_json_path, world_adm0_report_txt_path

from .app_config import AppConfig
from .app_config import get_secret
from .file_utils import ensure_folder

LogCallback = Optional[Callable[[str], None]]
RECOGNIZED_SOURCES = LIVE_API_DATABASES
CONCEPT_PROFILES = tuple(sorted(PIPELINE_CONCEPT_PROFILES))
DERIVED_MARKERS = ("h_index", "year_count", "edge", "rank")
LIVE_API_SOURCES = LIVE_API_DATABASES
ANALYSIS_OPTIONS = SUPPORTED_ANALYSIS_OPTIONS
MAP_OPTIONS = SUPPORTED_MAP_OPTIONS
GENERATED_RUN_SUFFIXES = (
    "_year_limited_records",
    "_target_year_range",
    "_raw",
    "_cleaned",
    "_source_records",
    "_all_records",
    "_main_results",
    "_results",
    "_publication_years",
    "_year_counts",
    "_year_counts_target_year_range",
    "_country_year_counts",
    "_institution_year_counts",
    "_top_authors",
    "_top_papers",
    "_author_edges",
    "_institution_edges",
    "_author_matrix",
    "_institution_matrix",
    "_deduplication_log",
    "_missing_metadata_log",
    "_excluded_records",
    "_geographic_terms",
    "_geographic_term_counts",
    "_geographic_terms_raw",
    "_geographic_term_counts_raw",
    "_geographic_terms_cleaned",
    "_geographic_term_counts_cleaned",
    "_demographic_terms",
    "_demographic_term_counts",
    "_demographic_terms_raw",
    "_demographic_term_counts_raw",
    "_demographic_terms_cleaned",
    "_demographic_term_counts_cleaned",
    "_drug_terms",
    "_drug_term_counts",
    "_drug_terms_raw",
    "_drug_term_counts_raw",
    "_drug_terms_cleaned",
    "_drug_term_counts_cleaned",
    "_procedure_terms",
    "_procedure_term_counts",
    "_procedure_terms_raw",
    "_procedure_term_counts_raw",
    "_procedure_terms_cleaned",
    "_procedure_term_counts_cleaned",
    "_intervention_terms",
    "_intervention_term_counts",
    "_geography_country_summary",
    "_geography_mapping_audit",
    "_unmapped_geography_terms",
    "_ambiguous_geography_matches",
    "_term_extraction_summary",
    "_geocensus_qa_report",
    "_geocensus_cleanup_qa",
    "_geocensus_cleanup_qa_summary",
    "_geography_map_plan",
    "_geography_map_validation",
    "_map_availability",
    "_visuals_folder_validation",
    "_visualization_validation_report",
    "_gui_pipeline_warnings",
    "_keyword_network_all_keywords_nodes",
    "_keyword_network_all_keywords_edges",
    "_cleaned_keyword_counts",
    "_keyword_network_edges",
    "_keywords_filtered",
    "_keywords_all",
    "_keyword_network_filtered",
    "_keyword_network_all",
    "_network_drugs",
    "_keywords_drugs",
    "_keywords_procedures",
    "_keyword_network_drugs",
    "_keyword_network_procedures",
    "_geography_heatmap_world",
    "_geography_heatmap_us",
    "_geography_heatmap_texas",
    "_geography_heatmap_europe",
    "_geography_heatmap_east_asia",
    "_geography_heatmap_latin_america",
    "_geography_heatmap_africa",
    "_geography_heatmap_middle_east",
    "_geography_heatmap_north_america",
    "_geography_overview_world_regions",
    "_keyword_frequencies_filtered",
    "_keyword_frequencies_all",
    "_keyword_network",
    "_author_network",
    "_institution_network",
)
CLEANUP_FOLDERS = ("outputs_dir", "processed_dir", "visuals_dir", "vos_dir")


@dataclass(frozen=True)
class PipelineRequest:
    query: str
    sources: list[str]
    filters: dict[str, bool | None]
    ris_files: list[Path]
    include_ris: bool
    scaling_mode: str
    ris_inputs: list[RisInput] | None = None
    ris_only_mode: bool = False
    ris_as_covidence: bool = False
    slug: str = ""
    start_year: int | None = None
    end_year: int | None = None
    concept_profile: str = ""
    qa_only: bool = False
    enforce_concept_blocks: bool = False
    rebuild_geo_cache: bool = False
    rebuild_demographic_cache: bool = False
    extract_geography: bool = False
    extract_drugs: bool = False
    extract_procedures: bool = False
    extract_demographics: bool = False
    extract_keywords: bool = True
    enrich_institutions: bool = False
    enrichment_source: str = "all"
    scopus_api_key: str = ""
    scopus_inst_token: str = ""
    scopus_base_url: str = ""
    openalex_email: str = ""
    use_system_proxy: bool = True
    http_proxy: str = ""
    https_proxy: str = ""
    use_certifi_ca_bundle: bool = False
    generate_maps: bool = False
    generate_visuals: bool = False
    generate_vos_networks: bool = False
    validate_outputs: bool = True
    run_audit: bool = False
    map_world: bool = True
    map_us: bool = True
    map_priority_adm1: bool = True
    map_priority_adm2: bool = False
    map_city_points: bool = True
    map_institution_points: bool = True
    label_point_maps: bool = True
    label_top_points: int = 5
    build_world_adm0: bool = True
    check_map_status: bool = True
    clear_previous_outputs: bool = False
    clear_similar_runs: bool = False
    dry_run: bool = False
    safe_local_test: bool = False
    log_path: Path | None = None


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    data_dir: Path
    raw_dir: Path
    outputs_dir: Path
    processed_dir: Path
    visuals_dir: Path
    vos_dir: Path
    matplotlib_cache_dir: Path
    logs_dir: Path


def paths_from_config(settings: AppConfig) -> RuntimePaths:
    root = settings.resolved_dansbib_path()
    data_dir = root / "data"
    return RuntimePaths(
        root=root,
        data_dir=data_dir,
        raw_dir=settings.resolved_ris_input_folder(),
        outputs_dir=settings.resolved_output_folder(),
        processed_dir=data_dir / "processed",
        visuals_dir=data_dir / "visuals",
        vos_dir=data_dir / "VOS",
        matplotlib_cache_dir=data_dir / "cache" / "matplotlib",
        logs_dir=settings.resolved_logs_folder(),
    )


def list_ris_files(settings: AppConfig) -> list[Path]:
    raw_dir = settings.resolved_ris_input_folder()
    return sorted(raw_dir.glob("*.ris")) if raw_dir.exists() else []


def create_run_log(settings: AppConfig, prefix: str = "run") -> Path:
    logs_dir = ensure_folder(settings.resolved_logs_folder())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return logs_dir / f"{prefix}_{stamp}.log"


def run_pipeline(request: PipelineRequest, settings: AppConfig, log_callback: LogCallback = None) -> dict[str, object]:
    started_at = time.time()
    runtime = paths_from_config(settings)
    log_path = request.log_path or create_run_log(settings, "pipeline")
    _preflight(settings, runtime, log_path, log_callback)
    active_slug = _active_slug_for_request(request)
    if request.clear_previous_outputs:
        cleanup = cleanup_generated_output_folders(
            runtime,
            dry_run=request.dry_run,
            log_callback=log_callback,
            log_path=log_path,
        )
        total = int(cleanup.get("total_removed", 0))
        _log(log_callback, log_path, f"Folder-wide generated-output cleanup complete. Total removed: {total}")

    sources = _normalize_sources(request.sources, log_callback)
    if request.ris_only_mode or request.safe_local_test:
        sources = []

    command = [_python_executable(settings), "main.py", _query_with_flags(request.query, request.filters)]
    if sources:
        command.extend(["--databases", ",".join(sources)])
    elif request.ris_only_mode or request.safe_local_test:
        command.extend(["--databases", ""])
    if request.include_ris:
        selected_ris: list[RisInput] = []
        requested_ris_inputs = request.ris_inputs or [
            RisInput(path=path, source="covidence" if request.ris_as_covidence else "unknown")
            for path in request.ris_files
        ]
        for ris_input in requested_ris_inputs:
            path = Path(ris_input.path)
            if path.exists():
                selected_ris.append(RisInput(path=path, source=normalize_ris_source(ris_input.source)))
            else:
                _log(log_callback, log_path, f"Skipping missing RIS file: {path}")
        if selected_ris:
            command.extend(["--ris-files", ",".join(str(item.path) for item in selected_ris)])
            command.extend(["--ris-file-sources", ",".join(item.source for item in selected_ris)])
    else:
        command.append("--no-ris")
    if active_slug:
        command.extend(["--slug", active_slug])
    if request.start_year is not None:
        command.extend(["--start-year", str(request.start_year)])
    if request.end_year is not None:
        command.extend(["--end-year", str(request.end_year)])
    if request.concept_profile.strip():
        command.extend(["--concept-profile", request.concept_profile.strip()])
    if request.qa_only:
        command.append("--qa-only")
    if request.enforce_concept_blocks:
        command.append("--enforce-concept-blocks")
    if request.rebuild_geo_cache:
        command.append("--rebuild-geo-cache")
    if request.rebuild_demographic_cache:
        command.append("--rebuild-demographic-cache")
    if request.extract_geography:
        command.append("--extract-geography")
    if request.extract_demographics:
        command.append("--extract-demographics")
    if request.enrich_institutions:
        command.append("--enrich-institutions")
        source = request.enrichment_source if request.enrichment_source in {"scopus", "openalex", "all"} else "all"
        command.extend(["--enrichment-source", source])
        if request.openalex_email:
            command.extend(["--openalex-email", request.openalex_email])

    env = _base_env()
    env["SCALING_MODE"] = request.scaling_mode if request.scaling_mode in SCALING_MODES else "medium"
    env["DANSBIB_RIS_RAW_DIR"] = str(runtime.raw_dir)
    env["DANSBIB_OUTPUT_DIR"] = str(runtime.outputs_dir)
    env["MPLCONFIGDIR"] = str(runtime.matplotlib_cache_dir)
    _apply_enrichment_credentials(request, settings, env, log_callback, log_path)

    _log(log_callback, log_path, f"DansBib root: {runtime.root}")
    _log(log_callback, log_path, f"Python executable: {command[0]}")
    _log(log_callback, log_path, f"Selected sources: {', '.join(sources) if sources else 'RIS/offline inputs only'}")
    _log(log_callback, log_path, f"RIS-only mode: {'yes' if request.ris_only_mode else 'no'}")
    _log(log_callback, log_path, f"Institution enrichment requested: {'yes' if request.enrich_institutions else 'no'}")
    if request.ris_only_mode and request.enrich_institutions:
        _log(log_callback, log_path, "RIS-only mode enabled. Institution enrichment still allowed because --enrich-institutions is enabled.")
        _log(log_callback, log_path, "Enrichment allowed despite RIS-only mode: yes")
    elif request.ris_only_mode:
        _log(log_callback, log_path, "Enrichment allowed despite RIS-only mode: no")
        _log(log_callback, log_path, "Institution enrichment blocked reason: --enrich-institutions not enabled.")
    else:
        _log(log_callback, log_path, f"Enrichment allowed despite RIS-only mode: {'yes' if request.enrich_institutions else 'not applicable'}")
    if request.include_ris:
        _log(log_callback, log_path, f"RIS sources: {_format_ris_inputs(request.ris_inputs or [])}")
    _log(log_callback, log_path, f"Run log: {log_path}")
    default_outputs = runtime.root / "data" / "outputs"
    if runtime.outputs_dir != default_outputs:
        _log(
            log_callback,
            log_path,
            "Configured output folder differs from DansBib/data/outputs. Current DansBib pipeline versions may still write to their internal data/outputs folder.",
        )
    _log(log_callback, log_path, f"Running DansBib command: {_format_command(command)}")
    dry_run_steps = _planned_post_main_commands(request, runtime, settings, "<core_dataset>")
    if dry_run_steps:
        _log(log_callback, log_path, "Planned ordered pipeline sequence:")
        _log(log_callback, log_path, f"1. Main parsing/normalization: {_format_command(command)}")
        for idx, (label, planned_command) in enumerate(dry_run_steps, start=2):
            _log(log_callback, log_path, f"{idx}. {label}: {_format_command(planned_command)}")
    if request.dry_run:
        return {
            "dry_run": True,
            "summary": {"rows": 0, "message": "Dry run completed. No pipeline command was executed."},
            "output_paths": {
                "output_folder": str(runtime.outputs_dir),
                "visuals_folder": str(runtime.visuals_dir),
                "vos_folder": str(runtime.vos_dir),
                "log_file": str(log_path),
            },
            "output_files": [str(log_path)],
            "core_dataset": None,
            "total_records": 0,
            "total_citations": 0,
            "h_index": 0,
            "duplicates_removed": 0,
            "qa_failures": 0,
        }

    _log_step(log_callback, log_path, "Running main data collection / parsing / normalization")
    completed_lines = _run_streamed(command, runtime, env=env, log_path=log_path, log_callback=log_callback)
    metrics = _parse_metrics(completed_lines)
    output_paths = dict(metrics.get("output_paths", {})) if isinstance(metrics.get("output_paths"), dict) else {}
    output_files = _collect_output_files(started_at, output_paths, runtime)
    output_files.append(log_path)
    core_dataset = output_paths.get("year_limited_records") or output_paths.get("main_results") or _detect_core_dataset(runtime.outputs_dir)
    if core_dataset:
        _log(log_callback, log_path, f"Core dataset detected: {core_dataset}")
    else:
        _log(log_callback, log_path, "No core dataset detected. TODO: provide a generated CSV with title, authors, and year columns.")
    summary = _summarize_core_dataset(core_dataset)
    warnings = _run_post_main_sequence(request, runtime, settings, log_path, log_callback, core_dataset)
    output_files = _collect_output_files(started_at, output_paths, runtime)
    output_files.append(log_path)

    return {
        "total_records": metrics.get("total_publications", summary.get("rows", 0)),
        "total_citations": metrics.get("total_citations", 0),
        "h_index": metrics.get("h_index", 0),
        "duplicates_removed": metrics.get("duplicates_removed", _dedup_count_from_logs(output_files)),
        "qa_failures": metrics.get("qa_failures", 0),
        "networks_generated": metrics.get("networks_generated", ""),
        "source_status": metrics.get("source_status", {}),
        "summary": {**summary, "warnings": warnings, "source_status": metrics.get("source_status", {})},
        "warnings": warnings,
        "output_paths": {
            **output_paths,
            "output_folder": str(runtime.outputs_dir),
            "visuals_folder": str(runtime.visuals_dir),
            "vos_folder": str(runtime.vos_dir),
            "log_file": str(log_path),
        },
        "output_files": [str(path) for path in output_files],
        "core_dataset": core_dataset,
    }


def run_visualizations(core_dataset: str | None, query: str, skip_rxnorm: bool, settings: AppConfig, log_callback: LogCallback = None) -> dict[str, object]:
    started_at = time.time()
    runtime = paths_from_config(settings)
    log_path = create_run_log(settings, "visualizations")
    _preflight(settings, runtime, log_path, log_callback)
    if not core_dataset:
        raise ValueError("Visualization generation requires an explicit current core dataset. No fallback dataset will be auto-selected.")
    core_path = Path(core_dataset)
    if not core_path.exists():
        raise FileNotFoundError(f"Core dataset not found: {core_path}")
    command = [_python_executable(settings), "visualize_bibliometrics.py"]
    command.extend(["--core", str(core_path), "--slug", _slug_from_core_dataset(str(core_path))])
    if query:
        command.extend(["--query", query])
    if skip_rxnorm:
        command.append("--skip-rxnorm")

    env = _base_env()
    env["MPLCONFIGDIR"] = str(runtime.matplotlib_cache_dir)
    _log(log_callback, log_path, f"Running visualization command: {_format_command(command)}")
    _run_streamed(command, runtime, env=env, log_path=log_path, log_callback=log_callback)
    return {"output_files": [str(path) for path in [*_recent_files(runtime.visuals_dir, started_at), log_path]]}


def run_vos_networks(core_dataset: str | None, settings: AppConfig, log_callback: LogCallback = None) -> dict[str, object]:
    started_at = time.time()
    runtime = paths_from_config(settings)
    log_path = create_run_log(settings, "vos_networks")
    _preflight(settings, runtime, log_path, log_callback)
    if not core_dataset:
        raise ValueError("VOS network generation requires an explicit current core dataset. No fallback dataset will be auto-selected.")
    core_path = Path(core_dataset)
    if not core_path.exists():
        raise FileNotFoundError(f"Core dataset not found: {core_path}")
    slug = _slug_from_core_dataset(str(core_path))
    command = [_python_executable(settings), "vos_network_converter.py"]
    command.extend(["--core", str(core_path)])

    env = _base_env()
    _log(log_callback, log_path, f"VOS core dataset: {core_path}")
    _log(log_callback, log_path, f"VOS output slug: {slug}")
    _log(log_callback, log_path, f"Running VOS network command: {_format_command(command)}")
    _run_streamed(command, runtime, env=env, log_path=log_path, log_callback=log_callback)
    return {"output_files": [str(path) for path in [*_recent_files(runtime.vos_dir, started_at), log_path]]}


def run_vos_validator(csv_path: str, settings: AppConfig, log_callback: LogCallback = None) -> dict[str, object]:
    started_at = time.time()
    runtime = paths_from_config(settings)
    log_path = create_run_log(settings, "vos_txt")
    _preflight(settings, runtime, log_path, log_callback)
    if not Path(csv_path).exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    command = [_python_executable(settings), "vos_validator.py", csv_path]
    env = _base_env()
    _log(log_callback, log_path, f"Running VOS TXT converter command: {_format_command(command)}")
    _run_streamed(command, runtime, env=env, log_path=log_path, log_callback=log_callback)
    return {"output_files": [str(path) for path in [*_recent_files(runtime.vos_dir, started_at), log_path]]}


def check_map_data_status(settings: AppConfig, log_callback: LogCallback = None) -> dict[str, object]:
    runtime = paths_from_config(settings)
    log_path = create_run_log(settings, "map_status")
    _preflight(settings, runtime, log_path, log_callback)
    warnings = _log_map_status(log_callback, log_path)
    return {
        "summary": {"warnings": warnings, "message": "Map data status check completed."},
        "warnings": warnings,
        "output_files": [str(log_path)],
        "output_paths": {
            "processed_folder": str(runtime.processed_dir),
            "visuals_folder": str(runtime.visuals_dir),
            "log_file": str(log_path),
        },
    }


def cleanup_previous_run_files(
    slug: str,
    runtime: RuntimePaths,
    include_similar: bool = False,
    dry_run: bool = False,
    log_callback: LogCallback = None,
    log_path: Path | None = None,
) -> dict[str, object]:
    if not slug:
        message = "Cleanup skipped: active slug could not be derived."
        _log(log_callback, log_path, message)
        return {"deleted": [], "preview": [], "message": message}
    matches = discover_previous_run_files(slug, runtime, include_similar=include_similar)
    deleted: list[str] = []
    for path in matches:
        if dry_run:
            _log(log_callback, log_path, f"Cleanup preview: {path}")
            continue
        try:
            path.unlink(missing_ok=True)
            deleted.append(str(path))
            _log(log_callback, log_path, f"Deleted previous run file: {path}")
        except FileNotFoundError:
            continue
        except OSError as exc:
            _log(log_callback, log_path, f"WARNING: could not delete {path}: {exc}")
    if matches:
        action = "Would clear" if dry_run else "Cleared"
        message = f"{action} {len(matches)} previous files for slug: {slug}"
    else:
        message = f"No previous generated files found for slug: {slug}"
    _log(log_callback, log_path, message)
    return {"deleted": deleted, "preview": [str(path) for path in matches], "message": message}


def cleanup_generated_output_folders(
    runtime: RuntimePaths,
    dry_run: bool = False,
    log_callback: LogCallback = None,
    log_path: Path | None = None,
) -> dict[str, object]:
    roots = {
        "data/visuals": runtime.visuals_dir,
        "data/outputs": runtime.outputs_dir,
        "data/processed": runtime.processed_dir,
        "data/VOS": runtime.vos_dir,
    }
    deleted_by_folder: dict[str, list[str]] = {label: [] for label in roots}
    skipped_by_folder: dict[str, list[str]] = {label: [] for label in roots}
    mode = "folder-wide generated-output cleanup"
    _log(log_callback, log_path, f"Cleanup mode: {mode}")
    _log(log_callback, log_path, "Folders checked:")
    for label, root in roots.items():
        _log(log_callback, log_path, f"- {label}: {root}")
        if not root.exists() or not _cleanup_root_allowed(root, runtime):
            skipped_by_folder[label].append(f"{root}: folder missing or not approved")
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if not _folder_cleanup_file_allowed(path, runtime):
                skipped_by_folder[label].append(str(path))
                continue
            if dry_run:
                deleted_by_folder[label].append(str(path))
                _log(log_callback, log_path, f"Cleanup preview: {path}")
                continue
            try:
                path.unlink(missing_ok=True)
                deleted_by_folder[label].append(str(path))
                _log(log_callback, log_path, f"Deleted generated file: {path}")
            except FileNotFoundError:
                continue
            except OSError as exc:
                skipped_by_folder[label].append(f"{path}: {exc}")
                _log(log_callback, log_path, f"WARNING: could not delete {path}: {exc}")

    counts_by_folder = {label: len(paths) for label, paths in deleted_by_folder.items()}
    skipped_counts_by_folder = {label: len(paths) for label, paths in skipped_by_folder.items()}
    total_removed = sum(counts_by_folder.values())
    _log(log_callback, log_path, "Files removed per folder:")
    for label, count in counts_by_folder.items():
        _log(log_callback, log_path, f"- {label}: {count}")
    protected_skips = sum(skipped_counts_by_folder.values())
    if protected_skips:
        _log(log_callback, log_path, "Skipped protected files:")
        for label, paths in skipped_by_folder.items():
            for path in paths:
                _log(log_callback, log_path, f"- {label}: {path}")
    _log(log_callback, log_path, f"Total removed: {total_removed}")
    return {
        "mode": mode,
        "deleted_by_folder": deleted_by_folder,
        "skipped_by_folder": skipped_by_folder,
        "counts_by_folder": counts_by_folder,
        "skipped_counts_by_folder": skipped_counts_by_folder,
        "total_removed": total_removed,
    }


def discover_previous_run_files(slug: str, runtime: RuntimePaths, include_similar: bool = False) -> list[Path]:
    roots = [getattr(runtime, name) for name in CLEANUP_FOLDERS]
    cache_runs = runtime.data_dir / "cache" / "runs"
    if cache_runs.exists():
        roots.append(cache_runs)
    matches: list[Path] = []
    for root in roots:
        if not root.exists() or not _cleanup_root_allowed(root, runtime):
            continue
        for path in root.rglob("*"):
            if not path.is_file() or not _cleanup_file_allowed(path, runtime):
                continue
            if generated_file_matches_slug(path, slug, include_similar=include_similar):
                matches.append(path)
    return sorted(set(matches))


def generated_file_matches_slug(path: Path, slug: str, include_similar: bool = False) -> bool:
    stem = path.stem
    if stem == slug:
        return True
    if any(stem == f"{slug}{suffix}" or stem.startswith(f"{slug}{suffix}_") for suffix in GENERATED_RUN_SUFFIXES):
        return True
    if stem.startswith(f"{slug}_geography_heatmap_country_"):
        return True
    if include_similar and stem.startswith(f"{slug}_"):
        return True
    return False


def _cleanup_root_allowed(root: Path, runtime: RuntimePaths) -> bool:
    allowed = {
        runtime.outputs_dir.resolve(),
        runtime.processed_dir.resolve(),
        runtime.visuals_dir.resolve(),
        runtime.vos_dir.resolve(),
        (runtime.data_dir / "cache" / "runs").resolve(),
    }
    try:
        resolved = root.resolve()
    except OSError:
        return False
    return resolved in allowed


def _cleanup_file_allowed(path: Path, runtime: RuntimePaths) -> bool:
    protected_parts = {".git", "venv", ".venv", "reference", "maps", "tests"}
    protected_names = {
        "institution_geocache.csv",
        "institution_aliases.csv",
        "institution_overrides.csv",
        "README.md",
    }
    if protected_parts & set(path.parts):
        return False
    if path.name in protected_names or path.suffix in {".py", ".md"}:
        return False
    return any(_is_under(path, root) for root in (runtime.outputs_dir, runtime.processed_dir, runtime.visuals_dir, runtime.vos_dir, runtime.data_dir / "cache" / "runs"))


def _folder_cleanup_file_allowed(path: Path, runtime: RuntimePaths) -> bool:
    protected_suffixes = {".py", ".pyc", ".md", ".toml", ".ini", ".cfg", ".yaml", ".yml"}
    protected_names = {".env", "config.json", "settings.json", "README.md"}
    if path.name in protected_names or path.suffix.lower() in protected_suffixes:
        return False
    approved_roots = (runtime.outputs_dir, runtime.processed_dir, runtime.visuals_dir, runtime.vos_dir)
    return _cleanup_file_allowed(path, runtime) and any(_is_under(path, root) for root in approved_roots)


def _is_under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False


def _planned_post_main_commands(request: PipelineRequest, runtime: RuntimePaths, settings: AppConfig, core_dataset: str) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    if _needs_geocensus(request):
        commands.append(("GeoCensus / term extraction", _geocensus_command(request, runtime, settings, core_dataset)))
    if request.build_world_adm0 and (request.generate_maps or request.map_world):
        commands.append(("Build WORLD_ADM0.geojson if needed", [_python_executable(settings), "-c", "from utils.map_providers import build_world_adm0_geojson; warnings=[]; path=build_world_adm0_geojson(warnings); print(path or '; '.join(warnings))"]))
    if request.generate_visuals or request.generate_maps or request.extract_keywords:
        command = [_python_executable(settings), "visualize_bibliometrics.py"]
        if core_dataset:
            command.extend(["--core", core_dataset])
            command.extend(["--slug", request.slug.strip() or _slug_from_core_dataset(core_dataset)])
        if request.query:
            command.extend(["--query", request.query])
        if request.map_city_points:
            command.append("--include-point-maps")
        if request.label_point_maps:
            command.extend(["--label-top-cities", str(max(1, int(request.label_top_points)))])
            command.extend(["--label-top-institutions", str(max(1, int(request.label_top_points)))])
        else:
            command.extend(["--no-city-labels", "--no-institution-labels"])
        commands.append(("Generate maps / standard bibliometric visuals / keywords", command))
    if request.generate_vos_networks:
        command = [_python_executable(settings), "vos_network_converter.py"]
        if core_dataset:
            command.extend(["--core", core_dataset])
        commands.append(("Generate VOS/network visuals", command))
    if request.validate_outputs:
        commands.append(("Validate PNG-only visuals", [_python_executable(settings), "validate_visuals_png_only.py"]))
    if request.run_audit:
        commands.append(("Repository audit", [_python_executable(settings), "../scripts/audit_repo.py"]))
    return commands


def _needs_geocensus(request: PipelineRequest) -> bool:
    return True


def _geocensus_command(request: PipelineRequest, runtime: RuntimePaths, settings: AppConfig, core_dataset: str) -> list[str]:
    slug = request.slug.strip() or _slug_from_core_dataset(core_dataset) or "results"
    command = [
        _python_executable(settings),
        "GeoCensus.py",
        "--slug",
        slug,
        "--input",
        core_dataset,
        "--output-dir",
        str(runtime.outputs_dir),
        "--qa-dir",
        str(runtime.processed_dir),
        "--progress",
        "--heartbeat-seconds",
        "20",
    ]
    command.append("--all")
    command.extend(["--run-geography", "--run-demographics", "--run-drugs", "--run-procedures"])
    command.extend(["--geo-scope", "global"])
    if request.rebuild_geo_cache:
        command.append("--rebuild-geo-cache")
    if request.rebuild_demographic_cache:
        command.append("--rebuild-demographic-cache")
    return command


def _run_post_main_sequence(
    request: PipelineRequest,
    runtime: RuntimePaths,
    settings: AppConfig,
    log_path: Path,
    log_callback: LogCallback,
    core_dataset: str | None,
) -> list[str]:
    warnings: list[str] = []
    env = _base_env()
    env["MPLCONFIGDIR"] = str(runtime.matplotlib_cache_dir)

    if request.check_map_status and (request.generate_maps or request.map_world):
        warnings.extend(_log_map_status(log_callback, log_path))

    if request.generate_maps and request.extract_geography is False and not _geography_outputs_exist(runtime, request):
        warning = "Generate maps requested but GeoCensus geography outputs are missing and geography extraction is not selected."
        warnings.append(warning)
        _log(log_callback, log_path, f"WARNING: {warning}")

    if core_dataset and _needs_geocensus(request):
        _log_step(log_callback, log_path, "Running GeoCensus: selected term extraction")
        _run_streamed(_geocensus_command(request, runtime, settings, core_dataset), runtime, env=env, log_path=log_path, log_callback=log_callback)
        warnings.extend(_check_requested_term_outputs(runtime, request))
    elif _needs_geocensus(request):
        warning = "GeoCensus requested but no core dataset was detected."
        warnings.append(warning)
        _log(log_callback, log_path, f"WARNING: {warning}")

    if request.build_world_adm0 and (request.generate_maps or request.map_world) and not world_adm0_geojson_path().exists():
        _log_step(log_callback, log_path, "Building world ADM0 map")
        build_warnings: list[str] = []
        built = build_world_adm0_geojson(build_warnings)
        warnings.extend(build_warnings)
        if built:
            _log(log_callback, log_path, f"WORLD_ADM0.geojson ready: {built}")
        else:
            _log(log_callback, log_path, "WARNING: WORLD_ADM0.geojson missing and could not be built from WORLD_ADM0_INDEX.json.")
        _log_world_adm0_report(log_callback, log_path)

    if core_dataset and (request.generate_visuals or request.generate_maps or request.extract_keywords):
        _log_step(log_callback, log_path, "Generating standard bibliometric visuals and maps")
        slug = request.slug.strip() or _slug_from_core_dataset(core_dataset)
        visual_command = [_python_executable(settings), "visualize_bibliometrics.py", "--core", core_dataset, "--slug", slug]
        if request.query:
            visual_command.extend(["--query", request.query])
        if request.map_city_points:
            visual_command.append("--include-point-maps")
        if request.label_point_maps:
            visual_command.extend(["--label-top-cities", str(max(1, int(request.label_top_points)))])
            visual_command.extend(["--label-top-institutions", str(max(1, int(request.label_top_points)))])
        else:
            visual_command.extend(["--no-city-labels", "--no-institution-labels"])
        _run_streamed(visual_command, runtime, env=env, log_path=log_path, log_callback=log_callback)
    elif request.generate_visuals or request.generate_maps or request.extract_keywords:
        warning = "Visualization generation requested but no core dataset was detected."
        warnings.append(warning)
        _log(log_callback, log_path, f"WARNING: {warning}")

    if core_dataset and request.generate_vos_networks:
        _log_step(log_callback, log_path, "Generating VOS/network visuals")
        _run_streamed([_python_executable(settings), "vos_network_converter.py", "--core", core_dataset], runtime, env=env, log_path=log_path, log_callback=log_callback)

    if request.validate_outputs:
        _log_step(log_callback, log_path, "Running visual/output validation")
        _run_streamed([_python_executable(settings), "validate_visuals_png_only.py"], runtime, env=env, log_path=log_path, log_callback=log_callback)

    if request.run_audit:
        _log_step(log_callback, log_path, "Running repository audit")
        _run_streamed([_python_executable(settings), "../scripts/audit_repo.py"], runtime, env=env, log_path=log_path, log_callback=log_callback)

    if warnings:
        _write_gui_warning_report(runtime, request, warnings)
    return warnings


def _log_map_status(log_callback: LogCallback, log_path: Path) -> list[str]:
    warnings: list[str] = []
    _log_step(log_callback, log_path, "Checking map data status")
    for status in report_missing_map_packages():
        prefix = "OK" if status.present else "WARNING"
        _log(log_callback, log_path, f"{prefix}: {status.label}: {status.message}")
        if not status.present:
            warnings.append(status.message)
    _log(log_callback, log_path, f"WORLD_ADM0_INDEX.json: {world_adm0_index_path()}")
    _log(log_callback, log_path, f"WORLD_ADM0.geojson: {world_adm0_geojson_path()}")
    return warnings


def _log_world_adm0_report(log_callback: LogCallback, log_path: Path) -> None:
    report_path = world_adm0_report_json_path()
    _log(log_callback, log_path, f"WORLD_ADM0 build report: {world_adm0_report_txt_path()}")
    if not report_path.exists():
        _log(log_callback, log_path, "WARNING: WORLD_ADM0 JSON build report was not created.")
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log(log_callback, log_path, f"WARNING: Could not read WORLD_ADM0 JSON build report: {exc}")
        return
    _log(log_callback, log_path, f"WORLD_ADM0 records read: {report.get('record_count', 0)}")
    _log(log_callback, log_path, f"WORLD_ADM0 URLs found: {report.get('url_count', 0)}")
    _log(log_callback, log_path, f"WORLD_ADM0 countries loaded: {report.get('success_count', 0)}")
    _log(log_callback, log_path, f"WORLD_ADM0 failures: {report.get('failure_count', 0)}")
    failures = report.get("failures") if isinstance(report.get("failures"), list) else []
    for failure in failures[:3]:
        if isinstance(failure, dict):
            _log(
                log_callback,
                log_path,
                "WORLD_ADM0 failure: "
                f"{failure.get('boundaryISO', 'unknown')} {failure.get('boundaryName', '')}: "
                f"{failure.get('reason', '')} {failure.get('exception_type', '')} {failure.get('exception_message', '')}",
            )


def _geography_outputs_exist(runtime: RuntimePaths, request: PipelineRequest) -> bool:
    slug = request.slug.strip()
    patterns = [f"{slug}_geographic_term_counts*.csv"] if slug else ["*_geographic_term_counts*.csv"]
    return any(runtime.outputs_dir.glob(pattern) for pattern in patterns)


def _check_requested_term_outputs(runtime: RuntimePaths, request: PipelineRequest) -> list[str]:
    slug = request.slug.strip() or "*"
    checks = []
    if request.extract_geography:
        checks.append(("GeoCensus geography output missing", f"{slug}_geographic_term_counts*.csv"))
    if request.extract_drugs:
        checks.append(("Drug term files missing", f"{slug}_drug_term_counts*.csv"))
    if request.extract_procedures:
        checks.append(("Procedure term files missing", f"{slug}_procedure_term_counts*.csv"))
    if request.extract_demographics:
        checks.append(("Demographic term files missing", f"{slug}_demographic_term_counts*.csv"))
    warnings = []
    for message, pattern in checks:
        if not any(runtime.outputs_dir.glob(pattern)):
            warnings.append(message)
    return warnings


def _write_gui_warning_report(runtime: RuntimePaths, request: PipelineRequest, warnings: list[str]) -> Path:
    runtime.processed_dir.mkdir(parents=True, exist_ok=True)
    slug = request.slug.strip() or "gui_pipeline"
    path = runtime.processed_dir / f"{slug}_gui_pipeline_warnings.txt"
    path.write_text("\n".join(["GUI pipeline warnings:", *[f"- {warning}" for warning in warnings]]) + "\n", encoding="utf-8")
    return path


def _active_slug_for_request(request: PipelineRequest) -> str:
    if request.slug.strip():
        return request.slug.strip()
    query = request.query.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", query).strip("_")
    return (slug[:80] or "results").rstrip("_")


def _slug_from_core_dataset(core_dataset: str | None) -> str:
    if not core_dataset:
        return ""
    stem = Path(core_dataset).stem
    for suffix in ("_year_limited_records", "_target_year_range", "_raw", "_cleaned"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _log_step(log_callback: LogCallback, log_path: Path | None, label: str) -> None:
    _log(log_callback, log_path, f"PIPELINE_STEP: {label}")


def _preflight(settings: AppConfig, runtime: RuntimePaths, log_path: Path, log_callback: LogCallback = None) -> None:
    if not runtime.root.exists():
        raise FileNotFoundError(f"DansBib folder not found: {runtime.root}")
    main_path = runtime.root / "main.py"
    if not main_path.exists():
        raise FileNotFoundError(f"DansBib pipeline entrypoint not found: {main_path}")
    if not settings.resolved_pipeline_python():
        _log(log_callback, log_path, "DansBib venv python not found; using the current Python interpreter. Missing packages may cause import errors.")
    for folder in (runtime.raw_dir, runtime.outputs_dir, runtime.processed_dir, runtime.visuals_dir, runtime.vos_dir, runtime.matplotlib_cache_dir, runtime.logs_dir):
        folder.mkdir(parents=True, exist_ok=True)


def _normalize_sources(sources: list[str], log_callback: LogCallback = None) -> list[str]:
    normalized: list[str] = []
    for source in sources:
        source_key = source.lower().strip()
        if not source_key:
            continue
        if source_key not in RECOGNIZED_SOURCES:
            if log_callback:
                log_callback(f"Skipping unrecognized source '{source}'. Recognized sources: {', '.join(RECOGNIZED_SOURCES)}")
            continue
        normalized.append(source_key)
    return list(dict.fromkeys(normalized))


def _format_ris_inputs(ris_inputs: list[RisInput]) -> str:
    if not ris_inputs:
        return "none"
    return ", ".join(f"{Path(item.path).name}:{normalize_ris_source(item.source)}" for item in ris_inputs)


def _python_executable(settings: AppConfig) -> str:
    return str(settings.resolved_pipeline_python() or Path(sys.executable))


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _apply_enrichment_credentials(
    request: PipelineRequest,
    settings: AppConfig,
    env: dict[str, str],
    log_callback: LogCallback,
    log_path: Path,
) -> None:
    if not request.enrich_institutions:
        return
    scopus_key = request.scopus_api_key.strip()
    scopus_key_source = "GUI request"
    if not scopus_key:
        scopus_key, scopus_key_source = get_secret("scopus_api_key")
    scopus_token = request.scopus_inst_token.strip()
    scopus_token_source = "GUI request"
    if not scopus_token:
        scopus_token, scopus_token_source = get_secret("scopus_inst_token")
    openalex_email = (request.openalex_email or settings.openalex_email or os.getenv("OPENALEX_EMAIL", "")).strip()
    if scopus_key:
        env["SCOPUS_API_KEY"] = scopus_key
        _log(log_callback, log_path, f"Scopus API key loaded from {scopus_key_source}.")
    else:
        _log(log_callback, log_path, "Scopus API key not configured.")
    if scopus_token:
        env["SCOPUS_INST_TOKEN"] = scopus_token
        _log(log_callback, log_path, f"Scopus InstToken loaded from {scopus_token_source}.")
    if request.scopus_base_url or settings.scopus_base_url:
        env["SCOPUS_BASE_URL"] = (request.scopus_base_url or settings.scopus_base_url).rstrip("/")
    if not (request.use_system_proxy and settings.use_system_proxy):
        for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
            env.pop(proxy_name, None)
        _log(log_callback, log_path, "Use system proxy settings: no")
    else:
        _log(log_callback, log_path, "Use system proxy settings: yes")
    if request.http_proxy or settings.http_proxy:
        env["HTTP_PROXY"] = request.http_proxy or settings.http_proxy
        env["http_proxy"] = request.http_proxy or settings.http_proxy
        _log(log_callback, log_path, "HTTP proxy configured for pipeline requests: yes")
    if request.https_proxy or settings.https_proxy:
        env["HTTPS_PROXY"] = request.https_proxy or settings.https_proxy
        env["https_proxy"] = request.https_proxy or settings.https_proxy
        _log(log_callback, log_path, "HTTPS proxy configured for pipeline requests: yes")
    if request.use_certifi_ca_bundle or settings.use_certifi_ca_bundle:
        env["DANSBIB_USE_CERTIFI"] = "1"
        _log(log_callback, log_path, "Use certifi CA bundle for pipeline requests: yes")
    else:
        env["DANSBIB_USE_CERTIFI"] = "0"
        _log(log_callback, log_path, "Use certifi CA bundle for pipeline requests: no")
    if openalex_email:
        env["OPENALEX_EMAIL"] = openalex_email
        _log(log_callback, log_path, "OpenAlex email configured: yes.")
    else:
        _log(log_callback, log_path, "OpenAlex email configured: no.")


def _query_with_flags(query: str, filters: dict[str, bool | None]) -> str:
    tokens = [query.strip()]
    flag_map = {
        "review": (True, "-revart", False, "-xrevart"),
        "early_access": (True, "-early", False, "-xearly"),
        "open_access": (True, "-oa", False, "-xoa"),
    }
    for key, (true_value, true_flag, false_value, false_flag) in flag_map.items():
        value = filters.get(key)
        if value is true_value:
            tokens.append(true_flag)
        elif value is false_value:
            tokens.append(false_flag)
    return " ".join(token for token in tokens if token)


def _run_streamed(command: list[str], runtime: RuntimePaths, env: dict[str, str], log_path: Path, log_callback: LogCallback) -> list[str]:
    try:
        process = subprocess.Popen(
            command,
            cwd=runtime.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Could not start DansBib command. Missing executable: {command[0]}") from exc

    lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        lines.append(line)
        if line:
            _log(log_callback, log_path, line)

    return_code = process.wait()
    if return_code != 0:
        hint = _failure_hint(lines)
        detail = f" {hint}" if hint else ""
        raise RuntimeError(f"DansBib command failed with exit code {return_code}.{detail} See log for traceback/details.")
    return lines


def _parse_metrics(lines: Iterable[str]) -> dict[str, object]:
    for line in reversed(list(lines)):
        stripped = line.strip()
        if not stripped.startswith("{") or "total_publications" not in stripped:
            continue
        try:
            parsed = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _recent_files(folder: Path, started_at: float) -> list[Path]:
    if not folder.exists():
        return []
    files = [path for path in folder.rglob("*") if path.is_file() and path.stat().st_mtime >= started_at - 1]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def _collect_output_files(started_at: float, output_paths: dict[str, object], runtime: RuntimePaths) -> list[Path]:
    files: list[Path] = []
    for value in output_paths.values():
        for raw_path in str(value or "").split(";"):
            path = Path(raw_path)
            if path.is_file():
                files.append(path)
    for folder in (runtime.outputs_dir, runtime.visuals_dir, runtime.vos_dir):
        files.extend(_recent_files(folder, started_at))
    return sorted(set(files), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)


def _detect_core_dataset(outputs_dir: Path) -> str | None:
    if not outputs_dir.exists():
        return None

    candidates: list[tuple[Path, int]] = []
    for path in sorted(outputs_dir.glob("*.csv")):
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                reader = csv.reader(handle)
                header = [column.strip().lower() for column in next(reader, [])]
        except OSError:
            continue
        if not {"title", "authors", "year"}.issubset(set(header)):
            continue
        if any(any(marker in column for marker in DERIVED_MARKERS) for column in header):
            continue
        candidates.append((path, path.stat().st_mtime_ns))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)
    return str(candidates[0][0])


def _summarize_core_dataset(core_dataset: str | None) -> dict[str, object]:
    if not core_dataset:
        return {}
    path = Path(core_dataset)
    if not path.exists():
        return {"path": str(path), "error": "file does not exist"}
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            rows = sum(1 for _ in reader)
    except OSError as exc:
        return {"path": str(path), "error": str(exc)}
    return {"path": str(path), "rows": rows, "columns": len(header), "column_names": header[:12]}


def _dedup_count_from_logs(files: list[Path]) -> int:
    for path in files:
        if path.name.endswith("deduplication_log.csv"):
            try:
                with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                    row = next(csv.DictReader(handle), {})
                return int(row.get("duplicates_removed") or 0)
            except (OSError, ValueError):
                return 0
    return 0


def _failure_hint(lines: list[str]) -> str:
    text = "\n".join(lines[-40:])
    if "ModuleNotFoundError" in text or "ImportError" in text:
        return "A Python dependency appears to be missing; install DansBib requirements in the interpreter shown above."
    if "ConnectionError" in text or "Read timed out" in text or "Max retries exceeded" in text:
        return "A network/source request failed."
    if "403" in text or "forbidden" in text.lower():
        return "A source rejected access, commonly due to API key or network restrictions."
    return ""


def _format_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def _log(log_callback: LogCallback, log_path: Path | None, message: str) -> None:
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"{message}\n")
    if log_callback:
        log_callback(message)
