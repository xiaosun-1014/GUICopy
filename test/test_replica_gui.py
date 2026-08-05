import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from batch_capture_replicate import validate_annotations
from main_gui import MainWindow, build_replica_annotations, export_preflight_errors, normalize_ftimage_codegen, replica_python_executable, write_source_text
from markers import DEFAULT_MARKERS


APP = QApplication.instance() or QApplication(sys.argv)


class ReplicaGuiTests(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()

    def test_export_is_enabled_only_for_stopped_marked_recording(self):
        self.assertFalse(self.window.export_replica_btn.isEnabled())
        self.assertFalse(self.window.cancel_export_btn.isEnabled())
        self.window._on_code_ready('def run():\n    page.goto("https://example.test")\n')
        self.assertFalse(self.window.export_replica_btn.isEnabled())
        cursor = self.window.code_view.textCursor()
        cursor.setPosition(0)
        self.window.code_view.setTextCursor(cursor)
        self.window._insert_marker(DEFAULT_MARKERS[0])

        self.assertFalse(self.window.export_replica_btn.isEnabled())
        self.window._saved_source_hash = hashlib.sha256(self.window._latest_code.encode("utf-8")).hexdigest()
        self.window._update_export_enabled()
        self.assertTrue(self.window.export_replica_btn.isEnabled())

    def test_annotations_preserve_marker_id_line_and_source_hash(self):
        source = 'page.goto("https://example.test")\n# [MARKER: 报告截图]\n'
        annotations = build_replica_annotations(
            [
                {"type": "codegen", "text": 'page.goto("https://example.test")'},
                {"type": "marker", "text": "# [MARKER: 报告截图]", "marker_id": "marker-1"},
            ],
            source,
        )

        self.assertEqual(annotations["markers"], [{"marker_id": "marker-1", "line": 2, "label": "报告截图"}])
        self.assertEqual(len(annotations["source_script_sha256"]), 64)

    def test_export_prefers_documented_conda_interpreter(self):
        self.assertTrue(replica_python_executable().replace("\\", "/").endswith("codegen-marker/python.exe"))

    def test_missing_interpreter_raises_runtime_error_not_sys_executable_fallback(self):
        # 解释器缺失时绝不允许静默回退到 sys.executable —— 必须触发明确失败。
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                replica_python_executable()
            self.assertIn("codegen-marker", str(ctx.exception))

    def test_present_interpreter_returns_documented_conda_path_unchanged(self):
        # 正常路径（解释器存在）行为不变：返回 codegen-marker/python.exe。
        with patch.object(Path, "is_file", return_value=True):
            resolved = replica_python_executable().replace("\\", "/")
            self.assertTrue(resolved.endswith("codegen-marker/python.exe"))


    def test_save_immediately_enables_export_for_stopped_marked_recording(self):
        self.window._on_code_ready('def run():\n    page.goto("https://example.test")\n')
        cursor = self.window.code_view.textCursor()
        cursor.setPosition(0)
        self.window.code_view.setTextCursor(cursor)
        self.window._insert_marker(DEFAULT_MARKERS[0])
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "processed_script.py"
            self.window.output_input.setText(str(destination))
            self.window._on_save()

            self.assertTrue(destination.exists())
            self.assertTrue(self.window.export_replica_btn.isEnabled())

    def test_save_uses_configured_output_path_without_a_second_dialog(self):
        self.window._on_code_ready('def run():\n    page.goto("https://example.test")\n')
        cursor = self.window.code_view.textCursor()
        cursor.setPosition(0)
        self.window.code_view.setTextCursor(cursor)
        self.window._insert_marker(DEFAULT_MARKERS[0])
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "configured.py"
            self.window.output_input.setText(str(destination))
            with patch("main_gui.QFileDialog.getSaveFileName", side_effect=AssertionError("save dialog should not open")):
                self.window._on_save()

            self.assertTrue(destination.exists())

    def test_ftimage_tags_actions_use_stable_toggle_locator(self):
        source = '''page.goto("https://yyx.ftimage.cn/dimage/index.html")
page.get_by_role("link", description="Tags", exact=True).click()
page.get_by_role("link").filter(has_text=re.compile(r"^$")).click()
'''

        normalized = normalize_ftimage_codegen(source)

        self.assertNotIn("description=\"Tags\"", normalized)
        self.assertNotIn("re.compile(r\"^$\")", normalized)
        self.assertIn('page.locator("#moreBox a.tool.tool-tags").click()', normalized)
        self.assertIn('page.locator("#tagsBox a.close").click()', normalized)
        self.assertNotIn('page.locator("a.tool.tool-tags").click()', normalized)

    def test_gui_defaults_to_ftimage_recording_inputs(self):
        expected_output = Path(__file__).resolve().parents[1] / "out" / "ftimage" / "processed_script_ftimage.py"
        from urllib.parse import urlsplit

        url = urlsplit(self.window.url_input.text())
        # assert scheme/host/path only — never embed a token in the query
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.netloc, "yyx.ftimage.cn")
        self.assertEqual(url.path, "/dimage/index.html")
        self.assertNotIn("stm=", self.window.url_input.text())
        self.assertEqual(Path(self.window.output_input.text()), expected_output)

    def test_export_preflight_allows_a_standalone_report_marker(self):
        incomplete = '# [MARKER: 报告截图]\n# page.screenshot(path="report.png")\n'
        complete = '# [MARKER: 报告截图]\npage.locator("#open-viewer").click()\n'

        self.assertEqual(export_preflight_errors(incomplete), [])
        self.assertEqual(export_preflight_errors(complete), [])

    def test_annotation_hash_matches_lf_persisted_source(self):
        source = 'page.goto("https://example.test")\n# [MARKER: 报告截图]\n'
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "processed_script.py"
            annotations = Path(tmp) / "replica_annotations.json"
            write_source_text(script, source)
            annotations.write_text(
                json.dumps(build_replica_annotations([], source), ensure_ascii=False),
                encoding="utf-8",
            )

            self.assertEqual(validate_annotations(script, annotations)["schema_version"], 1)

    def test_interactive_auth_continue_button_writes_jsonl_command(self):
        class FakeProcess:
            def __init__(self):
                self.messages = []

            def write(self, value):
                self.messages.append(value)

        process = FakeProcess()
        self.window._export_process = process
        self.window.continue_auth_btn.setEnabled(True)

        self.window._on_continue_auth()

        self.assertEqual(process.messages, [b'{"command":"continue_after_auth"}\n'])
        self.assertFalse(self.window.continue_auth_btn.isEnabled())
        self.window._export_process = None


if __name__ == "__main__":
    unittest.main()
