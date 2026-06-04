#!/usr/bin/env python3
"""Diagnostic runner for pybliometrics Scopus configuration issues."""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import platform
import re
import sys
import traceback
from pathlib import Path
from typing import Iterable

import pandas as pd

LOGGER = logging.getLogger("test_scopus")
DEFAULT_QUERY = 'TITLE-ABS-KEY("hemolytic uremic syndrome")'
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "data" / "outputs"
ENV_VARS_TO_LOG = (
    "PYBLIOMETRICS_CONFIG_FILE",
    "PYBLIOMETRICS_CONFIG_DIR",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "HOME",
    "XDG_CONFIG_HOME",
)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def log_header(title: str) -> None:
    LOGGER.info("=" * 30)
    LOGGER.info(title)
    LOGGER.info("=" * 30)


def format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def slugify_query(query: str, limit: int = 80) -> str:
    slug = query.lower()
    slug = re.sub(r'limit-to\([^)]*\)', '', slug)
    slug = re.sub(r'pubyear\s*[<>=]+\s*\d+', '', slug)
    slug = re.sub(r'title-abs-key', '', slug)
    slug = re.sub(r'[^a-z0-9]+', '_', slug).strip('_')
    return (slug[:limit] or "scopus_results").rstrip('_')


def get_output_path(query: str) -> str:
    filename = f"{slugify_query(query)}.csv"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return str(OUTPUT_DIR / filename)


