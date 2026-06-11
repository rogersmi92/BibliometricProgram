#!/usr/bin/env python3
"""Regression tests for WORLD_ADM0 index-to-GeoJSON building."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from DansBib.utils import map_providers


def tiny_feature(iso: str) -> dict[str, object]:
    return {
        "type": "Feature",
        "properties": {"source_iso": iso},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[(0, 0), (1, 0), (1, 1), (0, 0)]],
        },
    }


class WorldAdm0BuilderTests(unittest.TestCase):
    def test_valid_list_style_index_builds_feature_collection(self) -> None:
        with self._patched_paths() as paths:
            source = self._write_country(paths["source"], "AAA")
            records = [self._record(idx, source, f"A{idx:03d}") for idx in range(100)]
            self._write_index(records)
            output = map_providers.build_world_adm0_geojson(force=True)
            self.assertIsNotNone(output)
            payload = json.loads(Path(output).read_text(encoding="utf-8"))
            self.assertEqual(payload["type"], "FeatureCollection")
            self.assertEqual(len(payload["features"]), 100)
            self.assertTrue(map_providers.world_adm0_report_txt_path().exists())
            self.assertTrue(map_providers.world_adm0_report_json_path().exists())

    def test_dict_wrapped_index_builds_correctly(self) -> None:
        with self._patched_paths() as paths:
            source = self._write_country(paths["source"], "BBB")
            self._write_index({"data": [self._record(idx, source, f"B{idx:03d}") for idx in range(100)]})
            output = map_providers.build_world_adm0_geojson(force=True)
            self.assertIsNotNone(output)

    def test_missing_index_reports_useful_error(self) -> None:
        with self._patched_paths():
            warnings: list[str] = []
            self.assertIsNone(map_providers.build_world_adm0_geojson(warnings, force=True))
            self.assertTrue(any("index missing" in warning for warning in warnings))
            report = json.loads(map_providers.world_adm0_report_json_path().read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "missing_index")

    def test_malformed_json_reports_preview(self) -> None:
        with self._patched_paths():
            map_providers.world_adm0_index_path().parent.mkdir(parents=True, exist_ok=True)
            map_providers.world_adm0_index_path().write_text("{bad json", encoding="utf-8")
            warnings: list[str] = []
            self.assertIsNone(map_providers.build_world_adm0_geojson(warnings, force=True))
            self.assertTrue(any("JSON parse error" in warning for warning in warnings))
            self.assertTrue(any("Index preview" in warning for warning in warnings))

    def test_missing_urls_and_bad_country_are_reported_but_do_not_kill_build(self) -> None:
        with self._patched_paths() as paths:
            source = self._write_country(paths["source"], "CCC")
            bad = paths["source"] / "bad.geojson"
            bad.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
            records = [self._record(idx, source, f"C{idx:03d}") for idx in range(100)]
            records.append({"boundaryISO": "MISS", "boundaryName": "Missing URL", "boundaryType": "ADM0"})
            records.append(self._record(101, bad, "BAD"))
            self._write_index(records)
            output = map_providers.build_world_adm0_geojson(force=True)
            self.assertIsNotNone(output)
            report = json.loads(map_providers.world_adm0_report_json_path().read_text(encoding="utf-8"))
            self.assertGreaterEqual(report["failure_count"], 1)
            self.assertTrue(any("records missing simplifiedGeometryGeoJSON" in warning for warning in report["warnings"]))

    def test_all_downloads_failing_returns_none_and_creates_report(self) -> None:
        with self._patched_paths():
            self._write_index([{"boundaryISO": f"F{idx:03d}", "boundaryName": "Fail", "boundaryType": "ADM0", "simplifiedGeometryGeoJSON": "file:///missing.geojson"} for idx in range(3)])
            self.assertIsNone(map_providers.build_world_adm0_geojson(force=True))
            report = json.loads(map_providers.world_adm0_report_json_path().read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed_insufficient_features")
            self.assertEqual(report["feature_count"], 0)

    def test_cached_country_files_can_be_used_without_network(self) -> None:
        with self._patched_paths() as paths:
            cache = map_providers.geoboundaries_cache_dir("ADM0")
            cache.mkdir(parents=True, exist_ok=True)
            for idx in range(100):
                (cache / f"Z{idx:03d}_ADM0.geojson").write_text(json.dumps(tiny_feature(f"Z{idx:03d}")), encoding="utf-8")
            self._write_index([self._record(idx, paths["source"] / "missing.geojson", f"Z{idx:03d}") for idx in range(100)])
            output = map_providers.build_world_adm0_geojson(force=True)
            self.assertIsNotNone(output)

    def test_missing_india_is_repaired_from_local_adm1_boundary(self) -> None:
        with self._patched_paths() as paths:
            source = self._write_country(paths["source"], "AAA")
            records = [self._record(idx, source, f"A{idx:03d}") for idx in range(100)]
            self._write_index(records)
            india_dir = map_providers.geoboundaries_dir("IND", "ADM1")
            india_dir.mkdir(parents=True, exist_ok=True)
            (india_dir / "IND_ADM1.geojson").write_text(
                json.dumps({"type": "FeatureCollection", "features": [tiny_feature("IND")]}),
                encoding="utf-8",
            )

            output = map_providers.build_world_adm0_geojson(force=True)

            self.assertIsNotNone(output)
            payload = json.loads(Path(output).read_text(encoding="utf-8"))
            iso_values = {
                feature.get("properties", {}).get("boundaryISO")
                for feature in payload["features"]
                if isinstance(feature, dict)
            }
            self.assertIn("IND", iso_values)

    def _record(self, idx: int, source: Path, iso: str) -> dict[str, object]:
        return {
            "boundaryISO": iso,
            "boundaryName": f"Country {iso}",
            "boundaryType": "ADM0",
            "simplifiedGeometryGeoJSON": source.as_uri(),
        }

    def _write_country(self, folder: Path, iso: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{iso}.geojson"
        path.write_text(json.dumps(tiny_feature(iso)), encoding="utf-8")
        return path

    def _write_index(self, payload: object) -> None:
        map_providers.world_adm0_index_path().parent.mkdir(parents=True, exist_ok=True)
        map_providers.world_adm0_index_path().write_text(json.dumps(payload), encoding="utf-8")

    def _patched_paths(self):
        return _PatchedMapProviderPaths()


class _PatchedMapProviderPaths:
    def __enter__(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_root = map_providers.ROOT
        self.original_maps_root = map_providers.MAPS_ROOT
        self.original_processed_dir = map_providers.PROCESSED_DIR
        map_providers.ROOT = root / "DansBib"
        map_providers.MAPS_ROOT = map_providers.ROOT / "data" / "reference" / "maps"
        map_providers.PROCESSED_DIR = map_providers.ROOT / "data" / "processed"
        return {"root": root, "source": root / "source"}

    def __exit__(self, exc_type, exc, traceback):
        map_providers.ROOT = self.original_root
        map_providers.MAPS_ROOT = self.original_maps_root
        map_providers.PROCESSED_DIR = self.original_processed_dir
        self.temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
