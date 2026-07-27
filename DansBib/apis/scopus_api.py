"""Scopus API client."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import pandas as pd
import requests
from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectTimeout, ConnectionError as RequestsConnectionError, RequestException, RetryError
from urllib3.util import Retry

from processing.filters import FILTER_COLUMNS
from utils.config import SCOPUS_MAX_RESULTS, SCOPUS_PAGE_SIZE, get_requests_verify, get_scopus_api_key, get_scopus_inst_token

logger = logging.getLogger(__name__)

_HAS_CHECKED_SETUP = False
_SCOPUS_CONNECT_TIMEOUT_SECONDS = 5
_SCOPUS_READ_TIMEOUT_SECONDS = 20
_EMPTY_RESULT = pd.DataFrame(columns=["title", "doi", "authors", "year", "citations", "source"])
ENRICHED_COLUMNS = [
    "title",
    "doi",
    "authors",
    "year",
    "citations",
    "source",
    "institutions",
    "countries",
    "affiliations",
] + FILTER_COLUMNS
SCOPUS_API_URL = "https://api.elsevier.com/content/search/scopus"
_SCOPUS_TOTAL_ATTEMPTS = 2


def build_scopus_query(base_query: str, filters: dict[str, bool | None] | None = None) -> str:
    """Translate supported flags into Scopus search syntax where possible."""
    active_filters = filters or {}
    translated = _translate_wos_query_to_scopus(base_query)
    q = translated if translated.startswith("(") and translated.endswith(")") else f"({translated})"

    if active_filters.get("review") is True:
        q += " AND DOCTYPE(re)"
    elif active_filters.get("review") is False:
        q += " AND NOT DOCTYPE(re)"

    if active_filters.get("open_access") is True:
        q += " AND (OA(1))"
    elif active_filters.get("open_access") is False:
        q += " AND (OA(0))"

    return q


def _translate_wos_query_to_scopus(query: str) -> str:
    """Convert common Web of Science field tags into Scopus search syntax."""
    text = str(query or "").strip()
    replacements = {
        "TS": "TITLE-ABS-KEY",
        "TI": "TITLE",
        "AB": "ABS",
        "AK": "AUTHKEY",
        "KP": "KEY",
        "SO": "SRCTITLE",
        "PY": "PUBYEAR",
    }
    for wos_tag, scopus_tag in replacements.items():
        text = re.sub(rf"\b{wos_tag}\s*=", f"{scopus_tag}", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNEAR/\d+\b", "W/5", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSAME\b", "AND", text, flags=re.IGNORECASE)
    return text


def check_pybliometrics_setup_once() -> tuple[bool, bool]:
    """Backwards-compatible startup check for Scopus availability."""
    global _HAS_CHECKED_SETUP
    configured = bool(get_scopus_api_key())
    if not configured and not _HAS_CHECKED_SETUP:
        logger.info("Scopus API key not configured. Set SCOPUS_API_KEY to enable Scopus queries.")
    _HAS_CHECKED_SETUP = True
    return (configured, configured)


def _build_scopus_session() -> Session:
    """Create a Scopus session with a capped retry policy."""
    retry = Retry(
        total=_SCOPUS_TOTAL_ATTEMPTS - 1,
        connect=_SCOPUS_TOTAL_ATTEMPTS - 1,
        read=_SCOPUS_TOTAL_ATTEMPTS - 1,
        status=_SCOPUS_TOTAL_ATTEMPTS - 1,
        backoff_factor=0.1,
        status_forcelist=[500, 501, 502, 503, 504, 524],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.verify = get_requests_verify()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _execute_scopus_search(query: str, max_results: int, page_size: int) -> pd.DataFrame:
    """Fetch Scopus search results page-by-page using cursor pagination."""
    api_key = get_scopus_api_key()
    if not api_key:
        return _EMPTY_RESULT

    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/json",
    }
    inst_token = get_scopus_inst_token()
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    records: list[dict[str, Any]] = []
    cursor = "*"
    page = 1
    started_at = time.perf_counter()
    session = _build_scopus_session()

    try:
        while len(records) < max_results:
            batch_size = min(page_size, max_results - len(records))
            response = session.get(
                SCOPUS_API_URL,
                params={"query": query, "cursor": cursor, "count": batch_size, "view": "COMPLETE"},
                headers=headers,
                timeout=(_SCOPUS_CONNECT_TIMEOUT_SECONDS, _SCOPUS_READ_TIMEOUT_SECONDS),
            )
            response.raise_for_status()
            payload = response.json()
            search_results = payload.get("search-results", {})
            entries = search_results.get("entry", []) or []

            page_records = [_standardize_scopus_record(item) for item in entries[:batch_size]]
            records.extend(page_records)
            logger.info("Scopus page %d: %d records", page, len(page_records))
            if page % 5 == 0:
                logger.info("Scopus progress: %d / %d", len(records), max_results)

            next_cursor = search_results.get("cursor", {}).get("@next")
            if len(page_records) == 0 or len(page_records) < batch_size or not next_cursor or next_cursor == cursor:
                break

            cursor = next_cursor
            page += 1
    finally:
        session.close()

    dataframe = pd.DataFrame(records, columns=ENRICHED_COLUMNS)
    logger.info("Scopus total: %d (took %.1f seconds)", len(dataframe), time.perf_counter() - started_at)
    return dataframe


def _standardize_scopus_record(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a Scopus entry to the shared output schema."""
    authors = item.get("author") or []
    if isinstance(authors, dict):
        authors = [authors]

    author_names = []
    for author in authors:
        if not isinstance(author, dict):
            continue
        parts = [author.get("surname"), author.get("given-name")]
        formatted = ", ".join(part for part in parts if part)
        if formatted:
            author_names.append(formatted)

    authors_str = "; ".join(author_names) or str(item.get("dc:creator") or "")

    year = item.get("prism:coverDate") or item.get("coverDate") or item.get("publicationDate") or ""
    if isinstance(year, str) and len(year) >= 4:
        year = year[:4]

    citations = item.get("citedby-count") or item.get("citedby_count") or 0
    try:
        citations = int(citations)
    except (TypeError, ValueError):
        citations = 0

    return {
        "title": item.get("dc:title") or item.get("title") or item.get("document_title") or "",
        "doi": item.get("prism:doi") or item.get("doi") or "",
        "authors": authors_str,
        "year": year or "",
        "citations": citations,
        "source": "scopus",
        "institutions": str(item.get("affilname") or "").strip(),
        "countries": str(item.get("affiliation-country") or "").strip(),
        "affiliations": str(item.get("affilname") or "").strip(),
        "document_type": item.get("subtypeDescription") or item.get("prism:aggregationType") or item.get("subtype") or "",
        "open_access": item.get("openaccess") or item.get("openaccessFlag") or "",
    }


