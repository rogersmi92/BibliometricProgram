#!/usr/bin/env python3
"""Diagnostic runner for Web of Science configuration and live search."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apis.wos_api import query_wos
from utils.config import get_wos_api_key

LOGGER = logging.getLogger("test_webofscience")
DEFAULT_QUERY = "hemolytic uremic syndrome"


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="Web of Science query to run.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="Number of records to request.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    configure_logging(args.verbose)

    api_key = get_wos_api_key()
    LOGGER.info("wos_api_key_present=%s", bool(api_key))
    if not api_key:
        LOGGER.error("No Web of Science API key configured.")
        return 1

    dataframe = query_wos(args.query, count=args.count)
    LOGGER.info("rows_returned=%s", len(dataframe))
    if dataframe.empty:
        LOGGER.warning("Web of Science returned no records.")
        return 2

    print(dataframe.head(args.count).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
