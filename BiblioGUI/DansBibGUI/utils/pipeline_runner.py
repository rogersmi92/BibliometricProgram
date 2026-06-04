from __future__ import annotations

import ast
import csv
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from .app_config import AppConfig
from .file_utils import ensure_folder

LogCallback = Optional[Callable[[str], None]]
SCALING_MODES = ("small", "medium", "large", "extreme")
RECOGNIZED_SOURCES = ("openalex", "pubmed", "scopus", "wos", "covidence")
CONCEPT_PROFILES = ("telehealth_cancer_treatment",)
DERIVED_MARKERS = ("h_index", "year_count", "edge", "rank")
LIVE_API_SOURCES = ("openalex", "pubmed", "scopus", "wos")


@dataclass(frozen=True)
class PipelineRequest:
    query: str
    sources: list[str]
    filters: dict[str, bool | None]
    ris_files: list[Path]
    include_ris: bool
    ris_as_covidence: bool
    scaling_mode: str
    slug: str = ""
    start_year: int | None = None
    end_year: int | None = None
    concept_profile: str = ""
    qa_only: bool = False
    enforce_concept_blocks: bool = False
    rebuild_geo_cache: bool = False
    rebuild_demographic_cache: bool = False
    extract_geography: bool = False
    extract_demographics: bool = False
    dry_run: bool = False
    safe_local_test: bool = False
    log_path: Path | None = None


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    data_dir: Path
    raw_dir: Path
    outputs_dir: Path
    visuals_dir: Path
    vos_dir: Path
    logs_dir: Path


def paths_from_config(settings: AppConfig) -> RuntimePaths:
    root = settings.resolved_dansbib_path()
    data_dir = root / "data"
    return RuntimePaths(
        root=root,
        data_dir=data_dir,
        raw_dir=settings.resolved_ris_input_folder(),
        outputs_dir=settings.resolved_output_folder(),
        visuals_dir=data_dir / "visuals",
        vos_dir=data_dir / "VOS",
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

    sources = _normalize_sources(request.sources, log_callback)
    if request.include_ris and (request.ris_as_covidence or not sources) and "covidence" not in sources:
        sources.append("covidence")
    if request.safe_local_test:
        sources = ["covidence"] if request.include_ris else []

    command = [_python_executable(settings), "main.py", _query_with_flags(request.query, request.filters)]
    if sources:
        command.extend(["--databases", ",".join(sources)])
    if request.include_ris:
        selected_ris = []
        for path in request.ris_files:
            if path.exists():
                selected_ris.append(str(path))
            else:
                _log(log_callback, log_path, f"Skipping missing RIS file: {path}")
        if selected_ris:
            command.extend(["--ris-files", ",".join(selected_ris)])
    else:
        command.append("--no-ris")
    if request.slug.strip():
        command.extend(["--slug", request.slug.strip()])
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

    env = _base_env()
    env["SCALING_MODE"] = request.scaling_mode if request.scaling_mode in SCALING_MODES else "medium"
    env["DANSBIB_RIS_RAW_DIR"] = str(runtime.raw_dir)
    env["DANSBIB_OUTPUT_DIR"] = str(runtime.outputs_dir)
    env.setdefault("MPLCONFIGDIR", str(runtime.visuals_dir / ".mplconfig"))

    _log(log_callback, log_path, f"DansBib root: {runtime.root}")
    _log(log_callback, log_path, f"Python executable: {command[0]}")
    _log(log_callback, log_path, f"Selected sources: {', '.join(sources) if sources else 'RIS/offline inputs only'}")
    _log(log_callback, log_path, f"Run log: {log_path}")
    default_outputs = runtime.root / "data" / "outputs"
    if runtime.outputs_dir != default_outputs:
        _log(
            log_callback,
            log_path,
            "Configured output folder differs from DansBib/data/outputs. Current DansBib pipeline versions may still write to their internal data/outputs folder.",
        )
    _log(log_callback, log_path, f"Running DansBib command: {_format_command(command)}")
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

    return {
        "total_records": metrics.get("total_publications", summary.get("rows", 0)),
        "total_citations": metrics.get("total_citations", 0),
        "h_index": metrics.get("h_index", 0),
        "duplicates_removed": metrics.get("duplicates_removed", _dedup_count_from_logs(output_files)),
        "qa_failures": metrics.get("qa_failures", 0),
        "networks_generated": metrics.get("networks_generated", ""),
        "summary": summary,
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
    command = [_python_executable(settings), "visualize_bibliometrics.py"]
    if core_dataset:
        command.extend(["--core", core_dataset])
    if query:
        command.extend(["--query", query])
    if skip_rxnorm:
        command.append("--skip-rxnorm")

    env = _base_env()
    env.setdefault("MPLCONFIGDIR", str(runtime.visuals_dir / ".mplconfig"))
    _log(log_callback, log_path, f"Running visualization command: {_format_command(command)}")
    _run_streamed(command, runtime, env=env, log_path=log_path, log_callback=log_callback)
    return {"output_files": [str(path) for path in [*_recent_files(runtime.visuals_dir, started_at), log_path]]}


def run_vos_networks(core_dataset: str | None, settings: AppConfig, log_callback: LogCallback = None) -> dict[str, object]:
    started_at = time.time()
    runtime = paths_from_config(settings)
    log_path = create_run_log(settings, "vos_networks")
    _preflight(settings, runtime, log_path, log_callback)
    command = [_python_executable(settings), "vos_network_converter.py"]
    if core_dataset:
        command.extend(["--core", core_dataset])

    env = _base_env()
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


def _preflight(settings: AppConfig, runtime: RuntimePaths, log_path: Path, log_callback: LogCallback = None) -> None:
    if not runtime.root.exists():
        raise FileNotFoundError(f"DansBib folder not found: {runtime.root}")
    main_path = runtime.root / "main.py"
    if not main_path.exists():
        raise FileNotFoundError(f"DansBib pipeline entrypoint not found: {main_path}")
    if not settings.resolved_pipeline_python():
        _log(log_callback, log_path, "DansBib venv python not found; using the current Python interpreter. Missing packages may cause import errors.")
    for folder in (runtime.raw_dir, runtime.outputs_dir, runtime.visuals_dir, runtime.vos_dir, runtime.logs_dir):
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


def _python_executable(settings: AppConfig) -> str:
    return str(settings.resolved_pipeline_python() or Path(sys.executable))


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return env


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