def query_scopus(
    query: str,
    filters: dict[str, bool | None] | int | None = None,
    limit: int = SCOPUS_MAX_RESULTS,
) -> pd.DataFrame:
    """
    Perform a Scopus search and return a DataFrame
    with columns: ["title", "doi", "authors", "year", "citations", "source"].
    """
    logger.info("Scopus query started.")
    if isinstance(filters, int):
        limit = filters
        filters = None

    if not get_scopus_api_key():
        logger.info("Scopus API key not configured. Cannot run Scopus query.")
        logger.info("Scopus query skipped.")
        return _EMPTY_RESULT

    try:
        translated_query = build_scopus_query(query, filters)
        dataframe = _execute_scopus_search(translated_query, max_results=limit, page_size=SCOPUS_PAGE_SIZE)
        logger.info("Scopus query completed.")
        return dataframe
    except ConnectTimeout:
        logger.warning("Scopus connection failed (timeout)")
        logger.warning("Skipping Scopus after connection failure")
        logger.info("Scopus query skipped.")
        return _EMPTY_RESULT
    except (RequestsConnectionError, RetryError) as exc:
        logger.warning("Scopus query failed: %s", exc)
        logger.warning("Skipping Scopus after connection failure")
        logger.info("Scopus query skipped.")
        return _EMPTY_RESULT
    except RequestException as exc:
        logger.warning("Scopus query failed: %s", exc)
        logger.info("Scopus query skipped.")
        return _EMPTY_RESULT
    except Exception as exc:
        logger.warning("Scopus query failed: %s", exc)
        logger.debug("Full exception during Scopus query:", exc_info=True)
        logger.info("Scopus query skipped.")
        return _EMPTY_RESULT


def search_scopus(query: str, *args, **kwargs) -> pd.DataFrame:
    """Backwards-compatible alias for query_scopus."""
    return query_scopus(query, *args, **kwargs)
