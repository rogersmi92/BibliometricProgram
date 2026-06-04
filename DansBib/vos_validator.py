"""Convert bibliometric CSV exports into simple VOSviewer TXT inputs."""

from __future__ import annotations

import os
import sys

import pandas as pd

OUTPUT_DIR = "/Users/roger.smith/Desktop/BibliometricProgram/DansBib/data/VOS"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def format_authors(val: object) -> str:
    if pd.isna(val):
        return ""
    return str(val).replace(",", ";")


def format_keywords(val: object) -> str:
    if pd.isna(val):
        return ""
    return str(val).replace(";", ",")


def convert_to_vos(file_path: str) -> None:
    try:
        df = pd.read_csv(file_path)
    except Exception as exc:
        print(f"❌ Failed to read file: {exc}")
        return

    base = os.path.splitext(os.path.basename(file_path))[0]

    print(f"\n📄 Processing: {base}")

    if "authors" in df.columns:
        out_path = os.path.join(OUTPUT_DIR, f"{base}_authors.txt")
        with open(out_path, "w", encoding="utf-8") as handle:
            for val in df["authors"]:
                handle.write(format_authors(val) + "\n")
        print(f"✔ Authors TXT created: {out_path}")
    else:
        print("✘ No authors column — skipping authors file")

    if "keywords" in df.columns:
        out_path = os.path.join(OUTPUT_DIR, f"{base}_keywords.txt")
        with open(out_path, "w", encoding="utf-8") as handle:
            for val in df["keywords"]:
                handle.write(format_keywords(val) + "\n")
        print(f"✔ Keywords TXT created: {out_path}")
    else:
        print("✘ No keywords column — skipping keywords file")

    print("-" * 40)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python vos_validator.py <file.csv>")
        sys.exit(1)

    file_path = sys.argv[1]
    convert_to_vos(file_path)


if __name__ == "__main__":
    main()
