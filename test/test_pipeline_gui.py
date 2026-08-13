"""Task 9: one-click launch of the pipeline orchestrator from the GUI.

Covers:
- primary button launches pipeline_orchestrator.py with the right args (no
  double-hospital run root);
- per-stream JSONL chunk buffering across partial reads;
- exit code 0 is NOT success without a final validation report;
- graceful cancel writes a cancel command before any forced kill;
- closeEvent cancels an in-flight pipeline;
- annotations derive from the actual editor source after manual edits.
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from main_gui import MainWindow, build_annotations_from_source
from markers import DEFAULT_MARKERS


APP = QApplication.instance() or QApplication(sys.argv)


def _marked_source() -> str:
    return 'def run():\n    page.goto("https://example.test")\n'


def _mark_recording(window) -> None:
    """Populate the panel with code then insert a marker so export is enabled."""
    window._on_code_ready(_marked_source())
    cursor = window.code_view.textCursor()
    cursor.setPosition(0)
    window.code_view.setTextCursor(cursor)
    window._insert_marker(DEFAULT_MARKERS[0])  # 📸 报告截图


class PipelineGuiTests(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()

    def test_primary_button_launches_pipeline_orchestrator(self):
        _mark_recording(self.window)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "out" / "ftimage" / "processed_script_ftimage.py"
            self.window.output_input.setText(str(destination))
            self.window._on_save()
            with patch("main_gui.QProcess.start") as start:
                self.window._on_export_replica()
            proc = self.window._export_process
            self.assertIsNotNone(proc)
            self.assertIn("pipeline_orchestrator.py", proc.arguments()[0])
            start.assert_called_once()

    def test_output_root_prevents_double_hospital_run(self):
        _mark_recording(self.window)
        with tempfile.TemporaryDirectory() as tmp:
            hospital = "ftimage"
            recording = Path(tmp) / "out" / hospital / "processed_script_ftimage.py"
            self.window.output_input.setText(str(recording))
            self.window._on_save()
            with patch("main_gui.QProcess.start"):
                self.window._on_export_replica()
            args = self.window._export_process.arguments()
            out_root = Path(args[args.index("--output-root") + 1])
            # orchestrator computes <output_root>/<hospital>/runs/<run_id>
            run_root = out_root / hospital / "runs"
            self.assertEqual(run_root, recording.resolve().parent / "runs")
            self.assertEqual(run_root.parent.name, hospital)  # out/{h}/runs, not out/{h}/{h}/runs

    def test_launch_passes_hospital_auth_and_script_args(self):
        _mark_recording(self.window)
        with tempfile.TemporaryDirectory() as tmp:
            recording = Path(tmp) / "out" / "ftimage" / "processed_script_ftimage.py"
            self.window.output_input.setText(str(recording))
            self.window.replica_auth_mode.setCurrentIndex(
                self.window.replica_auth_mode.findData("interactive")
            )
            self.window._on_save()
            with patch("main_gui.QProcess.start"):
                self.window._on_export_replica()
            args = self.window._export_process.arguments()
            self.assertEqual(args[args.index("--hospital") + 1], "ftimage")
            self.assertEqual(args[args.index("--auth-mode") + 1], "interactive")
            self.assertEqual(args[args.index("--output-root") + 1], str(recording.resolve().parent.parent))

    def test_partial_jsonl_chunks_are_buffered_per_stream(self):
        self.window._consume_pipeline_chunk("stdout", b'{"event":"stage_')
        self.window._consume_pipeline_chunk("stdout", b'finished","status":"success"}\n')
        self.assertEqual(self.window._last_pipeline_event["status"], "success")
        # the fragment split point keeps the tail empty (no residual)
        self.assertEqual(self.window._pipeline_buffers["stdout"], "")

    def test_exit_zero_without_success_report_is_failure(self):
        self.window._on_export_finished(0, object())
        self.assertIn("未产生最终验证报告", self.window.statusBar().currentMessage())

    def test_cancel_sends_command_before_forced_kill(self):
        class FakeProcess:
            def __init__(self):
                self.messages = []

            def write(self, value):
                self.messages.append(value)

            def state(self):
                return __import__("PyQt6.QtCore", fromlist=["QProcess"]).QProcess.ProcessState.Running

        process = FakeProcess()
        self.window._export_process = process
        self.window._on_cancel_export()
        self.assertEqual(process.messages, [b'{"command":"cancel"}\n'])
        # no immediate force-kill on the synchronous path
        self.window._export_process = None

    def test_close_event_cancels_active_pipeline(self):
        self.window.close()
        self.assertTrue(self.window._pipeline_cancel_requested)

    def test_annotation_line_correct_after_manual_edit_above_marker(self):
        _mark_recording(self.window)
        # marker header now on source line 2 (1-based)
        lines = self.window._latest_code.split("\n")
        lines.insert(0, "page.goto('added-first')")
        edited = "\n".join(lines)
        self.window.code_view.setPlainText(edited)
        self.window._latest_code = self.window.code_view.toPlainText()

        marker_lines = [
            i for i, ln in enumerate(self.window._latest_code.split("\n"), start=1)
            if ln.strip().startswith("# [MARKER:")
        ]
        self.assertEqual(len(marker_lines), 1)

        annotations = self.window._annotations_for_export()
        self.assertEqual(annotations["markers"][0]["line"], marker_lines[0])
        self.assertEqual(annotations["markers"][0]["line"], 3)

    def test_annotations_from_source_reuses_anchor_id_on_unchanged_marker(self):
        source = 'page.goto("https://example.test")\n# [MARKER: 报告截图]\n'
        anchors = [
            {
                "marker_id": "anchor-1",
                "items": [
                    {"type": "marker", "text": "# [MARKER: 报告截图]", "marker_id": "anchor-1"},
                ],
            }
        ]
        annotations = build_annotations_from_source(source, anchors)
        self.assertEqual(annotations["markers"][0]["marker_id"], "anchor-1")
        self.assertEqual(annotations["markers"][0]["line"], 2)

    # ---- D3 / operation selection ----

    def test_operation_combo_defaults_to_full_and_passes_operation_arg(self):
        _mark_recording(self.window)
        self.assertEqual(self.window.replica_operation_combo.currentData(), "full")
        with tempfile.TemporaryDirectory() as tmp:
            recording = Path(tmp) / "out" / "ftimage" / "processed_script_ftimage.py"
            self.window.output_input.setText(str(recording))
            self.window._on_save()
            with patch("main_gui.QProcess.start"):
                self.window._on_export_replica()
            args = self.window._export_process.arguments()
            self.assertEqual(args[args.index("--operation") + 1], "full")

    def test_capture_build_option_passes_operation_arg(self):
        _mark_recording(self.window)
        self.window.replica_operation_combo.setCurrentIndex(
            self.window.replica_operation_combo.findData("capture-build")
        )
        self.assertEqual(self.window.replica_operation_combo.currentData(), "capture-build")
        with tempfile.TemporaryDirectory() as tmp:
            recording = Path(tmp) / "out" / "ftimage" / "processed_script_ftimage.py"
            self.window.output_input.setText(str(recording))
            self.window._on_save()
            with patch("main_gui.QProcess.start"):
                self.window._on_export_replica()
            args = self.window._export_process.arguments()
            self.assertEqual(args[args.index("--operation") + 1], "capture-build")

    def test_operation_combo_disabled_during_export_reenabled_after(self):
        _mark_recording(self.window)
        with tempfile.TemporaryDirectory() as tmp:
            recording = Path(tmp) / "out" / "ftimage" / "processed_script_ftimage.py"
            self.window.output_input.setText(str(recording))
            self.window._on_save()
            with patch("main_gui.QProcess.start"):
                self.window._on_export_replica()
            # during export the operation combo is locked (like replica_auth_mode)
            self.assertFalse(self.window.replica_operation_combo.isEnabled())
            # resume: mock a terminal success report then finish
            report = Path(tmp) / "pipeline_report.json"
            report.write_text('{"status":"success"}', encoding="utf-8")
            self.window._final_pipeline_report = report
            self.window._on_export_finished(0, object())
            self.assertTrue(self.window.replica_operation_combo.isEnabled())

    def test_capture_build_success_does_not_depend_on_adapter_artifact(self):
        _mark_recording(self.window)
        # A capture-build run never produces completed_<hospital>.py; the GUI
        # completion judgment reads only pipeline_report.json terminal status.
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "pipeline_report.json"
            report.write_text('{"status":"partial"}', encoding="utf-8")
            self.window._final_pipeline_report = report
            self.window._on_export_finished(0, object())
            self.assertIn("离线复刻完成", self.window.statusBar().currentMessage())

    # ---- D3 marker_result / summary consumption ----

    def test_marker_result_upsert_updates_counts(self):
        self.assertEqual(
            self.window._marker_counts(), {"success": 0, "partial": 0, "failed": 0, "skipped": 0}
        )
        self.window._consume_pipeline_chunk(
            "stdout", b'{"event":"marker_result","marker_id":"m1","status":"success"}\n'
        )
        self.assertEqual(self.window._marker_counts()["success"], 1)
        # same marker transitions success -> partial : counts 1,0 -> 0,1
        self.window._consume_pipeline_chunk(
            "stdout", b'{"event":"marker_result","marker_id":"m1","status":"partial"}\n'
        )
        counts = self.window._marker_counts()
        self.assertEqual((counts["success"], counts["partial"]), (0, 1))

    def test_summary_overlay_overrides_counts_authoritatively(self):
        self.window._consume_pipeline_chunk(
            "stdout", b'{"event":"marker_result","marker_id":"m1","status":"success"}\n'
        )
        self.window._consume_pipeline_chunk(
            "stdout",
            b'{"event":"summary","scope":"markers","status":"success",'
            b'"success":2,"partial":1,"failed":0,"skipped":3}\n',
        )
        counts = self.window._marker_counts()
        # overlay wins over the recomputed 1-success
        self.assertEqual((counts["success"], counts["partial"], counts["skipped"]), (2, 1, 3))

    # ---- interactive-auth continue button (regression) ----

    def test_auth_required_enables_continue_then_completed_disables(self):
        self.assertFalse(self.window.continue_auth_btn.isEnabled())
        self.window._consume_pipeline_chunk(
            "stdout", b'{"event":"auth_required"}\n'
        )
        self.assertTrue(self.window.continue_auth_btn.isEnabled())
        self.window._consume_pipeline_chunk(
            "stdout", b'{"event":"auth_completed"}\n'
        )
        self.assertFalse(self.window.continue_auth_btn.isEnabled())

    def test_continue_click_after_auth_required_writes_command(self):
        class FakeProcess:
            def __init__(self):
                self.messages = []

            def write(self, value):
                self.messages.append(value)

        self.window._export_process = FakeProcess()
        self.window._consume_pipeline_chunk("stdout", b'{"event":"auth_required"}\n')
        self.assertTrue(self.window.continue_auth_btn.isEnabled())
        self.window._on_continue_auth()
        self.assertEqual(
            self.window._export_process.messages, [b'{"command":"continue_after_auth"}\n']
        )
        self.assertFalse(self.window.continue_auth_btn.isEnabled())
        self.window._export_process = None

    def test_ready_handled_as_protocol_handshake(self):
        self.window._consume_pipeline_chunk("stdout", b'{"event":"ready","run_id":"r1"}\n')
        self.assertEqual(self.window._last_pipeline_event["event"], "ready")
        self.assertIsNone(self.window._final_pipeline_report)


if __name__ == "__main__":
    unittest.main()
