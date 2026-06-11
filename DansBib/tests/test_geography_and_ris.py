#!/usr/bin/env python3
"""Lightweight regression checks for geography extraction and RIS parsing."""

from __future__ import annotations

import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from processing.geography import apply_country_extraction, extract_country
from processing.analysis import run_institution_extraction, _run_openalex_institution_enrichment, _run_scopus_institution_enrichment
from processing.ris_parser import load_ris_files
from pipeline_capabilities import RisInput
import GeoCensus
import visualize_bibliometrics


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

    def test_state_term_is_excluded_from_city_map_candidates(self) -> None:
        row = pd.Series({"canonical_name": "Texas", "term": "Texas", "geo_type": "state"})
        self.assertIn("state-level", visualize_bibliometrics.city_point_exclusion_reason(row))

    def test_texas_city_place_is_allowed_when_resolved_as_place(self) -> None:
        row = pd.Series({"canonical_name": "Texas City", "term": "Texas City", "geo_type": "place", "state": "Texas"})
        self.assertEqual(visualize_bibliometrics.city_point_exclusion_reason(row), "")

    def test_country_name_city_alias_is_excluded_without_context(self) -> None:
        row = pd.Series({"canonical_name": "Australia", "term": "Australia", "geo_type": "place", "country": "Cuba"})
        self.assertIn("country-name", visualize_bibliometrics.city_point_exclusion_reason(row))


