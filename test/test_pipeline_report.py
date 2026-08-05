"""Level-1/2 tests for deterministic pipeline report aggregation and writing."""

import json
import tempfile
import unittest
from pathlib import Path

from pipeline_io import create_run_layout
from pipeline_models import PipelineConfig, PipelineStage, PipelineStatus, StageResult
from pipeline_report import aggregate_status, write_pipeline_report

BRIEF_CONFIG = PipelineConfig(
    hospital="fixture",
    source_script=Path("processed_fixture.py"),
    annotations_path=Path("annotations_fixture.json"),
    output_root=Path("out"),
)


class AggregateStatusTests(unittest.TestCase):
    def test_empty_is_success(self):
        self.assertEqual(aggregate_status([]), PipelineStatus.SUCCESS)

    def test_failed_beats_partial(self):
        results = [
            StageResult(PipelineStage.LIVE_CAPTURE, PipelineStatus.PARTIAL),
            StageResult(PipelineStage.REPLICA_VALIDATION, PipelineStatus.FAILED),
        ]
        self.assertEqual(aggregate_status(results), PipelineStatus.FAILED)

    def test_cancelled_beats_partial_not_failed(self):
        results = [
            StageResult(PipelineStage.PREFLIGHT, PipelineStatus.PARTIAL),
            StageResult(PipelineStage.REPORT, PipelineStatus.CANCELLED),
        ]
        self.assertEqual(aggregate_status(results), PipelineStatus.CANCELLED)
        with_failed = [
            StageResult(PipelineStage.REPLICA_BUILD, PipelineStatus.FAILED),
            StageResult(PipelineStage.REPORT, PipelineStatus.CANCELLED),
        ]
        self.assertEqual(aggregate_status(with_failed), PipelineStatus.FAILED)

    def test_partial_when_any_partial(self):
        results = [
            StageResult(PipelineStage.ADAPTER, PipelineStatus.SUCCESS),
            StageResult(PipelineStage.REPLICA_VALIDATION, PipelineStatus.PARTIAL),
        ]
        self.assertEqual(aggregate_status(results), PipelineStatus.PARTIAL)

    def test_all_success(self):
        results = [
            StageResult(PipelineStage.PREFLIGHT, PipelineStatus.SUCCESS),
            StageResult(PipelineStage.REPORT, PipelineStatus.SUCCESS),
        ]
        self.assertEqual(aggregate_status(results), PipelineStatus.SUCCESS)


class WriteReportTests(unittest.TestCase):
    def test_json_is_source_of_truth_and_stages_map_to_enums(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = create_run_layout(root, "fixture", "run-001")
            results = [
                StageResult(PipelineStage.PREFLIGHT, PipelineStatus.SUCCESS),
                StageResult(PipelineStage.REPORT, PipelineStatus.SUCCESS),
            ]
            json_path, html_path = write_pipeline_report(layout, BRIEF_CONFIG, results)
            self.assertTrue(json_path.is_file())
            self.assertTrue(html_path.is_file())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(
                [entry["stage"] for entry in payload["stages"]],
                ["preflight", "report"],
            )
            self.assertTrue(all(
                {"stage", "status"} <= set(entry) for entry in payload["stages"]
            ))

    def test_report_carries_drivers_and_capabilities_from_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = create_run_layout(root, "fixture", "run-002")
            results = [
                StageResult(
                    PipelineStage.REPLICA_VALIDATION,
                    PipelineStatus.SUCCESS,
                    metrics={"driver": "replica/replay_replica.py"},
                ),
                StageResult(
                    PipelineStage.ADAPTER_VALIDATION,
                    PipelineStatus.SUCCESS,
                    metrics={
                        "driver": "adapter/completed_fixture_offline.py",
                        "capabilities": {"viewer_js_api": "unsupported"},
                    },
                ),
            ]
            json_path, _ = write_pipeline_report(layout, BRIEF_CONFIG, results)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["drivers"]["replica_validation"], "replica/replay_replica.py"
            )
            self.assertEqual(
                payload["drivers"]["adapter_validation"],
                "adapter/completed_fixture_offline.py",
            )
            self.assertIn("capabilities", payload)
            self.assertEqual(payload["capabilities"]["viewer_js_api"], "unsupported")

    def test_html_is_escaped_json_no_patient_payload_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = create_run_layout(root, "fixture", "run-003")
            results = [StageResult(PipelineStage.PREFLIGHT, PipelineStatus.SUCCESS)]
            _, html_path = write_pipeline_report(layout, BRIEF_CONFIG, results)
            text = html_path.read_text(encoding="utf-8")
            self.assertIn("<pre>", text)
            self.assertIn("schema_version", text)
            # Nothing base64-embedded; no <img> tags.
            self.assertNotIn("<img", text)
            self.assertNotIn("base64", text)

    def test_aggregate_status_drives_payload_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = create_run_layout(root, "fixture", "run-004")
            results = [
                StageResult(PipelineStage.REPLICA_VALIDATION, PipelineStatus.PARTIAL),
            ]
            json_path, _ = write_pipeline_report(layout, BRIEF_CONFIG, results)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "partial")


if __name__ == "__main__":
    unittest.main()
