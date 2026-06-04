#!/usr/bin/env python3
"""Read-only repository hygiene audit."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from DansBib.utils.map_providers import (
    ALLOWED_MAP_DATA_EXTENSIONS,
    DENIED_MAP_DATA_EXTENSIONS,
    EXPECTED_INSTITUTION_HEADERS,
    IMPORTANT_COUNTRY_ISO3,
    MAPS_ROOT as DANSBIB_MAPS_ROOT,
    geoboundaries_dir,
    geoboundaries_geojson_path,
    expected_readme_paths,
    report_missing_map_packages,
    world_adm0_geojson_path,
    world_adm0_index_path,
)

LARGE_FILE_LIMIT = 50 * 1024 * 1024
LARGE_GEOSPATIAL_LIMIT = 25 * 1024 * 1024
GENERATED_DIRS = [
    Path("DansBib/data/outputs"),
    Path("DansBib/data/processed"),
    Path("DansBib/data/visuals"),
    Path("DansBib/data/cache"),
    Path("DansBib/data/VOS"),
]
OLD_MAPS_DIR = Path("DansBib/data/reference/Maps")
MAPS_DIR = Path("DansBib/data/reference/maps")
NON_POLITICAL_MAP_MARKERS = (
    "coastline",
    "land",
    "ocean",
    "lakes",
    "rivers",
    "reefs",
    "glaciated_areas",
    "antarctic_ice_shelves",
    "geography_regions",
    "geographic_lines",
    "marine_polys",
    "minor_islands",
    "playas",
    "raster",
    "physical",
    "urban_areas",
    "naturalearth",
    "natural_earth",
    "ne_10m",
    "ne_50m",
    "ne_110m",
)
EXPECTED_MAP_SUBTREES = (
    Path("DansBib/data/reference/maps/README.md"),
    Path("DansBib/data/reference/maps/MAP_PACKAGES_NEEDED.md"),
    Path("DansBib/data/reference/maps/boundaries"),
    Path("DansBib/data/reference/maps/gazetteers"),
    Path("DansBib/data/reference/maps/institutions"),
)
ALLOWED_MAP_JSON_FILES = {
    Path("DansBib/data/reference/maps/boundaries/geoboundaries/ALL/ADM0/WORLD_ADM0_INDEX.json"),
}
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".venv",
    "venv",
}


def run_git(args: list[str]) -> list[str]:
    """Return Git command output as lines; missing Git state becomes an empty list."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return result.stdout.splitlines()


def git_ls_files() -> list[Path]:
    return [Path(line) for line in run_git(["ls-files"]) if line.strip()]


