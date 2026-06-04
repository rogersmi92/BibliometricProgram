"""RIS parsing helpers for local bibliographic exports."""

from __future__ import annotations

import logging
import os
import re

import pandas as pd

from processing.normalize import parse_citations

LOGGER = logging.getLogger(__name__)
SCHEMA_COLUMNS = ["title", "doi", "authors", "year", "citations", "source"]


def _append_ris_value(record: dict[str, list[str]], tag: str, value: str) -> None:
    """Append a RIS value, expanding embedded carriage-return tag fragments."""
    parts = str(value or "").replace("\r", "\n").splitlines() or [""]
    for index, part in enumerate(parts):
        match = re.match(r"^([A-Z0-9]{2})  -\s*(.*)$", part)
        if index > 0 and match:
            record.setdefault(match.group(1), []).append(match.group(2).strip())
        else:
            record.setdefault(tag, []).append(part.strip())


def _detect_ris_source(path: str, records: list[dict[str, list[str]]], source_db: str | None = None) -> str:
    """Detect whether a RIS export should be treated as WoS or Covidence."""
    if source_db:
        source = source_db.strip().lower()
        if source in {"covidence", "wos", "wos_ris"}:
            return "covidence" if source == "covidence" else "wos"

    filename = os.path.basename(path).lower()
    if "covidence" in filename:
        return "covidence"

    for record in records[:10]:
        searchable = " ".join(
            str(value)
            for tag in ("DB", "DP", "N1", "PB", "SN", "UR")
            for value in record.get(tag, [])
        ).lower()
        if "covidence" in searchable:
            return "covidence"

    return "wos"


def _parse_ris_file(path: str, source_db: str | None = None) -> pd.DataFrame:
    """Parse a RIS export into the shared raw collection schema."""
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    last_tag = ""

    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            match = re.match(r"^([A-Z0-9]{2})  -\s*(.*)$", line)
            if match:
                tag, value = match.group(1), match.group(2)
                if tag == "TY" and current:
                    records.append(current)
                    current = {}
                _append_ris_value(current, tag, value)
                last_tag = tag
                if tag == "ER":
                    records.append(current)
                    current = {}
                    last_tag = ""
            elif line.strip() and current and last_tag:
                current.setdefault(last_tag, []).append(line.strip())

    if current:
        records.append(current)

    detected_source = _detect_ris_source(path, records, source_db)
    source_db_value = "covidence" if detected_source == "covidence" else "wos_ris"
    rows = []
    for record in records:
        notes = " ".join(record.get("N1", []))
        citation_match = re.search(r"Times Cited in Web of Science Core Collection:\s*(\d+)", notes)
        citation_text = (
            _first_ris_value(record, "TC", "Z9")
            or _first_ris_value(record, "Times Cited", "WoS Times Cited")
            or (citation_match.group(1) if citation_match else "")
        )
        citation_value = parse_citations(citation_text) if str(citation_text).strip() else None
        rows.append(
            {
                "title": _first_ris_value(record, "TI", "T1"),
                "doi": _first_ris_value(record, "DO"),
                "authors": "; ".join(record.get("AU", []) or record.get("A1", [])),
                "year": _first_ris_value(record, "PY", "Y1", "DA"),
                "citations": citation_value,
                "citation_available": citation_value is not None,
                "source": detected_source,
                "abstract": _first_ris_value(record, "AB", "N2"),
                "keywords": "; ".join(record.get("KW", []) or record.get("M1", [])),
                "institutions": "; ".join(dict.fromkeys(record.get("C3", []))),
                "countries": "",
                "affiliations": "; ".join(dict.fromkeys(record.get("AD", []))),
                "wos_uid": _first_ris_value(record, "AN", "UT"),
                "source_db": source_db_value,
                "ris_file": os.path.abspath(path),
            }
        )

    if detected_source == "covidence":
        LOGGER.info("Covidence RIS file loaded: %s (%d records)", path, len(rows))
    else:
        LOGGER.info("RIS file loaded: %s (%d records)", path, len(rows))
    return pd.DataFrame(rows)


def _first_ris_value(record: dict[str, list[str]], *tags: str) -> str:
    """Return the first non-empty RIS value for the given tags."""
    for tag in tags:
        for value in record.get(tag, []):
            text = str(value or "").strip()
            if text:
                return text
    return ""


def load_ris_files(ris_files: list[str], source_db: str | None = None) -> pd.DataFrame:
    """Load all available RIS files and combine them into one DataFrame."""
    frames = []
    for path in ris_files:
        if not os.path.exists(path):
            LOGGER.warning("RIS file not found: %s", path)
            continue
        frames.append(_parse_ris_file(path, source_db=source_db))
    combined = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame(columns=SCHEMA_COLUMNS)
    if not combined.empty:
        covidence_count = int(combined.get("source_db", pd.Series(dtype=str)).astype(str).str.lower().eq("covidence").sum())
        if covidence_count:
            LOGGER.info("Covidence records ingested: %d", covidence_count)
    return combined
