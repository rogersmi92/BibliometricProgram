"""Analysis helpers for bibliometric influence, contribution, and collaboration outputs."""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import pandas as pd

from processing.normalize import normalize_doi, normalize_title, parse_citations, parse_year, standardize_record

LOGGER = logging.getLogger(__name__)
CORE_COLUMNS = ["title", "doi", "authors", "year", "citations", "source"]
EXTRA_COLUMNS = ["institutions", "countries", "affiliations", "citation_available"]
ROOT = Path(__file__).resolve().parents[1]
INSTITUTION_REFERENCE_DIR = ROOT / "data" / "reference" / "maps" / "institutions"
PROCESSED_DIR = Path(os.getenv("DANSBIB_PROCESSED_DIR", str(ROOT / "data" / "processed")))
API_REQUEST_TIMEOUT_SECONDS = 20
INSTITUTION_DETAIL_TIMEOUT_SECONDS = 10
INSTITUTION_DETAIL_MAX_RETRIES = 2
APPROVED_INSTITUTION_MAP_LOCATION_SOURCES = {
    "scopus_structured_affiliation",
    "scopus_raw_affiliation_parse",
    "openalex_raw_affiliation_parse",
    "ror",
    "institution_geocache",
    "parsed_affiliation_city_geocode",
}
DISALLOWED_INSTITUTION_MAP_LOCATION_SOURCES = {
    "loose_text_geography",
    "geonames_loose_match",
    "geographic_terms",
    "text_mentions_debug",
}
AFFILIATION_FIELD_CANDIDATES = (
    "affiliations",
    "author affiliations",
    "author_affiliations",
    "affiliation",
    "address",
    "institution",
    "institutions",
    "organization",
    "organizations",
    "ris_affiliation_fields_present",
)
COUNTRY_ISO3_LOOKUP = {
    "afghanistan": "AFG",
    "australia": "AUS",
    "bangladesh": "BGD",
    "bhutan": "BTN",
    "brazil": "BRA",
    "canada": "CAN",
    "china": "CHN",
    "france": "FRA",
    "germany": "DEU",
    "india": "IND",
    "italy": "ITA",
    "japan": "JPN",
    "maldives": "MDV",
    "mexico": "MEX",
    "nepal": "NPL",
    "pakistan": "PAK",
    "south africa": "ZAF",
    "spain": "ESP",
    "sri lanka": "LKA",
    "united kingdom": "GBR",
    "uk": "GBR",
    "great britain": "GBR",
    "united states": "USA",
    "united states of america": "USA",
    "usa": "USA",
    "us": "USA",
    "deutschland": "DEU",
    "england": "GBR",
    "scotland": "GBR",
    "wales": "GBR",
    "northern ireland": "GBR",
}


