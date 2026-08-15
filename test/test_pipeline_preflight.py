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


EXPANSION_SOURCE = '''from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        # [MARKER: 序列选择]
        page.locator("#series").click()
        # [MARKER: Meta 信息工具]
        page.locator("#meta-open").click()
        # [MARKER: Meta 信息工具]
        page.locator("#meta-close").click()
        browser.close()

run()
'''


def _expansion_config(root: Path, overrides: dict | None = None) -> PipelineConfig:
    script = root / "processed_expansion.py"
    annotations = root / "replica_annotations_expansion.json"
    script.write_text(EXPANSION_SOURCE, encoding="utf-8", newline="\n")
    annotations.write_text(json.dumps({
        "schema_version": 1,
        "source_script_sha256": hashlib.sha256(EXPANSION_SOURCE.encode()).hexdigest(),
        "markers": [
            {"marker_id": "uuid-sel", "line": 7, "label": "序列选择"},
            {"marker_id": "uuid-open", "line": 9, "label": "Meta 信息工具"},
            {"marker_id": "uuid-close", "line": 11, "label": "Meta 信息工具"},
        ],
    }), encoding="utf-8")
    base = {
        "hospital": "fixture",
        "source_script": script,
        "annotations_path": annotations,
        "output_root": root / "runs",
        "expand_all_series": True,
    }
    base.update(overrides or {})
    return PipelineConfig(**base)


class ExpansionPreflightTests(unittest.TestCase):
    def test_expansion_disabled_by_default_needs_no_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Default expansion config (off) with a bare recording must pass.
            plan_root = root / "runs"
            script = root / "plain.py"
            annotations = root / "plain_annotations.json"
            plain = "# [MARKER: 报告截图]\npage.locator('#open-viewer').click()\n"
            script.write_text(plain, encoding="utf-8", newline="\n")
            annotations.write_text(json.dumps({
                "schema_version": 1,
                "source_script_sha256": hashlib.sha256(plain.encode()).hexdigest(),
                "markers": [{"marker_id": "m-1", "line": 1, "label": "报告截图"}],
            }), encoding="utf-8")
            result = run_preflight(PipelineConfig("fixture", script, annotations, plan_root))
        self.assertTrue(result.ok, result.errors)
        self.assertFalse(result.errors)

    def test_complete_template_passes_expansion_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(_expansion_config(Path(tmp)))
        self.assertTrue(result.ok, result.errors)

    def test_missing_metadata_close_fails_expansion_preflight(self):
        source = EXPANSION_SOURCE.replace(
            "        # [MARKER: Meta 信息工具]\n        page.locator(\"#meta-close\").click()\n", ""
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "p.py"
            script.write_text(source, encoding="utf-8", newline="\n")
            annotations = root / "a.json"
            annotations.write_text(json.dumps({
                "schema_version": 1,
                "source_script_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "markers": [
                    {"marker_id": "uuid-sel", "line": 7, "label": "序列选择"},
                    {"marker_id": "uuid-open", "line": 9, "label": "Meta 信息工具"},
                ],
            }), encoding="utf-8")
            result = run_preflight(PipelineConfig("fixture", script, annotations, root / "runs", expand_all_series=True))
        self.assertIn("expansion_missing_metadata_close", result.errors)
        self.assertFalse(result.ok)

    def test_missing_series_select_fails_expansion_preflight(self):
        source = EXPANSION_SOURCE.replace(
            "        # [MARKER: 序列选择]\n        page.locator(\"#series\").click()\n", ""
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "p2.py"
            script.write_text(source, encoding="utf-8", newline="\n")
            annotations = root / "a2.json"
            annotations.write_text(json.dumps({
                "schema_version": 1,
                "source_script_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "markers": [
                    {"marker_id": "uuid-open", "line": 7, "label": "Meta 信息工具"},
                    {"marker_id": "uuid-close", "line": 9, "label": "Meta 信息工具"},
                ],
            }), encoding="utf-8")
            result = run_preflight(PipelineConfig("fixture", script, annotations, root / "runs", expand_all_series=True))
        self.assertIn("expansion_missing_series_select", result.errors)
        self.assertFalse(result.ok)

    def test_max_series_over_cap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(_expansion_config(Path(tmp), {"max_series": 200}))
        self.assertIn("expansion_max_series_invalid", result.errors)
        self.assertFalse(result.ok)

    def test_budget_product_exceeding_total_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(
                _expansion_config(Path(tmp), {"max_series": 40, "per_series_timeout_s": 100, "total_series_timeout_s": 600})
            )
        self.assertIn("expansion_budget_product_exceeds_total", result.errors)
        self.assertFalse(result.ok)

    def test_unsupported_capture_mode_is_a_warning_not_a_hard_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(_expansion_config(Path(tmp), {"viewer_capture_mode": "all_frames"}))
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(any(w.startswith("expansion_viewer_capture_mode_unsupported") for w in result.warnings))

    def test_implemented_first_stable_frame_mode_produces_no_warning(self):
        """MVP: only first_stable_frame is implemented; using it (the default)
        must not raise an 'unsupported' warning."""
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(
                _expansion_config(Path(tmp), {"viewer_capture_mode": "first_stable_frame"})
            )
        self.assertTrue(result.ok, result.errors)
        self.assertFalse(any(
            w.startswith("expansion_viewer_capture_mode_unsupported")
            for w in result.warnings
        ))


if __name__ == "__main__":
    unittest.main()
