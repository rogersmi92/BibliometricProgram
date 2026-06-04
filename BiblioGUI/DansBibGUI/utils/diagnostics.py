from __future__ import annotations

import importlib.util
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from .app_config import APP_VERSION, AppConfig, config_path


REQUIRED_PIPELINE_PACKAGES = (
    "pandas",
    "requests",
)

OPTIONAL_PIPELINE_PACKAGES = (
    "matplotlib",
    "networkx",
    "plotly",
    "seaborn",
    "pycountry",
    "rapidfuzz",
)


@dataclass(frozen=True)
class DiagnosticReport:
    lines: list[str]

    def as_text(self) -> str:
        return "\n".join(self.lines)


def package_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def collect_diagnostics(settings: AppConfig) -> DiagnosticReport:
    dansbib_path = settings.resolved_dansbib_path()
    output_folder = settings.resolved_output_folder()
    ris_folder = settings.resolved_ris_input_folder()
    main_path = dansbib_path / "main.py"
    pipeline_python = settings.resolved_pipeline_python()
    required = {name: package_available(name) for name in REQUIRED_PIPELINE_PACKAGES}
    optional = {name: package_available(name) for name in OPTIONAL_PIPELINE_PACKAGES}

    lines = [
        f"DansBib GUI version: {APP_VERSION}",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Operating system: {platform.platform()}",
        f"Settings file: {config_path()}",
        f"DansBib path: {dansbib_path}",
        f"Output path: {output_folder}",
        f"RIS input path: {ris_folder}",
        f"Logs path: {settings.resolved_logs_folder()}",
        f"main.py found: {'yes' if main_path.exists() else 'no'}",
        f"Pipeline Python: {pipeline_python or 'current interpreter'}",
        f"Last run log: {settings.last_run_log or 'none'}",
        f"Output folder note: {'custom folder configured; DansBib must support custom output paths to write there directly' if output_folder != dansbib_path / 'data' / 'outputs' else 'default DansBib output folder'}",
        "",
        "Required package status:",
    ]
    lines.extend(f"- {name}: {'available' if available else 'missing'}" for name, available in required.items())
    lines.append("")
    lines.append("Optional package status:")
    lines.extend(f"- {name}: {'available' if available else 'missing'}" for name, available in optional.items())
    lines.append("")
    lines.append("API settings status:")
    lines.extend(api_status_lines(settings, dansbib_path))
    return DiagnosticReport(lines)


def api_status_lines(settings: AppConfig, dansbib_path: Path | None = None) -> list[str]:
    configured = settings.api_settings_status or {}
    source_names = ("OpenAlex", "PubMed", "Scopus", "Web of Science")
    lines = []
    for source in source_names:
        status = configured.get(source.lower().replace(" ", "_"), "not checked")
        lines.append(f"- {source}: {status}")
    if dansbib_path and (dansbib_path / "utils" / "config.py").exists():
        lines.append("- DansBib config file: found")
    else:
        lines.append("- DansBib config file: not found")
    return lines


def friendly_error_message(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, FileNotFoundError):
        return "A required file or folder could not be found. Open Setup and check the DansBib, output, and RIS folders."
    if "modulenotfounderror" in lowered or "importerror" in lowered or "missing" in lowered and "dependency" in lowered:
        return "A required Python package is missing. The technical log has the package name for troubleshooting."
    if "connection" in lowered or "timed out" in lowered or "network" in lowered:
        return "A network or API source did not respond. Try RIS-only mode or check network/API access."
    if "403" in lowered or "forbidden" in lowered:
        return "An API source rejected access. Check API credentials or institutional network access."
    if "exit code" in lowered:
        return "DansBib stopped before finishing. The run log was saved with technical details."
    return "Something went wrong while running DansBib. The technical log was saved for troubleshooting."