def _clean_filename(query: str) -> str:
    """Return a filesystem-safe filename stem."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", query.lower()).strip("_") or "results"


def _split_multi_value(value: object) -> list[str]:
    """Split a semicolon-separated field into clean tokens.

    Author names use commas internally ("Last, Initials"), so comma is not a
    safe generic delimiter for bibliometric metadata.
    """
    if value is None:
        return []
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "na", "n/a"}:
        return []
    parts = text.split(";") if ";" in text else [text]
    return [part.strip() for part in parts if part.strip()]


def _institution_key(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"\b(the|at|of|dept|department|division|school|college)\b", " ", text)
    text = re.sub(r"\buniv\b\.?", "university", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _country_iso3(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[A-Za-z]{3}", text):
        return text.upper()
    return COUNTRY_ISO3_LOOKUP.get(_institution_key(text), "")


def _load_institution_aliases() -> dict[str, str]:
    aliases = {
        _institution_key("Univ Texas"): "University of Texas at Austin",
        _institution_key("University of Texas"): "University of Texas at Austin",
        _institution_key("The University of Texas"): "University of Texas at Austin",
        _institution_key("UT Austin"): "University of Texas at Austin",
        _institution_key("University of Texas at Austin"): "University of Texas at Austin",
    }
    for filename, raw_column in (("institution_aliases.csv", "alias"), ("institution_overrides.csv", "institution_raw")):
        path = INSTITUTION_REFERENCE_DIR / filename
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        for _, row in frame.iterrows():
            raw = str(row.get(raw_column) or "").strip()
            normalized = str(row.get("institution_normalized") or "").strip()
            if raw and normalized:
                aliases[_institution_key(raw)] = normalized
    return aliases


def _load_institution_geocache() -> dict[str, dict[str, object]]:
    path = INSTITUTION_REFERENCE_DIR / "institution_geocache.csv"
    if not path.exists():
        return {}
    frame = pd.read_csv(path)
    cache: dict[str, dict[str, object]] = {}
    for _, row in frame.iterrows():
        name = str(row.get("institution_normalized") or row.get("institution_raw") or "").strip()
        if not name:
            continue
        cache[_institution_key(name)] = {
            "city": str(row.get("city") or "").strip(),
            "admin1": str(row.get("admin1") or "").strip(),
            "country": str(row.get("country") or row.get("country_iso3") or "").strip(),
            "country_iso3": str(row.get("country_iso3") or "").strip(),
            "latitude": row.get("latitude", ""),
            "longitude": row.get("longitude", ""),
            "source": str(row.get("source") or path.name).strip(),
            "confidence": str(row.get("confidence") or "").strip() or "cache",
        }
    return cache


def _normalize_institution(value: object, aliases: dict[str, str]) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip(" ;,."))
    if not text:
        return ""
    key = _institution_key(text)
    if key in aliases:
        return aliases[key]
    text = re.sub(r"^the\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bUniv\b\.?", "University", text, flags=re.IGNORECASE)
    return text


def _has_usable_affiliation_text(value: object) -> bool:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none"}:
        return False
    if len(text) < 8:
        return False
    return any(keyword in text.lower() for keyword in INSTITUTION_KEYWORDS) or "," in text


def write_institution_extraction_debug(dataframe: pd.DataFrame, slug: str) -> tuple[bool, Path]:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    usable_any = False
    for field in AFFILIATION_FIELD_CANDIDATES:
        present = field in dataframe.columns
        if present and field in {"institution", "institutions", "organization", "organizations"}:
            candidate_count = int(dataframe[field].map(lambda value: bool(_split_multi_value(value))).sum())
        else:
            candidate_count = int(dataframe[field].map(_has_usable_affiliation_text).sum()) if present else 0
        usable = candidate_count > 0 and field not in {"ris_affiliation_fields_present"}
        usable_any = usable_any or usable
        rows.append(
            {
                "field_checked": field,
                "field_present": "yes" if present else "no",
                "usable_for_affiliation": "yes" if usable else "no",
                "candidate_count": candidate_count,
                "reason_rejected": "" if usable else "missing or no usable native affiliation/institution values",
            }
        )
    path = PROCESSED_DIR / f"{slug}_institution_extraction_debug.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    LOGGER.info("Native affiliation fields found: %s", "yes" if usable_any else "no")
    LOGGER.info("RIS fields checked for affiliations: %s", ", ".join(AFFILIATION_FIELD_CANDIDATES))
    if not usable_any:
        LOGGER.info("No usable affiliation/institution fields found in RIS export.")
    return usable_any, path


def _normalize_debug_doi(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    return text.strip(" .;,").lower()


def _extract_scopus_identifier(value: object) -> tuple[str, str, str, str]:
    text = str(value or "")
    if not text:
        return "", "", "no", ""
    url_match = re.search(r"https?://\S*scopus\.com/\S+", text, flags=re.IGNORECASE)
    scopus_url = url_match.group(0).rstrip(").,;") if url_match else ""
    eid_match = re.search(r"\b2-s2\.0-\d+\b", text)
    if eid_match:
        return scopus_url, eid_match.group(0), "eid", "yes"
    id_match = re.search(r"(?:eid|scopus(?:_id)?|documentId|origin=recordpage&zone=docId)[:=/ ]+([A-Za-z0-9.-]+)", text, flags=re.IGNORECASE)
    if id_match:
        return scopus_url, id_match.group(1).strip(), "scopus_identifier", "yes"
    return scopus_url, "", "", "yes" if scopus_url else "no"


def write_record_identifier_debug(dataframe: pd.DataFrame, slug: str) -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    identifier_columns = [
        column
        for column in dataframe.columns
        if str(column).lower() in {"ris_identifier_blob", "ur", "l1", "l2", "l3", "lk", "n1", "notes", "note", "doi", "wos_uid", "scopus_id", "eid"}
        or "url" in str(column).lower()
        or "scopus" in str(column).lower()
    ]
    if "ris_identifier_blob" not in identifier_columns and "ris_identifier_blob" in dataframe.columns:
        identifier_columns.insert(0, "ris_identifier_blob")
    for record_index, row in dataframe.reset_index(drop=True).iterrows():
        checked = identifier_columns or ["ris_identifier_blob", "source", "doi", "wos_uid"]
        blob = " ".join(str(row.get(column, "")) for column in checked)
        scopus_url, scopus_eid, scopus_identifier_type, success = _extract_scopus_identifier(blob)
        original_doi = str(row.get("doi") or "").strip()
        rows.append(
            {
                "record_id": record_index + 1,
                "title": row.get("title", ""),
                "year": row.get("year", ""),
                "doi": _normalize_debug_doi(original_doi),
                "original_doi": original_doi,
                "scopus_url": scopus_url,
                "scopus_eid": scopus_eid,
                "scopus_identifier_type": scopus_identifier_type,
                "identifier_parse_success": success,
                "parse_error": "" if success == "yes" else "no DOI/Scopus identifier detected",
                "fields_checked": "; ".join(str(column) for column in checked),
            }
        )
    path = PROCESSED_DIR / f"{slug}_record_identifier_debug.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    LOGGER.info("DOI count: %d", sum(1 for row in rows if row["doi"]))
    scopus_url_count = sum(1 for row in rows if row["scopus_url"])
    LOGGER.info("Scopus URLs detected: %d", scopus_url_count)
    if scopus_url_count == 0:
        LOGGER.info("Scopus URLs detected: 0; no Scopus URL fields found in RIS records.")
    LOGGER.info("Scopus IDs parsed: %d", sum(1 for row in rows if row["scopus_eid"]))
    return path


def _openalex_cache_key(row: pd.Series) -> str:
    doi = _normalize_debug_doi(row.get("doi"))
    if doi:
        return f"doi:{doi}"
    title = normalize_title(str(row.get("title") or ""))
    year = str(row.get("year") or "").strip()
    return f"title:{title}|{year}"


ATTEMPT_DEBUG_COLUMNS = [
    "record_id",
    "title",
    "year",
    "doi",
    "scopus_eid",
    "enrichment_source",
    "attempted",
    "not_attempted_reason",
    "request_identifier_used",
    "endpoint_type",
    "response_status",
    "match_status",
    "institution_count",
    "failure_reason",
]


def _write_attempt_debug(slug: str, source: str, rows: list[dict[str, object]]) -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    path = PROCESSED_DIR / f"{slug}_{source}_enrichment_attempt_debug.csv"
    pd.DataFrame(rows, columns=ATTEMPT_DEBUG_COLUMNS).to_csv(path, index=False)
    return path


def _attempt_debug_row(
    row: pd.Series,
    record_index: int,
    source: str,
    *,
    attempted: bool,
    not_attempted_reason: str = "",
    request_identifier_used: str = "",
    endpoint_type: str = "",
    response_status: str = "",
    match_status: str = "",
    institution_count: int = 0,
    failure_reason: str = "",
) -> dict[str, object]:
    _scopus_url, scopus_eid, _kind, _success = _extract_scopus_identifier(row.get("ris_identifier_blob"))
    return {
        "record_id": record_index + 1,
        "title": row.get("title", ""),
        "year": row.get("year", ""),
        "doi": _normalize_debug_doi(row.get("doi")),
        "scopus_eid": scopus_eid,
        "enrichment_source": source,
        "attempted": "yes" if attempted else "no",
        "not_attempted_reason": not_attempted_reason,
        "request_identifier_used": request_identifier_used,
        "endpoint_type": endpoint_type,
        "response_status": response_status,
        "match_status": match_status,
        "institution_count": institution_count,
        "failure_reason": failure_reason,
    }


def _safe_failure_reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}: {str(exc.reason)[:120]}"
    details = _request_exception_details(exc, "", "", API_REQUEST_TIMEOUT_SECONDS)
    bits = [details["exception_class"]]
    if details["exception_message"]:
        bits.append(details["exception_message"])
    if details["underlying_reason"]:
        bits.append(f"reason={details['underlying_reason']}")
    flags = [
        label
        for key, label in (
            ("ssl_error_detected", "ssl_error"),
            ("dns_error_detected", "dns_error"),
            ("timeout_detected", "timeout"),
            ("malformed_url_detected", "malformed_url"),
        )
        if details.get(key) == "yes"
    ]
    if flags:
        bits.append(",".join(flags))
    return "; ".join(str(bit) for bit in bits if bit)


def _failure_category(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {401, 403}:
            return "auth_or_permission"
        if exc.code in {402, 429}:
            return "quota_or_rate_limit"
        return "api_http"
    details = _request_exception_details(exc, "", "", API_REQUEST_TIMEOUT_SECONDS)
    if details.get("dns_error_detected") == "yes":
        return "network_dns"
    if details.get("timeout_detected") == "yes":
        return "network_timeout"
    if details.get("ssl_error_detected") == "yes":
        return "network_ssl"
    if details.get("malformed_url_detected") == "yes":
        return "client_url"
    return "network_or_client"


def _proxy_detected() -> str:
    proxies = urllib.request.getproxies()
    env_proxy = any(os.getenv(name) for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"))
    return "yes" if proxies or env_proxy else "no"


def _request_exception_details(exc: Exception, domain: str, endpoint_type: str, timeout_seconds: int) -> dict[str, str]:
    reason = getattr(exc, "reason", "")
    reason_text = repr(reason) if reason else ""
    message = str(exc) or repr(exc)
    combined = f"{message} {reason_text}".lower()
    errno_value = getattr(exc, "errno", "")
    if not errno_value and reason is not None:
        errno_value = getattr(reason, "errno", "")
    return {
        "exception_class": exc.__class__.__name__,
        "exception_message": message,
        "underlying_reason": reason_text,
        "errno": str(errno_value or ""),
        "endpoint_domain": domain,
        "endpoint_type": endpoint_type,
        "timeout_seconds": str(timeout_seconds),
        "proxy_detected": _proxy_detected(),
        "ssl_error_detected": "yes" if isinstance(reason, ssl.SSLError) or "certificate" in combined or "ssl" in combined else "no",
        "dns_error_detected": "yes" if isinstance(reason, socket.gaierror) or "nodename" in combined or "name resolution" in combined or "temporary failure in name resolution" in combined else "no",
        "timeout_detected": "yes" if isinstance(reason, TimeoutError) or "timed out" in combined or "timeout" in combined else "no",
        "malformed_url_detected": "yes" if isinstance(exc, ValueError) or "unknown url type" in combined or "no host given" in combined else "no",
    }


def _log_request_exception(prefix: str, exc: Exception, domain: str, endpoint_type: str, timeout_seconds: int) -> None:
    details = _request_exception_details(exc, domain, endpoint_type, timeout_seconds)
    certifi_enabled, certifi_path = _certifi_status()
    LOGGER.info("%s API client type: urllib.request", prefix)
    LOGGER.info("%s certifi enabled: %s", prefix, certifi_enabled)
    if certifi_path:
        LOGGER.info("%s certifi path: %s", prefix, certifi_path)
    LOGGER.info("%s SSL verification enabled: yes", prefix)
    LOGGER.info("%s failure class: %s", prefix, details["exception_class"])
    LOGGER.info("%s failure message: %s", prefix, details["exception_message"])
    LOGGER.info("%s underlying reason: %s", prefix, details["underlying_reason"] or "n/a")
    LOGGER.info("%s errno: %s", prefix, details["errno"] or "n/a")
    LOGGER.info("%s endpoint domain: %s", prefix, domain)
    LOGGER.info("%s endpoint type: %s", prefix, endpoint_type)
    LOGGER.info("%s timeout seconds: %s", prefix, timeout_seconds)
    LOGGER.info("%s proxy settings detected: %s", prefix, details["proxy_detected"])
    LOGGER.info("%s SSL/certificate verification failed: %s", prefix, details["ssl_error_detected"])
    LOGGER.info("%s DNS/name resolution failed: %s", prefix, details["dns_error_detected"])
    LOGGER.info("%s connection timed out: %s", prefix, details["timeout_detected"])
    LOGGER.info("%s request URL malformed: %s", prefix, details["malformed_url_detected"])
    if details["ssl_error_detected"] == "yes":
        LOGGER.info(
            "Python SSL certificate verification failed. Try enabling 'Use certifi CA bundle for API requests' in API Settings. "
            "If this is a python.org macOS install, also run the bundled Install Certificates.command for that Python version."
        )


def _log_diagnostic_command() -> None:
    LOGGER.info("Safe API connectivity diagnostic command:")
    LOGGER.info("python - <<'PY'")
    LOGGER.info("import urllib.request")
    LOGGER.info('for url in ["https://api.openalex.org/works?per-page=1", "https://api.elsevier.com", "https://doi.org"]:')
    LOGGER.info("    try:")
    LOGGER.info("        with urllib.request.urlopen(url, timeout=10) as r:")
    LOGGER.info("            print(url, r.status)")
    LOGGER.info("    except Exception as e:")
    LOGGER.info("        print(url, type(e).__name__, repr(e))")
    LOGGER.info("PY")


def _api_urlopen(request: urllib.request.Request | str, timeout: int = API_REQUEST_TIMEOUT_SECONDS):
    use_certifi = os.getenv("DANSBIB_USE_CERTIFI", "1").strip().lower() not in {"0", "false", "no", "off"}
    if use_certifi:
        try:
            import certifi  # type: ignore

            context = ssl.create_default_context(cafile=certifi.where())
            return urllib.request.urlopen(request, timeout=timeout, context=context)
        except ImportError:
            LOGGER.info("certifi CA bundle requested but certifi is not installed; using system certificate store.")
    return urllib.request.urlopen(request, timeout=timeout)


def _certifi_status() -> tuple[str, str]:
    if os.getenv("DANSBIB_USE_CERTIFI", "1").strip().lower() in {"0", "false", "no", "off"}:
        return "no", ""
    try:
        import certifi  # type: ignore
    except ImportError:
        return "no", ""
    return "yes", certifi.where()


CONNECTIVITY_DEBUG_COLUMNS = [
    "source",
    "domain",
    "endpoint_tested",
    "endpoint_type",
    "api_client_type",
    "certifi_enabled",
    "certifi_path",
    "ssl_verification_enabled",
    "attempted",
    "success",
    "response_status",
    "exception_class",
    "exception_message",
    "underlying_reason",
    "timeout_seconds",
    "proxy_detected",
    "ssl_error_detected",
    "dns_error_detected",
    "timeout_detected",
    "malformed_url_detected",
]


def _write_api_connectivity_debug(slug: str) -> tuple[Path, bool]:
    certifi_enabled, certifi_path = _certifi_status()
    LOGGER.info("API client type: urllib.request")
    LOGGER.info("certifi enabled: %s", certifi_enabled)
    if certifi_path:
        LOGGER.info("certifi path: %s", certifi_path)
    LOGGER.info("SSL verification enabled: yes")
    LOGGER.info("proxy detected: %s", _proxy_detected())
    checks = [
        ("OpenAlex", "api.openalex.org", "connectivity probe", "https://api.openalex.org/works?per-page=1"),
        ("Elsevier", "api.elsevier.com", "connectivity probe", "https://api.elsevier.com"),
        ("DOI", "doi.org", "connectivity probe", "https://doi.org"),
    ]
    rows: list[dict[str, object]] = []
    any_failure = False
    for source, domain, endpoint_type, url in checks:
        row = {
            "source": source,
            "domain": domain,
            "endpoint_tested": url,
            "endpoint_type": endpoint_type,
            "api_client_type": "urllib.request",
            "certifi_enabled": certifi_enabled,
            "certifi_path": certifi_path,
            "ssl_verification_enabled": "yes",
            "attempted": "yes",
            "success": "no",
            "response_status": "",
            "exception_class": "",
            "exception_message": "",
            "underlying_reason": "",
            "timeout_seconds": API_REQUEST_TIMEOUT_SECONDS,
            "proxy_detected": _proxy_detected(),
            "ssl_error_detected": "no",
            "dns_error_detected": "no",
            "timeout_detected": "no",
            "malformed_url_detected": "no",
        }
        try:
            with _api_urlopen(url, timeout=API_REQUEST_TIMEOUT_SECONDS) as response:
                row["response_status"] = int(getattr(response, "status", 0) or 0)
                row["success"] = "yes"
        except urllib.error.HTTPError as exc:
            row["response_status"] = int(exc.code)
            row["success"] = "yes"
            LOGGER.info(
                "API connectivity check reached %s and returned normal HTTP status %s.",
                domain,
                exc.code,
            )
        except Exception as exc:
            any_failure = True
            details = _request_exception_details(exc, domain, endpoint_type, API_REQUEST_TIMEOUT_SECONDS)
            row.update(
                {
                    "exception_class": details["exception_class"],
                    "exception_message": details["exception_message"],
                    "underlying_reason": details["underlying_reason"],
                    "ssl_error_detected": details["ssl_error_detected"],
                    "dns_error_detected": details["dns_error_detected"],
                    "timeout_detected": details["timeout_detected"],
                    "malformed_url_detected": details["malformed_url_detected"],
                }
            )
            _log_request_exception(f"{source} connectivity check", exc, domain, endpoint_type, API_REQUEST_TIMEOUT_SECONDS)
        LOGGER.info(
            "API connectivity check: %s %s attempted=%s success=%s status=%s",
            source,
            domain,
            row["attempted"],
            row["success"],
            row["response_status"] or "n/a",
        )
        rows.append(row)
    path = PROCESSED_DIR / f"{slug}_api_connectivity_debug.csv"
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=CONNECTIVITY_DEBUG_COLUMNS).to_csv(path, index=False)
    LOGGER.info("API connectivity debug output: %s", path)
    if any_failure:
        _log_diagnostic_command()
    return path, not any_failure


def _openalex_request(params: dict[str, str], email: str) -> tuple[dict[str, object], int]:
    if email:
        params = {**params, "mailto": email}
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "DansBibGUI institution enrichment"})
    with _api_urlopen(request, timeout=API_REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8")), int(getattr(response, "status", 0) or 0)


def _openalex_institution_detail(institution_id: str, email: str, cache: dict[str, object]) -> dict[str, object]:
    if not institution_id:
        return {}
    key = institution_id.rstrip("/").split("/")[-1]
    if not key:
        return {}
    if key in cache and isinstance(cache[key], dict):
        return cache[key]  # type: ignore[return-value]
    disabled = cache.get("__live_lookup_disabled__")
    if isinstance(disabled, dict):
        cache[key] = {
            "lookup_status": "failed",
            "failure_reason": str(disabled.get("failure_reason") or "OpenAlex institution detail lookup disabled after repeated failures"),
            "failure_category": str(disabled.get("failure_category") or "network_or_client"),
        }
        return {}
    url = f"https://api.openalex.org/institutions/{urllib.parse.quote(key)}"
    if email:
        url += "?" + urllib.parse.urlencode({"mailto": email})
    failure = ""
    for attempt in range(1, INSTITUTION_DETAIL_MAX_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "DansBibGUI institution enrichment"})
            with _api_urlopen(request, timeout=INSTITUTION_DETAIL_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict):
                cache[key] = payload
                return payload
        except Exception as exc:
            failure = _safe_failure_reason(exc)
            category = _failure_category(exc)
            LOGGER.info(
                "OpenAlex institution detail lookup failed for %s attempt %d/%d category=%s: %s",
                key,
                attempt,
                INSTITUTION_DETAIL_MAX_RETRIES,
                category,
                failure,
            )
    failure_meta = cache.get("__failure_meta__")
    failure_count = int(failure_meta.get("count", 0)) if isinstance(failure_meta, dict) else 0
    failure_count += 1
    cache["__failure_meta__"] = {"count": failure_count, "last_failure_reason": failure, "last_failure_category": category if "category" in locals() else ""}
    if failure_count >= 3:
        cache["__live_lookup_disabled__"] = {
            "failure_reason": failure or "OpenAlex institution detail lookup disabled after repeated failures",
            "failure_category": category if "category" in locals() else "",
        }
        LOGGER.info(
            "OpenAlex institution detail live lookups disabled after %d failed institutions category=%s: %s",
            failure_count,
            category if "category" in locals() else "",
            failure,
        )
    cache[key] = {"lookup_status": "failed", "failure_reason": failure, "failure_category": category if "category" in locals() else ""}
    return {}


def _institution_geo_from_openalex(
    institution: dict[str, object],
    email: str = "",
    institution_cache: dict[str, object] | None = None,
) -> tuple[dict[str, object], str]:
    geo = institution.get("geo") if isinstance(institution.get("geo"), dict) else {}
    if geo and (geo.get("latitude") not in {"", None} or geo.get("city")):
        return geo, "openalex_work_institution_geo"
    country_code = str(institution.get("country_code") or "").strip()
    if country_code:
        return {"country_code": country_code}, "openalex_work_institution_country_code"
    return {}, "openalex"


def _institution_rows_from_openalex_work(
    work: dict[str, object],
    row: pd.Series,
    record_index: int,
    match_method: str,
    email: str = "",
    institution_cache: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    authorships = work.get("authorships") if isinstance(work, dict) else []
    if not isinstance(authorships, list):
        return rows
    seen: set[str] = set()
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        author_name = str(author.get("display_name") or "") if isinstance(author, dict) else ""
        raw_affiliations = authorship.get("raw_affiliation_strings")
        if isinstance(raw_affiliations, list):
            raw_affiliation_string = "; ".join(str(item).strip() for item in raw_affiliations if str(item).strip())
        else:
            raw_affiliation_string = str(raw_affiliations or "").strip()
        institutions = authorship.get("institutions")
        if not isinstance(institutions, list):
            continue
        for institution in institutions:
            if not isinstance(institution, dict):
                continue
            name = str(institution.get("display_name") or "").strip()
            if not name:
                continue
            inst_id = str(institution.get("id") or "")
            key = inst_id or _institution_key(name)
            if key in seen:
                continue
            seen.add(key)
            geo, geo_source = _institution_geo_from_openalex(institution, email, institution_cache)
            country = str(geo.get("country") or geo.get("country_code") or institution.get("country_code") or "")
            rows.append(
                {
                    "record_id": record_index + 1,
                    "source_title": row.get("title", ""),
                    "year": row.get("year", ""),
                    "institution": name,
                    "normalized_institution": name,
                    "affiliation": "",
                    "city": str(geo.get("city") or ""),
                    "state_or_region": str(geo.get("region") or ""),
                    "country": country,
                    "latitude": geo.get("latitude", ""),
                    "longitude": geo.get("longitude", ""),
                    "match_confidence": "high" if match_method == "openalex_doi" else "medium",
                    "geocode_source": geo_source,
                    "_enrichment": {
                        "author_name": author_name,
                        "institution_id": inst_id,
                        "institution_openalex_id": inst_id,
                        "institution_ror": str(institution.get("ror") or ""),
                        "raw_affiliation_string": raw_affiliation_string,
                        "matched_work_id": str(work.get("id") or ""),
                        "match_method": match_method,
                        "matched_title": str(work.get("display_name") or ""),
                        "matched_year": str(work.get("publication_year") or ""),
                    },
                }
            )
    return rows


def _openalex_citation_count(work: dict[str, object] | None) -> int | None:
    if not isinstance(work, dict):
        return None
    value = pd.to_numeric(pd.Series([work.get("cited_by_count")]), errors="coerce").iloc[0]
    return None if pd.isna(value) else int(value)


def _run_openalex_institution_enrichment(
    dataframe: pd.DataFrame,
    email: str,
    cache_path: Path,
    use_cache: bool,
    rebuild_cache: bool,
    skipped_record_ids: set[int] | None = None,
) -> tuple[list[dict[str, object]], dict[str, int], list[dict[str, object]]]:
    counters = Counter()
    cache: dict[str, object] = {}
    institution_cache_path = cache_path.with_name(f"{cache_path.stem}_institutions.json")
    institution_cache: dict[str, object] = {}
    if use_cache and not rebuild_cache and cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            cache = loaded if isinstance(loaded, dict) else {}
        except Exception:
            cache = {}
    if use_cache and not rebuild_cache and institution_cache_path.exists():
        try:
            loaded_institutions = json.loads(institution_cache_path.read_text(encoding="utf-8"))
            institution_cache = loaded_institutions if isinstance(loaded_institutions, dict) else {}
        except Exception:
            institution_cache = {}
    rows: list[dict[str, object]] = []
    debug_rows: list[dict[str, object]] = []
    skipped_record_ids = skipped_record_ids or set()
    start_time = time.time()
    total_records = len(dataframe)
    for record_index, row in dataframe.reset_index(drop=True).iterrows():
        record_id = record_index + 1
        counters["records_eligible"] += 1
        doi = _normalize_debug_doi(row.get("doi"))
        title = str(row.get("title") or "").strip()
        if doi:
            counters["records_with_doi"] += 1
        if title:
            counters["records_with_title"] += 1
        if record_id in skipped_record_ids:
            counters["records_not_attempted"] += 1
            debug_rows.append(_attempt_debug_row(row, record_index, "openalex", attempted=False, not_attempted_reason="prior source already matched"))
            continue
        if not doi and not title:
            counters["records_not_attempted"] += 1
            counters["records_skipped_missing_identifier"] += 1
            debug_rows.append(_attempt_debug_row(row, record_index, "openalex", attempted=False, not_attempted_reason="missing DOI/title"))
            continue
        cache_key = _openalex_cache_key(row)
        work: dict[str, object] | None = None
        match_method = "openalex_doi" if doi else "openalex_title"
        endpoint_type = "works filter doi" if doi else "works search title"
        identifier_used = doi or title
        response_status = ""
        if use_cache and cache_key in cache:
            cached = cache.get(cache_key)
            if isinstance(cached, dict):
                work = cached
                counters["loaded_from_cache"] += 1
                response_status = "cache"
        if work is None:
            params = {"per-page": "1"}
            if doi:
                params["filter"] = f"doi:{doi}"
                counters["records_attempted_by_doi"] += 1
            else:
                params["search"] = title
                counters["records_attempted_by_title"] += 1
            try:
                counters["records_attempted"] += 1
                payload, status = _openalex_request(params, email)
                response_status = str(status)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    counters["rate_limited_retried"] += 1
                    time.sleep(2)
                    try:
                        payload, status = _openalex_request(params, email)
                        response_status = str(status)
                    except Exception as retry_exc:
                        counters["records_failed_after_request"] += 1
                        counters["request_failures"] += 1
                        debug_rows.append(_attempt_debug_row(row, record_index, "openalex", attempted=True, request_identifier_used=identifier_used, endpoint_type=endpoint_type, response_status="HTTP 429", match_status="request failed", failure_reason=_safe_failure_reason(retry_exc)))
                        continue
                else:
                    counters["records_failed_after_request"] += 1
                    counters["request_failures"] += 1
                    debug_rows.append(_attempt_debug_row(row, record_index, "openalex", attempted=True, request_identifier_used=identifier_used, endpoint_type=endpoint_type, response_status=f"HTTP {exc.code}", match_status="request failed", failure_reason=_safe_failure_reason(exc)))
                    continue
            except Exception as exc:
                counters["records_failed_after_request"] += 1
                counters["request_failures"] += 1
                debug_rows.append(_attempt_debug_row(row, record_index, "openalex", attempted=True, request_identifier_used=identifier_used, endpoint_type=endpoint_type, match_status="request failed", failure_reason=_safe_failure_reason(exc)))
                continue
            results = payload.get("results") if isinstance(payload, dict) else []
            first = results[0] if isinstance(results, list) and results else None
            if isinstance(first, dict):
                work = first
                if use_cache:
                    cache[cache_key] = work
            else:
                counters["records_returned_no_institutions"] += 1
                debug_rows.append(_attempt_debug_row(row, record_index, "openalex", attempted=True, request_identifier_used=identifier_used, endpoint_type=endpoint_type, response_status=response_status, match_status="no work match", institution_count=0))
                continue
        if doi:
            counters["matched_by_doi"] += 1
        else:
            counters["matched_by_title"] += 1
        extracted = _institution_rows_from_openalex_work(work, row, record_index, match_method, email, institution_cache)
        if extracted:
            counters["records_matched"] += 1
        else:
            counters["records_returned_no_institutions"] += 1
        counters["institution_links_extracted"] += len(extracted)
        counters["institutions_with_coordinates"] += sum(1 for item in extracted if item.get("latitude") not in {"", None} and item.get("longitude") not in {"", None})
        counters["institutions_without_coordinates"] += sum(1 for item in extracted if item.get("latitude") in {"", None} or item.get("longitude") in {"", None})
        loaded_from_cache = response_status == "cache"
        if loaded_from_cache:
            counters["records_not_attempted"] += 1
        debug_rows.append(
            _attempt_debug_row(
                row,
                record_index,
                "openalex",
                attempted=not loaded_from_cache,
                not_attempted_reason="loaded from cache" if loaded_from_cache else "",
                request_identifier_used=identifier_used,
                endpoint_type=endpoint_type,
                response_status=response_status,
                match_status="matched" if extracted else "no institutions",
                institution_count=len(extracted),
            )
        )
        rows.extend(extracted)
        if use_cache and record_id % 25 == 0:
            institution_cache_path.write_text(json.dumps(institution_cache, indent=2) + "\n", encoding="utf-8")
            LOGGER.info("OpenAlex institution detail cache saved after %d records.", record_id)
        if record_id == total_records or record_id % 25 == 0:
            elapsed = max(time.time() - start_time, 0.1)
            rate = record_id / elapsed
            remaining = int((total_records - record_id) / rate) if rate > 0 else 0
            LOGGER.info(
                "OpenAlex enrichment: %d/%d records checked, %d institution links extracted, %d citation counts found, elapsed %.1fs, estimated remaining %ds.",
                record_id,
                total_records,
                counters.get("institution_links_extracted", 0),
                counters.get("citation_counts_extracted", 0),
                elapsed,
                remaining,
            )
    if use_cache:
        cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
        institution_cache_path.write_text(json.dumps(institution_cache, indent=2) + "\n", encoding="utf-8")
    return rows, dict(counters), debug_rows


def _scopus_request(identifier_type: str, identifier: str, api_key: str, inst_token: str, base_url: str) -> tuple[dict[str, object], int]:
    path_type = "eid" if identifier_type == "eid" else "doi"
    url = f"{base_url.rstrip('/')}/abstract/{path_type}/{urllib.parse.quote(identifier)}"
    request = urllib.request.Request(url, headers={"X-ELS-APIKey": api_key, "Accept": "application/json"})
    if inst_token:
        request.add_header("X-ELS-Insttoken", inst_token)
    with _api_urlopen(request, timeout=API_REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8")), int(getattr(response, "status", 0) or 0)


def _scopus_affiliations(payload: dict[str, object]) -> list[dict[str, object]]:
    root = payload.get("abstracts-retrieval-response") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return []
    affiliations = root.get("affiliation") or root.get("affiliations")
    if isinstance(affiliations, dict):
        affiliations = [affiliations]
    if not isinstance(affiliations, list):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for affiliation in affiliations:
        if not isinstance(affiliation, dict):
            continue
        name = str(affiliation.get("affilname") or affiliation.get("name") or "").strip()
        if not name:
            continue
        key = _institution_key(name)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "institution": name,
                "city": str(affiliation.get("affiliation-city") or affiliation.get("city") or ""),
                "country": str(affiliation.get("affiliation-country") or affiliation.get("country") or ""),
                "institution_id": str(affiliation.get("@id") or affiliation.get("afid") or ""),
            }
        )
    return rows


def _scopus_citation_count(payload: dict[str, object]) -> int | None:
    root = payload.get("abstracts-retrieval-response") if isinstance(payload, dict) else {}
    if not isinstance(root, dict):
        return None
    core = root.get("coredata") if isinstance(root.get("coredata"), dict) else {}
    candidates = [
        core.get("citedby-count"),
        core.get("citedby_count"),
        root.get("citedby-count"),
        root.get("citedby_count"),
    ]
    for value in candidates:
        parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
        if not pd.isna(parsed):
            return int(parsed)
    return None


def _run_scopus_institution_enrichment(
    dataframe: pd.DataFrame,
    api_key: str,
    inst_token: str,
    base_url: str,
) -> tuple[list[dict[str, object]], dict[str, int], list[dict[str, object]]]:
    counters = Counter()
    rows: list[dict[str, object]] = []
    debug_rows: list[dict[str, object]] = []
    start_time = time.time()
    total_records = len(dataframe)
    for record_index, row in dataframe.reset_index(drop=True).iterrows():
        counters["records_eligible"] += 1
        scopus_url, scopus_eid, _kind, success = _extract_scopus_identifier(row.get("ris_identifier_blob"))
        doi = _normalize_debug_doi(row.get("doi"))
        if doi:
            counters["records_with_doi"] += 1
        if scopus_eid:
            counters["records_with_eid"] += 1
        if scopus_eid:
            identifier_type = "eid"
            identifier = scopus_eid
        elif doi:
            identifier_type = "doi"
            identifier = doi
        else:
            counters["records_not_attempted"] += 1
            counters["records_skipped_missing_identifier"] += 1
            debug_rows.append(_attempt_debug_row(row, record_index, "scopus", attempted=False, not_attempted_reason="missing DOI/EID"))
            continue
        endpoint_type = f"abstract retrieval by {identifier_type}"
        try:
            counters["records_attempted"] += 1
            counters[f"records_attempted_by_{identifier_type}"] += 1
            payload, status = _scopus_request(identifier_type, identifier, api_key, inst_token, base_url)
            response_status = str(status)
        except urllib.error.HTTPError as exc:
            counters["records_failed_after_request"] += 1
            counters["request_failures"] += 1
            if exc.code in {401, 403}:
                counters["auth_failed"] += 1
            failure_reason = _safe_failure_reason(exc)
            if exc.code in {401, 403}:
                failure_reason = f"InstToken required or entitlement missing; {failure_reason}"
            debug_rows.append(_attempt_debug_row(row, record_index, "scopus", attempted=True, request_identifier_used=identifier, endpoint_type=endpoint_type, response_status=f"HTTP {exc.code}", match_status="request failed", failure_reason=failure_reason))
            continue
        except Exception as exc:
            counters["records_failed_after_request"] += 1
            counters["request_failures"] += 1
            debug_rows.append(_attempt_debug_row(row, record_index, "scopus", attempted=True, request_identifier_used=identifier, endpoint_type=endpoint_type, match_status="request failed", failure_reason=_safe_failure_reason(exc)))
            continue
        affiliations = _scopus_affiliations(payload)
        if not affiliations:
            counters["records_returned_no_institutions"] += 1
            debug_rows.append(_attempt_debug_row(row, record_index, "scopus", attempted=True, request_identifier_used=identifier, endpoint_type=endpoint_type, response_status=response_status, match_status="no institutions", institution_count=0))
            continue
        counters["records_matched"] += 1
        debug_rows.append(_attempt_debug_row(row, record_index, "scopus", attempted=True, request_identifier_used=identifier, endpoint_type=endpoint_type, response_status=response_status, match_status="matched", institution_count=len(affiliations)))
        for affiliation in affiliations:
            counters["institution_links_extracted"] += 1
            rows.append(
                {
                    "record_id": record_index + 1,
                    "source_title": row.get("title", ""),
                    "year": row.get("year", ""),
                    "institution": affiliation.get("institution", ""),
                    "normalized_institution": affiliation.get("institution", ""),
                    "affiliation": "",
                    "city": affiliation.get("city", ""),
                    "state_or_region": "",
                    "country": affiliation.get("country", ""),
                    "latitude": "",
                    "longitude": "",
                    "match_confidence": "high",
                    "geocode_source": "scopus",
                    "_enrichment": {
                        "author_name": "",
                        "institution_id": affiliation.get("institution_id", ""),
                        "institution_openalex_id": "",
                        "institution_ror": "",
                        "raw_affiliation_string": "",
                        "matched_work_id": scopus_eid or scopus_url,
                        "match_method": f"scopus_{identifier_type}",
                        "matched_title": row.get("title", ""),
                        "matched_year": row.get("year", ""),
                    },
                }
            )
        if record_index + 1 == total_records or (record_index + 1) % 25 == 0:
            elapsed = max(time.time() - start_time, 0.1)
            rate = (record_index + 1) / elapsed
            remaining = int((total_records - (record_index + 1)) / rate) if rate > 0 else 0
            LOGGER.info(
                "Scopus enrichment: %d/%d records checked, %d institution links extracted, %d citation counts found, elapsed %.1fs, estimated remaining %ds.",
                record_index + 1,
                total_records,
                counters.get("institution_links_extracted", 0),
                counters.get("citation_counts_extracted", 0),
                elapsed,
                remaining,
            )
    return rows, dict(counters), debug_rows


INSTITUTION_KEYWORDS = (
    "university",
    "univ",
    "college",
    "institute",
    "hospital",
    "medical center",
    "centre",
    "clinic",
    "laboratory",
    "lab",
    "school",
    "foundation",
    "department",
    "ministry",
    "agency",
    "organization",
    "organisation",
)


def _extract_institutions_from_affiliations(value: object) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for affiliation in _split_multi_value(value):
        parts = [part.strip(" .") for part in affiliation.split(",") if part.strip(" .")]
        institution = next((part for part in parts if any(keyword in part.lower() for keyword in INSTITUTION_KEYWORDS)), "")
        if institution:
            rows.append((institution, affiliation))
    return rows


US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "district of columbia", "florida", "georgia", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
    "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
}
US_STATE_ABBREVIATIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
US_STATE_NAME_TO_ABBR = {value.lower(): key for key, value in US_STATE_ABBREVIATIONS.items()}
INSTITUTION_LOCATION_WORDS = {
    "university", "hospital", "center", "centre", "college", "school", "department", "dept",
    "institute", "institution", "clinic", "medical", "medicine", "health", "sciences",
    "foundation", "laboratory", "labs", "division", "faculty", "program", "service",
}
COUNTRY_ALIAS_TO_NAME = {
    "de": "Germany", "deu": "Germany", "deutschland": "Germany", "germany": "Germany",
    "us": "United States", "usa": "United States", "united states": "United States",
    "united states of america": "United States", "uk": "United Kingdom", "gb": "United Kingdom",
    "gbr": "United Kingdom", "united kingdom": "United Kingdom", "england": "United Kingdom",
    "ca": "Canada", "can": "Canada", "canada": "Canada",
}
_GEONAMES_CITY_INDEX: dict[tuple[str, str, str], dict[str, str]] | None = None
_COUNTRY_INFO_CACHE: dict[str, dict[str, str]] | None = None


def _country_info_lookup() -> dict[str, dict[str, str]]:
    global _COUNTRY_INFO_CACHE
    if _COUNTRY_INFO_CACHE is not None:
        return _COUNTRY_INFO_CACHE
    path = ROOT / "data" / "reference" / "maps" / "gazetteers" / "cities" / "geonames" / "countryInfo.txt"
    lookup: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 5:
                    continue
                iso2, iso3, name = parts[0].upper(), parts[1].upper(), parts[4]
                record = {"iso2": iso2, "iso3": iso3, "name": name}
                lookup[iso2.lower()] = record
                lookup[iso3.lower()] = record
                lookup[_institution_key(name)] = record
    for alias, name in COUNTRY_ALIAS_TO_NAME.items():
        key = _institution_key(name)
        if key in lookup:
            lookup[alias] = lookup[key]
    _COUNTRY_INFO_CACHE = lookup
    return lookup


def _country_name_and_iso(value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    lookup = _country_info_lookup()
    record = lookup.get(text.lower()) or lookup.get(_institution_key(text))
    if record:
        return record["name"], record["iso3"]
    iso3 = _country_iso3(text)
    if iso3:
        record = lookup.get(iso3.lower())
        return (record["name"], iso3) if record else (text, iso3)
    return text, ""


def _clean_location_chunk(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip(" .;"))
    text = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", text).strip(" ,")
    return text


def _looks_like_institution_chunk(value: object) -> bool:
    words = set(re.findall(r"[a-z]+", str(value or "").lower()))
    return bool(words & INSTITUTION_LOCATION_WORDS)


def _parse_raw_affiliation_location(affiliation: object) -> dict[str, str]:
    text = str(affiliation or "").strip()
    if not text or text.lower() in {"nan", "none"}:
        return {}
    if ";" in text:
        for item in (part.strip() for part in text.split(";") if part.strip()):
            parsed = _parse_raw_affiliation_location(item)
            if parsed:
                return parsed
        return {}
    parts = [_clean_location_chunk(part) for part in text.split(",")]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return {}
    country = ""
    country_iso3 = ""
    state = ""
    city = ""
    tail = parts[-4:]
    last = tail[-1]
    country_name, iso3 = _country_name_and_iso(last)
    if iso3:
        country, country_iso3 = country_name, iso3
        tail = tail[:-1]
    if tail:
        state_candidate = tail[-1]
        state_key = state_candidate.upper()
        if state_key in US_STATE_ABBREVIATIONS:
            state = US_STATE_ABBREVIATIONS[state_key]
            country = country or "United States"
            country_iso3 = country_iso3 or "USA"
            tail = tail[:-1]
        elif state_candidate.lower() in US_STATE_NAMES:
            state = state_candidate
            country = country or "United States"
            country_iso3 = country_iso3 or "USA"
            tail = tail[:-1]
    for candidate in reversed(tail):
        cleaned = _clean_location_chunk(candidate)
        if not cleaned or _looks_like_institution_chunk(cleaned):
            continue
        if cleaned.lower() in US_STATE_NAMES or cleaned.upper() in US_STATE_ABBREVIATIONS:
            continue
        if _country_name_and_iso(cleaned)[1]:
            continue
        city = cleaned
        break
    if city and (country or state):
        return {
            "city": city,
            "state_or_region": state,
            "country": country or "United States",
            "country_iso3": country_iso3 or _country_iso3(country),
            "method": "raw_affiliation_parse",
            "confidence": "high" if country_iso3 else "medium",
        }
    return {}


def _geonames_city_index() -> dict[tuple[str, str, str], dict[str, str]]:
    global _GEONAMES_CITY_INDEX
    if _GEONAMES_CITY_INDEX is not None:
        return _GEONAMES_CITY_INDEX
    path = ROOT / "data" / "reference" / "maps" / "gazetteers" / "cities" / "geonames" / "cities5000.txt"
    index: dict[tuple[str, str, str], dict[str, str]] = {}
    country_lookup = _country_info_lookup()
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 15:
                    continue
                name, ascii_name, alternates = parts[1], parts[2], parts[3]
                lat, lon, iso2, admin1 = parts[4], parts[5], parts[8].upper(), parts[10]
                population = int(parts[14] or 0) if str(parts[14] or "").isdigit() else 0
                country = country_lookup.get(iso2.lower(), {}).get("name", "")
                iso3 = country_lookup.get(iso2.lower(), {}).get("iso3", "")
                record = {
                    "city": name,
                    "state_or_region": admin1,
                    "country": country,
                    "country_iso3": iso3,
                    "latitude": lat,
                    "longitude": lon,
                    "geocode_source": "geonames_cities5000",
                    "confidence": "high",
                    "population": str(population),
                }
                names = {name, ascii_name, *(item for item in alternates.split(",") if item)}
                for candidate in names:
                    city_key = _institution_key(candidate)
                    if not city_key:
                        continue
                    for country_key in {iso2.lower(), iso3.lower(), _institution_key(country)}:
                        key = (city_key, country_key, admin1.lower())
                        existing = index.get(key)
                        if not existing or int(existing.get("population") or 0) < population:
                            index[key] = record
                        key_no_admin = (city_key, country_key, "")
                        existing = index.get(key_no_admin)
                        if not existing or int(existing.get("population") or 0) < population:
                            index[key_no_admin] = record
    _GEONAMES_CITY_INDEX = index
    return index


def _load_city_geocode_cache(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    if not path.exists():
        return {}
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    cache: dict[tuple[str, str, str], dict[str, str]] = {}
    for _, row in frame.iterrows():
        key = (_institution_key(row.get("city")), _institution_key(row.get("country_iso3") or row.get("country")), _institution_key(row.get("state_or_region")))
        cache[key] = {column: str(row.get(column) or "") for column in frame.columns}
    return cache


def _write_city_geocode_cache(path: Path, cache: dict[tuple[str, str, str], dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(cache.values())
    pd.DataFrame(
        rows,
        columns=["city", "state_or_region", "country", "country_iso3", "latitude", "longitude", "geocode_source", "confidence", "failure_reason"],
    ).to_csv(path, index=False)


def _geocode_city(
    city: str,
    state_or_region: str,
    country: str,
    country_iso3: str,
    cache: dict[tuple[str, str, str], dict[str, str]],
) -> dict[str, str]:
    city = _clean_location_chunk(city)
    country_name, iso3 = _country_name_and_iso(country_iso3 or country)
    country = country_name or country
    country_iso3 = iso3 or country_iso3
    state_key = state_or_region.upper() if len(state_or_region) == 2 else US_STATE_NAME_TO_ABBR.get(state_or_region.lower(), state_or_region)
    key = (_institution_key(city), _institution_key(country_iso3 or country), _institution_key(state_key))
    if key in cache:
        return cache[key]
    index = _geonames_city_index()
    match = (
        index.get(key)
        or index.get((_institution_key(city), _institution_key(country_iso3 or country), ""))
        or index.get((_institution_key(city), _institution_key(country), _institution_key(state_key)))
        or index.get((_institution_key(city), _institution_key(country), ""))
    )
    if match:
        result = {
            "city": city,
            "state_or_region": state_or_region or match.get("state_or_region", ""),
            "country": country or match.get("country", ""),
            "country_iso3": country_iso3 or match.get("country_iso3", ""),
            "latitude": match.get("latitude", ""),
            "longitude": match.get("longitude", ""),
            "geocode_source": match.get("geocode_source", "geonames_cities5000"),
            "confidence": match.get("confidence", "high"),
            "failure_reason": "",
        }
    else:
        result = {
            "city": city,
            "state_or_region": state_or_region,
            "country": country,
            "country_iso3": country_iso3,
            "latitude": "",
            "longitude": "",
            "geocode_source": "",
            "confidence": "",
            "failure_reason": "city/country not found in local GeoNames cities5000",
        }
    cache[key] = result
    return result


def _ror_id(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    return text.rsplit("/", 1)[-1]


def _load_ror_cache(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_ror_cache(path: Path, cache: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _extract_ror_location(payload: dict[str, object]) -> dict[str, str]:
    locations = payload.get("locations")
    if isinstance(locations, list) and locations:
        location = next((item for item in locations if isinstance(item, dict) and item.get("geonames_details")), locations[0])
        details = location.get("geonames_details") if isinstance(location, dict) and isinstance(location.get("geonames_details"), dict) else {}
        if details:
            country_name, iso3 = _country_name_and_iso(details.get("country_code") or details.get("country_name"))
            return {
                "city": str(details.get("name") or details.get("city") or ""),
                "state_or_region": str(details.get("admin1_name") or details.get("admin1_code") or ""),
                "country": country_name or str(details.get("country_name") or ""),
                "country_iso3": iso3,
                "latitude": str(details.get("lat") or details.get("latitude") or ""),
                "longitude": str(details.get("lng") or details.get("longitude") or ""),
            }
    addresses = payload.get("addresses")
    if isinstance(addresses, list) and addresses:
        address = next((item for item in addresses if isinstance(item, dict) and item.get("lat") and item.get("lng")), addresses[0])
        if isinstance(address, dict):
            country = payload.get("country") if isinstance(payload.get("country"), dict) else {}
            country_name, iso3 = _country_name_and_iso(country.get("country_code") or country.get("country_name") if isinstance(country, dict) else "")
            return {
                "city": str(address.get("city") or ""),
                "state_or_region": str(address.get("state") or ""),
                "country": country_name,
                "country_iso3": iso3,
                "latitude": str(address.get("lat") or ""),
                "longitude": str(address.get("lng") or ""),
            }
    country = payload.get("country") if isinstance(payload.get("country"), dict) else {}
    country_name, iso3 = _country_name_and_iso(country.get("country_code") or country.get("country_name") if isinstance(country, dict) else "")
    return {"city": "", "state_or_region": "", "country": country_name, "country_iso3": iso3, "latitude": "", "longitude": ""}


def _resolve_ror_location(ror: str, cache: dict[str, dict[str, object]], cache_path: Path, lookup_count: int) -> dict[str, str]:
    rid = _ror_id(ror)
    if not rid:
        return {}
    cached = cache.get(rid)
    if isinstance(cached, dict):
        location = cached.get("location")
        return location if isinstance(location, dict) else {}
    disabled = cache.get("__live_lookup_disabled__")
    if isinstance(disabled, dict):
        cache[rid] = {
            "status": "failed",
            "failure_reason": str(disabled.get("failure_reason") or "ROR live lookup disabled after repeated failures"),
            "failure_category": str(disabled.get("failure_category") or "network_or_client"),
            "location": {},
        }
        return {}
    url = f"https://api.ror.org/organizations/{urllib.parse.quote(rid)}"
    failure = ""
    for attempt in range(1, INSTITUTION_DETAIL_MAX_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "DansBibGUI institution location resolver"})
            with _api_urlopen(request, timeout=INSTITUTION_DETAIL_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            location = _extract_ror_location(payload if isinstance(payload, dict) else {})
            cache[rid] = {"status": "ok", "location": location}
            if lookup_count % 25 == 0:
                _write_ror_cache(cache_path, cache)
                LOGGER.info("ROR institution location lookups completed: %d", lookup_count)
            return location
        except Exception as exc:
            failure = _safe_failure_reason(exc)
            category = _failure_category(exc)
            LOGGER.info(
                "ROR lookup failed for %s attempt %d/%d category=%s: %s",
                rid,
                attempt,
                INSTITUTION_DETAIL_MAX_RETRIES,
                category,
                failure,
            )
    cache[rid] = {"status": "failed", "failure_reason": failure, "failure_category": category if "category" in locals() else "", "location": {}}
    failure_meta = cache.get("__failure_meta__")
    failure_count = int(failure_meta.get("count", 0)) if isinstance(failure_meta, dict) else 0
    failure_count += 1
    cache["__failure_meta__"] = {"count": failure_count, "last_failure_reason": failure, "last_failure_category": category if "category" in locals() else ""}
    if failure_count >= 3:
        cache["__live_lookup_disabled__"] = {
            "failure_reason": failure or "ROR live lookup disabled after repeated failures",
            "failure_category": category if "category" in locals() else "",
        }
        LOGGER.info(
            "ROR live lookups disabled after %d failed institutions category=%s: %s",
            failure_count,
            category if "category" in locals() else "",
            failure,
        )
    if lookup_count % 25 == 0:
        _write_ror_cache(cache_path, cache)
    return {}


def _has_text(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and text.lower() not in {"nan", "none", "null", "na", "n/a"})


def _positive_int(value: object) -> bool:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return bool(pd.notna(number) and int(number) > 0)


def _same_city(left: object, right: object) -> bool:
    return bool(_institution_key(left) and _institution_key(left) == _institution_key(right))


def _same_country(left: object, right: object) -> bool:
    _, left_iso = _country_name_and_iso(left)
    _, right_iso = _country_name_and_iso(right)
    if left_iso and right_iso:
        return left_iso == right_iso
    return bool(_institution_key(left) and _institution_key(left) == _institution_key(right))


def _clean_openalex_geo_country(value: object) -> tuple[str, str]:
    return _country_name_and_iso(value)


def _infer_city_from_affiliation(affiliation: object) -> tuple[str, str, str, str]:
    parts = [part.strip(" .") for part in str(affiliation or "").split(",") if part.strip(" .")]
    if len(parts) < 2:
        return "", "", "", "unmatched"
    for index, part in enumerate(parts[1:], start=1):
        lowered = part.lower()
        state_or_region = ""
        country = parts[-1] if len(parts) > index + 1 else ""
        if re.fullmatch(r"[A-Z]{2}", part):
            continue
        if index + 1 < len(parts):
            next_part = parts[index + 1]
            if re.fullmatch(r"[A-Z]{2}(?:\s+\d{5})?", next_part) or next_part.lower() in US_STATE_NAMES:
                state_or_region = re.sub(r"\s+\d{5}.*$", "", next_part)
                country = "USA"
        if lowered not in US_STATE_NAMES and not re.fullmatch(r"[A-Z]{2}(?:\s+\d{5})?", part):
            return part, state_or_region, country, "affiliation"
    return "", "", "", "unmatched"


def _not_attempted_rows(dataframe: pd.DataFrame, source: str, reason: str) -> list[dict[str, object]]:
    return [
        _attempt_debug_row(row, record_index, source, attempted=False, not_attempted_reason=reason)
        for record_index, row in dataframe.reset_index(drop=True).iterrows()
    ]


def _source_base_counts(dataframe: pd.DataFrame, source: str) -> dict[str, int]:
    counts = Counter()
    for _record_index, row in dataframe.reset_index(drop=True).iterrows():
        counts["records_eligible"] += 1
        doi = _normalize_debug_doi(row.get("doi"))
        title = str(row.get("title") or "").strip()
        _scopus_url, scopus_eid, _kind, _success = _extract_scopus_identifier(row.get("ris_identifier_blob"))
        if doi:
            counts["records_with_doi"] += 1
        if title:
            counts["records_with_title"] += 1
        if scopus_eid:
            counts["records_with_eid"] += 1
        if source == "scopus" and not doi and not scopus_eid:
            counts["records_skipped_missing_identifier"] += 1
        if source == "openalex" and not doi and not title:
            counts["records_skipped_missing_identifier"] += 1
    return dict(counts)


def _log_scopus_summary(counts: dict[str, int], fallback_count: int) -> None:
    LOGGER.info("Scopus enrichment summary:")
    LOGGER.info("Scopus DOI lookup enabled: yes")
    LOGGER.info("Scopus EID lookup enabled: yes")
    LOGGER.info("Eligible records: %d", counts.get("records_eligible", 0))
    LOGGER.info("Records with DOI: %d", counts.get("records_with_doi", 0))
    LOGGER.info("Records with EID: %d", counts.get("records_with_eid", 0))
    LOGGER.info("Records attempted: %d", counts.get("records_attempted", 0))
    LOGGER.info("Records attempted by DOI: %d", counts.get("records_attempted_by_doi", 0))
    LOGGER.info("Records attempted by EID: %d", counts.get("records_attempted_by_eid", 0))
    LOGGER.info("Records not attempted: %d", counts.get("records_not_attempted", 0))
    LOGGER.info("Skipped missing DOI/EID: %d", counts.get("records_skipped_missing_identifier", 0))
    LOGGER.info("API/auth failures: %d", counts.get("auth_failed", 0))
    LOGGER.info("Request failures: %d", counts.get("request_failures", 0))
    LOGGER.info("Matched records: %d", counts.get("records_matched", 0))
    LOGGER.info("Records returned no institution data: %d", counts.get("records_returned_no_institutions", 0))
    LOGGER.info("Institution links extracted: %d", counts.get("institution_links_extracted", 0))
    LOGGER.info("Sent to OpenAlex fallback: %d", fallback_count)


def _log_openalex_summary(counts: dict[str, int]) -> None:
    LOGGER.info("OpenAlex enrichment summary:")
    LOGGER.info("OpenAlex fallback enabled: yes")
    LOGGER.info("Eligible fallback records: %d", counts.get("records_eligible", 0))
    LOGGER.info("Records with DOI: %d", counts.get("records_with_doi", 0))
    LOGGER.info("Records with title: %d", counts.get("records_with_title", 0))
    LOGGER.info("Records attempted: %d", counts.get("records_attempted", 0))
    LOGGER.info("OpenAlex DOI requests attempted: %d", counts.get("records_attempted_by_doi", 0))
    LOGGER.info("OpenAlex title requests attempted: %d", counts.get("records_attempted_by_title", 0))
    LOGGER.info("Records not attempted: %d", counts.get("records_not_attempted", 0))
    LOGGER.info("Skipped missing DOI/title: %d", counts.get("records_skipped_missing_identifier", 0))
    LOGGER.info("Request failures: %d", counts.get("request_failures", 0))
    LOGGER.info("Matched by DOI: %d", counts.get("matched_by_doi", 0))
    LOGGER.info("Matched by title: %d", counts.get("matched_by_title", 0))
    LOGGER.info("Records returned no institution data: %d", counts.get("records_returned_no_institutions", 0))
    LOGGER.info("Institution links extracted: %d", counts.get("institution_links_extracted", 0))
    LOGGER.info("Institutions with coordinates: %d", counts.get("institutions_with_coordinates", 0))
    LOGGER.info("Institutions without coordinates: %d", counts.get("institutions_without_coordinates", 0))


def _log_scopus_smoke_test(dataframe: pd.DataFrame, api_key: str, inst_token: str, base_url: str) -> bool:
    test_row = next((row for _, row in dataframe.iterrows() if _normalize_debug_doi(row.get("doi"))), None)
    LOGGER.info("Scopus smoke test selected DOI present: %s", "yes" if test_row is not None else "no")
    if test_row is None:
        LOGGER.info("Scopus smoke test request built: no")
        return False
    doi = _normalize_debug_doi(test_row.get("doi"))
    LOGGER.info("Scopus smoke test selected DOI: %s", doi)
    LOGGER.info("Scopus smoke test request built: %s", "yes" if doi else "no")
    LOGGER.info("Scopus smoke test endpoint type: abstract retrieval by DOI")
    LOGGER.info("Scopus smoke test request attempted: yes")
    try:
        payload, status = _scopus_request("doi", doi, api_key, inst_token, base_url)
        affiliations = _scopus_affiliations(payload)
        LOGGER.info("Scopus smoke test response status: %s", status)
        LOGGER.info("Scopus smoke test parsed affiliation/institution count: %d", len(affiliations))
        return True
    except Exception as exc:
        LOGGER.info("Scopus smoke test response status: request failed")
        LOGGER.info("Scopus smoke test parsed affiliation/institution count: 0")
        LOGGER.info("Scopus smoke test failure reason: %s", _safe_failure_reason(exc))
        _log_request_exception("Scopus smoke test", exc, "api.elsevier.com", "abstract retrieval by DOI", API_REQUEST_TIMEOUT_SECONDS)
        _log_diagnostic_command()
        return False


def _log_openalex_smoke_test(dataframe: pd.DataFrame, email: str) -> bool:
    test_row = next((row for _, row in dataframe.iterrows() if _normalize_debug_doi(row.get("doi"))), None)
    LOGGER.info("OpenAlex smoke test selected DOI present: %s", "yes" if test_row is not None else "no")
    if test_row is None:
        LOGGER.info("OpenAlex smoke test request built: no")
        return False
    doi = _normalize_debug_doi(test_row.get("doi"))
    LOGGER.info("OpenAlex smoke test selected DOI: %s", doi)
    LOGGER.info("OpenAlex smoke test request built: %s", "yes" if doi else "no")
    LOGGER.info("OpenAlex smoke test request attempted: yes")
    try:
        payload, status = _openalex_request({"per-page": "1", "filter": f"doi:{doi}"}, email)
        results = payload.get("results") if isinstance(payload, dict) else []
        work = results[0] if isinstance(results, list) and results else None
        count = len(_institution_rows_from_openalex_work(work, test_row, 0, "openalex_doi")) if isinstance(work, dict) else 0
        LOGGER.info("OpenAlex smoke test response status: %s", status)
        LOGGER.info("OpenAlex smoke test matched work: %s", "yes" if isinstance(work, dict) else "no")
        LOGGER.info("OpenAlex smoke test parsed authorships/institution count: %d", count)
        return True
    except Exception as exc:
        LOGGER.info("OpenAlex smoke test response status: request failed")
        LOGGER.info("OpenAlex smoke test matched work: no")
        LOGGER.info("OpenAlex smoke test parsed authorships/institution count: 0")
        LOGGER.info("OpenAlex smoke test failure reason: %s", _safe_failure_reason(exc))
        _log_request_exception("OpenAlex smoke test", exc, "api.openalex.org", "works filter DOI", API_REQUEST_TIMEOUT_SECONDS)
        _log_diagnostic_command()
        return False


def run_citation_enrichment(
    dataframe: pd.DataFrame,
    outputs_dir: str,
    query: str,
    enrichment_source: str = "all",
    openalex_email: str = "",
    scopus_api_key: str = "",
    scopus_inst_token: str = "",
) -> pd.DataFrame:
    """Enrich citation counts from Scopus/OpenAlex and write audit outputs."""
    stem = _clean_filename(query)
    source_key = enrichment_source if enrichment_source in {"scopus", "openalex", "all"} else "all"
    scopus_key = scopus_api_key or os.getenv("SCOPUS_API_KEY", "")
    scopus_token = scopus_inst_token or os.getenv("SCOPUS_INST_TOKEN", "")
    openalex_email = openalex_email or os.getenv("OPENALEX_EMAIL", "")
    base_url = os.getenv("SCOPUS_BASE_URL", "https://api.elsevier.com/content")
    enriched = dataframe.copy()
    if "citations" not in enriched.columns:
        enriched["citations"] = 0
    rows: list[dict[str, object]] = []
    debug_rows: list[dict[str, object]] = []
    start_time = time.time()
    counts_found = 0
    total_records = len(enriched)
    for record_index, row in enriched.reset_index(drop=True).iterrows():
        record_id = record_index + 1
        title = str(row.get("title") or "").strip()
        year = row.get("year", "")
        doi = _normalize_debug_doi(row.get("doi"))
        scopus_url, scopus_eid, _kind, _success = _extract_scopus_identifier(row.get("ris_identifier_blob"))
        exported_raw = pd.to_numeric(pd.Series([row.get("citations")]), errors="coerce").iloc[0]
        exported_count = None if pd.isna(exported_raw) else int(exported_raw)
        scopus_attempted = "no"
        scopus_status = "not attempted"
        scopus_count: int | None = None
        openalex_attempted = "no"
        openalex_status = "not attempted"
        openalex_count: int | None = None
        match_confidence = ""
        matched_work_id = ""
        exclusion_reason = ""
        if source_key in {"all", "scopus"} and scopus_key and (scopus_eid or doi):
            identifier_type = "eid" if scopus_eid else "doi"
            identifier = scopus_eid or doi
            scopus_attempted = "yes"
            try:
                payload, _status = _scopus_request(identifier_type, identifier, scopus_key, scopus_token, base_url)
                scopus_count = _scopus_citation_count(payload)
                scopus_status = "matched" if scopus_count is not None else "matched_no_citation_count"
                if scopus_count is not None:
                    match_confidence = "high"
                    matched_work_id = scopus_eid or scopus_url
            except Exception as exc:
                scopus_status = f"request failed: {_safe_failure_reason(exc)}"
        elif source_key in {"all", "scopus"} and not scopus_key:
            scopus_status = "credentials missing"
        elif source_key in {"all", "scopus"}:
            scopus_status = "missing DOI/EID"
        if source_key in {"all", "openalex"} and (doi or title):
            openalex_attempted = "yes"
            params = {"per-page": "1"}
            endpoint_method = "openalex_doi" if doi else "openalex_title"
            if doi:
                params["filter"] = f"doi:{doi}"
            else:
                params["search"] = title
            try:
                payload, _status = _openalex_request(params, openalex_email)
                results = payload.get("results") if isinstance(payload, dict) else []
                work = results[0] if isinstance(results, list) and results else None
                if isinstance(work, dict):
                    openalex_count = _openalex_citation_count(work)
                    matched_work_id = matched_work_id or str(work.get("id") or "")
                    match_confidence = match_confidence or ("high" if doi else "medium")
                    openalex_status = "matched" if openalex_count is not None else "matched_no_citation_count"
                else:
                    openalex_status = "no work match"
            except Exception as exc:
                openalex_status = f"request failed: {_safe_failure_reason(exc)}"
        elif source_key in {"all", "openalex"}:
            openalex_status = "missing DOI/title"
        final_count: int | None = None
        final_source = ""
        lookup_method = ""
        priority_used = ""
        enrichment_used = ""
        if scopus_count is not None:
            final_count = scopus_count
            final_source = "scopus"
            lookup_method = "eid" if scopus_eid else "doi"
            priority_used = "1"
            enrichment_used = "scopus"
        elif openalex_count is not None:
            final_count = openalex_count
            final_source = "openalex"
            lookup_method = "doi" if doi else "title"
            priority_used = "2"
            enrichment_used = "openalex"
        elif exported_count is not None:
            final_count = exported_count
            final_source = "source_export"
            lookup_method = "existing citation field"
            priority_used = "3"
            enrichment_used = "source_export"
        else:
            exclusion_reason = "no trusted citation count returned"
        disagreement = (
            scopus_count is not None
            and openalex_count is not None
            and int(scopus_count) != int(openalex_count)
        )
        if final_count is not None:
            counts_found += 1
            enriched.at[record_index, "citations"] = int(final_count)
            enriched.at[record_index, "citation_available"] = True
            enriched.at[record_index, "citation_source"] = final_source
        else:
            enriched.at[record_index, "citation_available"] = False
        rows.append(
            {
                "record_id": record_id,
                "title": title,
                "year": year,
                "doi": doi,
                "scopus_eid": scopus_eid,
                "final_citation_count": "" if final_count is None else final_count,
                "final_citation_source": final_source,
                "final_citation_lookup_method": lookup_method,
                "scopus_citation_count": "" if scopus_count is None else scopus_count,
                "openalex_citation_count": "" if openalex_count is None else openalex_count,
                "citation_count_disagreement": "yes" if disagreement else "no",
                "match_confidence": match_confidence,
                "matched_work_id": matched_work_id,
                "enrichment_source_used": enrichment_used,
                "exclusion_reason": exclusion_reason,
            }
        )
        debug_rows.append(
            {
                "record_id": record_id,
                "title": title,
                "doi": doi,
                "scopus_lookup_attempted": scopus_attempted,
                "scopus_match_status": scopus_status,
                "scopus_citation_count": "" if scopus_count is None else scopus_count,
                "openalex_lookup_attempted": openalex_attempted,
                "openalex_match_status": openalex_status,
                "openalex_citation_count": "" if openalex_count is None else openalex_count,
                "final_citation_count": "" if final_count is None else final_count,
                "final_citation_source": final_source,
                "source_priority_used": priority_used,
                "disagreement_flag": "yes" if disagreement else "no",
                "exclusion_reason": exclusion_reason,
            }
        )
        if record_id == total_records or record_id % 25 == 0:
            elapsed = max(time.time() - start_time, 0.1)
            rate = record_id / elapsed
            remaining = int((total_records - record_id) / rate) if rate > 0 else 0
            LOGGER.info(
                "Citation enrichment: %d/%d records checked, %d citation counts found, elapsed %.1fs, estimated remaining %ds.",
                record_id,
                total_records,
                counts_found,
                elapsed,
                remaining,
            )
    output_path = os.path.join(outputs_dir, f"{stem}_citation_enrichment.csv")
    debug_path = PROCESSED_DIR / f"{stem}_citation_enrichment_debug.csv"
    pd.DataFrame(rows).to_csv(output_path, index=False)
    pd.DataFrame(debug_rows).to_csv(debug_path, index=False)
    source_summary = Counter(str(row["final_citation_source"]) for row in rows if row.get("final_citation_source"))
    LOGGER.info("Citation counts extracted: %d", sum(1 for row in rows if row.get("final_citation_count") != ""))
    LOGGER.info("Citation source summary: %s", dict(source_summary))
    LOGGER.info("Citation enrichment output: %s", output_path)
    LOGGER.info("Citation enrichment debug output: %s", debug_path)
    return enriched


def run_institution_extraction(
    dataframe: pd.DataFrame,
    outputs_dir: str,
    query: str,
    enrich_institutions: bool = False,
    enrichment_source: str = "all",
    openalex_email: str = "",
    scopus_api_key: str = "",
    scopus_inst_token: str = "",
    institution_enrichment_cache: bool = True,
    rebuild_institution_enrichment_cache: bool = False,
) -> None:
    """Write normalized institution and institution-city outputs."""
    stem = _clean_filename(query)
    native_usable, _debug_path = write_institution_extraction_debug(dataframe, stem)
    write_record_identifier_debug(dataframe, stem)
    aliases = _load_institution_aliases()
    geocache = _load_institution_geocache()
    rows: list[dict[str, object]] = []
    unmatched: Counter[str] = Counter()
    enrichment_rows: list[dict[str, object]] = []
    source_key = enrichment_source if enrichment_source in {"scopus", "openalex", "all"} else "all"
    scopus_key = scopus_api_key or os.getenv("SCOPUS_API_KEY", "")
    scopus_token = scopus_inst_token or os.getenv("SCOPUS_INST_TOKEN", "")
    openalex_email = openalex_email or os.getenv("OPENALEX_EMAIL", "")
    enrichment_network_failed = False
    if not native_usable and not enrich_institutions:
        LOGGER.info("Institution maps skipped: no usable native or enriched institution data found.")
        LOGGER.info("Institution enrichment disabled; no external APIs were queried.")
    elif enrich_institutions:
        if not native_usable:
            LOGGER.info("No usable affiliation/institution fields found in RIS export.")
        else:
            LOGGER.info("Native institution fields found; enrichment enabled to resolve affiliation city/country/coordinates.")
        LOGGER.info("Records eligible for enrichment: %d", len(dataframe))
        _write_api_connectivity_debug(stem)
        LOGGER.info("Scopus enrichment enabled: %s", "yes" if source_key in {"all", "scopus"} else "no")
        LOGGER.info("Scopus credentials configured: %s", "yes" if bool(scopus_key) else "no")
        scopus_debug_rows: list[dict[str, object]] = []
        scopus_matched_record_ids: set[int] = set()
        scopus_counts: dict[str, int] = {}
        if source_key in {"all", "scopus"}:
            if not scopus_key:
                LOGGER.info("Scopus enrichment skipped: API credentials not configured.")
                scopus_debug_rows = _not_attempted_rows(dataframe, "scopus", "credentials missing")
                scopus_counts = _source_base_counts(dataframe, "scopus")
                scopus_counts["records_not_attempted"] = len(dataframe)
                _log_scopus_summary(scopus_counts, len(dataframe) if source_key == "all" else 0)
            else:
                LOGGER.info("Scopus InstToken configured: %s", "yes" if bool(scopus_token) else "no")
                scopus_smoke_ok = _log_scopus_smoke_test(dataframe, scopus_key, scopus_token, os.getenv("SCOPUS_BASE_URL", "https://api.elsevier.com/content"))
                if not scopus_smoke_ok:
                    LOGGER.info("Scopus smoke test failed; skipping full Scopus enrichment loop for this run.")
                    scopus_debug_rows = _not_attempted_rows(dataframe, "scopus", "scopus_smoke_test_failed")
                    scopus_counts = _source_base_counts(dataframe, "scopus")
                    scopus_counts["records_not_attempted"] = len(dataframe)
                    _log_scopus_summary(scopus_counts, len(dataframe) if source_key == "all" else 0)
                else:
                    scopus_rows, scopus_counts, scopus_debug_rows = _run_scopus_institution_enrichment(
                        dataframe,
                        scopus_key,
                        scopus_token,
                        os.getenv("SCOPUS_BASE_URL", "https://api.elsevier.com/content"),
                    )
                    scopus_matched_record_ids = {int(item.get("record_id", 0)) for item in scopus_rows if item.get("record_id")}
                    _log_scopus_summary(scopus_counts, len(dataframe) - len(scopus_matched_record_ids) if source_key == "all" else 0)
                    rows.extend(scopus_rows)
                    for enriched in scopus_rows:
                        meta = enriched.get("_enrichment") if isinstance(enriched.get("_enrichment"), dict) else {}
                        enrichment_rows.append(
                            {
                                "record_id": enriched.get("record_id", ""),
                                "title": enriched.get("source_title", ""),
                                "year": enriched.get("year", ""),
                                "doi": "",
                                "scopus_url": "",
                                "scopus_eid": meta.get("matched_work_id", ""),
                                "enrichment_source": "scopus",
                                "source_priority": 1,
                                "matched_work_id": meta.get("matched_work_id", ""),
                                "match_method": meta.get("match_method", "scopus"),
                                "match_confidence": enriched.get("match_confidence", ""),
                                "matched_title": meta.get("matched_title", ""),
                                "matched_year": meta.get("matched_year", ""),
                                "author_name": "",
                                "institution_name": enriched.get("institution", ""),
                                "normalized_institution": enriched.get("normalized_institution", ""),
                                "institution_id": meta.get("institution_id", ""),
                                "institution_openalex_id": "",
                                "institution_ror": "",
                                "institution_country": enriched.get("country", ""),
                                "institution_city": enriched.get("city", ""),
                                "institution_latitude": "",
                                "institution_longitude": "",
                                "raw_affiliation_string": "",
                                "included_in_institution_map": "no",
                                "exclusion_reason": "missing institution coordinates",
                            }
                        )
        else:
            LOGGER.info("Scopus enrichment not requested.")
            scopus_debug_rows = _not_attempted_rows(dataframe, "scopus", "source disabled")
            scopus_counts = {"records_eligible": len(dataframe), "records_not_attempted": len(dataframe)}
            _log_scopus_summary(scopus_counts, len(dataframe) if source_key in {"all", "openalex"} else 0)
        scopus_debug_path = _write_attempt_debug(stem, "scopus", scopus_debug_rows)
        LOGGER.info("Scopus enrichment attempt debug output: %s", scopus_debug_path)
        LOGGER.info("OpenAlex enrichment enabled: %s", "yes" if source_key in {"all", "openalex"} else "no")
        LOGGER.info("OpenAlex email configured: %s", "yes" if bool(openalex_email) else "no")
        cache_path = PROCESSED_DIR / f"{stem}_openalex_enrichment_cache.json"
        openalex_debug_rows: list[dict[str, object]] = []
        openalex_network_failed = False
        if source_key in {"all", "openalex"}:
            openalex_cache_available = bool(institution_enrichment_cache and not rebuild_institution_enrichment_cache and cache_path.exists())
            openalex_smoke_ok = True if openalex_cache_available else _log_openalex_smoke_test(dataframe, openalex_email)
            if not openalex_smoke_ok:
                LOGGER.info("OpenAlex smoke test failed; skipping full OpenAlex enrichment loop for this run.")
                openalex_network_failed = True
                enrichment_network_failed = True
                openalex_debug_rows = _not_attempted_rows(dataframe, "openalex", "openalex_smoke_test_failed")
                openalex_counts = _source_base_counts(dataframe, "openalex")
                openalex_counts["records_not_attempted"] = len(dataframe)
                _log_openalex_summary(openalex_counts)
            else:
                enriched_rows, openalex_counts, openalex_debug_rows = _run_openalex_institution_enrichment(
                    dataframe,
                    openalex_email,
                    cache_path,
                    institution_enrichment_cache,
                    rebuild_institution_enrichment_cache,
                    skipped_record_ids=scopus_matched_record_ids,
                )
                _log_openalex_summary(openalex_counts)
                LOGGER.info("OpenAlex records loaded from cache: %d", openalex_counts.get("loaded_from_cache", 0))
                LOGGER.info("OpenAlex records rate-limited/retried: %d", openalex_counts.get("rate_limited_retried", 0))
                if openalex_cache_available:
                    LOGGER.info("OpenAlex smoke test skipped: using existing enrichment cache %s", cache_path)
                rows.extend(enriched_rows)
                for enriched in enriched_rows:
                    meta = enriched.get("_enrichment") if isinstance(enriched.get("_enrichment"), dict) else {}
                    enrichment_rows.append(
                        {
                            "record_id": enriched.get("record_id", ""),
                            "title": enriched.get("source_title", ""),
                            "year": enriched.get("year", ""),
                            "doi": "",
                            "scopus_url": "",
                            "scopus_eid": "",
                            "enrichment_source": "openalex",
                            "source_priority": 2 if source_key == "all" else 1,
                            "matched_work_id": meta.get("matched_work_id", ""),
                            "match_method": meta.get("match_method", "openalex"),
                            "match_confidence": enriched.get("match_confidence", ""),
                            "matched_title": meta.get("matched_title", ""),
                            "matched_year": meta.get("matched_year", ""),
                            "author_name": meta.get("author_name", ""),
                            "institution_name": enriched.get("institution", ""),
                            "normalized_institution": enriched.get("normalized_institution", ""),
                            "institution_id": meta.get("institution_id", ""),
                            "institution_openalex_id": meta.get("institution_openalex_id", ""),
                            "institution_ror": meta.get("institution_ror", ""),
                            "institution_country": enriched.get("country", ""),
                            "institution_city": enriched.get("city", ""),
                            "institution_latitude": enriched.get("latitude", ""),
                            "institution_longitude": enriched.get("longitude", ""),
                            "raw_affiliation_string": meta.get("raw_affiliation_string", ""),
                            "included_in_institution_map": "yes" if enriched.get("latitude") and enriched.get("longitude") else "no",
                            "exclusion_reason": "" if enriched.get("latitude") and enriched.get("longitude") else "missing institution coordinates",
                        }
                    )
        else:
            LOGGER.info("OpenAlex enrichment did not run: source disabled.")
            openalex_debug_rows = _not_attempted_rows(dataframe, "openalex", "source disabled")
            _log_openalex_summary({"records_eligible": len(dataframe), "records_not_attempted": len(dataframe)})
        openalex_debug_path = _write_attempt_debug(stem, "openalex", openalex_debug_rows)
        LOGGER.info("OpenAlex enrichment attempt debug output: %s", openalex_debug_path)
        if not rows:
            if openalex_network_failed:
                LOGGER.info("Institution enrichment could not run because API requests failed at the network/client level.")
            else:
                LOGGER.info("Institution maps skipped: enrichment was attempted but returned no usable institution metadata.")
    use_native = native_usable
    for record_index, row in dataframe.reset_index(drop=True).iterrows():
        if not use_native:
            continue
        raw_items = [(institution, "") for institution in _split_multi_value(row.get("institutions"))]
        raw_items.extend(_extract_institutions_from_affiliations(row.get("affiliations")))
        if not raw_items:
            continue
        seen: set[str] = set()
        for raw, affiliation in raw_items:
            normalized = _normalize_institution(raw, aliases)
            key = _institution_key(normalized)
            if not key or key in seen:
                continue
            seen.add(key)
            cache = geocache.get(key, {})
            city, admin1, country, city_source = _infer_city_from_affiliation(affiliation or row.get("affiliations"))
            if cache:
                city = str(cache.get("city") or city)
                admin1 = str(cache.get("admin1") or admin1)
                country = str(cache.get("country") or country)
            if not city and not cache:
                unmatched[raw] += 1
            rows.append(
                {
                    "record_id": record_index + 1,
                    "source_title": row.get("title", ""),
                    "year": row.get("year", ""),
                    "institution": raw,
                    "normalized_institution": normalized,
                    "affiliation": affiliation or row.get("affiliations", ""),
                    "city": city,
                    "state_or_region": admin1,
                    "country": country,
                    "latitude": cache.get("latitude", ""),
                    "longitude": cache.get("longitude", ""),
                    "match_confidence": cache.get("confidence", "medium" if city else "unmatched"),
                    "geocode_source": cache.get("source", city_source),
                }
            )
            enrichment_rows.append(
                {
                    "record_id": record_index + 1,
                    "title": row.get("title", ""),
                    "year": row.get("year", ""),
                    "doi": _normalize_debug_doi(row.get("doi")),
                    "scopus_url": _extract_scopus_identifier(row.get("ris_identifier_blob"))[0],
                    "scopus_eid": _extract_scopus_identifier(row.get("ris_identifier_blob"))[1],
                    "enrichment_source": "native_ris",
                    "source_priority": 0,
                    "matched_work_id": "",
                    "match_method": "native_ris_affiliation",
                    "match_confidence": "high",
                    "matched_title": row.get("title", ""),
                    "matched_year": row.get("year", ""),
                    "author_name": "",
                    "institution_name": raw,
                    "normalized_institution": normalized,
                    "institution_id": "",
                    "institution_openalex_id": "",
                    "institution_ror": "",
                    "institution_country": country,
                    "institution_city": city,
                    "institution_latitude": cache.get("latitude", ""),
                    "institution_longitude": cache.get("longitude", ""),
                    "raw_affiliation_string": affiliation or row.get("affiliations", ""),
                    "included_in_institution_map": "yes" if cache.get("latitude") and cache.get("longitude") else "no",
                    "exclusion_reason": "" if cache.get("latitude") and cache.get("longitude") else "missing institution coordinates",
                }
            )
    if not rows and not native_usable:
        status_path = PROCESSED_DIR / f"{stem}_institution_no_usable_data_status.txt"
        first_line = (
            "Institution enrichment could not run because API requests failed at the network/client level."
            if enrichment_network_failed
            else "Institution maps skipped: enrichment was attempted but returned no usable institution metadata."
            if enrich_institutions
            else "Institution maps skipped: no usable native or enriched institution data found."
        )
        status_path.write_text(
            f"{first_line}\n"
            "Normal institution output CSVs were not generated for this run.\n",
            encoding="utf-8",
        )
        LOGGER.info("Institution no-data status written: %s", status_path)
        return
    if rows and not native_usable:
        LOGGER.info("Native affiliations unavailable. Using Scopus/OpenAlex-enriched institution links for institution outputs.")
    for row in rows:
        normalized = _normalize_institution(row.get("normalized_institution") or row.get("institution"), aliases)
        if normalized:
            row["normalized_institution"] = normalized
        cache = geocache.get(_institution_key(normalized or row.get("institution")))
        if cache:
            row["city"] = row.get("city") or cache.get("city", "")
            row["state_or_region"] = row.get("state_or_region") or cache.get("admin1", "")
            row["country"] = row.get("country") or cache.get("country", "")
            row["latitude"] = row.get("latitude") or cache.get("latitude", "")
            row["longitude"] = row.get("longitude") or cache.get("longitude", "")
            row["geocode_source"] = row.get("geocode_source") or cache.get("source", "")
    for row in rows:
        row.pop("_enrichment", None)
    institutions = pd.DataFrame(
        rows,
        columns=[
            "record_id", "source_title", "year", "institution", "normalized_institution", "affiliation",
            "city", "state_or_region", "country", "latitude", "longitude", "match_confidence", "geocode_source",
        ],
    )
    institutions.to_csv(os.path.join(outputs_dir, f"{stem}_institutions.csv"), index=False)
    enrichment_meta_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for item in enrichment_rows:
        key = (str(item.get("record_id") or ""), _institution_key(item.get("normalized_institution") or item.get("institution_name")))
        enrichment_meta_by_key[key] = item
    city_geocode_cache_path = PROCESSED_DIR / f"{stem}_institution_city_geocode_cache.csv"
    city_geocode_cache = _load_city_geocode_cache(city_geocode_cache_path)
    ror_cache_path = PROCESSED_DIR / f"{stem}_ror_institution_location_cache.json"
    ror_cache = _load_ror_cache(ror_cache_path)
    openalex_detail_cache_path = PROCESSED_DIR / f"{stem}_openalex_enrichment_cache_institutions.json"
    openalex_detail_cache: dict[str, object] = {}
    if openalex_detail_cache_path.exists():
        try:
            loaded_openalex_details = json.loads(openalex_detail_cache_path.read_text(encoding="utf-8"))
            openalex_detail_cache = loaded_openalex_details if isinstance(loaded_openalex_details, dict) else {}
        except Exception:
            openalex_detail_cache = {}
    location_rows: list[dict[str, object]] = []
    unresolved_rows: list[dict[str, object]] = []
    location_source_counts: Counter[str] = Counter()
    resolution_counts: Counter[str] = Counter()
    top_unresolved: Counter[str] = Counter()
    ror_lookup_count = 0
    openalex_detail_lookup_count = 0
    for keys, group in institutions.groupby(["record_id", "normalized_institution", "city", "state_or_region", "country"], dropna=False, sort=False):
        record_id, normalized, city, state_or_region, country = keys
        first = group.iloc[0]
        meta = enrichment_meta_by_key.get((str(record_id), _institution_key(normalized)), {})
        existing_lat = group["latitude"].replace("", pd.NA).dropna().iloc[0] if not group["latitude"].replace("", pd.NA).dropna().empty else ""
        existing_lon = group["longitude"].replace("", pd.NA).dropna().iloc[0] if not group["longitude"].replace("", pd.NA).dropna().empty else ""
        source = str(meta.get("enrichment_source") or first.get("geocode_source") or "").strip()
        raw_affiliation = str(meta.get("raw_affiliation_string") or first.get("affiliation") or "").strip()
        institution_name = str(first.get("institution") or "").strip()
        publication_count = int(group["record_id"].nunique())
        source_priority = str(meta.get("source_priority") or "").strip()
        source_key_for_resolution = source.lower()
        parsed_affiliation = _parse_raw_affiliation_location(raw_affiliation)
        parsed_affiliation_city = parsed_affiliation.get("city", "")
        attempted_raw_affiliation_parse = "yes" if raw_affiliation else "no"
        attempted_city_geocode = "no"
        attempted_ror_lookup = "no"
        attempted_openalex_detail = "no"
        failure_reasons: list[str] = []
        resolution = {
            "city": "",
            "state_or_region": "",
            "country": "",
            "country_iso3": "",
            "latitude": "",
            "longitude": "",
            "source": "",
            "method": "",
            "confidence": "",
            "is_headquarters": "no",
            "is_affiliation_city": "no",
        }
        if source_key_for_resolution == "scopus" and _has_text(city) and _has_text(country):
            country_name, country_iso3 = _country_name_and_iso(country)
            resolution.update(
                {
                    "city": str(city).strip(),
                    "state_or_region": str(state_or_region or "").strip(),
                    "country": country_name or str(country).strip(),
                    "country_iso3": country_iso3 or _country_iso3(country),
                    "latitude": str(existing_lat or "").strip(),
                    "longitude": str(existing_lon or "").strip(),
                    "source": "scopus_structured_affiliation",
                    "method": "scopus_structured_affiliation",
                    "confidence": "high",
                    "is_affiliation_city": "yes",
                }
            )
        elif source_key_for_resolution == "scopus" and parsed_affiliation:
            resolution.update(
                {
                    "city": parsed_affiliation.get("city", ""),
                    "state_or_region": parsed_affiliation.get("state_or_region", ""),
                    "country": parsed_affiliation.get("country", ""),
                    "country_iso3": parsed_affiliation.get("country_iso3", ""),
                    "source": "scopus_raw_affiliation_parse",
                    "method": "scopus_raw_affiliation_parse",
                    "confidence": parsed_affiliation.get("confidence", "medium"),
                    "is_affiliation_city": "yes",
                }
            )
        elif source_key_for_resolution == "openalex" and parsed_affiliation:
            resolution.update(
                {
                    "city": parsed_affiliation.get("city", ""),
                    "state_or_region": parsed_affiliation.get("state_or_region", ""),
                    "country": parsed_affiliation.get("country", ""),
                    "country_iso3": parsed_affiliation.get("country_iso3", ""),
                    "source": "openalex_raw_affiliation_parse",
                    "method": "openalex_raw_affiliation_parse",
                    "confidence": parsed_affiliation.get("confidence", "medium"),
                    "is_affiliation_city": "yes",
                }
            )
        ror_location: dict[str, str] = {}
        ror_city = ""
        ror = str(meta.get("institution_ror") or "").strip()
        if ror:
            attempted_ror_lookup = "yes"
            ror_lookup_count += 1
            ror_location = _resolve_ror_location(ror, ror_cache, ror_cache_path, ror_lookup_count)
            ror_city = str(ror_location.get("city") or "").strip()
        location_conflict = ""
        conflict_resolution = ""
        if ror_location and parsed_affiliation_city and ror_city and not _same_city(parsed_affiliation_city, ror_city):
            location_conflict = "ror_city_differs_from_parsed_affiliation_city"
            conflict_resolution = "ROR location differs from parsed affiliation city; using affiliation city for publication geography."
            LOGGER.info("%s Institution: %s; parsed city: %s; ROR city: %s", conflict_resolution, institution_name, parsed_affiliation_city, ror_city)
        if resolution["city"] and resolution["country"]:
            if not resolution["latitude"] or not resolution["longitude"]:
                if ror_location and ror_city and _same_city(resolution["city"], ror_city) and (
                    not resolution["country"] or not ror_location.get("country") or _same_country(resolution["country"], ror_location.get("country"))
                ):
                    resolution["latitude"] = str(ror_location.get("latitude") or "").strip()
                    resolution["longitude"] = str(ror_location.get("longitude") or "").strip()
                    resolution["method"] = "ror_coordinates_for_matching_affiliation_city"
                    resolution["is_headquarters"] = "yes"
                if (not resolution["latitude"] or not resolution["longitude"]) and resolution["city"] and (resolution["country"] or resolution["country_iso3"]):
                    attempted_city_geocode = "yes"
                    geocoded = _geocode_city(
                        resolution["city"],
                        resolution["state_or_region"],
                        resolution["country"],
                        resolution["country_iso3"],
                        city_geocode_cache,
                    )
                    if geocoded.get("latitude") and geocoded.get("longitude"):
                        resolution["latitude"] = geocoded.get("latitude", "")
                        resolution["longitude"] = geocoded.get("longitude", "")
                        resolution["state_or_region"] = resolution["state_or_region"] or geocoded.get("state_or_region", "")
                        resolution["country"] = resolution["country"] or geocoded.get("country", "")
                        resolution["country_iso3"] = resolution["country_iso3"] or geocoded.get("country_iso3", "")
                        if not resolution["source"]:
                            resolution["source"] = "parsed_affiliation_city_geocode"
                        resolution["method"] = (
                            f"{resolution['method']}+parsed_affiliation_city_geocode"
                            if resolution["method"]
                            else "parsed_affiliation_city_geocode"
                        )
                        resolution_counts["parsed_affiliation_city_geocode"] += 1
                    else:
                        failure_reasons.append(geocoded.get("failure_reason", "parsed city geocode failed"))
        if not resolution["city"]:
            if ror_location and ror_location.get("city") and (ror_location.get("country") or ror_location.get("country_iso3")):
                resolution.update(
                    {
                        "city": ror_location.get("city", ""),
                        "state_or_region": ror_location.get("state_or_region", ""),
                        "country": ror_location.get("country", ""),
                        "country_iso3": ror_location.get("country_iso3", ""),
                        "latitude": ror_location.get("latitude", ""),
                        "longitude": ror_location.get("longitude", ""),
                        "source": "ror",
                        "method": "ror",
                        "confidence": "high",
                        "is_headquarters": "yes",
                        "is_affiliation_city": "no",
                    }
                )
            elif geocache.get(_institution_key(normalized or institution_name)):
                cache = geocache.get(_institution_key(normalized or institution_name), {})
                country_name, country_iso3 = _country_name_and_iso(cache.get("country_iso3") or cache.get("country"))
                resolution.update(
                    {
                        "city": str(cache.get("city") or "").strip(),
                        "state_or_region": str(cache.get("admin1") or "").strip(),
                        "country": country_name or str(cache.get("country") or "").strip(),
                        "country_iso3": country_iso3 or str(cache.get("country_iso3") or "").strip(),
                        "latitude": str(cache.get("latitude") or "").strip(),
                        "longitude": str(cache.get("longitude") or "").strip(),
                        "source": "institution_geocache",
                        "method": str(cache.get("source") or "institution_geocache").strip(),
                        "confidence": str(cache.get("confidence") or "high").strip(),
                        "is_headquarters": "yes",
                        "is_affiliation_city": "no",
                    }
                )
            elif str(meta.get("institution_id") or "").strip():
                attempted_openalex_detail = "yes"
                openalex_detail_lookup_count += 1
                detail = _openalex_institution_detail(str(meta.get("institution_id") or ""), openalex_email, openalex_detail_cache)
                detail_geo = detail.get("geo") if isinstance(detail.get("geo"), dict) else {}
                if detail_geo and (detail_geo.get("city") or detail_geo.get("latitude")):
                    detail_country, detail_iso3 = _country_name_and_iso(detail_geo.get("country") or detail_geo.get("country_code"))
                    resolution.update(
                        {
                            "city": str(detail_geo.get("city") or "").strip(),
                            "state_or_region": str(detail_geo.get("region") or "").strip(),
                            "country": detail_country,
                            "country_iso3": detail_iso3,
                            "latitude": str(detail_geo.get("latitude") or "").strip(),
                            "longitude": str(detail_geo.get("longitude") or "").strip(),
                            "source": "openalex_detail_fallback",
                            "method": "openalex_detail_fallback",
                            "confidence": "medium",
                            "is_headquarters": "yes",
                            "is_affiliation_city": "no",
                        }
                    )
                elif openalex_detail_lookup_count % 25 == 0:
                    openalex_detail_cache_path.write_text(json.dumps(openalex_detail_cache, indent=2) + "\n", encoding="utf-8")
                    LOGGER.info("OpenAlex institution detail fallback processed: %d", openalex_detail_lookup_count)
        if not resolution["city"]:
            failure_reasons.append("no usable affiliation city resolved")
        if not (resolution["country"] or resolution["country_iso3"]):
            failure_reasons.append("no country resolved")
        if not (resolution["latitude"] and resolution["longitude"]):
            failure_reasons.append("missing latitude/longitude")
        source_allowed = resolution["source"] in APPROVED_INSTITUTION_MAP_LOCATION_SOURCES
        disallowed_source = resolution["source"] in DISALLOWED_INSTITUTION_MAP_LOCATION_SOURCES or (
            resolution["source"] and not source_allowed
        )
        include_final = (
            _has_text(institution_name)
            and _has_text(resolution["city"])
            and (_has_text(resolution["country"]) or _has_text(resolution["country_iso3"]))
            and _has_text(resolution["latitude"])
            and _has_text(resolution["longitude"])
            and publication_count > 0
            and source_allowed
            and not disallowed_source
        )
        if not _has_text(institution_name):
            failure_reasons.append("missing institution name")
        if publication_count <= 0:
            failure_reasons.append("publication_count not positive")
        if disallowed_source:
            failure_reasons.append(f"location source not approved for final maps: {resolution['source']}")
        location_source_counts[resolution["source"] or "unresolved"] += 1
        if not include_final:
            top_unresolved[institution_name or str(normalized or "")] += publication_count
        exclusion_reason = "" if include_final else "; ".join(dict.fromkeys(reason for reason in failure_reasons if reason)) or "unresolved"
        location_rows.append(
            {
                "record_id": record_id,
                "title": first.get("source_title", ""),
                "doi": _normalize_debug_doi(dataframe.iloc[int(record_id) - 1].get("doi")) if str(record_id).isdigit() and int(record_id) - 1 < len(dataframe) else "",
                "institution_name": institution_name,
                "normalized_institution": normalized,
                "institution_id": meta.get("institution_id", ""),
                "institution_ror": meta.get("institution_ror", ""),
                "source": source or "native_affiliation",
                "raw_affiliation_string": raw_affiliation,
                "affiliation_city": resolution["city"],
                "affiliation_state_or_region": resolution["state_or_region"],
                "affiliation_country": resolution["country"],
                "affiliation_country_iso3": resolution["country_iso3"] or _country_iso3(resolution["country"]),
                "latitude": resolution["latitude"],
                "longitude": resolution["longitude"],
                "publication_count": publication_count,
                "record_count": publication_count,
                "author_count": int(len(group)),
                "location_source": resolution["source"],
                "location_confidence": resolution["confidence"] or first.get("match_confidence", ""),
                "location_method": resolution["method"],
                "location_is_headquarters": resolution["is_headquarters"],
                "location_is_affiliation_city": resolution["is_affiliation_city"],
                "ror_city": ror_city,
                "parsed_affiliation_city": parsed_affiliation_city,
                "location_conflict": location_conflict,
                "conflict_resolution": conflict_resolution,
                "included_in_final_maps": "yes" if include_final else "no",
                "exclusion_reason": exclusion_reason,
            }
        )
        if not include_final:
            unresolved_rows.append(
                {
                    "institution_name": institution_name,
                    "normalized_institution": normalized,
                    "institution_id": meta.get("institution_id", ""),
                    "institution_ror": meta.get("institution_ror", ""),
                    "publication_count": publication_count,
                    "raw_affiliation_string": raw_affiliation,
                    "affiliation_country": resolution["country"] or country,
                    "attempted_raw_affiliation_parse": attempted_raw_affiliation_parse,
                    "attempted_city_geocode": attempted_city_geocode,
                    "attempted_ror_lookup": attempted_ror_lookup,
                    "attempted_openalex_detail": attempted_openalex_detail,
                    "failure_reason": exclusion_reason,
                    "ror_city": ror_city,
                    "parsed_affiliation_city": parsed_affiliation_city,
                    "location_conflict": location_conflict,
                    "conflict_resolution": conflict_resolution,
                }
            )
    _write_city_geocode_cache(city_geocode_cache_path, city_geocode_cache)
    _write_ror_cache(ror_cache_path, ror_cache)
    if openalex_detail_cache:
        openalex_detail_cache_path.write_text(json.dumps(openalex_detail_cache, indent=2) + "\n", encoding="utf-8")
    unresolved_path = PROCESSED_DIR / f"{stem}_institution_location_unresolved_debug.csv"
    pd.DataFrame(
        unresolved_rows,
        columns=[
            "institution_name", "normalized_institution", "institution_id", "institution_ror",
            "publication_count", "raw_affiliation_string", "affiliation_country",
            "attempted_raw_affiliation_parse", "attempted_city_geocode", "attempted_ror_lookup",
            "attempted_openalex_detail", "failure_reason", "ror_city", "parsed_affiliation_city",
            "location_conflict", "conflict_resolution",
        ],
    ).to_csv(unresolved_path, index=False)
    LOGGER.info("Institution location unresolved debug output: %s", unresolved_path)
    pd.DataFrame(
        location_rows,
        columns=[
            "record_id", "title", "doi", "institution_name", "normalized_institution", "institution_id",
            "institution_ror", "source", "raw_affiliation_string", "affiliation_city",
            "affiliation_state_or_region", "affiliation_country", "affiliation_country_iso3",
            "latitude", "longitude", "publication_count", "record_count", "author_count",
            "location_source", "location_confidence", "location_method", "location_is_headquarters",
            "location_is_affiliation_city", "ror_city", "parsed_affiliation_city", "location_conflict",
            "conflict_resolution", "included_in_final_maps", "exclusion_reason",
        ],
    ).to_csv(os.path.join(outputs_dir, f"{stem}_institution_locations.csv"), index=False)
    LOGGER.info("Institution location table output: %s", os.path.join(outputs_dir, f"{stem}_institution_locations.csv"))
    location_frame = pd.DataFrame(location_rows)
    LOGGER.info("INSTITUTION LOCATION RESOLUTION DIAGNOSTIC")
    LOGGER.info("* total rows: %d", len(location_frame))
    LOGGER.info("* rows with ROR: %d", int(location_frame.get("institution_ror", pd.Series(dtype=str)).map(_has_text).sum()) if not location_frame.empty else 0)
    LOGGER.info("* rows with OpenAlex institution ID: %d", int(location_frame.get("institution_id", pd.Series(dtype=str)).map(_has_text).sum()) if not location_frame.empty else 0)
    LOGGER.info("* rows with raw affiliation string: %d", int(location_frame.get("raw_affiliation_string", pd.Series(dtype=str)).map(_has_text).sum()) if not location_frame.empty else 0)
    LOGGER.info("* rows with country: %d", int(((location_frame.get("affiliation_country", pd.Series(dtype=str)).map(_has_text)) | (location_frame.get("affiliation_country_iso3", pd.Series(dtype=str)).map(_has_text))).sum()) if not location_frame.empty else 0)
    LOGGER.info("* rows with city: %d", int(location_frame.get("affiliation_city", pd.Series(dtype=str)).map(_has_text).sum()) if not location_frame.empty else 0)
    LOGGER.info("* rows with lat/lon: %d", int(((location_frame.get("latitude", pd.Series(dtype=str)).map(_has_text)) & (location_frame.get("longitude", pd.Series(dtype=str)).map(_has_text))).sum()) if not location_frame.empty else 0)
    LOGGER.info("* rows included in final maps: %d", int((location_frame.get("included_in_final_maps", pd.Series(dtype=str)).astype(str).str.lower() == "yes").sum()) if not location_frame.empty else 0)
    LOGGER.info("* top 20 unresolved institutions by publication_count: %s", dict(top_unresolved.most_common(20)))
    LOGGER.info("INSTITUTION LOCATION SOURCE DIAGNOSTIC")
    LOGGER.info("* rows from Scopus structured affiliation fields: %d", location_source_counts.get("scopus_structured_affiliation", 0))
    LOGGER.info("* rows from Scopus raw affiliation parsing: %d", location_source_counts.get("scopus_raw_affiliation_parse", 0))
    LOGGER.info("* rows from OpenAlex raw affiliation parsing: %d", location_source_counts.get("openalex_raw_affiliation_parse", 0))
    LOGGER.info("* rows from ROR lookup: %d", location_source_counts.get("ror", 0))
    LOGGER.info("* rows from local geocache: %d", location_source_counts.get("institution_geocache", 0))
    LOGGER.info("* rows from parsed city geocoding: %d", resolution_counts.get("parsed_affiliation_city_geocode", 0))
    LOGGER.info("* rows from OpenAlex detail fallback: %d", location_source_counts.get("openalex_detail_fallback", 0))
    LOGGER.info("* unresolved rows: %d", len(unresolved_rows))
    LOGGER.info("* final included map rows: %d", int((location_frame.get("included_in_final_maps", pd.Series(dtype=str)).astype(str).str.lower() == "yes").sum()) if not location_frame.empty else 0)
    if enrich_institutions and source_key in {"all", "scopus"} and location_source_counts.get("scopus_structured_affiliation", 0) == 0:
        if not scopus_key:
            scopus_reason = "Scopus did not contribute institution city rows because API credentials were not configured."
        elif scopus_counts.get("records_not_attempted", 0) == len(dataframe):
            scopus_reason = "Scopus did not contribute institution city rows because Scopus records were not attempted."
        elif scopus_counts.get("institution_links_extracted", 0) == 0:
            scopus_reason = "Scopus API response did not include expected institution affiliation fields or returned no usable institution links."
        else:
            scopus_reason = "Scopus fields were parsed, but no rows contained usable structured affiliation city/country fields."
        LOGGER.info("Scopus institution city diagnostic: %s", scopus_reason)
    pd.DataFrame(
        enrichment_rows,
        columns=[
            "record_id", "title", "year", "doi", "scopus_url", "scopus_eid", "enrichment_source",
            "source_priority", "matched_work_id", "match_method", "match_confidence", "matched_title",
            "matched_year", "author_name", "institution_name", "normalized_institution", "institution_id",
            "institution_openalex_id", "institution_ror", "institution_country", "institution_city",
            "institution_latitude", "institution_longitude", "raw_affiliation_string",
            "included_in_institution_map", "exclusion_reason",
        ],
    ).to_csv(os.path.join(outputs_dir, f"{stem}_institution_enrichment_matches.csv"), index=False)
    handoff_rows: list[dict[str, object]] = []
    for row in institutions.to_dict(orient="records"):
        included_counts = bool(str(row.get("normalized_institution") or row.get("institution") or "").strip())
        included_maps = bool(str(row.get("latitude") or "").strip() and str(row.get("longitude") or "").strip())
        handoff_rows.append(
            {
                "record_id": row.get("record_id", ""),
                "title": row.get("source_title", ""),
                "doi": _normalize_debug_doi(dataframe.iloc[int(row.get("record_id", 1)) - 1].get("doi")) if str(row.get("record_id", "")).isdigit() and int(row.get("record_id", 1)) - 1 < len(dataframe) else "",
                "enrichment_source": row.get("geocode_source", ""),
                "institution_name": row.get("institution", ""),
                "normalized_institution": row.get("normalized_institution", ""),
                "institution_country": row.get("country", ""),
                "institution_city": row.get("city", ""),
                "latitude": row.get("latitude", ""),
                "longitude": row.get("longitude", ""),
                "included_in_counts": "yes" if included_counts else "no",
                "included_in_edges": "yes" if included_counts else "no",
                "included_in_maps": "yes" if included_maps else "no",
                "exclusion_reason": "" if included_counts else "missing institution name",
            }
        )
    handoff_path = PROCESSED_DIR / f"{stem}_enriched_institution_handoff_debug.csv"
    pd.DataFrame(
        handoff_rows,
        columns=[
            "record_id", "title", "doi", "enrichment_source", "institution_name", "normalized_institution",
            "institution_country", "institution_city", "latitude", "longitude", "included_in_counts",
            "included_in_edges", "included_in_maps", "exclusion_reason",
        ],
    ).to_csv(handoff_path, index=False)
    LOGGER.info("Enriched institution handoff debug output: %s", handoff_path)
    counts = (
        institutions.groupby("normalized_institution", as_index=False)
        .agg(publication_count=("record_id", "nunique"), mention_count=("record_id", "size"), institution=("institution", "first"))
        .sort_values(["publication_count", "normalized_institution"], ascending=[False, True])
        if not institutions.empty
        else pd.DataFrame(columns=["normalized_institution", "publication_count", "mention_count", "institution"])
    )
    counts.to_csv(os.path.join(outputs_dir, f"{stem}_institution_counts.csv"), index=False)
    link_columns = [
        "city", "state_or_region", "country", "latitude", "longitude", "institution", "normalized_institution",
        "publication_count", "record_count", "source_record_ids", "source_titles", "match_confidence",
        "geocode_source", "included_in_city_map", "exclusion_reason",
    ]
    if institutions.empty:
        links = pd.DataFrame(columns=link_columns)
    else:
        grouped_rows = []
        for keys, group in institutions.groupby(["city", "state_or_region", "country", "normalized_institution"], dropna=False, sort=False):
            city, state_or_region, country, normalized = keys
            included = bool(str(city or "").strip())
            grouped_rows.append(
                {
                    "city": city,
                    "state_or_region": state_or_region,
                    "country": country,
                    "latitude": group["latitude"].replace("", pd.NA).dropna().iloc[0] if not group["latitude"].replace("", pd.NA).dropna().empty else "",
                    "longitude": group["longitude"].replace("", pd.NA).dropna().iloc[0] if not group["longitude"].replace("", pd.NA).dropna().empty else "",
                    "institution": group["institution"].iloc[0],
                    "normalized_institution": normalized,
                    "publication_count": int(group["record_id"].nunique()),
                    "record_count": int(group["record_id"].nunique()),
                    "source_record_ids": "; ".join(str(value) for value in sorted(set(group["record_id"]))),
                    "source_titles": "; ".join(dict.fromkeys(str(value) for value in group["source_title"] if str(value).strip())),
                    "match_confidence": group["match_confidence"].iloc[0],
                    "geocode_source": group["geocode_source"].iloc[0],
                    "included_in_city_map": "yes" if included else "no",
                    "exclusion_reason": "" if included else "no city/location resolved for institution",
                }
            )
        links = pd.DataFrame(grouped_rows, columns=link_columns)
    links.to_csv(os.path.join(outputs_dir, f"{stem}_institution_city_links.csv"), index=False)
    links.to_csv(os.path.join(outputs_dir, f"{stem}_city_institution_links.csv"), index=False)
    pd.DataFrame(
        [{"institution": name, "count": count, "reason": "no city/geocode match"} for name, count in unmatched.most_common()],
        columns=["institution", "count", "reason"],
    ).to_csv(os.path.join(outputs_dir, f"{stem}_institution_unmatched_debug.csv"), index=False)
    LOGGER.info("Institution extraction output: %s", os.path.join(outputs_dir, f"{stem}_institutions.csv"))


def apply_enriched_institution_handoff(dataframe: pd.DataFrame, outputs_dir: str, query: str) -> pd.DataFrame:
    """Use generated institution links as downstream institution fields."""
    stem = _clean_filename(query)
    path = os.path.join(outputs_dir, f"{stem}_institutions.csv")
    if not os.path.exists(path):
        return dataframe
    institutions = pd.read_csv(path, dtype=str, keep_default_na=False)
    if institutions.empty or "record_id" not in institutions.columns:
        return dataframe
    enriched = dataframe.copy()
    if "institutions" not in enriched.columns:
        enriched["institutions"] = ""
    if "countries" not in enriched.columns:
        enriched["countries"] = ""
    grouped = institutions.groupby("record_id", sort=False)
    records_updated = 0
    for record_id, group in grouped:
        if not str(record_id).isdigit():
            continue
        idx = int(record_id) - 1
        if idx < 0 or idx >= len(enriched):
            continue
        institution_names = _combine_unique(group["normalized_institution"].where(group["normalized_institution"].astype(str).str.strip().ne(""), group["institution"]))
        countries = _combine_unique(group["country"])
        if institution_names:
            enriched.at[idx, "institutions"] = institution_names
            records_updated += 1
        if countries:
            enriched.at[idx, "countries"] = countries
    if records_updated:
        LOGGER.info("Native affiliations unavailable. Using Scopus/OpenAlex-enriched institution links for institution outputs.")
        LOGGER.info("Enriched institution links handed off to downstream outputs for %d records.", records_updated)
    return enriched


def _canonical_author_name(value: object) -> str:
    """Return a stable author label while preserving surname-comma-initials style."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text or "," not in text:
        return text

    surname, given = (part.strip() for part in text.split(",", 1))
    if not surname or not given:
        return text

    compact_given = re.sub(r"[\s.]+", "", given)
    looks_like_initials = "." in given or compact_given.isupper() or len(compact_given) <= 3
    if compact_given and looks_like_initials and re.fullmatch(r"[A-Za-z-]+", compact_given) and len(compact_given) <= 6:
        return f"{surname}, {compact_given.upper()}"
    return f"{surname}, {given}"


