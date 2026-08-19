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


class SeriesCoverageReportTests(unittest.TestCase):
    """Phase 8: the report carries series-coverage semantics with only safe
    branch ids / stages — never patient text or full metadata."""

    def test_report_includes_not_requested_coverage_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = create_run_layout(Path(tmp), "fixture", "run-sc-0")
            results = [StageResult(PipelineStage.REPLICA_VALIDATION, PipelineStatus.SUCCESS)]
            json_path, _ = write_pipeline_report(layout, BRIEF_CONFIG, results)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            coverage = payload["series_coverage"]
            self.assertEqual(coverage["status"], "not_requested")
            self.assertEqual(coverage["discovered"], 0)
            self.assertEqual(coverage["branches"], [])

    def test_report_warns_when_series_selection_did_not_request_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "processed_fixture.py"
            source.write_text(
                '# [MARKER: 序列选择]\npage.locator("#series").click()\n',
                encoding="utf-8",
            )
            config = PipelineConfig(
                hospital="fixture",
                source_script=source,
                annotations_path=root / "annotations.json",
                output_root=root,
            )
            layout = create_run_layout(root, "fixture", "run-sc-warning")
            json_path, _ = write_pipeline_report(
                layout,
                config,
                [StageResult(PipelineStage.REPLICA_VALIDATION, PipelineStatus.SUCCESS)],
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertIn("series_expansion_not_requested", payload["warnings"])
        self.assertEqual(
            payload["series_coverage"]["warning"],
            "series_expansion_not_requested",
        )

    @staticmethod
    def _coverage_payload() -> dict:
        return {
            "enabled": True,
            "status": "partial",
            "discovered": 2,
            "captured": 1,
            "partial": 1,
            "failed": 0,
            "reached_end": True,
            "expansion_completed": True,
            "warning": None,
            "branches": [
                {"branch_id": "safe-br-0", "ordinal": 0, "status": "captured", "stage": ""},
                {"branch_id": "safe-br-1", "ordinal": 1, "status": "partial",
                 "stage": "metadata_timeout"},
            ],
        }

    def test_report_carries_series_coverage_from_capture_stage_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = create_run_layout(Path(tmp), "fixture", "run-sc-1")
            results = [
                StageResult(
                    PipelineStage.LIVE_CAPTURE,
                    PipelineStatus.PARTIAL,
                    metrics={"series_coverage": self._coverage_payload()},
                ),
            ]
            json_path, _ = write_pipeline_report(layout, BRIEF_CONFIG, results)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            coverage = payload["series_coverage"]
            self.assertEqual(coverage["status"], "partial")
            self.assertEqual(coverage["discovered"], 2)
            self.assertEqual(coverage["captured"], 1)
            self.assertEqual(coverage["partial"], 1)

    def test_replica_build_recovers_series_coverage_from_capture_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = create_run_layout(Path(tmp), "fixture", "run-sc-resume")
            manifest_dir = layout.capture_dir / "series_branches"
            manifest_dir.mkdir(parents=True)
            branches = [
                {
                    "branch_id": f"safe-{index}",
                    "ordinal": index,
                    "capture_status": "captured",
                    "series_key_sha256": "secret-not-for-report",
                    "metadata_captured": True,
                }
                for index in range(8)
            ]
            (manifest_dir / "series_capture_manifest.json").write_text(
                json.dumps({
                    "discovered_count": 8,
                    "captured_count": 8,
                    "partial_count": 0,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "count_conserved": True,
                    "reached_end": True,
                    "overall_ok": True,
                    "warning": None,
                    "branches": branches,
                }),
                encoding="utf-8",
            )

            json_path, _ = write_pipeline_report(
                layout,
                BRIEF_CONFIG,
                [StageResult(PipelineStage.REPLICA_VALIDATION, PipelineStatus.SUCCESS)],
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            coverage = payload["series_coverage"]

            self.assertEqual(coverage["status"], "complete")
            self.assertEqual((coverage["discovered"], coverage["captured"]), (8, 8))
            self.assertTrue(coverage["count_conserved"])
            self.assertEqual(
                set(coverage["branches"][0]),
                {"branch_id", "ordinal", "status", "stage"},
            )
            self.assertNotIn("secret-not-for-report", json.dumps(payload))

    def test_resume_recovers_skipped_branches_as_partial_with_conserved_counts(self):
        """Skipped terminals (budget/duplicate tails) must fold into ``partial``
        on the resume path, exactly as the live tracker folds them into the
        ``series_capture_partial`` event stream. Public counts stay conserved and
        branch status rows normalize to ``partial`` while ``stage`` retains the
        original skip reason."""
        with tempfile.TemporaryDirectory() as tmp:
            layout = create_run_layout(Path(tmp), "fixture", "run-sc-skips")
            manifest_dir = layout.capture_dir / "series_branches"
            manifest_dir.mkdir(parents=True)
            branches = [
                {
                    "branch_id": f"safe-{index}",
                    "ordinal": index,
                    "capture_status": status,
                    "fail_stage": stage,
                }
                for index, (status, stage) in enumerate([
                    ("captured", ""),
                    ("captured", ""),
                    ("skipped_budget", "budget"),
                    ("skipped_duplicate", "duplicate"),
                    ("partial", "metadata_timeout"),
                    ("failed", "transaction"),
                ])
            ]
            (manifest_dir / "series_capture_manifest.json").write_text(
                json.dumps({
                    "discovered_count": 6,
                    "captured_count": 2,
                    "partial_count": 1,
                    "failed_count": 1,
                    "skipped_count": 2,
                    "count_conserved": True,
                    "reached_end": True,
                    "overall_ok": False,
                    "warning": "series_budget_exhausted",
                    "branches": branches,
                }),
                encoding="utf-8",
            )

            json_path, _ = write_pipeline_report(
                layout,
                BRIEF_CONFIG,
                [StageResult(PipelineStage.REPLICA_VALIDATION, PipelineStatus.SUCCESS)],
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            coverage = payload["series_coverage"]

            # 2 captured + 1 native partial + 2 skipped = 3 partial, 1 failed.
            self.assertEqual(coverage["captured"], 2)
            self.assertEqual(coverage["partial"], 3)
            self.assertEqual(coverage["failed"], 1)
            # Conservation holds the same way the live tracker reports it
            # (captured + partial + failed == discovered).
            self.assertTrue(coverage["count_conserved"])
            # Skipped terminals are never the public entry status `complete`.
            self.assertEqual(coverage["status"], "partial")

            by_branch = {b["branch_id"]: b for b in coverage["branches"]}
            self.assertEqual(by_branch["safe-2"]["status"], "partial")
            self.assertEqual(by_branch["safe-2"]["stage"], "budget")
            self.assertEqual(by_branch["safe-3"]["status"], "partial")
            self.assertEqual(by_branch["safe-3"]["stage"], "duplicate")
            self.assertEqual(by_branch["safe-4"]["status"], "partial")
            self.assertEqual(by_branch["safe-4"]["stage"], "metadata_timeout")
            self.assertEqual(by_branch["safe-5"]["status"], "failed")
            # Stage holds the original skip reason, never a bare "partial".
            self.assertNotIn("skipped_", json.dumps(by_branch["safe-2"]))

    def test_report_coverage_branches_expose_only_safe_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = create_run_layout(Path(tmp), "fixture", "run-sc-2")
            results = [StageResult(
                PipelineStage.LIVE_CAPTURE, PipelineStatus.PARTIAL,
                metrics={"series_coverage": self._coverage_payload()},
            )]
            json_path, _ = write_pipeline_report(layout, BRIEF_CONFIG, results)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            text = json.dumps(payload)
            for branch in payload["series_coverage"]["branches"]:
                self.assertEqual(set(branch), {"branch_id", "ordinal", "status", "stage"})
            # No patient / UID / metadata body leaks into the report.
            for sensitive in ("张三", "PatientName", "SeriesInstanceUID", "Accession"):
                self.assertNotIn(sensitive, text)


if __name__ == "__main__":
    unittest.main()
