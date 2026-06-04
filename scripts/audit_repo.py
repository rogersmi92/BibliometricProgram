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
LARGE_FILE_LIMIT = 50 * 1024 * 1024
GENERATED_DIRS = [
    Path("DansBib/data/outputs"),
    Path("DansBib/data/processed"),
    Path("DansBib/data/visuals"),
    Path("DansBib/data/cache"),
    Path("DansBib/data/VOS"),
]
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