def _combine_unique(values: Iterable[object]) -> str:
    """Combine multi-valued fields into a stable semicolon-separated string."""
    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in _split_multi_value(value):
            normalized = part.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_values.append(part)
    return "; ".join(unique_values)


def _first_non_empty(values: Iterable[object]) -> str:
    """Return the first non-empty text value."""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _merge_analysis_group(group: pd.DataFrame) -> dict[str, object]:
    """Merge duplicate analysis records while preserving extra metadata."""
    year_series = group["year"].dropna()
    return {
        "title": _first_non_empty(group["title"]),
        "doi": normalize_doi(_first_non_empty(group["doi"])),
        "authors": _combine_unique(group["authors"]),
        "year": int(year_series.iloc[0]) if not year_series.empty else None,
        "citations": int(group["citations"].fillna(0).max()),
        "source": ", ".join(sorted({part for value in group["source"] for part in _split_multi_value(value)})),
        "institutions": _combine_unique(group["institutions"]),
        "countries": _combine_unique(group["countries"]),
        "affiliations": _combine_unique(group["affiliations"]),
        "citation_available": bool(group.get("citation_available", pd.Series(dtype=bool)).fillna(False).astype(bool).any()),
    }


def _merge_by_key(dataframe: pd.DataFrame, key_column: str) -> pd.DataFrame:
    """Merge records sharing the same non-empty key."""
    with_key = dataframe[dataframe[key_column] != ""].copy()
    without_key = dataframe[dataframe[key_column] == ""].copy()
    merged_records = [_merge_analysis_group(group) for _, group in with_key.groupby(key_column, sort=False, dropna=False)]
    if not without_key.empty:
        passthrough_columns = CORE_COLUMNS + EXTRA_COLUMNS
        merged_records.extend(without_key[passthrough_columns].to_dict(orient="records"))
    return pd.DataFrame(merged_records, columns=CORE_COLUMNS + EXTRA_COLUMNS)