class InstitutionExtractionTests(unittest.TestCase):
    def test_institution_outputs_are_written_and_normalized(self) -> None:
        dataframe = pd.DataFrame(
            [
                {
                    "title": "Example",
                    "year": 2024,
                    "institutions": "UT Austin",
                    "affiliations": "University of Texas at Austin, Austin, TX, USA",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            run_institution_extraction(dataframe, temp_dir, "Scope Test")
            base = Path(temp_dir)
            institutions = pd.read_csv(base / "scope_test_institutions.csv")
            counts = pd.read_csv(base / "scope_test_institution_counts.csv")
            links = pd.read_csv(base / "scope_test_city_institution_links.csv")

        self.assertIn("University of Texas at Austin", set(institutions["normalized_institution"]))
        self.assertIn("University of Texas at Austin", set(counts["normalized_institution"]))
        self.assertIn("Austin", set(links["city"]))

    def test_missing_native_affiliations_do_not_infer_from_abstract_text(self) -> None:
        dataframe = pd.DataFrame(
            [
                {
                    "title": "Abstract mentions hospital",
                    "year": 2024,
                    "institutions": "",
                    "affiliations": "",
                    "abstract": "This hospital and university appear in topic text only.",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            run_institution_extraction(dataframe, temp_dir, "No Native")
            status_path = Path("data/processed/no_native_institution_no_usable_data_status.txt")
            institutions_path = Path(temp_dir) / "no_native_institutions.csv"
            status_text = status_path.read_text(encoding="utf-8")
        self.assertFalse(institutions_path.exists())
        self.assertIn("Institution maps skipped", status_text)

    def test_scopus_enrichment_attempts_doi_when_eid_missing(self) -> None:
        dataframe = pd.DataFrame(
            [
                {"title": "Has DOI", "year": 2024, "doi": "10.1000/example", "ris_identifier_blob": ""},
                {"title": "Missing DOI", "year": 2024, "doi": "", "ris_identifier_blob": ""},
            ]
        )
        payload = {
            "abstracts-retrieval-response": {
                "affiliation": [{"affilname": "Example University", "affiliation-city": "Austin", "affiliation-country": "United States"}]
            }
        }
        with patch("processing.analysis._scopus_request", return_value=(payload, 200)) as request:
            rows, counts, debug_rows = _run_scopus_institution_enrichment(dataframe, "key", "", "https://api.example.test")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(counts["records_attempted"], 1)
        self.assertEqual(counts["records_skipped_missing_identifier"], 1)
        self.assertEqual(counts.get("records_failed_after_request", 0), 0)
        self.assertEqual(counts["records_matched"], 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("missing DOI/EID", {row["not_attempted_reason"] for row in debug_rows})

    def test_openalex_missing_identifier_is_not_request_failure(self) -> None:
        dataframe = pd.DataFrame(
            [
                {"title": "", "year": 2024, "doi": "", "ris_identifier_blob": ""},
                {"title": "Has DOI", "year": 2024, "doi": "10.1000/example", "ris_identifier_blob": ""},
            ]
        )
        error = urllib.error.HTTPError("https://api.openalex.org/works", 500, "Server error", hdrs=None, fp=None)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("processing.analysis._openalex_request", side_effect=error) as request:
                rows, counts, debug_rows = _run_openalex_institution_enrichment(
                    dataframe,
                    "user@example.org",
                    Path(temp_dir) / "cache.json",
                    use_cache=False,
                    rebuild_cache=False,
                )
        self.assertEqual(request.call_count, 1)
        self.assertEqual(counts["records_attempted"], 1)
        self.assertEqual(counts["records_skipped_missing_identifier"], 1)
        self.assertEqual(counts["records_failed_after_request"], 1)
        self.assertFalse(rows)
        missing_debug = [row for row in debug_rows if row["not_attempted_reason"] == "missing DOI/title"]
        self.assertEqual(len(missing_debug), 1)


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
        self.assertIn("AD", dataframe.loc[0, "ris_affiliation_fields_present"])

    def test_ris_parser_captures_scopus_identifier_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ris_path = Path(temp_dir) / "sample.ris"
            ris_path.write_text(
                "\n".join(
                    [
                        "TY  - JOUR",
                        "TI  - Scopus Example",
                        "UR  - https://www.scopus.com/record/display.uri?eid=2-s2.0-123456789",
                        "ER  -",
                    ]
                ),
                encoding="utf-8",
            )

            dataframe = load_ris_files([str(ris_path)])

        self.assertIn("2-s2.0-123456789", dataframe.loc[0, "ris_identifier_blob"])

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


class GeoNamesLoadingTests(unittest.TestCase):
    def test_empty_optional_csv_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            empty = Path(temp_dir) / "empty.csv"
            empty.write_text("", encoding="utf-8")
            self.assertTrue(GeoCensus.safe_read_csv(empty).empty)

    def test_geonames_cities5000_loads_global_city_aliases(self) -> None:
        original_geonames_dir = GeoCensus.geonames_dir
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "countryInfo.txt").write_text("UG\tUGA\t800\tUG\tUganda\nUS\tUSA\t840\tUS\tUnited States\n", encoding="utf-8")
            (base / "admin1CodesASCII.txt").write_text("UG.C\tCentral Region\tCentral Region\t1\nUS.TX\tTexas\tTexas\t2\n", encoding="utf-8")
            (base / "admin2Codes.txt").write_text("UG.C.101\tKampala District\tKampala District\t3\n", encoding="utf-8")
            (base / "cities5000.txt").write_text(
                "\n".join(
                    [
                        "232422\tKampala\tKampala\t\t0.3136\t32.5811\tP\tPPLC\tUG\t\tC\t101\t\t\t1680600\t\t1190\tAfrica/Kampala\t2024-01-01",
                        "4701458\tItaly\tItaly\t\t32.1840\t-96.8847\tP\tPPL\tUS\t\tTX\t139\t\t\t2000\t\t180\tAmerica/Chicago\t2024-01-01",
                    ]
                ),
                encoding="utf-8",
            )
            try:
                GeoCensus.geonames_dir = lambda: base
                cache = GeoCensus.load_geonames_city_cache()
            finally:
                GeoCensus.geonames_dir = original_geonames_dir

        self.assertIn("kampala", cache)
        self.assertEqual(cache["kampala"]["country"], "Uganda")
        self.assertEqual(cache["kampala"]["source"], "geonames_cities5000")
        self.assertEqual(cache["kampala"]["latitude"], "0.3136")
        self.assertIn("kampala uganda", cache)
        self.assertIn("italy", cache)
        self.assertEqual(cache["italy"]["country"], "United States")


class VisualizerSlugScopingTests(unittest.TestCase):
    def test_visualizer_detects_only_exact_active_slug_support_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = root / "outputs"
            vos = root / "VOS"
            outputs.mkdir()
            vos.mkdir()
            core = outputs / "all_addiction_treatment_and_weightloss_surgery_year_limited_records.csv"
            exact_years = outputs / "all_addiction_treatment_and_weightloss_surgery_publication_years.csv"
            stale_years = outputs / "all_addiction_treatment_and_weightloss_surgery_and_py_2020_publication_years.csv"
            header = "title,authors,year,keywords\n"
            core.write_text(header + "A,Smith,2024,health\n", encoding="utf-8")
            exact_years.write_text("year,publications\n2024,1\n", encoding="utf-8")
            stale_years.write_text("year,publications\n2020,99\n", encoding="utf-8")
            with patch.object(visualize_bibliometrics, "OUTPUTS_DIR", outputs), patch.object(visualize_bibliometrics, "VOS_DIR", vos):
                files = visualize_bibliometrics.detect_files(core=core)
            self.assertEqual(files["core"], core)
            self.assertEqual(files["year_counts"], exact_years)
            self.assertNotEqual(files["year_counts"], stale_years)

    def test_keyword_extraction_does_not_require_keyword_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            core = Path(temp_dir) / "sample_year_limited_records.csv"
            core.write_text("title,authors,year\nAddiction treatment surgery,Smith,2024\n", encoding="utf-8")
            with patch.object(visualize_bibliometrics, "PROCESSED_DIR", Path(temp_dir) / "processed"), patch.object(visualize_bibliometrics, "VOS_DIR", Path(temp_dir) / "vos"):
                outputs = visualize_bibliometrics.extract_visual_keyword_pipelines(core, "", [])
            self.assertIn("filtered", outputs)


if __name__ == "__main__":
    unittest.main()
