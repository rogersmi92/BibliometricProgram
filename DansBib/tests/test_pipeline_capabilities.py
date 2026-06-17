#!/usr/bin/env python3
"""Regression checks for shared pipeline capabilities and GUI command building."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from BiblioGUI.DansBibGUI.utils import app_config
from BiblioGUI.DansBibGUI.utils.pipeline_runner import (
    ANALYSIS_OPTIONS,
    CONCEPT_PROFILES,
    LIVE_API_SOURCES,
    MAP_OPTIONS,
    PipelineRequest,
    RuntimePaths,
    cleanup_previous_run_files,
    discover_previous_run_files,
    generated_file_matches_slug,
    run_pipeline,
)
from DansBib.pipeline_capabilities import CONCEPT_PROFILES as PIPELINE_CONCEPT_PROFILES
from DansBib.pipeline_capabilities import LIVE_API_DATABASES, SCALING_MODES, RisInput


class PipelineCapabilitiesTests(unittest.TestCase):
    def test_gui_constants_follow_pipeline_capabilities(self) -> None:
        self.assertEqual(LIVE_API_SOURCES, LIVE_API_DATABASES)
        self.assertEqual(set(CONCEPT_PROFILES), set(PIPELINE_CONCEPT_PROFILES))
        self.assertEqual(set(app_config.SOURCE_DEFAULTS), set(LIVE_API_DATABASES))
        self.assertNotIn("covidence", LIVE_API_SOURCES)

    def test_ris_only_command_omits_live_databases(self) -> None:
        command_log = self._run_dry_command(ris_only=True, sources=["openalex", "pubmed"], ris_source="unknown")
        self.assertIn("--databases", command_log)
        self.assertNotIn("openalex,pubmed", command_log)
        self.assertIn("--ris-files", command_log)
        self.assertIn("--ris-file-sources unknown", command_log)

    def test_ris_plus_live_sources_command_includes_both(self) -> None:
        command_log = self._run_dry_command(ris_only=False, sources=["openalex", "pubmed"], ris_source="wos")
        self.assertIn("--databases openalex,pubmed", command_log)
        self.assertIn("--ris-files", command_log)
        self.assertIn("--ris-file-sources wos", command_log)

    def test_pipeline_runner_includes_geocensus_when_selected(self) -> None:
        command_log = self._run_dry_command(
            ris_only=True,
            sources=[],
            ris_source="unknown",
            extract_geography=True,
            extract_drugs=True,
            extract_procedures=True,
            extract_demographics=True,
            generate_visuals=True,
        )
        self.assertIn("GeoCensus / term extraction", command_log)
        self.assertIn("GeoCensus.py", command_log)
        self.assertIn("--run-geography", command_log)
        self.assertIn("--geo-scope global", command_log)
        self.assertIn("--run-drugs", command_log)
        self.assertIn("--run-procedures", command_log)
        self.assertIn("--run-demographics", command_log)
        self.assertIn("visualize_bibliometrics.py", command_log)

    def test_gui_capability_labels_include_requested_options(self) -> None:
        qt_text = (REPO_ROOT / "BiblioGUI/DansBibGUI/qt_gui.py").read_text(encoding="utf-8")
        self.assertIn("Analysis Options", qt_text)
        self.assertIn("Map Options", qt_text)
        for key in ("geography", "drugs", "procedures", "demographics", "keywords"):
            self.assertIn(key, ANALYSIS_OPTIONS)
        for key in ("world", "us", "priority_adm1", "priority_adm2", "build_world_adm0", "check_map_status"):
            self.assertIn(key, MAP_OPTIONS)

    def test_sample_librarian_ris_fixture_exists(self) -> None:
        fixture = REPO_ROOT / "DansBib/tests/fixtures/sample_librarian_training.ris"
        text = fixture.read_text(encoding="utf-8")
        self.assertIn("TY  - JOUR", text)
        self.assertIn("TI  - Telehealth Support for Rural Cancer Treatment Access", text)
        self.assertIn("AD  - University of Texas", text)

    def test_cleanup_deletes_only_exact_current_slug_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime_paths(Path(temp_dir))
            exact = runtime.outputs_dir / "all_addiction_treatment_and_weightloss_surgery_geographic_terms.csv"
            similar = runtime.outputs_dir / "all_addiction_treatment_and_weightloss_surgery_and_py_2020_geographic_terms.csv"
            reference = runtime.data_dir / "reference" / "institution_geocache.csv"
            for path in (exact, similar, reference):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            result = cleanup_previous_run_files(
                "all_addiction_treatment_and_weightloss_surgery",
                runtime,
            )
            self.assertFalse(exact.exists())
            self.assertTrue(similar.exists())
            self.assertTrue(reference.exists())
            self.assertIn("Cleared 1 previous files", str(result["message"]))

    def test_cleanup_similar_runs_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime_paths(Path(temp_dir))
            similar = runtime.outputs_dir / "base_slug_and_old_year_geographic_terms.csv"
            similar.parent.mkdir(parents=True, exist_ok=True)
            similar.write_text("x", encoding="utf-8")
            self.assertEqual(discover_previous_run_files("base_slug", runtime), [])
            self.assertEqual(discover_previous_run_files("base_slug", runtime, include_similar=True), [similar])

    def test_missing_cleanup_targets_do_not_crash_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime_paths(Path(temp_dir))
            messages: list[str] = []
            result = cleanup_previous_run_files("missing_slug", runtime, log_callback=messages.append)
            self.assertIn("No previous generated files found", str(result["message"]))
            self.assertTrue(messages)

    def test_generated_file_exact_match_does_not_use_unsafe_prefix(self) -> None:
        self.assertTrue(generated_file_matches_slug(Path("slug_geographic_terms.csv"), "slug"))
        self.assertFalse(generated_file_matches_slug(Path("slug_and_old_geographic_terms.csv"), "slug"))
        self.assertTrue(generated_file_matches_slug(Path("slug_and_old_geographic_terms.csv"), "slug", include_similar=True))

    def _run_dry_command(self, ris_only: bool, sources: list[str], ris_source: str, **request_overrides: object) -> str:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ris_file = temp_path / "sample.ris"
            ris_file.write_text("TY  - JOUR\nTI  - Example\nER  -\n", encoding="utf-8")
            log_path = temp_path / "pipeline.log"
            settings = app_config.AppConfig(
                dansbib_path=str(REPO_ROOT / "DansBib"),
                output_folder=str(temp_path / "outputs"),
                ris_input_folder=str(temp_path),
                logs_folder=str(temp_path / "logs"),
                pipeline_python=sys.executable,
            )
            request = PipelineRequest(
                query="test query",
                sources=sources,
                filters={"review": None, "early_access": None, "open_access": None},
                ris_files=[ris_file],
                include_ris=True,
                scaling_mode=SCALING_MODES[0],
                ris_inputs=[RisInput(path=ris_file, source=ris_source)],
                ris_only_mode=ris_only,
                dry_run=True,
                log_path=log_path,
                **request_overrides,
            )
            run_pipeline(request, settings)
            return log_path.read_text(encoding="utf-8")

    def _runtime_paths(self, temp_path: Path) -> RuntimePaths:
        data_dir = temp_path / "data"
        return RuntimePaths(
            root=temp_path,
            data_dir=data_dir,
            raw_dir=data_dir / "raw",
            outputs_dir=data_dir / "outputs",
            processed_dir=data_dir / "processed",
            visuals_dir=data_dir / "visuals",
            vos_dir=data_dir / "VOS",
            matplotlib_cache_dir=data_dir / "cache" / "matplotlib",
            logs_dir=temp_path / "logs",
        )


if __name__ == "__main__":
    unittest.main()
