"""RIS parsing helpers for local bibliographic exports."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pandas as pd

try:
    from pipeline_capabilities import RisInput, normalize_ris_source
    from processing.normalize import parse_citations
except ModuleNotFoundError:  # pragma: no cover - package import path
    from DansBib.pipeline_capabilities import RisInput, normalize_ris_source
    from DansBib.processing.normalize import parse_citations

LOGGER = logging.getLogger(__name__)
SCHEMA_COLUMNS = ["title", "doi", "authors", "year", "citations", "source"]
AFFILIATION_TAGS = ("AD", "C1", "AF")
INSTITUTION_TAGS = ("C3",)
IDENTIFIER_TAGS = ("UR", "L1", "L2", "L3", "LK", "AN", "UT", "N1")


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
    """Detect the RIS export origin when it is not supplied explicitly."""
    explicit_source = normalize_ris_source(source_db)
    if source_db and explicit_source != "unknown":
        return explicit_source

    filename = os.path.basename(path).lower()
    if "covidence" in filename:
        return "covidence"
    if "scopus" in filename:
        return "scopus"
    if "pubmed" in filename:
        return "pubmed"
    if "openalex" in filename:
        return "openalex"
    if "wos" in filename or "webofscience" in filename or "web_of_science" in filename:
        return "wos"

    for record in records[:10]:
        searchable = " ".join(
            str(value)
            for tag in ("DB", "DP", "N1", "PB", "SN", "UR", "AN", "UT")
            for value in record.get(tag, [])
        ).lower()
        if "covidence" in searchable:
            return "covidence"
        if "scopus" in searchable:
            return "scopus"
        if "pubmed" in searchable or "medline" in searchable:
            return "pubmed"
        if "openalex" in searchable:
            return "openalex"
        if "web of science" in searchable or "wos" in searchable:
            return "wos"
        if any(record.get(tag) for tag in ("TC", "Z9", "UT")):
            return "wos"

    return explicit_source


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
    source_db_value = "covidence" if detected_source == "covidence" else f"{detected_source}_ris"
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
                "ris_source": detected_source,
                "abstract": _first_ris_value(record, "AB", "N2"),
                "keywords": "; ".join(record.get("KW", []) or record.get("M1", [])),
                "institutions": "; ".join(dict.fromkeys(value for tag in INSTITUTION_TAGS for value in record.get(tag, []))),
                "countries": "",
                "affiliations": "; ".join(dict.fromkeys(value for tag in AFFILIATION_TAGS for value in record.get(tag, []))),
                "ris_fields_present": "; ".join(sorted(record)),
                "ris_affiliation_fields_present": "; ".join(tag for tag in AFFILIATION_TAGS + INSTITUTION_TAGS if record.get(tag)),
                "ris_identifier_blob": " ".join(value for tag in IDENTIFIER_TAGS for value in record.get(tag, [])),
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


def _coerce_ris_input(value: str | os.PathLike[str] | RisInput, source_db: str | None = None) -> RisInput:
    if isinstance(value, RisInput):
        if source_db is not None:
            return RisInput(path=value.path, source=normalize_ris_source(source_db))
        return value
    return RisInput(path=Path(value), source=normalize_ris_source(source_db))


def load_ris_files(ris_files: list[str | os.PathLike[str] | RisInput], source_db: str | None = None) -> pd.DataFrame:
    """Load all available RIS files and combine them into one DataFrame."""
    frames = []
    for ris_input in (_coerce_ris_input(value, source_db=source_db) for value in ris_files):
        path = os.fspath(ris_input.path)
        if not os.path.exists(path):
            LOGGER.warning("RIS file not found: %s", path)
            continue
        frames.append(_parse_ris_file(path, source_db=ris_input.source))
    combined = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame(columns=SCHEMA_COLUMNS)
    if not combined.empty:
        covidence_count = int(combined.get("source_db", pd.Series(dtype=str)).astype(str).str.lower().eq("covidence").sum())
        if covidence_count:
            LOGGER.info("Covidence records ingested: %d", covidence_count)
    return combined
