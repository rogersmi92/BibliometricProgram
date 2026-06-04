"""Web of Science API client."""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
import requests

from processing.filters import FILTER_COLUMNS
from processing.normalize import standardize_record
from utils.config import WOS_MAX_RESULTS, WOS_PAGE_SIZE, get_wos_api_key

LOGGER = logging.getLogger(__name__)
WOS_API_URL = "https://api.clarivate.com/api/wos"
SCHEMA_COLUMNS = ["title", "doi", "authors", "year", "citations", "source"]
ENRICHED_COLUMNS = SCHEMA_COLUMNS + ["institutions", "countries", "affiliations"] + FILTER_COLUMNS


def build_wos_query(base_query: str, filters: dict[str, bool | None] | None = None) -> str:
    """Translate supported flags into Web of Science query syntax where possible."""
    active_filters = filters or {}
    q = f"({base_query})"

    if active_filters.get("review") is True:
        q += " AND DT=(Review)"
    elif active_filters.get("review") is False:
        q += " NOT DT=(Review)"

    if active_filters.get("early_access") is True:
        q += " AND DT=(Early Access)"
    elif active_filters.get("early_access") is False:
        q += " NOT DT=(Early Access)"

    return q


def _extract_wos_authors(names: list[dict[str, Any]] | None) -> str:
    """Convert a WoS author payload into a single string."""
    if not names:
        return ""
    authors = []
    for author in names:
        full_name = author.get("full_name") or author.get("display_name") or author.get("wos_standard")
        if full_name:
            authors.append(str(full_name))
    return "; ".join(authors)


def _extract_wos_doi(item: dict[str, Any]) -> str:
    """Extract the DOI from a WoS record when present."""
    identifiers = (
        item.get("dynamic_data", {})
        .get("cluster_related", {})
        .get("identifiers", {})
        .get("identifier", [])
    )
    if isinstance(identifiers, dict):
        identifiers = [identifiers]
    for identifier in identifiers:
        if str(identifier.get("type", "")).lower() == "doi":
            return str(identifier.get("value", "")).strip()
    return ""


def _extract_wos_title(item: dict[str, Any]) -> str:
    """Extract a title from a WoS record."""
    summary = item.get("static_data", {}).get("summary", {})
    titles = summary.get("titles", {}).get("title", [])
    if isinstance(titles, dict):
        titles = [titles]
    for entry in titles:
        if entry.get("type") == "item":
            return entry.get("content", "")
    if titles:
        return titles[0].get("content", "")
    return ""


def _extract_wos_addresses(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Extract institutions and countries from WoS address metadata when present."""
    institutions = []
    countries = []
    addresses = item.get("static_data", {}).get("fullrecord_metadata", {}).get("addresses", {}).get("address_name", [])
    if isinstance(addresses, dict):
        addresses = [addresses]
    for address in addresses:
        address_spec = address.get("address_spec", {})
        country = str(address_spec.get("country") or "").strip()
        if country:
            countries.append(country)
        organizations = address_spec.get("organizations", {}).get("organization", [])
        if isinstance(organizations, dict):
            organizations = [organizations]
        for organization in organizations:
            name = str(organization.get("content") or organization.get("pref") or "").strip()
            if name:
                institutions.append(name)
    return (institutions, countries)


def _standardize_wos_record(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a WoS record to the shared output schema."""
    summary = item.get("static_data", {}).get("summary", {})
    pub_info = summary.get("pub_info", {})
    document_types = item.get("static_data", {}).get("fullrecord_metadata", {}).get("normalized_doctypes", {}).get("doctype", [])
    if isinstance(document_types, str):
        document_types = [document_types]
    names = summary.get("names", {}).get("name")
    normalized_names = names if isinstance(names, list) else [names] if names else []
    institutions, countries = _extract_wos_addresses(item)
    base_record = standardize_record(
        {
            "title": _extract_wos_title(item),
            "doi": _extract_wos_doi(item),
            "authors": _extract_wos_authors(normalized_names),
            "year": pub_info.get("pubyear"),
            "citations": item.get("dynamic_data", {})
            .get("citation_related", {})
            .get("tc_list", {})
            .get("silo_tc", [{}])[0]
            .get("local_count"),
            "source": "Web of Science",
            "document_type": "; ".join(str(value) for value in document_types),
        }
    )
    base_record.update(
        {
            "institutions": "; ".join(dict.fromkeys(institutions)),
            "countries": "; ".join(dict.fromkeys(countries)),
            "affiliations": "; ".join(dict.fromkeys(institutions)),
        }
    )
    return base_record


def query_wos(
    query: str,
    filters: dict[str, bool | None] | str | None = None,
    api_key: str | None = None,
    count: int = WOS_PAGE_SIZE,
    max_results: int = WOS_MAX_RESULTS,
) -> pd.DataFrame:
    """Query Web of Science and return standardized results as a DataFrame."""
    if isinstance(filters, str) and api_key is None:
        api_key = filters
        filters = None

    resolved_api_key = api_key or get_wos_api_key()
    if not resolved_api_key:
        LOGGER.info("Web of Science returned 0 records.")
        return pd.DataFrame(columns=SCHEMA_COLUMNS)

    try:
        headers = {"X-ApiKey": resolved_api_key}
        records = []
        first_record = 1
        page = 1
        started_at = time.perf_counter()
        LOGGER.info("Web of Science query started.")

        while len(records) < max_results:
            batch_size = min(count, max_results - len(records))
            params = {
                "databaseId": "WOS",
                "usrQuery": build_wos_query(query, filters),
                "count": batch_size,
                "firstRecord": first_record,
            }
            response = requests.get(WOS_API_URL, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()

            hits = payload.get("Data", {}).get("Records", {}).get("records", {}).get("REC", [])
            if isinstance(hits, dict):
                hits = [hits]

            page_records = [_standardize_wos_record(item) for item in hits]
            records.extend(page_records)
            LOGGER.info("Web of Science page %d: %d records", page, len(page_records))
            if page % 5 == 0:
                LOGGER.info("Web of Science progress: %d / %d", len(records), max_results)

            if len(page_records) == 0 or len(page_records) < batch_size:
                break

            first_record += len(page_records)
            page += 1

        dataframe = pd.DataFrame(records, columns=ENRICHED_COLUMNS)
        LOGGER.info("Web of Science total: %d (took %.1f seconds)", len(dataframe), time.perf_counter() - started_at)
        return dataframe
    except requests.HTTPError as exc:  # pragma: no cover - depends on network/API state
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 403:
            LOGGER.warning("Skipping Web of Science: access forbidden (likely network restriction)")
            return pd.DataFrame(columns=ENRICHED_COLUMNS)
        LOGGER.warning("Web of Science query failed: %s", exc)
        return pd.DataFrame(columns=ENRICHED_COLUMNS)
    except requests.RequestException as exc:  # pragma: no cover - depends on network/API state
        LOGGER.warning("Web of Science query failed: %s", exc)
        return pd.DataFrame(columns=ENRICHED_COLUMNS)


def search_wos(*args, **kwargs):
    """Backwards-compatible alias for query_wos."""
    return query_wos(*args, **kwargs)
