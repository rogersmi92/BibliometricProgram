#!/usr/bin/env python3
"""Validate that data/visuals contains only PNG files."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent
VISUALS_DIR = ROOT / "data" / "visuals"


def find_non_png_files(visuals_dir: Path = VISUALS_DIR) -> list[Path]:
    if not visuals_dir.exists():
        return []
    return sorted(path for path in visuals_dir.rglob("*") if path.is_file() and path.suffix.lower() != ".png")


def main() -> int:
    offenders = find_non_png_files()
    if not offenders:
        print(f"PASS: {VISUALS_DIR.relative_to(ROOT)} contains only PNG files.")
        return 0
    print(f"FAIL: {VISUALS_DIR.relative_to(ROOT)} contains non-PNG files:")
    for path in offenders:
        print(f"- {path.relative_to(ROOT)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
