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
from BiblioGUI.DansBibGUI.utils.pipeline_runner import CONCEPT_PROFILES, LIVE_API_SOURCES, PipelineRequest, run_pipeline
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

    def _run_dry_command(self, ris_only: bool, sources: list[str], ris_source: str) -> str:
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
            )
            run_pipeline(request, settings)
            return log_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
