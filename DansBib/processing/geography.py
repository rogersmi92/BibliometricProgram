"""Country and affiliation extraction helpers."""

from __future__ import annotations

import re

import pandas as pd

try:
    import pycountry
except ImportError:  # pragma: no cover - optional dependency until installed
    pycountry = None


def _build_country_map() -> dict[str, str]:
    country_map: dict[str, str] = {}
    if pycountry is not None:
        for country in pycountry.countries:
            name = str(country.name)
            country_map[name.lower()] = name
            official_name = getattr(country, "official_name", "")
            if official_name:
                country_map[str(official_name).lower()] = name
            alpha_2 = getattr(country, "alpha_2", "")
            if alpha_2:
                country_map[str(alpha_2).lower()] = name
            alpha_3 = getattr(country, "alpha_3", "")
            if alpha_3:
                country_map[str(alpha_3).lower()] = name
    return country_map


COUNTRY_MAP = _build_country_map()

SPECIAL_CASES = {
    "usa": "USA",
    "united states": "USA",
    "us": "USA",
    "uk": "UK",
    "united kingdom": "UK",
    "gbr": "UK",
    "england": "UK",
    "scotland": "UK",
    "wales": "UK",
    "china": "China",
    "chn": "China",
    "r china": "China",
    "p r china": "China",
    "peoples r china": "China",
    "people's republic of china": "China",
    "hong kong": "Hong Kong",
    "isle of man": "UK",
}

FALLBACK_COUNTRY_MAP = {
    "germany": "Germany",
    "france": "France",
    "spain": "Spain",
    "italy": "Italy",
    "canada": "Canada",
    "australia": "Australia",
    "brazil": "Brazil",
    "belgium": "Belgium",
    "switzerland": "Switzerland",
    "sweden": "Sweden",
    "netherlands": "Netherlands",
    "india": "India",
    "ind": "India",
    "ita": "Italy",
    "austria": "Austria",
    "denmark": "Denmark",
    "finland": "Finland",
    "hungary": "Hungary",
    "indonesia": "Indonesia",
    "armenia": "Armenia",
    "chile": "Chile",
    "greece": "Greece",
}
COUNTRY_MAP.update(FALLBACK_COUNTRY_MAP)

US_STATES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "district of columbia",
    "minnesota",
    "texas",
    "new york",
    "florida",
    "illinois",
    "ohio",
    "georgia",
    "pennsylvania",
    "washington",
    "hawaii",
    "idaho",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "north carolina",
    "north dakota",
    "oklahoma",
    "oregon",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "utah",
    "vermont",
    "virginia",
    "west virginia",
    "wisconsin",
    "wyoming",
}

US_STATE_ABBREV = {
    "tx",
    "ca",
    "ny",
    "mn",
    "fl",
    "il",
    "pa",
    "oh",
    "ga",
    "wa",
    "ma",
    "nc",
    "mi",
    "nj",
    "va",
    "az",
    "co",
    "tn",
    "mo",
    "md",
    "wi",
    "al",
    "sc",
    "la",
    "ky",
    "or",
    "ok",
    "ct",
    "ut",
    "nv",
    "ar",
    "ms",
    "ks",
    "nm",
    "ne",
    "id",
    "wv",
    "hi",
    "nh",
    "me",
    "ri",
    "mt",
    "de",
    "sd",
    "nd",
    "ak",
    "vt",
    "wy",
    "dc",
}

CITY_TO_COUNTRY = {
    "zagreb": "Croatia",
    "vienna": "Austria",
    "toronto": "Canada",
    "london": "UK",
    "paris": "France",
    "berlin": "Germany",
    "madrid": "Spain",
    "rome": "Italy",
    "neijiang": "China",
}


def clean_affiliation(text: object) -> str:
    """Normalize affiliation text for country detection."""
    if text in (None, ""):
        return ""
    cleaned = str(text).lower()
    cleaned = re.sub(r"\S+@\S+", "", cleaned)
    cleaned = re.sub(r"electronic address:?", "", cleaned)
    cleaned = re.sub(r"corresponding author", "", cleaned)
    cleaned = re.sub(r"[.,;:/()]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _country_search_terms(text: str) -> set[str]:
    """Build token ngrams so short codes like US only match whole tokens."""
    tokens = text.split()
    terms = set(tokens)
    for size in range(2, 5):
        terms.update(" ".join(tokens[index:index + size]) for index in range(len(tokens) - size + 1))
    junk = {"and", "of", "the", "department", "division", "email"}
    return {term for term in terms if term not in junk}


def _has_tr_email_domain(text: object) -> bool:
    return bool(re.search(r"@\S*\.tr\b", str(text or "").lower()))


def extract_country(text: object) -> str | None:
    """Extract one normalized country label from an affiliation/country string."""
    has_tr_email_domain = _has_tr_email_domain(text)
    cleaned = clean_affiliation(text)
    if not cleaned:
        return None

    search_terms = _country_search_terms(cleaned)

    if any(state in search_terms for state in US_STATES):
        return "USA"
    if any(term in US_STATE_ABBREV for term in search_terms):
        return "USA"
    if "hong kong" in search_terms:
        return "Hong Kong"

    for key, value in SPECIAL_CASES.items():
        if key in search_terms:
            return value

    for term in sorted(search_terms, key=len, reverse=True):
        if term == "tr" and has_tr_email_domain:
            continue
        if term in COUNTRY_MAP:
            return COUNTRY_MAP[term]

    for term in search_terms:
        if term in CITY_TO_COUNTRY:
            return CITY_TO_COUNTRY[term]

    return None


def _split_country_source(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[;,]", text) if part.strip()]


def _normalize_country_values(*values: object) -> str:
    countries: list[str] = []
    seen: set[str] = set()

    for value in values:
        for part in _split_country_source(value):
            country = extract_country(part) or part.strip()
            if not country:
                continue
            key = country.casefold()
            if key in seen:
                continue
            seen.add(key)
            countries.append(country)

    return "; ".join(countries)


def _append_unique_country(countries: list[str], seen: set[str], country: str | None) -> None:
    if not country:
        return
    key = country.casefold()
    if key in seen:
        return
    seen.add(key)
    countries.append(country)


def _extract_countries_from_affiliations(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    countries: list[str] = []
    seen: set[str] = set()
    for part in [segment.strip() for segment in text.split(";") if segment.strip()]:
        _append_unique_country(countries, seen, extract_country(part))
    return countries


def apply_country_extraction(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Normalize country metadata, keeping Hong Kong distinct from China."""
    if dataframe.empty:
        return dataframe

    fixed = dataframe.copy()
    if "countries" not in fixed.columns:
        fixed["countries"] = ""

    def normalize_row(row: pd.Series) -> str:
        countries: list[str] = []
        seen: set[str] = set()

        for part in _split_country_source(row.get("countries")):
            _append_unique_country(countries, seen, extract_country(part) or part.strip())

        for country in _extract_countries_from_affiliations(row.get("affiliations")):
            _append_unique_country(countries, seen, country)

        return "; ".join(countries)

    fixed["countries"] = fixed.apply(normalize_row, axis=1)

    def primary_country(row: pd.Series) -> str | None:
        affiliation_countries = _extract_countries_from_affiliations(row.get("affiliations"))
        if affiliation_countries:
            return affiliation_countries[0]
        countries = _split_country_source(row.get("countries"))
        return countries[0] if countries else None

    fixed["country"] = fixed.apply(primary_country, axis=1)
    return fixed
