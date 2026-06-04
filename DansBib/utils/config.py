"""Configuration helpers for API access."""

from __future__ import annotations

import os

try:
    from pipeline_capabilities import DEFAULT_DATABASES, RECOGNIZED_DATABASES, SCALING_PROFILES
except ModuleNotFoundError:  # pragma: no cover - package import path
    from DansBib.pipeline_capabilities import DEFAULT_DATABASES, RECOGNIZED_DATABASES, SCALING_PROFILES

OPENALEX_API_KEY = "wOElT0DimZRXLOV2l3Qd3i"
SCOPUS_API_KEY = "c704eb588ffd3d9e3fa5971d92430c62"
WOS_API_KEY = "a6a9bf726926d89d5f93242172c5fa2be833a134"
SCALING_MODE = os.getenv("SCALING_MODE", "extreme").lower()
OPENALEX_PAGE_SIZE = 25
PUBMED_PAGE_SIZE = 100
SCOPUS_PAGE_SIZE = 25
WOS_PAGE_SIZE = 100


def get_scaling_mode() -> str:
    """Return the active scaling mode, defaulting safely to medium."""
    return SCALING_MODE if SCALING_MODE in SCALING_PROFILES else "medium"


def get_source_max_results(source: str) -> int:
    """Return the configured result cap for a source under the active scaling mode."""
    mode = get_scaling_mode()
    return int(SCALING_PROFILES[mode][source])


OPENALEX_MAX_RESULTS = get_source_max_results("openalex")
PUBMED_MAX_RESULTS = get_source_max_results("pubmed")
SCOPUS_MAX_RESULTS = get_source_max_results("scopus")
WOS_MAX_RESULTS = get_source_max_results("wos")


def get_scopus_api_key() -> str | None:
    """Return the configured Scopus API key from env or local config."""
    return os.getenv("SCOPUS_API_KEY") or SCOPUS_API_KEY


def get_wos_api_key() -> str | None:
    """Return the configured Web of Science API key from env or local config."""
    return os.getenv("WOS_API_KEY") or WOS_API_KEY
