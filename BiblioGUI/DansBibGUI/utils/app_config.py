from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_VERSION = "0.2.0"
APP_CONFIG_DIR = "DansBibGUI"
CONFIG_FILENAME = "settings.json"

SOURCE_DEFAULTS = {
    "pubmed": True,
    "openalex": True,
    "wos": True,
    "scopus": True,
    "covidence": False,
}


@dataclass
class AppConfig:
    dansbib_path: str = ""
    output_folder: str = ""
    ris_input_folder: str = ""
    logs_folder: str = ""
    default_sources: dict[str, bool] = field(default_factory=lambda: dict(SOURCE_DEFAULTS))
    default_start_year: int | None = None
    default_end_year: int | None = None
    api_settings_status: dict[str, str] = field(default_factory=dict)
    pipeline_python: str = ""
    last_run_log: str = ""

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "AppConfig":
        defaults = cls()
        merged = asdict(defaults)
        merged.update({key: value for key, value in values.items() if key in merged})
        merged["default_sources"] = {**SOURCE_DEFAULTS, **dict(merged.get("default_sources") or {})}
        return cls(**merged)

    def resolved_dansbib_path(self) -> Path:
        return Path(self.dansbib_path).expanduser() if self.dansbib_path else default_dansbib_path()

    def resolved_output_folder(self) -> Path:
        if self.output_folder:
            return Path(self.output_folder).expanduser()
        return self.resolved_dansbib_path() / "data" / "outputs"

    def resolved_ris_input_folder(self) -> Path:
        if self.ris_input_folder:
            return Path(self.ris_input_folder).expanduser()
        return self.resolved_dansbib_path() / "data" / "raw"

    def resolved_logs_folder(self) -> Path:
        if self.logs_folder:
            return Path(self.logs_folder).expanduser()
        return user_config_dir() / "logs"

    def resolved_pipeline_python(self) -> Path | None:
        if self.pipeline_python:
            return Path(self.pipeline_python).expanduser()
        venv_python = self.resolved_dansbib_path() / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        return venv_python if venv_python.exists() else None

    def is_ready(self) -> bool:
        root = self.resolved_dansbib_path()
        return root.exists() and (root / "main.py").exists()

    def with_defaults_materialized(self) -> "AppConfig":
        self.dansbib_path = str(self.resolved_dansbib_path())
        self.output_folder = str(self.resolved_output_folder())
        self.ris_input_folder = str(self.resolved_ris_input_folder())
        self.logs_folder = str(self.resolved_logs_folder())
        return self


def default_dansbib_path() -> Path:
    return Path(__file__).resolve().parents[3] / "DansBib"


def user_config_dir() -> Path:
    env_path = os.getenv("DANSBIB_GUI_CONFIG_DIR")
    if env_path:
        return Path(env_path).expanduser()
    if os.name == "nt":
        base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys_platform_is_macos():
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_CONFIG_DIR


def config_path() -> Path:
    env_path = os.getenv("DANSBIB_GUI_CONFIG")
    return Path(env_path).expanduser() if env_path else user_config_dir() / CONFIG_FILENAME


def load_config() -> AppConfig:
    path = config_path()
    if not path.exists():
        return AppConfig().with_defaults_materialized()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return AppConfig().with_defaults_materialized()
    if not isinstance(data, dict):
        return AppConfig().with_defaults_materialized()
    return AppConfig.from_dict(data).with_defaults_materialized()


def save_config(settings: AppConfig) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    materialized = settings.with_defaults_materialized()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(asdict(materialized), handle, indent=2)
        handle.write("\n")
    return path


def sys_platform_is_macos() -> bool:
    return os.sys.platform == "darwin"
