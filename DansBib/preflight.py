#!/usr/bin/env python3
"""Lightweight setup preflight for DansBib staff workstations."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIRED_PACKAGES = ("pandas", "requests")
OPTIONAL_PACKAGES = (
    "PySide6",
    "adjustText",
    "geopandas",
    "kaleido",
    "matplotlib",
    "networkx",
    "numpy",
    "plotly",
    "pycountry",
    "rapidfuzz",
    "seaborn",
    "shapefile",
    "tqdm",
)


def _package_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _path_from_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser()


def _check_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker = path / ".dansbib_write_test"
        marker.write_text("ok\n", encoding="utf-8")
        marker.unlink(missing_ok=True)
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"
    return True, "writable"


def _map_statuses() -> list[dict[str, object]]:
    try:
        from utils.map_providers import expected_readme_paths, report_missing_map_packages
    except Exception as exc:
        return [{"label": "map provider registry", "ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}]

    statuses = [
        {"label": status.label, "ok": status.present, "detail": status.message}
        for status in report_missing_map_packages()
    ]
    missing_readmes = [path for path in expected_readme_paths() if not path.exists()]
    statuses.append(
        {
            "label": "map reference placeholders",
            "ok": not missing_readmes,
            "detail": "all README placeholders present" if not missing_readmes else f"{len(missing_readmes)} placeholder README(s) missing",
        }
    )
    return statuses


def build_report() -> dict[str, object]:
    outputs_dir = _path_from_env("DANSBIB_OUTPUT_DIR", ROOT / "data" / "outputs")
    visuals_dir = _path_from_env("DANSBIB_VISUALS_DIR", ROOT / "data" / "visuals")
    processed_dir = _path_from_env("DANSBIB_PROCESSED_DIR", ROOT / "data" / "processed")
    vos_dir = _path_from_env("DANSBIB_VOS_DIR", ROOT / "data" / "VOS")
    raw_dir = _path_from_env("DANSBIB_RIS_RAW_DIR", ROOT / "data" / "raw")

    package_checks = [
        {"name": name, "required": True, "ok": _package_available(name)}
        for name in REQUIRED_PACKAGES
    ] + [
        {"name": name, "required": False, "ok": _package_available(name)}
        for name in OPTIONAL_PACKAGES
    ]
    folder_checks = []
    for label, path in (
        ("RIS input folder", raw_dir),
        ("Output folder", outputs_dir),
        ("Visuals folder", visuals_dir),
        ("Processed folder", processed_dir),
        ("VOS folder", vos_dir),
    ):
        ok, detail = _check_writable(path)
        folder_checks.append({"label": label, "path": str(path), "ok": ok, "detail": detail})

    credential_checks = [
        {"label": "Scopus API key", "ok": bool(os.getenv("SCOPUS_API_KEY")), "detail": "configured" if os.getenv("SCOPUS_API_KEY") else "not configured"},
        {"label": "Web of Science API key", "ok": bool(os.getenv("WOS_API_KEY")), "detail": "configured" if os.getenv("WOS_API_KEY") else "not configured"},
        {"label": "OpenAlex email", "ok": bool(os.getenv("OPENALEX_EMAIL")), "detail": "configured" if os.getenv("OPENALEX_EMAIL") else "optional but recommended"},
    ]
    map_checks = _map_statuses()

    errors = sum(1 for item in package_checks if item["required"] and not item["ok"])
    errors += sum(1 for item in folder_checks if not item["ok"])
    warnings = sum(1 for item in package_checks if not item["required"] and not item["ok"])
    warnings += sum(1 for item in credential_checks if not item["ok"])
    warnings += sum(1 for item in map_checks if not item["ok"])

    return {
        "ok": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "summary": f"Preflight found {errors} error(s) and {warnings} warning(s).",
        "python": sys.executable,
        "folders": folder_checks,
        "packages": package_checks,
        "credentials": credential_checks,
        "maps": map_checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check DansBib setup before running the full pipeline.")
    parser.add_argument("--json", action="store_true", help="Print a JSON report.")
    args = parser.parse_args()
    report = build_report()
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(report["summary"])
        print(f"Python: {report['python']}")
        for section in ("folders", "packages", "credentials", "maps"):
            print(f"\n{section.title()}:")
            for item in report[section]:
                label = item.get("label") or item.get("name")
                print(f"- {'OK' if item.get('ok') else 'WARN'}: {label} - {item.get('detail', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
