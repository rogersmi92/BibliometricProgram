"""PubMed API client built on NCBI E-utilities."""

from __future__ import annotations

import logging
import time
from xml.etree import ElementTree

import pandas as pd
import requests

from processing.filters import FILTER_COLUMNS
from processing.normalize import standardize_record
from utils.config import PUBMED_MAX_RESULTS, PUBMED_PAGE_SIZE

LOGGER = logging.getLogger(__name__)
SCHEMA_COLUMNS = ["title", "doi", "authors", "year", "citations", "source"]
ENRICHED_COLUMNS = SCHEMA_COLUMNS + ["institutions", "countries", "affiliations"] + FILTER_COLUMNS
PUBMED_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def build_pubmed_query(base_query: str, filters: dict[str, bool | None] | None = None) -> str:
    """Translate supported flags into PubMed query syntax where possible."""
    active_filters = filters or {}
    q = f"({base_query})"

    if active_filters.get("review") is True:
        q += " AND review[Publication Type]"
    elif active_filters.get("review") is False:
        q += " NOT review[Publication Type]"

    if active_filters.get("early_access") is True:
        q += " AND ahead of print[Publication Status]"
    elif active_filters.get("early_access") is False:
        q += " NOT ahead of print[Publication Status]"

    return q


def _extract_text(node: ElementTree.Element | None, path: str) -> str:
    """Safely extract stripped text from an XML child path."""
    if node is None:
        return ""
    target = node.find(path)
    if target is None:
        return ""
    return "".join(target.itertext()).strip()


def _extract_authors(article: ElementTree.Element) -> str:
    """Convert PubMed author metadata into a semicolon-delimited string."""
    authors: list[str] = []
    for author in article.findall(".//AuthorList/Author"):
        collective_name = _extract_text(author, "CollectiveName")
        if collective_name:
            authors.append(collective_name)
            continue

        last_name = _extract_text(author, "LastName")
        fore_name = _extract_text(author, "ForeName")
        initials = _extract_text(author, "Initials")

        if last_name and fore_name:
            authors.append(f"{last_name}, {fore_name}")
        elif last_name and initials:
            authors.append(f"{last_name}, {initials}")
        elif last_name:
            authors.append(last_name)

    return "; ".join(authors)


def _extract_doi(article: ElementTree.Element) -> str:
    """Return the DOI from a PubMed article when present."""
    for article_id in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if str(article_id.attrib.get("IdType", "")).lower() == "doi":
            return "".join(article_id.itertext()).strip()
    for elocation_id in article.findall(".//Article/ELocationID"):
        if str(elocation_id.attrib.get("EIdType", "")).lower() == "doi":
            return "".join(elocation_id.itertext()).strip()
    return ""


def _extract_year(article: ElementTree.Element) -> str:
    """Extract the publication year from common PubMed date fields."""
    for path in (
        ".//Article/Journal/JournalIssue/PubDate/Year",
        ".//Article/ArticleDate/Year",
        ".//PubmedData/History/PubMedPubDate/Year",
    ):
        year = _extract_text(article, path)
        if year:
            return year
    return _extract_text(article, ".//Article/Journal/JournalIssue/PubDate/MedlineDate")


def _extract_affiliations(article: ElementTree.Element) -> list[str]:
    """Extract affiliation strings from a PubMed article."""
    affiliations = []
    for affiliation in article.findall(".//AuthorList/Author/AffiliationInfo/Affiliation"):
        text = "".join(affiliation.itertext()).strip()
        if text:
            affiliations.append(text)
    return affiliations


def _extract_institutions_and_countries(affiliations: list[str]) -> tuple[list[str], list[str]]:
    """Heuristically extract institutions and countries from PubMed affiliations."""
    institutions = []
    countries = []
    institution_keywords = (
        "university",
        "hospital",
        "institute",
        "center",
        "centre",
        "school",
        "college",
        "clinic",
        "laboratory",
        "lab",
        "department",
    )
    for affiliation in affiliations:
        parts = [part.strip(" .") for part in affiliation.split(",") if part.strip(" .")]
        if not parts:
            continue
        country = parts[-1]
        if country:
            countries.append(country)
        institution = next(
            (part for part in parts if any(keyword in part.lower() for keyword in institution_keywords)),
            parts[0],
        )
        if institution:
            institutions.append(institution)
    return (institutions, countries)


