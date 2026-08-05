import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_models import PipelineConfig
from pipeline_preflight import run_preflight


SOURCE = '''from playwright.sync_api import sync_playwright
# [MARKER: 报告截图]
page.locator("#report").click()
'''


class PipelinePreflightTests(unittest.TestCase):
    def make_config(self, root: Path, source: str = SOURCE) -> PipelineConfig:
        script = root / "processed.py"
        annotations = root / "replica_annotations.json"
        script.write_text(source, encoding="utf-8", newline="\n")
        annotations.write_text(json.dumps({
            "schema_version": 1,
            "source_script_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "markers": [{"marker_id": "m-1", "line": 2, "label": "报告截图"}],
        }), encoding="utf-8")
        return PipelineConfig("fixture", script, annotations, root / "runs")

    def test_valid_recording_passes_without_starting_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(self.make_config(Path(tmp)))
        self.assertTrue(result.ok, result.errors)

    def test_stale_annotations_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_config(root)
            config.annotations_path.write_text(
                '{"schema_version":1,"source_script_sha256":"stale","markers":[]}',
                encoding="utf-8",
            )
            result = run_preflight(config)
        self.assertIn("annotations_hash_mismatch", result.errors)

    def test_missing_marker_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(self.make_config(Path(tmp), "print('no markers')\n"))
        self.assertIn("no_supported_markers", result.errors)

    def test_storage_state_mode_requires_an_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(Path(tmp))
            config = PipelineConfig(
                **{**config.__dict__, "auth_mode": "storage-state",
                   "storage_state": Path(tmp) / "missing.json"}
            )
            result = run_preflight(config)
        self.assertIn("storage_state_missing", result.errors)

    def test_hospital_name_cannot_escape_output_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(Path(tmp))
            config = PipelineConfig(**{**config.__dict__, "hospital": "../escape"})
            result = run_preflight(config)
        self.assertIn("hospital_name_invalid", result.errors)

    def test_missing_pinned_interpreter_fails_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "pipeline_preflight.codegen_python_executable",
                side_effect=RuntimeError("missing"),
            ):
                result = run_preflight(self.make_config(Path(tmp)))
        self.assertIn("interpreter_missing", result.errors)

    def test_annotation_line_or_label_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_config(root)
            payload = json.loads(config.annotations_path.read_text(encoding="utf-8"))
            payload["markers"][0]["line"] = 3
            config.annotations_path.write_text(
                json.dumps(payload), encoding="utf-8"
            )
            result = run_preflight(config)
        self.assertIn("marker_identity_mismatch", result.errors)


if __name__ == "__main__":
    unittest.main()
