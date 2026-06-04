"""OpenAlex API client."""

from __future__ import annotations

import logging
import time

import pandas as pd
import requests

from processing.filters import FILTER_COLUMNS
from processing.normalize import standardize_record
from utils.config import OPENALEX_MAX_RESULTS, OPENALEX_PAGE_SIZE

LOGGER = logging.getLogger(__name__)
OPENALEX_API_URL = "https://api.openalex.org/works"
SCHEMA_COLUMNS = ["title", "doi", "authors", "year", "citations", "source"]
ENRICHED_COLUMNS = SCHEMA_COLUMNS + ["institutions", "countries", "affiliations"] + FILTER_COLUMNS


def _openalex_filter_param(filters: dict[str, bool | None] | None = None) -> str:
    """Build OpenAlex filter params for fields with stable API support."""
    active_filters = filters or {}
    values = []
    if active_filters.get("open_access") is True:
        values.append("is_oa:true")
    elif active_filters.get("open_access") is False:
        values.append("is_oa:false")
    return ",".join(values)


def query_openalex(
    query: str,
    filters: dict[str, bool | None] | int | None = None,
    per_page: int = OPENALEX_PAGE_SIZE,
    max_results: int = OPENALEX_MAX_RESULTS,
) -> pd.DataFrame:
    """Query OpenAlex works and return standardized results as a DataFrame."""
    if isinstance(filters, int):
        per_page = filters
        filters = None

    try:
        records = []
        page = 1
        started_at = time.perf_counter()
        LOGGER.info("OpenAlex query started.")

        while len(records) < max_results:
            page_size = min(per_page, max_results - len(records))
            params = {"search": query, "per-page": page_size, "page": page}
            filter_param = _openalex_filter_param(filters)
            if filter_param:
                params["filter"] = filter_param
            response = requests.get(OPENALEX_API_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()

            results = payload.get("results", [])
            if not results:
                break

            page_records = []
            for item in results:
                authorships = item.get("authorships", [])
                authors = "; ".join(
                    authorship.get("author", {}).get("display_name", "")
                    for authorship in authorships
                    if authorship.get("author", {}).get("display_name")
                )
                institutions = []
                countries = []
                for authorship in authorships:
                    for institution in authorship.get("institutions", []) or []:
                        name = str(institution.get("display_name") or "").strip()
                        country = str(institution.get("country_code") or "").strip()
                        if name:
                            institutions.append(name)
                        if country:
                            countries.append(country)

                base_record = standardize_record(
                    {
                        "title": item.get("display_name"),
                        "doi": item.get("doi"),
                        "authors": authors,
                        "year": item.get("publication_year"),
                        "citations": item.get("cited_by_count"),
                        "source": "OpenAlex",
                        "is_oa": item.get("open_access", {}).get("is_oa"),
                        "oa_status": item.get("open_access", {}).get("oa_status"),
                    }
                )
                base_record.update(
                    {
                        "institutions": "; ".join(dict.fromkeys(institutions)),
                        "countries": "; ".join(dict.fromkeys(countries)),
                        "affiliations": "; ".join(dict.fromkeys(institutions)),
                    }
                )
                page_records.append(base_record)

            records.extend(page_records)
            LOGGER.info("OpenAlex page %d: %d records", page, len(page_records))
            if page % 5 == 0:
                LOGGER.info("OpenAlex progress: %d / %d", len(records), max_results)

            if len(page_records) == 0 or len(page_records) < page_size:
                break
            page += 1

        dataframe = pd.DataFrame(records, columns=ENRICHED_COLUMNS)
        LOGGER.info("OpenAlex total: %d (took %.1f seconds)", len(dataframe), time.perf_counter() - started_at)
        return dataframe
    except requests.RequestException as exc:  # pragma: no cover - depends on network/API state
        LOGGER.warning("OpenAlex request failed: %s", exc)
        return pd.DataFrame(columns=ENRICHED_COLUMNS)
    except ValueError as exc:  # pragma: no cover - depends on response payload
        LOGGER.warning("OpenAlex response parsing failed: %s", exc)
        return pd.DataFrame(columns=ENRICHED_COLUMNS)


def search_openalex(*args, **kwargs):
    """Backwards-compatible alias for query_openalex."""
    return query_openalex(*args, **kwargs)