def _extract_publication_types(article: ElementTree.Element) -> str:
    """Extract PubMed publication type labels."""
    types = []
    for publication_type in article.findall(".//Article/PublicationTypeList/PublicationType"):
        text = "".join(publication_type.itertext()).strip()
        if text:
            types.append(text)
    return "; ".join(types)


def _extract_publication_status(article: ElementTree.Element) -> str:
    """Extract PubMed publication status text when available."""
    statuses = []
    for status in article.findall(".//PubmedData/PublicationStatus"):
        text = "".join(status.itertext()).strip()
        if text:
            statuses.append(text)
    return "; ".join(statuses)


def _fetch_pubmed_articles(id_list: list[str]) -> list[dict[str, str | int]]:
    """Fetch a PubMed batch and convert it into standardized record dictionaries."""
    if not id_list:
        return []

    fetch_response = requests.get(
        PUBMED_EFETCH_URL,
        params={"db": "pubmed", "id": ",".join(id_list), "retmode": "xml"},
        timeout=30,
    )
    fetch_response.raise_for_status()
    root = ElementTree.fromstring(fetch_response.content)

    records = []
    for article in root.findall(".//PubmedArticle"):
        citation = article.find("MedlineCitation")
        article_data = citation.find("Article") if citation is not None else None
        title = _extract_text(article_data, "ArticleTitle")
        affiliations = _extract_affiliations(article)
        institutions, countries = _extract_institutions_and_countries(affiliations)
        base_record = standardize_record(
            {
                "title": title,
                "doi": _extract_doi(article),
                "authors": _extract_authors(article),
                "year": _extract_year(article),
                "citations": 0,
                "source": "pubmed",
                "publication_type": _extract_publication_types(article),
                "publication_status": _extract_publication_status(article),
            }
        )
        base_record.update(
            {
                "institutions": "; ".join(dict.fromkeys(institutions)),
                "countries": "; ".join(dict.fromkeys(countries)),
                "affiliations": "; ".join(dict.fromkeys(affiliations)),
            }
        )
        records.append(base_record)
    return records


def query_pubmed(
    query: str,
    filters: dict[str, bool | None] | int | None = None,
    retmax: int = PUBMED_PAGE_SIZE,
    max_results: int = PUBMED_MAX_RESULTS,
) -> pd.DataFrame:
    """Query PubMed and return standardized results as a DataFrame."""
    if isinstance(filters, int):
        retmax = filters
        filters = None

    try:
        records = []
        retstart = 0
        page = 1
        started_at = time.perf_counter()
        LOGGER.info("PubMed query started.")

        while len(records) < max_results:
            batch_size = min(retmax, max_results - len(records))
            search_response = requests.get(
                PUBMED_ESEARCH_URL,
                params={
                    "db": "pubmed",
                    "term": build_pubmed_query(query, filters),
                    "retstart": retstart,
                    "retmax": batch_size,
                    "retmode": "json",
                },
                timeout=30,
            )
            search_response.raise_for_status()
            search_payload = search_response.json()
            id_list = search_payload.get("esearchresult", {}).get("idlist", [])

            if not id_list:
                break

            page_records = _fetch_pubmed_articles(id_list)
            records.extend(page_records)
            LOGGER.info("PubMed page %d: %d records", page, len(page_records))
            if page % 5 == 0:
                LOGGER.info("PubMed progress: %d / %d", len(records), max_results)

            if len(page_records) == 0 or len(id_list) < batch_size or len(page_records) < len(id_list):
                break

            retstart += len(id_list)
            page += 1

        dataframe = pd.DataFrame(records, columns=ENRICHED_COLUMNS)
        LOGGER.info("PubMed total: %d (took %.1f seconds)", len(dataframe), time.perf_counter() - started_at)
        return dataframe
    except requests.RequestException as exc:  # pragma: no cover - depends on network/API state
        LOGGER.warning("PubMed query failed: %s", exc)
        return pd.DataFrame(columns=ENRICHED_COLUMNS)
    except (ValueError, ElementTree.ParseError) as exc:  # pragma: no cover - depends on response payload
        LOGGER.warning("PubMed response parsing failed: %s", exc)
        return pd.DataFrame(columns=ENRICHED_COLUMNS)


def search_pubmed(*args, **kwargs):
    """Backwards-compatible wrapper matching the other API modules."""
    return query_pubmed(*args, **kwargs)