def format_size(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def is_under(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def iter_project_files(suffix: str | None = None) -> list[Path]:
    files: list[Path] = []
    for current_root, dirs, filenames in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        root_path = Path(current_root)
        for filename in filenames:
            path = root_path / filename
            if suffix is None or path.suffix == suffix:
                files.append(path.relative_to(ROOT))
    return sorted(files)


def audit_large_tracked_files(tracked_files: list[Path]) -> list[tuple[Path, int]]:
    findings: list[tuple[Path, int]] = []
    for path in tracked_files:
        absolute = ROOT / path
        if not absolute.is_file():
            continue
        size = absolute.stat().st_size
        if size > LARGE_FILE_LIMIT:
            findings.append((path, size))
    return sorted(findings, key=lambda item: item[1], reverse=True)


def audit_visuals_non_png() -> list[Path]:
    visuals_dir = ROOT / "DansBib/data/visuals"
    if not visuals_dir.exists():
        return []
    findings: list[Path] = []
    for path in visuals_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() != ".png":
            findings.append(path.relative_to(ROOT))
    return sorted(findings)


def read_gitignore_patterns(path: Path) -> list[tuple[int, str]]:
    patterns: list[tuple[int, str]] = []
    try:
        lines = (ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return patterns

    for line_number, raw_line in enumerate(lines, start=1):
        pattern = raw_line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        patterns.append((line_number, pattern))
    return patterns


def audit_gitignore_duplicates() -> dict[str, list[tuple[Path, int]]]:
    locations: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    paths = sorted(set(Path(path) for path in run_git(["ls-files", ".gitignore", "**/.gitignore"])))
    paths.extend(path for path in iter_project_files() if path.name == ".gitignore" and path not in paths)

    for path in sorted(set(paths)):
        for line_number, pattern in read_gitignore_patterns(path):
            locations[pattern].append((path, line_number))

    return {pattern: places for pattern, places in locations.items() if len(places) > 1}


def imports_from_python(path: Path) -> set[str]:
    try:
        source = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def audit_python_imports(module_name: str) -> list[Path]:
    return [path for path in iter_project_files(".py") if module_name in imports_from_python(path)]


def audit_tracked_generated_outputs(tracked_files: list[Path]) -> dict[Path, list[Path]]:
    findings: dict[Path, list[Path]] = {}
    for folder in GENERATED_DIRS:
        matches = sorted(path for path in tracked_files if is_under(path, folder))
        if matches:
            findings[folder] = matches
    return findings


def audit_old_maps_paths(tracked_files: list[Path]) -> list[Path]:
    findings = [path for path in tracked_files if is_under(path, OLD_MAPS_DIR)]
    reference_dir = ROOT / "DansBib/data/reference"
    has_exact_old_dir = reference_dir.exists() and any(child.name == "Maps" for child in reference_dir.iterdir())
    if has_exact_old_dir:
        old_path = ROOT / OLD_MAPS_DIR
        findings.extend(path.relative_to(ROOT) for path in old_path.rglob("*") if path.is_file())
    return sorted(set(findings))


def audit_tracked_map_files(tracked_files: list[Path]) -> list[tuple[Path, str]]:
    findings: list[tuple[Path, str]] = []
    for path in tracked_files:
        if not is_under(path, MAPS_DIR):
            continue
        path_text = str(path).lower()
        suffix = path.suffix.lower()
        if path.name == "README.md" or path.name == "MAP_PACKAGES_NEEDED.md":
            continue
        if path in ALLOWED_MAP_JSON_FILES:
            continue
        if any(marker in path_text for marker in NON_POLITICAL_MAP_MARKERS):
            findings.append((path, "non-political/physical map marker"))
            continue
        if suffix in DENIED_MAP_DATA_EXTENSIONS:
            findings.append((path, "denied map data extension"))
            continue
        if suffix not in ALLOWED_MAP_DATA_EXTENSIONS and suffix not in {".csv", ".txt", ".md"}:
            findings.append((path, "unexpected map file extension"))
            continue
    return sorted(findings)


def audit_priority_geoboundaries_layout() -> list[tuple[Path, str]]:
    findings: list[tuple[Path, str]] = []
    required_dirs = [world_adm0_index_path().parent, world_adm0_geojson_path().parent]
    for iso3 in IMPORTANT_COUNTRY_ISO3:
        required_dirs.append(geoboundaries_dir(iso3, "ADM1"))
        required_dirs.append(geoboundaries_dir(iso3, "ADM2"))
    for path in required_dirs:
        if not path.exists():
            findings.append((path.relative_to(ROOT), "missing expected folder"))
    for iso3 in IMPORTANT_COUNTRY_ISO3:
        expected_adm1 = geoboundaries_geojson_path(iso3, "ADM1")
        expected_adm2 = geoboundaries_geojson_path(iso3, "ADM2")
        if expected_adm1.exists() and expected_adm1.name != f"{iso3}_ADM1.geojson":
            findings.append((expected_adm1.relative_to(ROOT), "unexpected ADM1 filename"))
        if expected_adm2.exists() and expected_adm2.name != f"{iso3}_ADM2.geojson":
            findings.append((expected_adm2.relative_to(ROOT), "unexpected ADM2 filename"))
    return sorted(findings)


def audit_map_files_in_unexpected_folders(tracked_files: list[Path]) -> list[Path]:
    findings: list[Path] = []
    for path in tracked_files:
        if not is_under(path, MAPS_DIR):
            continue
        if path.suffix.lower() not in ALLOWED_MAP_DATA_EXTENSIONS and path.suffix.lower() not in {".csv", ".txt"}:
            continue
        if any(is_under(path, subtree) for subtree in EXPECTED_MAP_SUBTREES):
            continue
        findings.append(path)
    return sorted(findings)


def audit_missing_map_readmes() -> list[Path]:
    return sorted(path.relative_to(ROOT) for path in expected_readme_paths() if not path.exists())


def audit_institution_csv_headers() -> list[tuple[Path, str]]:
    findings: list[tuple[Path, str]] = []
    institution_root = ROOT / "DansBib/data/reference/maps/institutions"
    for filename, expected in EXPECTED_INSTITUTION_HEADERS.items():
        path = institution_root / filename
        if not path.exists():
            findings.append((path.relative_to(ROOT), "missing file"))
            continue
        try:
            header = path.read_text(encoding="utf-8", errors="replace").splitlines()[0].split(",")
        except IndexError:
            findings.append((path.relative_to(ROOT), "missing header"))
            continue
        if header != expected:
            findings.append((path.relative_to(ROOT), "header mismatch"))
    return findings


def audit_natural_earth_required_code() -> list[Path]:
    findings: list[Path] = []
    patterns = ("data/reference/Maps", 'REFERENCE_DIR / "Maps"', "ne_10m", "ne_50m", "ne_110m")
    for path in iter_project_files(".py"):
        if path in {Path("scripts/audit_repo.py"), Path("DansBib/tests/test_map_providers.py")}:
            continue
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        if any(pattern in text for pattern in patterns):
            findings.append(path)
    return sorted(findings)


def audit_large_tracked_geospatial_files(tracked_files: list[Path]) -> list[tuple[Path, int]]:
    findings: list[tuple[Path, int]] = []
    geospatial_exts = ALLOWED_MAP_DATA_EXTENSIONS | DENIED_MAP_DATA_EXTENSIONS
    for path in tracked_files:
        if path.suffix.lower() not in geospatial_exts:
            continue
        absolute = ROOT / path
        if absolute.is_file() and absolute.stat().st_size > LARGE_GEOSPATIAL_LIMIT:
            findings.append((path, absolute.stat().st_size))
    return sorted(findings, key=lambda item: item[1], reverse=True)


def requirement_name(line: str) -> str | None:
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("-"):
        return None
    text = text.split("#", 1)[0].strip()
    match = re.match(r"([A-Za-z0-9_.-]+)", text)
    if not match:
        return None
    return match.group(1).lower().replace("_", "-")


def parse_requirements(path: Path) -> tuple[set[str], list[str]]:
    packages: set[str] = set()
    includes: list[str] = []
    try:
        lines = (ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return packages, includes

    for raw_line in lines:
        text = raw_line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("-r ") or text.startswith("--requirement "):
            includes.append(text.split(maxsplit=1)[1])
            continue
        name = requirement_name(text)
        if name:
            packages.add(name)
    return packages, includes


def audit_requirements() -> tuple[dict[Path, set[str]], dict[Path, list[str]], dict[tuple[Path, Path], set[str]]]:
    paths = sorted(path for path in iter_project_files() if path.name.startswith("requirements") and path.suffix == ".txt")
    packages_by_file: dict[Path, set[str]] = {}
    includes_by_file: dict[Path, list[str]] = {}
    overlaps: dict[tuple[Path, Path], set[str]] = {}

    for path in paths:
        packages, includes = parse_requirements(path)
        packages_by_file[path] = packages
        includes_by_file[path] = includes

    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            overlap = packages_by_file[left] & packages_by_file[right]
            if overlap:
                overlaps[(left, right)] = overlap

    return packages_by_file, includes_by_file, overlaps


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def print_empty() -> None:
    print("OK: no findings.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 when findings are present.")
    args = parser.parse_args(argv)

    tracked_files = git_ls_files()
    finding_count = 0

    print(f"Repository audit: {ROOT}")
    print("Mode: read-only")

    print_section("Tracked Git Files Over 50 MB")
    large_files = audit_large_tracked_files(tracked_files)
    finding_count += len(large_files)
    if large_files:
        for path, size in large_files:
            print(f"WARN: {path} ({format_size(size)})")
    else:
        print_empty()

    print_section("Non-PNG Files Under DansBib/data/visuals")
    non_png_visuals = audit_visuals_non_png()
    finding_count += len(non_png_visuals)
    if non_png_visuals:
        for path in non_png_visuals:
            print(f"WARN: {path}")
    else:
        print_empty()

    print_section("Duplicate .gitignore Patterns")
    duplicate_patterns = audit_gitignore_duplicates()
    finding_count += len(duplicate_patterns)
    if duplicate_patterns:
        for pattern, places in sorted(duplicate_patterns.items()):
            location_text = ", ".join(f"{path}:{line}" for path, line in places)
            print(f"WARN: {pattern!r} appears in {location_text}")
    else:
        print_empty()

    print_section("Python Files Importing tkinter")
    tkinter_imports = audit_python_imports("tkinter")
    finding_count += len(tkinter_imports)
    if tkinter_imports:
        for path in tkinter_imports:
            print(f"WARN: {path}")
    else:
        print_empty()

    print_section("Python Files Importing PySide6")
    pyside_imports = audit_python_imports("PySide6")
    if pyside_imports:
        for path in pyside_imports:
            print(f"INFO: {path}")
    else:
        print_empty()

    print_section("Generated-Output Folders Tracked By Git")
    tracked_generated = audit_tracked_generated_outputs(tracked_files)
    finding_count += sum(len(paths) for paths in tracked_generated.values())
    if tracked_generated:
        for folder, paths in tracked_generated.items():
            print(f"WARN: {folder} ({len(paths)} tracked files)")
            for path in paths[:20]:
                print(f"  - {path}")
            if len(paths) > 20:
                print(f"  - ... {len(paths) - 20} more")
    else:
        print_empty()

    print_section("Old Map Paths")
    old_maps = audit_old_maps_paths(tracked_files)
    finding_count += len(old_maps)
    if old_maps:
        for path in old_maps:
            print(f"WARN: {path}")
    else:
        print_empty()

    print_section("Tracked Map Files")
    tracked_map_findings = audit_tracked_map_files(tracked_files)
    finding_count += len(tracked_map_findings)
    if tracked_map_findings:
        for path, reason in tracked_map_findings:
            print(f"WARN: {path} ({reason})")
    else:
        print_empty()

    print_section("Map Files In Unexpected Folders")
    unexpected_map_files = audit_map_files_in_unexpected_folders(tracked_files)
    finding_count += len(unexpected_map_files)
    if unexpected_map_files:
        for path in unexpected_map_files:
            print(f"WARN: {path}")
    else:
        print_empty()

    print_section("Map README Placeholders")
    missing_readmes = audit_missing_map_readmes()
    finding_count += len(missing_readmes)
    if missing_readmes:
        for path in missing_readmes:
            print(f"WARN: missing {path}")
    else:
        print_empty()

    print_section("Priority geoBoundaries Layout")
    layout_findings = audit_priority_geoboundaries_layout()
    finding_count += len(layout_findings)
    if layout_findings:
        for path, reason in layout_findings:
            print(f"WARN: {path} ({reason})")
    else:
        print_empty()

    print_section("Map Provider Package Status")
    for status in report_missing_map_packages():
        prefix = "OK" if status.present else "INFO"
        print(f"{prefix}: {status.label}: {status.message}")
    print(f"INFO: WORLD_ADM0_INDEX.json is index metadata: {world_adm0_index_path().relative_to(ROOT)}")
    print(f"INFO: WORLD_ADM0.geojson is drawable world geometry: {world_adm0_geojson_path().relative_to(ROOT)}")

    print_section("Institution CSV Headers")
    institution_header_findings = audit_institution_csv_headers()
    finding_count += len(institution_header_findings)
    if institution_header_findings:
        for path, reason in institution_header_findings:
            print(f"WARN: {path} ({reason})")
    else:
        print_empty()

    print_section("Natural Earth Required Code References")
    natural_earth_refs = audit_natural_earth_required_code()
    finding_count += len(natural_earth_refs)
    if natural_earth_refs:
        for path in natural_earth_refs:
            print(f"WARN: {path}")
    else:
        print_empty()

    print_section("Large Tracked Geospatial Files")
    large_geospatial = audit_large_tracked_geospatial_files(tracked_files)
    finding_count += len(large_geospatial)
    if large_geospatial:
        for path, size in large_geospatial:
            print(f"WARN: {path} ({format_size(size)})")
    else:
        print_empty()

    print_section("Requirements Files And Overlap")
    packages_by_file, includes_by_file, overlaps = audit_requirements()
    if packages_by_file:
        for path in sorted(packages_by_file):
            packages = packages_by_file[path]
            includes = includes_by_file[path]
            package_text = ", ".join(sorted(packages)) if packages else "none"
            include_text = ", ".join(includes) if includes else "none"
            print(f"INFO: {path}: packages={package_text}; includes={include_text}")
    else:
        print("WARN: no requirements files found.")
        finding_count += 1

    if overlaps:
        finding_count += len(overlaps)
        print("WARN: direct package overlaps:")
        for (left, right), packages in sorted(overlaps.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
            print(f"  - {left} <-> {right}: {', '.join(sorted(packages))}")
    else:
        print("OK: no direct package overlaps.")

    print_section("Summary")
    print(f"Findings: {finding_count}")
    if args.strict and finding_count:
        print("Strict mode: failing because findings were reported.")
        return 1
    print("Exit: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