def prepare_analysis_dataset(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Create a deduplicated enriched dataset for downstream analyses."""
    if dataframe.empty:
        return pd.DataFrame(columns=CORE_COLUMNS + EXTRA_COLUMNS)

    records = []
    for record in dataframe.to_dict(orient="records"):
        base = standardize_record(record)
        base.update(
            {
                "institutions": _combine_unique([record.get("institutions")]),
                "countries": _combine_unique([record.get("countries")]),
                "affiliations": _combine_unique([record.get("affiliations")]),
                "citation_available": bool(record.get("citation_available") or False),
            }
        )
        records.append(base)

    prepared = pd.DataFrame(records, columns=CORE_COLUMNS + EXTRA_COLUMNS)
    prepared["doi_normalized"] = prepared["doi"].map(normalize_doi)
    prepared = _merge_by_key(prepared, "doi_normalized")
    prepared["title_normalized"] = prepared["title"].map(normalize_title)
    prepared = _merge_by_key(prepared.assign(title_normalized=prepared["title"].map(normalize_title)), "title_normalized")
    return prepared.drop(columns=["title_normalized"], errors="ignore").reset_index(drop=True)


def run_citation_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str, citation_available: bool = True) -> None:
    """Generate citation-focused outputs and logs."""
    if dataframe.empty:
        LOGGER.info("Citation analysis skipped: no records available.")
        return

    stem = _clean_filename(query)
    citations = dataframe["citations"].fillna(0).astype(int)

    def write_entity_rankings(field: str, entity_col: str, pub_name: str, citation_name: str) -> None:
        rows: list[dict[str, object]] = []
        for record_id, row in dataframe.reset_index(drop=True).iterrows():
            entities = {value.strip() for value in _split_multi_value(row.get(field))}
            entities = {entity for entity in entities if entity}
            for entity in entities:
                rows.append(
                    {
                        entity_col: entity,
                        "record_id": record_id,
                        "citations": int(row.get("citations") or 0),
                        "citation_available": bool(row.get("citation_available", False)),
                    }
                )
        if not rows:
            empty_columns = [entity_col, "publication_count", "total_citations", "average_citations", "records_with_citation_counts"]
            pd.DataFrame(columns=empty_columns).to_csv(os.path.join(outputs_dir, pub_name), index=False)
            pd.DataFrame(columns=empty_columns).to_csv(os.path.join(outputs_dir, citation_name), index=False)
            return
        frame = pd.DataFrame(rows)
        grouped = (
            frame.groupby(entity_col, as_index=False)
            .agg(
                publication_count=("record_id", "nunique"),
                total_citations=("citations", "sum"),
                average_citations=("citations", "mean"),
                records_with_citation_counts=("citation_available", "sum"),
            )
        )
        grouped["average_citations"] = grouped["average_citations"].round(2)
        grouped.sort_values(["publication_count", "total_citations", entity_col], ascending=[False, False, True]).to_csv(
            os.path.join(outputs_dir, pub_name),
            index=False,
        )
        grouped.sort_values(["total_citations", "publication_count", entity_col], ascending=[False, False, True]).to_csv(
            os.path.join(outputs_dir, citation_name),
            index=False,
        )

    write_entity_rankings(
        "countries",
        "country",
        f"{stem}_country_publication_rankings.csv",
        f"{stem}_country_citation_rankings.csv",
    )
    write_entity_rankings(
        "institutions",
        "institution",
        f"{stem}_institution_publication_rankings.csv",
        f"{stem}_institution_citation_rankings.csv",
    )

    author_rows = []
    for _, row in dataframe.iterrows():
        for author in _split_multi_value(row.get("authors")):
            canonical_author = _canonical_author_name(author)
            if canonical_author:
                author_rows.append({"author": canonical_author, "citations": int(row.get("citations") or 0), "papers": 1})
    if author_rows:
        authors_df = pd.DataFrame(author_rows)
        top_authors = (
            authors_df.groupby("author", as_index=False)
            .agg(total_citations=("citations", "sum"), publications=("papers", "sum"))
        )
        if citation_available:
            top_authors = top_authors.sort_values(["total_citations", "publications", "author"], ascending=[False, False, True])
        else:
            top_authors = top_authors.drop(columns=["total_citations"]).sort_values(["publications", "author"], ascending=[False, True])
    else:
        top_authors = pd.DataFrame(columns=["author", "publications"] if not citation_available else ["author", "total_citations", "publications"])
    top_authors.to_csv(os.path.join(outputs_dir, f"{stem}_top_authors.csv"), index=False)

    if citation_available:
        top_papers = dataframe.sort_values(["citations", "year"], ascending=[False, False]).head(20).copy()
        top_papers.to_csv(os.path.join(outputs_dir, f"{stem}_top_papers.csv"), index=False)
        metric_rows: list[dict[str, object]] = []
        citation_sources = dataframe.get("citation_source", pd.Series(["source_export"] * len(dataframe), index=dataframe.index)).fillna("").astype(str)
        author_groups = pd.DataFrame(author_rows).groupby("author", sort=False) if author_rows else []
        for author, group in author_groups:
            author_citations = sorted((int(value) for value in group["citations"].fillna(0)), reverse=True)
            h_index = 0
            for position, count in enumerate(author_citations, start=1):
                if count >= position:
                    h_index = position
                else:
                    break
            source_counter: Counter[str] = Counter()
            missing = 0
            for _, source_row in dataframe.iterrows():
                if author not in [_canonical_author_name(value) for value in _split_multi_value(source_row.get("authors"))]:
                    continue
                source = str(citation_sources.loc[source_row.name] or "source_export")
                available = bool(source_row.get("citation_available", False))
                if available:
                    source_counter[source] += 1
                else:
                    missing += 1
            metric_rows.append(
                {
                    "author": author,
                    "publication_count": int(group["papers"].sum()),
                    "total_citations": int(group["citations"].sum()),
                    "average_citations": round(float(group["citations"].mean()), 2),
                    "max_citations": int(group["citations"].max()),
                    "h_index_within_dataset": h_index,
                    "citation_source_summary": "; ".join(f"{source}: {count}" for source, count in sorted(source_counter.items())),
                    "records_with_citation_counts": int(sum(source_counter.values())),
                    "records_missing_citation_counts": int(missing),
                }
            )
        pd.DataFrame(
            metric_rows,
            columns=[
                "author", "publication_count", "total_citations", "average_citations", "max_citations",
                "h_index_within_dataset", "citation_source_summary", "records_with_citation_counts",
                "records_missing_citation_counts",
            ],
        ).sort_values(["total_citations", "publication_count", "author"], ascending=[False, False, True]).to_csv(
            os.path.join(outputs_dir, f"{stem}_author_citation_metrics.csv"),
            index=False,
        )
    else:
        LOGGER.warning("Citation counts unavailable in source export. Citation-based rankings were not generated.")
        LOGGER.info("Top authors output: %s", os.path.join(outputs_dir, f"{stem}_top_authors.csv"))
        return

    avg_citations = float(citations.mean()) if not citations.empty else 0.0
    quantile_cutoff = float(citations.quantile(0.95)) if len(citations) > 1 else float(citations.max())
    outliers = dataframe[citations >= quantile_cutoff].copy() if not dataframe.empty else dataframe.copy()
    histogram = pd.cut(
        citations,
        bins=[-1, 0, 1, 5, 10, 25, 50, 100, 250, 500, max(int(citations.max()), 5000)],
        include_lowest=True,
    ).value_counts(sort=False)

    LOGGER.info("Average citations per paper: %.2f", avg_citations)
    LOGGER.info("Citation distribution: %s", {str(index): int(value) for index, value in histogram.items()})
    LOGGER.info("Highly cited outliers (top 5%% threshold %.2f): %d", quantile_cutoff, len(outliers))
    if citation_available:
        LOGGER.info("Top cited papers output: %s", os.path.join(outputs_dir, f"{stem}_top_papers.csv"))
        LOGGER.info("Author citation metrics output: %s", os.path.join(outputs_dir, f"{stem}_author_citation_metrics.csv"))
    LOGGER.info("Top authors output: %s", os.path.join(outputs_dir, f"{stem}_top_authors.csv"))


def run_h_index_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str = "results") -> None:
    """Write h-index summary outputs for the deduplicated result set."""
    stem = _clean_filename(query)
    output_path = os.path.join(outputs_dir, f"{stem}_h_index.csv")
    if dataframe.empty:
        pd.DataFrame([{"h_index": 0, "h_index_within_dataset": 0, "metric_scope": "within-dataset h-index", "total_publications": 0, "total_citations": 0}]).to_csv(output_path, index=False)
        LOGGER.info("H-index analysis skipped: no records available.")
        return

    citations = sorted((int(value) for value in dataframe["citations"].fillna(0)), reverse=True)
    h_index = 0
    for position, count in enumerate(citations, start=1):
        if count >= position:
            h_index = position
        else:
            break

    summary = pd.DataFrame(
        [
            {
                "h_index": h_index,
                "h_index_within_dataset": h_index,
                "metric_scope": "within-dataset h-index",
                "total_publications": int(len(dataframe)),
                "total_citations": int(sum(citations)),
            }
        ]
    )
    summary.to_csv(output_path, index=False)
    LOGGER.info("H-index analysis output: %s", output_path)


def run_publication_year_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str = "results") -> None:
    """Write publication counts by year for the deduplicated result set."""
    stem = _clean_filename(query)
    output_path = os.path.join(outputs_dir, f"{stem}_publication_years.csv")
    if dataframe.empty:
        pd.DataFrame(columns=["year", "publications"]).to_csv(output_path, index=False)
        LOGGER.info("Publication year analysis skipped: no records available.")
        return

    years = dataframe["year"].map(parse_year).dropna().astype(int)
    year_counts = (
        years.value_counts()
        .sort_index()
        .rename_axis("year")
        .reset_index(name="publications")
    )
    year_counts.to_csv(output_path, index=False)
    LOGGER.info("Publication year analysis output: %s", output_path)


def run_institutional_temporal_analysis(
    dataframe: pd.DataFrame,
    outputs_dir: str,
    query: str,
    enrich_institutions: bool = False,
    enrichment_source: str = "all",
    openalex_email: str = "",
    scopus_api_key: str = "",
    scopus_inst_token: str = "",
    institution_enrichment_cache: bool = True,
    rebuild_institution_enrichment_cache: bool = False,
) -> pd.DataFrame:
    """Generate institution/country trend outputs and logs."""
    if dataframe.empty:
        LOGGER.info("Institutional analysis skipped: no records available.")
        return dataframe

    stem = _clean_filename(query)
    run_institution_extraction(
        dataframe,
        outputs_dir,
        query,
        enrich_institutions=enrich_institutions,
        enrichment_source=enrichment_source,
        openalex_email=openalex_email,
        scopus_api_key=scopus_api_key,
        scopus_inst_token=scopus_inst_token,
        institution_enrichment_cache=institution_enrichment_cache,
        rebuild_institution_enrichment_cache=rebuild_institution_enrichment_cache,
    )
    dataframe = apply_enriched_institution_handoff(dataframe, outputs_dir, query)
    records = []
    country_records = []
    for _, row in dataframe.iterrows():
        year = parse_year(row.get("year"))
        if year is None:
            continue
        for institution in _split_multi_value(row.get("institutions")):
            records.append({"year": year, "institution": institution})
        for country in _split_multi_value(row.get("countries")):
            country_records.append({"year": year, "country": country})

    institutions_df = pd.DataFrame(records, columns=["year", "institution"])
    countries_df = pd.DataFrame(country_records, columns=["year", "country"])

    institution_counts = (
        institutions_df.groupby(["year", "institution"], as_index=False)
        .size()
        .rename(columns={"size": "publications"})
        if not institutions_df.empty
        else pd.DataFrame(columns=["year", "institution", "publications"])
    )
    country_counts = (
        countries_df.groupby(["year", "country"], as_index=False)
        .size()
        .rename(columns={"size": "publications"})
        if not countries_df.empty
        else pd.DataFrame(columns=["year", "country", "publications"])
    )

    institution_counts.to_csv(os.path.join(outputs_dir, f"{stem}_institution_year_counts.csv"), index=False)
    country_counts.to_csv(os.path.join(outputs_dir, f"{stem}_country_year_counts.csv"), index=False)

    top_institutions = (
        institution_counts.groupby("institution", as_index=False)["publications"].sum().sort_values("publications", ascending=False).head(10)
        if not institution_counts.empty
        else pd.DataFrame(columns=["institution", "publications"])
    )

    growth_rows = []
    if not institution_counts.empty:
        for institution, group in institution_counts.groupby("institution", sort=False):
            ordered = group.sort_values("year")
            growth_rows.append(
                {
                    "institution": institution,
                    "growth": int(ordered["publications"].iloc[-1] - ordered["publications"].iloc[0]),
                    "start_year": int(ordered["year"].iloc[0]),
                    "end_year": int(ordered["year"].iloc[-1]),
                }
            )
    growth_df = pd.DataFrame(growth_rows).sort_values(["growth", "institution"], ascending=[False, True]).head(10) if growth_rows else pd.DataFrame(columns=["institution", "growth", "start_year", "end_year"])

    LOGGER.info("Top institutions overall: %s", top_institutions.to_dict(orient="records"))
    LOGGER.info("Top institution growth: %s", growth_df.to_dict(orient="records"))
    return dataframe


def _normalize_entity_name(value: str) -> str:
    """Normalize an author/institution label for network analysis."""
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _build_edge_list(dataframe: pd.DataFrame, column: str, entity_label: str) -> pd.DataFrame:
    """Build a co-occurrence edge list for the provided column."""
    edge_counter: Counter[tuple[str, str]] = Counter()
    for _, row in dataframe.iterrows():
        entities = []
        seen = set()
        for value in _split_multi_value(row.get(column)):
            normalized = _normalize_entity_name(value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            entities.append(normalized)
        for source, target in itertools.combinations(sorted(entities), 2):
            if source == target:
                continue
            edge_counter[(source, target)] += 1

    edge_rows = [{"source": source, "target": target, "weight": weight} for (source, target), weight in edge_counter.items()]
    edge_df = pd.DataFrame(edge_rows, columns=["source", "target", "weight"]).sort_values(["weight", "source", "target"], ascending=[False, True, True]) if edge_rows else pd.DataFrame(columns=["source", "target", "weight"])
    LOGGER.info("%s collaboration edges: %d", entity_label, len(edge_df))
    return edge_df


def _build_matrix(edge_df: pd.DataFrame, top_n: int = 100) -> pd.DataFrame:
    """Build a symmetric co-occurrence matrix from an edge list."""
    if edge_df.empty:
        return pd.DataFrame()

    totals: defaultdict[str, int] = defaultdict(int)
    for _, row in edge_df.iterrows():
        totals[row["source"]] += int(row["weight"])
        totals[row["target"]] += int(row["weight"])

    top_entities = {entity for entity, _ in sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:top_n]}
    filtered = edge_df[edge_df["source"].isin(top_entities) & edge_df["target"].isin(top_entities)].copy()
    if filtered.empty:
        return pd.DataFrame()

    entities = sorted(top_entities)
    matrix = pd.DataFrame(0, index=entities, columns=entities, dtype=int)
    for _, row in filtered.iterrows():
        matrix.at[row["source"], row["target"]] = int(row["weight"])
        matrix.at[row["target"], row["source"]] = int(row["weight"])
    return matrix


def run_collaboration_analysis(dataframe: pd.DataFrame, outputs_dir: str, query: str) -> None:
    """Generate author and institution collaboration outputs."""
    if dataframe.empty:
        LOGGER.info("Collaboration analysis skipped: no records available.")
        return

    stem = _clean_filename(query)
    author_edges = _build_edge_list(dataframe, "authors", "Author")
    institution_edges = _build_edge_list(dataframe, "institutions", "Institution")

    author_edges.to_csv(os.path.join(outputs_dir, f"{stem}_author_edges.csv"), index=False)
    institution_edges.to_csv(os.path.join(outputs_dir, f"{stem}_institution_edges.csv"), index=False)

    author_matrix = _build_matrix(author_edges)
    institution_matrix = _build_matrix(institution_edges)
    author_matrix.to_csv(os.path.join(outputs_dir, f"{stem}_author_matrix.csv"))
    institution_matrix.to_csv(os.path.join(outputs_dir, f"{stem}_institution_matrix.csv"))
