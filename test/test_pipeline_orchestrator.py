"""Level-2 orchestrator tests: stage ordering, status semantics, resume gates,
and the D4 event protocol. All external stages are mocked; no real browser or
LLM is ever launched."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_models import PipelineConfig, PipelineStatus, PipelineStage, StageResult
from orchestrator_events import normalize_child_event
from pipeline_orchestrator import (
    PipelineController,
    _build_parser,
    _prepare_full_run,
    _stage_plan,
    execute_pipeline_stages,
    exit_code_for,
    main,
    resume_pipeline,
    run_pipeline,
)
from pipeline_io import create_run_layout
from pipeline_validation import ValidationResult

SUCCESS = PipelineStatus.SUCCESS
PARTIAL = PipelineStatus.PARTIAL
FAILED = PipelineStatus.FAILED
CANCELLED = PipelineStatus.CANCELLED


def make_config(tmp: str) -> PipelineConfig:
    root = Path(tmp)
    (root / "processed_fixture.py").write_text(
        "# [MARKER: 报告截图]\npage.locator('#open-viewer').click()\n",
        encoding="utf-8",
    )
    (root / "annotations_fixture.json").write_text("{}", encoding="utf-8")
    return PipelineConfig(
        hospital="fixture",
        source_script=root / "processed_fixture.py",
        annotations_path=root / "annotations_fixture.json",
        output_root=root,
        retry_count=3,
    )


class StageOrderTests(unittest.TestCase):
    def test_pipeline_runs_stages_in_declared_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            layout = config.output_root / "fixture" / "runs"
            events: list[dict] = []
            with _patches_for(config, layout):
                result = run_pipeline(config, emit=events.append)
            self.assertEqual(result.status, SUCCESS)
            self.assertEqual(
                [event["stage"] for event in events if event["event"] == "stage_started"],
                [
                    "preflight",
                    "generating_adapter",
                    "capturing_live",
                    "building_replica",
                    "validating_replica",
                    "validating_adapter",
                    "report",
                ],
            )
            # D4: exactly one completed, and it is the final business event.
            completed = [e for e in events if e["event"] == "completed"]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["status"], "success")
            # Each stage_finished is immediately followed by a summary (§5.5/D3).
            started = [e for e in events if e["event"] == "stage_started"]
            finished = [e for e in events if e["event"] == "stage_finished"]
            finish_idx = [i for i, e in enumerate(events) if e["event"] == "stage_finished"]
            for i in finish_idx:
                self.assertEqual(events[i + 1]["event"], "summary")
                self.assertEqual(events[i + 1].get("scope"), "markers")
                self.assertIn("success", events[i + 1])
            self.assertEqual(len(finished), len(started))
            # completed carries entrypoint artifacts + the final summary snapshot.
            final = completed[0]
            expected_artifacts = {
                "adapter", "manifest", "replica", "offline_adapter",
                "report_json", "report_html",
            }
            self.assertLessEqual(expected_artifacts, set(final["artifacts"]))
            self.assertEqual(final["summary"]["event"], "summary")
            self.assertEqual(final["summary"]["scope"], "markers")
            self.assertEqual(final["summary"]["status"], "success")
            # authoritative marker counts exposed through the summary snapshot.
            self.assertEqual(final["summary"]["skipped"], 1)

    def test_ready_is_emitted_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            events = []
            with _patches_for(config, config.output_root / "fixture" / "runs"):
                result = run_pipeline(config, emit=events.append)
            self.assertEqual(result.status, SUCCESS)
            self.assertEqual(events[0]["event"], "ready")
            self.assertEqual(events[0]["run_id"], result.run_id)

    def test_capture_build_stage_plan_skips_adapter(self):
        """capture-build = preflight -> live_capture -> replica_build -> replica_validation.

        It must NOT schedule ADAPTER (generating_adapter) or ADAPTER_VALIDATION
        (validating_adapter); the pipeline is pure capture + build (no LLM).
        """
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            c = PipelineController(config, operation="capture-build")
            plan = _stage_plan(c)
            stages = [stage.value for stage, _cat, _fn in plan]
            self.assertEqual(
                stages,
                ["preflight", "capturing_live", "building_replica", "validating_replica"],
            )
            self.assertNotIn("generating_adapter", stages)
            self.assertNotIn("validating_adapter", stages)


class CliRunIdRulesTests(unittest.TestCase):
    """CLI run-id rules exercised through main() / _build_parser (not just the
    controller): capture-build behaves like full (new run, no --run-id)."""

    _BASE = [
        "--script", "processed_fixture.py",
        "--annotations", "annotations_fixture.json",
        "--hospital", "fixture",
        "--output-root", "out",
    ]

    def test_capture_build_without_run_id_is_accepted(self):
        argv = self._BASE + ["--operation", "capture-build"]
        args = _build_parser().parse_args(argv)
        self.assertEqual(args.operation, "capture-build")
        self.assertIsNone(args.run_id)

    def test_capture_build_with_run_id_is_rejected(self):
        argv = self._BASE + ["--operation", "capture-build", "--run-id", "run-x"]
        with self.assertRaises(SystemExit) as ctx:
            main(argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_full_with_run_id_is_rejected(self):
        argv = self._BASE + ["--operation", "full", "--run-id", "run-x"]
        with self.assertRaises(SystemExit) as ctx:
            main(argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_non_full_without_run_id_is_rejected(self):
        for op in ("adapter-only", "replica-build", "offline-validation"):
            argv = self._BASE + ["--operation", op]
            with self.assertRaises(SystemExit) as ctx:
                main(argv)
            self.assertEqual(ctx.exception.code, 2, f"operation {op} should be rejected")

    def test_main_accepts_capture_build_and_mints_new_run(self):
        """A bare main() capture-build must reach controller construction (new
        run path), not hit the parser.error gate."""
        class _FakeResult:
            status = SUCCESS
            run_id = "r1"

        class _FakeController:
            def __init__(self, config, emit=None, run_id=None, operation="full"):
                self._emit = emit
                self.active_process = None

            def send_command(self, command):
                pass

            def run(self):
                return _FakeResult()

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "processed_fixture.py"
            script.write_text("# [MARKER: 报告截图]\n", encoding="utf-8")
            ann = Path(tmp) / "annotations_fixture.json"
            ann.write_text("{}", encoding="utf-8")
            out_root = Path(tmp) / "out"
            argv = [
                "--script", str(script),
                "--annotations", str(ann),
                "--hospital", "fixture",
                "--output-root", str(out_root),
                "--operation", "capture-build",
            ]
            with patch("pipeline_orchestrator.PipelineController", _FakeController):
                code = main(argv)
            self.assertEqual(code, 0)  # no parser.error; new-run path executed


class StatusSemanticsTests(unittest.TestCase):
    def test_critical_validation_failure_produces_failed_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            events: list[dict] = []
            with _patches_for(config, config.output_root / "fixture" / "runs",
                             replica_validation=StageResult(
                                 PipelineStage.REPLICA_VALIDATION, FAILED,
                                 "replica_build", "critical_locator_not_unique")):
                result = run_pipeline(config, emit=events.append)
            self.assertEqual(result.status, FAILED)
            # D4 on failure: one fatal carrying the failing stage, one completed.
            fatal = [e for e in events if e["event"] == "fatal"]
            self.assertEqual(len(fatal), 1)
            self.assertEqual(fatal[0]["stage"], "validating_replica")
            completed = [e for e in events if e["event"] == "completed"]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["status"], "failed")
            # Summary still emitted after the short-circuited failing stage_finished.
            finish_idx = [i for i, e in enumerate(events) if e["event"] == "stage_finished"]
            for i in finish_idx:
                self.assertEqual(events[i + 1]["event"], "summary")

    def test_noncritical_locator_risk_produces_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            with _patches_for(config, config.output_root / "fixture" / "runs",
                             replica_validation=StageResult(
                                 PipelineStage.REPLICA_VALIDATION, PARTIAL)):
                result = run_pipeline(config)
            self.assertEqual(result.status, PARTIAL)

    def test_full_success_writes_atomic_latest_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            with _patches_for(config, config.output_root / "fixture" / "runs"):
                result = run_pipeline(config)
            latest = config.output_root / "fixture" / "latest.json"
            self.assertTrue(latest.is_file())
            payload = json.loads(latest.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["run_id"], result.run_id)


class ExitCodeTests(unittest.TestCase):
    def test_success_and_partial_map_to_zero(self):
        self.assertEqual(exit_code_for(SUCCESS), 0)
        self.assertEqual(exit_code_for(PARTIAL), 0)

    def test_failed_and_cancelled_map_to_one(self):
        self.assertEqual(exit_code_for(FAILED), 1)
        self.assertEqual(exit_code_for(CANCELLED), 1)


class CancelTests(unittest.TestCase):
    def test_cancel_command_sets_cancelled_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            controller = PipelineController(config)
            controller.cancel()
            with _patches_for(config, config.output_root / "fixture" / "runs"):
                result = controller.run()
            self.assertEqual(result.status, CANCELLED)
            self.assertTrue(result.layout.report_json.exists())

    def test_cancel_immediately_after_ready_reports_cancelled_not_success(self):
        """F2: cancel before any stage result must make pipeline_report.json.status
        ``cancelled`` (matching the run's terminal ``cancelled``), not ``success``
        from aggregate_status([])."""
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            events: list[dict] = []
            controller = PipelineController(config, emit=events.append)
            controller.cancel()  # cancel after ready/server, before any stage result
            with _patches_for(config, config.output_root / "fixture" / "runs"):
                result = controller.run()
            self.assertEqual(result.status, CANCELLED)
            report = json.loads(result.layout.report_json.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "cancelled")
            # completed.status agrees with the report
            completed = [e for e in events if e["event"] == "completed"]
            self.assertEqual(completed[0]["status"], "cancelled")


class MarkerOutcomeAggregationTests(unittest.TestCase):
    """F3: child marker outcomes are upserted into the orchestrator tracker and
    emitted as top-level marker_result, so summary/completed counts reflect real
    outcomes rather than the preflight ``skipped`` seed."""

    def _new_controller(self, tmp: str, events):
        config = make_config(tmp)
        controller = PipelineController(config, emit=events.append)
        return config, controller

    def _envelope(self, child: dict, run_id: str) -> dict:
        from orchestrator_events import normalize_child_event
        return normalize_child_event(child, "validating_adapter", run_id)

    def test_adapter_marker_finished_upserts_and_emits_marker_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            events: list[dict] = []
            config, controller = self._new_controller(tmp, events)
            controller._track_markers(["报告截图", "影像画布交互"])  # seeded skipped
            controller._issue(
                self._envelope(
                    {"event": "marker_finished", "marker": "报告截图", "status": "supported"},
                    controller.run_id,
                )
            )
            counts = controller._summary_counts()
            self.assertEqual(counts["success"], 1)
            self.assertEqual(counts["skipped"], 1)  # the other marker stays skipped
            released = [e for e in events if e["event"] == "marker_result"]
            self.assertEqual(len(released), 1)
            self.assertEqual(released[0]["marker_id"], "报告截图")
            self.assertEqual(released[0]["status"], "success")

    def test_degraded_marker_counts_as_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            events: list[dict] = []
            _, controller = self._new_controller(tmp, events)
            for child in (
                {"event": "marker_degraded", "marker": "影像画布交互"},
                {"event": "marker_finished", "marker": "影像画布交互", "status": "degraded"},
            ):
                controller._issue(self._envelope(child, controller.run_id))
            counts = controller._summary_counts()
            self.assertEqual(counts["partial"], 1)
            self.assertEqual(counts["success"], 0)

    def test_full_pipeline_reports_real_counts_not_all_skipped(self):
        """End-to-end: the adapter stage emits a child marker_finished through the
        child event path; the final completed.summary carries success=1, not the
        preflight all-skipped seed."""
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            events: list[dict] = []

            def adapter_fn(config, layout, controller, marker_names=()):
                controller._track_markers(list(marker_names))
                controller._issue(
                    normalize_child_event(
                        {"event": "marker_finished", "marker": "报告截图", "status": "supported"},
                        "validating_adapter",
                        controller.run_id,
                    )
                )
                return StageResult(
                    PipelineStage.ADAPTER_VALIDATION, SUCCESS,
                    artifacts={"offline_adapter": str(layout.adapter_dir / "completed_fixture_offline.py")},
                    metrics={"driver": "adapter/completed_fixture_offline.py"},
                )

            from unittest.mock import patch
            with _patches_for(config, config.output_root / "fixture" / "runs"):
                with patch("pipeline_orchestrator.run_adapter_validation", adapter_fn):
                    result = run_pipeline(config, emit=events.append)
            self.assertEqual(result.status, SUCCESS)
            completed = [e for e in events if e["event"] == "completed"][0]
            summary = completed["summary"]
            self.assertEqual(summary["success"], 1)
            self.assertEqual(summary["skipped"], 0)


class ResumeTests(unittest.TestCase):
    def test_replica_build_resume_requires_existing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            run_root = config.output_root / "fixture" / "runs" / "run-without-manifest"
            run_root.mkdir(parents=True)
            (run_root / "source").mkdir()
            (run_root / "capture").mkdir()
            (run_root / "adapter").mkdir()
            (run_root / "replica").mkdir()
            (run_root / "validation").mkdir()
            (run_root / "logs").mkdir()
            with self.assertRaisesRegex(ValueError, "manifest"):
                resume_pipeline(config, run_id="run-without-manifest",
                                operation="replica-build")

    def test_unsupported_operation_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            run_root = config.output_root / "fixture" / "runs" / "run-x"
            run_root.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "unsupported pipeline operation"):
                resume_pipeline(config, run_id="run-x", operation="nonsense")

    def test_offline_validation_resume_does_not_repeat_live_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            run_root = config.output_root / "fixture" / "runs" / "run-offline"
            (run_root / "source").mkdir(parents=True)
            (run_root / "adapter").mkdir(parents=True)
            (run_root / "capture").mkdir(parents=True)
            (run_root / "replica").mkdir(parents=True)
            (run_root / "validation").mkdir(parents=True)
            (run_root / "logs").mkdir(parents=True)
            (run_root / "replica" / "index.html").write_text("<html></html>", encoding="utf-8")
            (run_root / "capture" / "manifest.json").write_text("{}", encoding="utf-8")
            # Resume gate requires the real hospital-named adapter (completed_<hospital>.py).
            (run_root / "adapter" / "completed_fixture.py").write_text("x", encoding="utf-8")
            with patch("pipeline_orchestrator.capture_to_manifest") as capture, \
                 _patches_for(config, config.output_root / "fixture" / "runs"):
                result = resume_pipeline(config, "run-offline",
                                         operation="offline-validation")
            capture.assert_not_called()
            self.assertIn(result.status, {SUCCESS, PARTIAL})

    def test_new_full_run_copies_source_into_immutable_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            original_source = config.source_script.read_text(encoding="utf-8")
            with _patches_for(config, config.output_root / "fixture" / "runs"):
                result = run_pipeline(config)
            run_copy = result.layout.source_dir / "processed_fixture.py"
            self.assertTrue(run_copy.is_file())
            self.assertEqual(run_copy.read_text(encoding="utf-8"), original_source)
            # Storage state is never copied into the run.
            self.assertFalse(any(
                p.name.startswith("storage_state") for p in result.layout.root.rglob("*")
            ))

    def test_capture_build_is_not_resumable(self):
        """capture-build is a new-run operation: it is deliberately NOT in the
        validate_resume_prerequisites whitelist, so a --run-id resume must be
        rejected by the else branch (second line of defense)."""
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            run_root = config.output_root / "fixture" / "runs" / "run-cb"
            run_root.mkdir(parents=True)
            (run_root / "source").mkdir()
            (run_root / "adapter").mkdir()
            (run_root / "capture").mkdir()
            (run_root / "replica").mkdir()
            (run_root / "validation").mkdir()
            (run_root / "logs").mkdir()
            with self.assertRaisesRegex(ValueError, "unsupported pipeline operation"):
                resume_pipeline(config, run_id="run-cb", operation="capture-build")

    def test_prepare_full_run_normalizes_crlf_source_to_lf(self):
        """_copy_lf copies text source into the run as LF only.

        Contract: the run copy is LF-normalized, while the inlined
        ``source_script_sha256`` in annotations is computed against the ORIGINAL
        file — so a CRLF source would trip preflight's annotations_hash_mismatch
        (GUI normally saves LF, so this only bites hand-edited CRLF inputs).
        """
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            crlf = "def run():\r\n    page.goto(\"x\")\r\n"
            config.source_script.write_bytes(crlf.encode("utf-8"))
            layout = create_run_layout(config.output_root, config.hospital, "run-crlf")
            effective = _prepare_full_run(config, layout)
            run_copy_text = effective.source_script.read_text(encoding="utf-8")
            # LF normalization: no carriage returns survive into the run copy.
            self.assertNotIn("\r", run_copy_text)
            self.assertEqual(
                run_copy_text, "def run():\n    page.goto(\"x\")\n"
            )


class ReportDriverTests(unittest.TestCase):
    def test_report_distinguishes_manifest_replay_and_completed_adapter_drivers(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            with _patches_for(config, config.output_root / "fixture" / "runs"):
                result = run_pipeline(config)
            report = json.loads(result.layout.report_json.read_text(encoding="utf-8"))
            self.assertEqual(
                report["drivers"]["replica_validation"], "replica/replay_replica.py"
            )
            self.assertEqual(
                report["drivers"]["adapter_validation"],
                "adapter/completed_fixture_offline.py",
            )
            self.assertIn("capabilities", report)


class _PatchStack:
    """Exit-stack style context manager for a list of patch objects."""

    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _patches_for(config, layout, replica_validation=None, adapter_validation=None):
    """Build a context manager that mocks all external stages.

    Defaults to success with realistic entrypoint artifacts; override a
    validation stage to force failed/partial.
    """
    run_dir = layout / "runs" / "x"
    patches = [
        patch("pipeline_orchestrator.run_preflight_stage",
              return_value=StageResult(PipelineStage.PREFLIGHT, SUCCESS,
                                       metrics={"markers": ("报告截图",)})),
        patch("pipeline_orchestrator.run_adapter_generation",
              return_value=StageResult(
                  PipelineStage.ADAPTER, SUCCESS,
                  artifacts={"completed": str(run_dir / "adapter" / "completed_fixture.py")})),
        patch("pipeline_orchestrator.run_capture",
              return_value=StageResult(
                  PipelineStage.LIVE_CAPTURE, SUCCESS,
                  artifacts={"manifest_path": str(run_dir / "capture" / "manifest.json")})),
        patch("pipeline_orchestrator.run_replica_build",
              return_value=StageResult(
                  PipelineStage.REPLICA_BUILD, SUCCESS,
                  artifacts={"entrypoint": str(run_dir / "replica" / "index.html")})),
        patch("pipeline_orchestrator.run_replica_validation",
              return_value=(replica_validation or StageResult(
                  PipelineStage.REPLICA_VALIDATION, SUCCESS,
                  metrics={"driver": "replica/replay_replica.py"}))),
        patch("pipeline_orchestrator.run_adapter_validation",
              return_value=(adapter_validation or StageResult(
                  PipelineStage.ADAPTER_VALIDATION, SUCCESS,
                  artifacts={"offline_adapter": str(run_dir / "adapter" / "completed_fixture_offline.py")},
                  metrics={
                      "driver": "adapter/completed_fixture_offline.py",
                      "capabilities": {"viewer_js_api": "unsupported"},
                  }))),
    ]
    return _PatchStack(patches)


if __name__ == "__main__":
    unittest.main()
