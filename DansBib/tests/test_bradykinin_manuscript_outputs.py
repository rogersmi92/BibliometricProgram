#!/usr/bin/env python3
"""Regression tests for the Bradykinin manuscript bundle generator."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bradykinin_manuscript_outputs as brady
from processing.metrics import calculate_h_index


SLUG = "bradykinin_mediated_angioedema"


def synthetic_records(count: int = brady.EXPECTED_RECORDS) -> pd.DataFrame:
    countries = [
        "USA",
        "Germany",
        "United Kingdom",
        "France",
        "Italy",
        "Spain",
        "Canada",
        "Japan",
        "Brazil",
        "Netherlands",
        "Turkey",
    ]
    journals = ["Allergy", "Journal of Allergy and Clinical Immunology", "Clinical Immunology"]
    authors = [
        "levy, j; owers-james, c; cristea, m",
        "smith, a; van dyke, b; o'neil, c",
        "garcia-lopez, m; martin, p; chen, q",
    ]
    rows = []
    for idx in range(count):
        year = 2007 + (idx % 20)
        country = countries[idx % len(countries)]
        second_country = countries[(idx + 3) % len(countries)]
        rows.append(
            {
                "title": f"Bradykinin mediated angioedema treatment study {idx:04d}",
                "doi": f"10.1000/brady.{idx}",
                "authors": authors[idx % len(authors)],
                "year": year,
                "citations": max(0, count - idx),
                "source": journals[idx % len(journals)],
                "abstract": "Treatment registry evidence for hereditary angioedema and bradykinin pathway inhibitor therapy.",
                "keywords": "HAE; bradykinin; angioedema; due; can; C1-INH; treatment; kallikrein inhibitor",
                "countries": f"{country}; {second_country}",
                "citation_available": True,
            }
        )
    return pd.DataFrame(rows)


class BradykininManuscriptOutputTests(unittest.TestCase):
    def test_validation_rejects_non_bradykinin_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            synthetic_records().to_csv(base / "transhealthcare_year_limited_records.csv", index=False)
            with self.assertRaises(brady.ValidationError):
                brady.load_validated_dataset("transhealthcare", base)

    def test_dataset_validation_requires_1481_records_and_20_years(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            synthetic_records().to_csv(base / f"{SLUG}_year_limited_records.csv", index=False)
            ctx = brady.load_validated_dataset(SLUG, base)
        self.assertEqual(len(ctx.records), 1481)
        self.assertEqual((ctx.min_year, ctx.max_year), (2007, 2026))
        self.assertTrue(ctx.partial_final_year)

    def test_country_top10_and_keyword_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            records = synthetic_records()
            records.to_csv(base / f"{SLUG}_year_limited_records.csv", index=False)
            with patch.object(brady, "OUTPUTS_DIR", base):
                ctx = brady.load_validated_dataset(SLUG, base)
                _, _, rankings_path, plotted, rankings = brady.country_productivity(ctx)
                _, keyword_csv, _, keyword_counts, per_record = brady.keyword_frequencies(ctx)
                self.assertEqual(rankings.head(10)["country"].nunique(), 10)
                self.assertEqual(plotted["country"].nunique(), 10)
                self.assertTrue(rankings_path.read_text(encoding="utf-8").strip())
                displayed = set(keyword_counts.head(25)["normalized_keyword"])
                self.assertNotIn("due", displayed)
                self.assertNotIn("can", displayed)
                self.assertNotIn("treatment", displayed)
                self.assertIn("hae", set(keyword_counts["normalized_keyword"]))
                self.assertTrue(keyword_csv.read_text(encoding="utf-8").strip())
                self.assertEqual(len(per_record), 1481)

    def test_author_labels_preserve_canonical_ids(self) -> None:
        self.assertEqual(brady.display_author_name("levy, j"), "Levy, J")
        self.assertEqual(brady.display_author_name("owers-james, c"), "Owers-James, C")
        self.assertEqual(brady.display_author_name("cristea, m"), "Cristea, M")
        self.assertEqual(brady.normalize_author_id("Levy, J"), "levy, j")

    def test_full_bundle_outputs_and_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            records = synthetic_records()
            records.to_csv(base / f"{SLUG}_year_limited_records.csv", index=False)
            with patch.object(brady, "OUTPUTS_DIR", base):
                files = brady.generate_bundle(SLUG)
                table1 = pd.read_csv(base / f"{SLUG}_table1_bibliometric_characteristics.csv")
                table2 = pd.read_csv(base / f"{SLUG}_table2_top_publications.csv")
                figure6_nodes = pd.read_csv(base / f"{SLUG}_figure6_keyword_nodes.csv")
                label_audit = pd.read_csv(base / f"{SLUG}_figure6_keyword_label_audit.csv")
                expected_names = {
                    f"{SLUG}_figure2_annual_publication_trends.png",
                    f"{SLUG}_figure2_annual_publication_trends.csv",
                    f"{SLUG}_figure3_country_trends_top10.png",
                    f"{SLUG}_figure3_country_trends_top10.csv",
                    f"{SLUG}_figure3_country_rankings.csv",
                    f"{SLUG}_figure4_author_collaboration_network.png",
                    f"{SLUG}_figure4_author_nodes.csv",
                    f"{SLUG}_figure4_author_edges.csv",
                    f"{SLUG}_figure5_keyword_frequencies.png",
                    f"{SLUG}_figure5_keyword_frequencies.csv",
                    f"{SLUG}_figure5_keyword_filter_audit.csv",
                    f"{SLUG}_figure6_keyword_cooccurrence_network.png",
                    f"{SLUG}_figure6_keyword_nodes.csv",
                    f"{SLUG}_figure6_keyword_edges.csv",
                    f"{SLUG}_figure6_keyword_label_audit.csv",
                    f"{SLUG}_table1_bibliometric_characteristics.csv",
                    f"{SLUG}_table1_bibliometric_characteristics.md",
                    f"{SLUG}_table2_top_publications.csv",
                    f"{SLUG}_table2_top_publications_review.csv",
                    f"{SLUG}_table2_top_publications.md",
                    f"{SLUG}_manuscript_results_summary.md",
                }
                self.assertEqual({path.name for path in files.values()}, expected_names)
                self.assertTrue(all(path.exists() and path.stat().st_size > 0 for path in files.values()))
                self.assertTrue(all(path.name.startswith(SLUG) for path in files.values()))
                self.assertNotIn("Filtered Filtered", (base / f"{SLUG}_manuscript_results_summary.md").read_text(encoding="utf-8"))
                self.assertNotIn("Cleaned", (base / f"{SLUG}_figure5_keyword_frequencies.csv").read_text(encoding="utf-8"))
                self.assertEqual(table1.loc[table1["metric"].eq("Total publications"), "value"].iloc[0], "1481")
                h_index_value = table1.loc[table1["metric"].eq("Corpus h-index"), "value"].iloc[0]
                self.assertEqual(int(h_index_value), calculate_h_index(records["citations"]))
                self.assertEqual(len(table2), 10)
                self.assertTrue((figure6_nodes["labeled"] == "yes").all())
                self.assertFalse((label_audit["labeled"] == "no").any())


if __name__ == "__main__":
    unittest.main()
