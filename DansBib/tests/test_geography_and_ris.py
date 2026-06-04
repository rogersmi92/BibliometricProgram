#!/usr/bin/env python3
"""Lightweight regression checks for geography extraction and RIS parsing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from processing.geography import apply_country_extraction, extract_country
from processing.ris_parser import load_ris_files
from pipeline_capabilities import RisInput


class GeographyExtractionTests(unittest.TestCase):
    def test_hong_kong_remains_distinct_from_china(self) -> None:
        self.assertEqual(extract_country("University of Hong Kong, Hong Kong, China"), "Hong Kong")

    def test_texas_affiliation_maps_to_usa(self) -> None:
        dataframe = pd.DataFrame(
            [
                {
                    "countries": "",
                    "affiliations": "University of Texas Southwestern Medical Center, Dallas, TX",
                }
            ]
        )
        result = apply_country_extraction(dataframe)
        self.assertEqual(result.loc[0, "country"], "USA")
        self.assertEqual(result.loc[0, "countries"], "USA")

    def test_tr_email_domain_does_not_create_turkey_false_positive(self) -> None:
        self.assertIsNone(extract_country("Electronic address: author@example.tr"))


class RisParserTests(unittest.TestCase):
    def test_ris_parser_returns_expected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ris_path = Path(temp_dir) / "sample.ris"
            ris_path.write_text(
                "\n".join(
                    [
                        "TY  - JOUR",
                        "TI  - Example Title",
                        "AU  - Smith, Jane",
                        "PY  - 2024",
                        "DO  - 10.123/example",
                        "AB  - Abstract text",
                        "KW  - kidney",
                        "AD  - University of Texas, Austin, TX",
                        "ER  -",
                    ]
                ),
                encoding="utf-8",
            )

            dataframe = load_ris_files([str(ris_path)])

        expected_columns = {
            "title",
            "doi",
            "authors",
            "year",
            "citations",
            "citation_available",
            "source",
            "abstract",
            "keywords",
            "institutions",
            "countries",
            "affiliations",
            "wos_uid",
            "source_db",
            "ris_file",
        }
        self.assertTrue(expected_columns.issubset(set(dataframe.columns)))
        self.assertEqual(dataframe.loc[0, "title"], "Example Title")
        self.assertEqual(dataframe.loc[0, "source"], "unknown")
        self.assertEqual(dataframe.loc[0, "source_db"], "unknown_ris")

    def test_ris_marked_covidence_is_labeled_covidence(self) -> None:
        dataframe = self._load_sample_with_source("covidence")
        self.assertEqual(dataframe.loc[0, "source"], "covidence")
        self.assertEqual(dataframe.loc[0, "source_db"], "covidence")

    def test_ris_marked_wos_is_labeled_wos(self) -> None:
        dataframe = self._load_sample_with_source("wos")
        self.assertEqual(dataframe.loc[0, "source"], "wos")
        self.assertEqual(dataframe.loc[0, "source_db"], "wos_ris")

    def test_ris_without_source_is_not_automatically_covidence(self) -> None:
        dataframe = self._load_sample_with_source(None)
        self.assertNotEqual(dataframe.loc[0, "source"], "covidence")

    def _load_sample_with_source(self, source: str | None) -> pd.DataFrame:
        with tempfile.TemporaryDirectory() as temp_dir:
            ris_path = Path(temp_dir) / "sample.ris"
            ris_path.write_text(
                "\n".join(
                    [
                        "TY  - JOUR",
                        "TI  - Example Title",
                        "AU  - Smith, Jane",
                        "PY  - 2024",
                        "ER  -",
                    ]
                ),
                encoding="utf-8",
            )
            if source is None:
                return load_ris_files([str(ris_path)])
            return load_ris_files([RisInput(path=ris_path, source=source)])


if __name__ == "__main__":
    unittest.main()
