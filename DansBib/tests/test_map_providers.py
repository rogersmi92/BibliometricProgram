#!/usr/bin/env python3
"""Tests for the local map provider registry and map hygiene rules."""

from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from DansBib.validate_visuals_png_only import find_non_png_files
from DansBib.utils import map_providers


class MapProviderRegistryTests(unittest.TestCase):
    def test_registry_paths_use_lowercase_maps_layout(self) -> None:
        self.assertIn("data/reference/maps", str(map_providers.MAPS_ROOT))
        self.assertNotIn("data/reference/Maps", str(map_providers.MAPS_ROOT))
        self.assertEqual(map_providers.census_boundary_dir("states").name, "states")
        self.assertEqual(map_providers.geoboundaries_dir("GBR", "ADM1").name, "ADM1")
        self.assertEqual(map_providers.geonames_dir().name, "geonames")
        self.assertEqual(map_providers.institutions_dir().name, "institutions")

    def test_expected_readme_placeholders_exist(self) -> None:
        missing = [path for path in map_providers.expected_readme_paths() if not path.exists()]
        self.assertEqual(missing, [])

    def test_institution_csv_headers(self) -> None:
        for filename, expected in map_providers.EXPECTED_INSTITUTION_HEADERS.items():
            path = map_providers.institutions_dir() / filename
            with path.open("r", encoding="utf-8", newline="") as handle:
                header = next(csv.reader(handle))
            self.assertEqual(header, expected)

    def test_old_maps_folder_is_not_expected(self) -> None:
        self.assertNotIn(map_providers.REFERENCE_DIR / "Maps", map_providers.expected_readme_paths())
        result = subprocess.run(
            ["git", "ls-files", "DansBib/data/reference/Maps"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "")

    def test_natural_earth_is_not_required(self) -> None:
        text = (map_providers.MAPS_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Natural Earth is no longer required", text)
        statuses = map_providers.report_missing_map_packages()
        self.assertTrue(statuses)
        self.assertTrue(all("Natural Earth" not in status.label for status in statuses))

    def test_priority_country_registry_includes_added_countries(self) -> None:
        for iso3 in ("AUS", "BRA", "ISR", "SGP", "POL", "COL", "HRV", "TUR"):
            self.assertIn(iso3, map_providers.IMPORTANT_COUNTRY_ISO3)

    def test_priority_country_paths_include_expected_adm_geojson_files(self) -> None:
        for iso3 in map_providers.IMPORTANT_COUNTRY_ISO3:
            adm1 = map_providers.geoboundaries_geojson_path(iso3, "ADM1")
            adm2 = map_providers.geoboundaries_geojson_path(iso3, "ADM2")
            self.assertEqual(adm1.name, f"{iso3}_ADM1.geojson")
            self.assertEqual(adm2.name, f"{iso3}_ADM2.geojson")
            self.assertIn(f"geoboundaries/{iso3}/ADM1", str(adm1))
            self.assertIn(f"geoboundaries/{iso3}/ADM2", str(adm2))

    def test_world_adm0_index_and_drawable_paths_are_distinct(self) -> None:
        index_path = map_providers.world_adm0_index_path()
        drawable_path = map_providers.world_adm0_geojson_path()
        self.assertEqual(index_path.name, "WORLD_ADM0_INDEX.json")
        self.assertEqual(drawable_path.name, "WORLD_ADM0.geojson")
        self.assertTrue(map_providers.is_world_adm0_index(index_path))
        self.assertFalse(map_providers.is_world_adm0_index(drawable_path))

    def test_missing_adm2_uses_adm1_country_boundary(self) -> None:
        original_root = map_providers.MAPS_ROOT
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                map_providers.MAPS_ROOT = Path(temp_dir)
                adm1 = map_providers.geoboundaries_geojson_path("AUS", "ADM1")
                adm1.parent.mkdir(parents=True)
                adm1.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
                self.assertEqual(map_providers.country_admin_boundary_file("AUS"), adm1)
                self.assertIsNone(map_providers.geoboundaries_boundary_file("AUS", "ADM2"))
        finally:
            map_providers.MAPS_ROOT = original_root

    def test_gbr_provider_prefers_ons_lad_before_geoboundaries(self) -> None:
        original_root = map_providers.MAPS_ROOT
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                map_providers.MAPS_ROOT = Path(temp_dir)
                adm2 = map_providers.geoboundaries_geojson_path("GBR", "ADM2")
                adm2.parent.mkdir(parents=True)
                adm2.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
                ons = map_providers.gbr_ons_dir() / "Local_Authority_Districts_December_2024_BGC.geojson"
                ons.parent.mkdir(parents=True)
                ons.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

                selected = map_providers.select_country_boundary_provider("GBR")

                self.assertEqual(selected.provider, "ONS.gov.uk")
                self.assertEqual(selected.admin_level, "ONS_LAD")
                self.assertEqual(selected.path, ons)
                self.assertFalse(selected.fallback_used)
        finally:
            map_providers.MAPS_ROOT = original_root

    def test_gbr_provider_falls_back_to_adm2_when_ons_missing(self) -> None:
        original_root = map_providers.MAPS_ROOT
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                map_providers.MAPS_ROOT = Path(temp_dir)
                adm2 = map_providers.geoboundaries_geojson_path("GBR", "ADM2")
                adm2.parent.mkdir(parents=True)
                adm2.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

                selected = map_providers.select_country_boundary_provider("GBR")

                self.assertEqual(selected.provider, "geoBoundaries")
                self.assertEqual(selected.admin_level, "ADM2")
                self.assertTrue(selected.fallback_used)
        finally:
            map_providers.MAPS_ROOT = original_root

    def test_geoboundaries_api_url_uses_current_endpoint(self) -> None:
        self.assertEqual(
            map_providers.geoboundaries_api_url("tur", "adm2"),
            "https://www.geoboundaries.org/api/current/gbOpen/TUR/ADM2/",
        )

    def test_missing_world_adm0_warns_without_crashing(self) -> None:
        original_root = map_providers.MAPS_ROOT
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                map_providers.MAPS_ROOT = Path(temp_dir)
                warnings: list[str] = []
                self.assertIsNone(map_providers.build_world_adm0_geojson(warnings, timeout=1))
                self.assertTrue(any("world ADM0 index missing" in warning for warning in warnings))
        finally:
            map_providers.MAPS_ROOT = original_root

    def test_visuals_png_only_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "figure.png").write_text("", encoding="utf-8")
            (root / "notes.txt").write_text("", encoding="utf-8")
            offenders = find_non_png_files(root)
        self.assertEqual([path.name for path in offenders], ["notes.txt"])


if __name__ == "__main__":
    unittest.main()