def safe_stat_dict(path: Path) -> dict[str, object]:
    try:
        stat = path.stat()
    except Exception as exc:  # pragma: no cover - diagnostic path
        return {"error": repr(exc)}
    return {
        "mode_octal": oct(stat.st_mode & 0o777),
        "uid": stat.st_uid,
        "gid": stat.st_gid,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def inspect_path(path: Path) -> dict[str, object]:
    info: dict[str, object] = {
        "raw": str(path),
        "expanded": str(path.expanduser()),
    }
    try:
        resolved = path.expanduser().resolve()
    except Exception as exc:
        resolved = None
        info["resolve_error"] = repr(exc)
    else:
        info["resolved"] = str(resolved)

    target = path.expanduser()
    info["exists"] = target.exists()
    info["is_file"] = target.is_file()
    info["is_dir"] = target.is_dir()
    info["stat"] = safe_stat_dict(target)
    return info


def load_config_preview(path: Path) -> dict[str, object]:
    target = path.expanduser()
    preview: dict[str, object] = {"path": str(target)}
    if not target.exists():
        preview["error"] = "missing"
        return preview

    parser = configparser.ConfigParser()
    parser.optionxform = str
    try:
        with target.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except Exception as exc:
        preview["parse_error"] = repr(exc)
        return preview

    preview["sections"] = parser.sections()
    preview["has_required_sections"] = {
        section: parser.has_section(section)
        for section in ("Directories", "Authentication", "Requests")
    }
    preview["directories_has_scopussearch"] = parser.has_option("Directories", "ScopusSearch")
    preview["authentication_has_apikey"] = parser.has_option("Authentication", "APIKey")
    preview["authentication_has_insttoken"] = parser.has_option("Authentication", "InstToken")
    if parser.has_option("Directories", "ScopusSearch"):
        preview["directories_scopussearch"] = parser.get("Directories", "ScopusSearch")
    if parser.has_option("Requests", "Timeout"):
        preview["requests_timeout"] = parser.get("Requests", "Timeout")
    return preview


def log_runtime_context() -> None:
    log_header("Runtime Context")
    LOGGER.info("cwd=%s", Path.cwd())
    LOGGER.info("argv=%s", sys.argv)
    LOGGER.info("python_executable=%s", sys.executable)
    LOGGER.info("python_version=%s", sys.version.replace("\n", " "))
    LOGGER.info("platform=%s", platform.platform())
    LOGGER.info("sys_prefix=%s", sys.prefix)
    LOGGER.info("sys_base_prefix=%s", getattr(sys, "base_prefix", ""))
    LOGGER.info("user_home=%s", Path.home())
    LOGGER.info("sys_path=%s", json.dumps(sys.path, indent=2))
    for key in ENV_VARS_TO_LOG:
        LOGGER.info("env[%s]=%r", key, os.environ.get(key))


def get_candidate_config_paths() -> list[Path]:
    candidates = []
    seen = set()
    for raw in (
        os.environ.get("PYBLIOMETRICS_CONFIG_FILE"),
        os.environ.get("PYBLIOMETRICS_CONFIG_DIR"),
        str(Path.home() / ".scopus" / "config.ini"),
        str(Path.home() / ".pybliometrics" / "Scopus" / "config.ini"),
        str(Path.home() / ".config" / "pybliometrics.cfg"),
    ):
        if not raw:
            continue
        path = Path(raw).expanduser()
        key = str(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)
    return candidates


def log_candidate_configs() -> None:
    log_header("Candidate Config Paths")
    for candidate in get_candidate_config_paths():
        LOGGER.info("candidate=%s", json.dumps(inspect_path(candidate), indent=2, default=str))
        LOGGER.info("candidate_preview=%s", json.dumps(load_config_preview(candidate), indent=2, default=str))


def log_pybliometrics_state() -> None:
    log_header("Pybliometrics State")
    try:
        import pybliometrics
        from pybliometrics.utils import constants, startup
    except Exception as exc:
        LOGGER.exception("Failed to import pybliometrics: %s", exc)
        return

    LOGGER.info("pybliometrics_version=%s", getattr(pybliometrics, "__version__", "unknown"))
    LOGGER.info("pybliometrics_module=%s", getattr(pybliometrics, "__file__", "unknown"))
    LOGGER.info("constants.CONFIG_FILE=%s", getattr(constants, "CONFIG_FILE", None))
    LOGGER.info("constants.CACHE_PATH=%s", getattr(constants, "CACHE_PATH", None))
    LOGGER.info(
        "constants.config_path_options=%s",
        json.dumps([str(path) for path in getattr(constants, "config_path_options", [])], indent=2),
    )
    LOGGER.info("startup.CONFIG_is_none=%s", getattr(startup, "CONFIG", None) is None)
    if getattr(startup, "CONFIG", None) is not None:
        LOGGER.info("startup.CONFIG_sections=%s", startup.CONFIG.sections())


def force_init(config_path: Path | None) -> None:
    from pybliometrics import init
    from pybliometrics.utils import startup

    log_header("Pybliometrics Init")
    LOGGER.info("startup.CONFIG before init is None=%s", startup.CONFIG is None)
    LOGGER.info("requested_config_path=%s", config_path)
    init(config_path=config_path)
    LOGGER.info("startup.CONFIG after init is None=%s", startup.CONFIG is None)
    LOGGER.info("startup.CONFIG sections=%s", startup.CONFIG.sections())
    LOGGER.info(
        "loaded_APIKey_entries=%s",
        len(startup.get_keys()) if startup.CONFIG is not None else "unavailable",
    )
    LOGGER.info(
        "loaded_InstToken_entries=%s",
        len(startup.get_insttokens()) if startup.CONFIG is not None else "unavailable",
    )


def run_search(query: str, download: bool) -> None:
    from pybliometrics.scopus import ScopusSearch

    log_header("ScopusSearch Invocation")
    LOGGER.info("query=%s", query)
    LOGGER.info("download=%s", download)
    search = ScopusSearch(query, download=download)
    results = search.results or []
    LOGGER.info("cache_file_path=%s", getattr(search, "_cache_file_path", None))
    LOGGER.info("results_count=%s", len(results))

    rows = [
        {
            "title": getattr(record, "title", None),
            "doi": getattr(record, "doi", None),
            "coverDate": getattr(record, "coverDate", None),
            "publicationName": getattr(record, "publicationName", None),
            "citedby_count": getattr(record, "citedby_count", None),
            "author_names": getattr(record, "author_names", None),
            "affiliation_country": getattr(record, "affiliation_country", None),
        }
        for record in results
    ]
    df = pd.DataFrame(rows)
    output_path = get_output_path(query)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"Saved to: {output_path}")
    LOGGER.info("csv_output=%s", output_path)

    for index, record in enumerate(results[:5], start=1):
        LOGGER.info(
            "result_%s title=%r authors=%r citations=%r doi=%r",
            index,
            getattr(record, "title", "N/A"),
            getattr(record, "author_names", "N/A"),
            getattr(record, "citedby_count", "N/A"),
            getattr(record, "doi", "N/A"),
        )


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("PYBLIOMETRICS_CONFIG_FILE", "~/.config/pybliometrics.cfg")).expanduser(),
        help="Explicit config file to pass into pybliometrics.init().",
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="Scopus query to run after initialization succeeds.",
    )
    parser.add_argument(
        "--skip-search",
        action="store_true",
        help="Only diagnose configuration loading without hitting the Scopus API.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Force a live API request. Omit this to avoid network calls while validating config initialization.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    configure_logging(args.verbose)

    log_runtime_context()
    log_candidate_configs()
    log_pybliometrics_state()

    try:
        force_init(args.config)
        log_pybliometrics_state()
    except Exception as exc:
        LOGGER.error("pybliometrics init failed: %s", exc)
        LOGGER.error("full_traceback:\n%s", format_exception(exc))
        return 1

    if args.skip_search:
        LOGGER.info("Skipping Scopus query as requested.")
        return 0

    try:
        run_search(args.query, download=args.download)
        return 0
    except Exception as exc:
        LOGGER.error("ScopusSearch failed: %s", exc)
        LOGGER.error("full_traceback:\n%s", format_exception(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
