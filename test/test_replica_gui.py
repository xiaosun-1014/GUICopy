import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from batch_capture_replicate import validate_annotations
from main_gui import MainWindow, build_annotations_from_source, build_replica_annotations, export_preflight_errors, normalize_ftimage_codegen, rebuild_display_state_from_source, replica_python_executable, write_source_text
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

    def test_rebuild_display_state_preserves_marker_id_after_multiline_edit(self):
        source = '''page.locator(
    "#open"
).click()
# [MARKER: 报告截图]
# page.screenshot(path="report.png")
'''
        anchors = [{
            "marker_id": "marker-1",
            "codegen_idx": 0,
            "fingerprint": 'page.locator("#old").click()',
            "items": [
                {
                    "type": "marker",
                    "text": "# [MARKER: 报告截图]",
                    "marker_id": "marker-1",
                },
                {
                    "type": "marker",
                    "text": '# page.screenshot(path="report.png")',
                    "marker_id": "marker-1",
                },
            ],
        }]

        items, rebuilt = rebuild_display_state_from_source(source, anchors)

        marker_items = [item for item in items if item["type"] == "marker"]
        self.assertEqual(len(marker_items), 2)
        self.assertTrue(all(item["marker_id"] == "marker-1" for item in marker_items))
        self.assertEqual(rebuilt[0]["marker_id"], "marker-1")
        self.assertEqual(rebuilt[0]["fingerprint"], ").click()")

    def test_rebuild_display_state_assigns_uuid_to_manually_typed_marker(self):
        source = 'page.locator("#open").click()\n# [MARKER: 报告截图]\n'

        items, anchors = rebuild_display_state_from_source(source, [])

        marker = next(item for item in items if item["type"] == "marker")
        self.assertTrue(marker["marker_id"])
        self.assertEqual(anchors[0]["marker_id"], marker["marker_id"])

    def _load_editable_locator_source(self):
        source = '''def run(page):
    # [MARKER: 序列选择]
    page.locator(".series").nth(2).dblclick()
'''
        self.window._set_editor_source(source)
        self.window._manager = None
        self.window._refresh_annotation_panel()
        return source

    def test_annotation_panel_is_read_only_while_recording(self):
        self._load_editable_locator_source()
        self.window._manager = object()
        self.window._refresh_annotation_panel()

        self.assertFalse(self.window.annotation_panel.editable)

    def test_annotation_panel_applies_locator_and_marks_source_unsaved(self):
        self._load_editable_locator_source()
        self.window._saved_source_hash = hashlib.sha256(
            self.window._latest_code.encode("utf-8")
        ).hexdigest()
        self.window._apply_locator_edit(
            "a_000_001",
            'page.get_by_test_id("series-primary")',
        )

        self.assertIn(
            'page.get_by_test_id("series-primary").dblclick()',
            self.window._latest_code,
        )
        self.assertFalse(self.window.export_replica_btn.isEnabled())
        action = (
            self.window.annotation_panel.plan
            .marker_groups[0]
            .actions[0]
        )
        self.assertEqual(action.locator.locator_kind, "test_id")

    def test_annotation_panel_failed_apply_keeps_source_unchanged(self):
        source = self._load_editable_locator_source()

        self.window._apply_locator_edit(
            "a_000_001",
            "page.locator(selector)",
        )

        self.assertEqual(self.window._latest_code, source)

    def test_annotation_selection_jumps_to_receiver_source(self):
        self._load_editable_locator_source()
        action = self.window.annotation_panel.plan.marker_groups[0].actions[0]
        span = self.window.annotation_panel.plan.locator_source_spans[action.action_id]

        self.window._select_source_span(span)

        self.assertEqual(
            self.window.code_view.textCursor().selectedText(),
            'page.locator(".series").nth(2)',
        )

    def test_invalid_manual_source_keeps_last_plan_as_read_only_reference(self):
        self._load_editable_locator_source()
        previous_plan = self.window.annotation_panel.plan
        self.window._latest_code = "def broken(:\n"

        self.window._refresh_annotation_panel()

        self.assertIs(self.window.annotation_panel.plan, previous_plan)
        self.assertFalse(self.window.annotation_panel.tree.isEnabled())
        self.assertIn(
            "当前源码无法解析",
            self.window.annotation_panel.status_label.text(),
        )

    def test_parse_error_recovers_after_source_is_fixed(self):
        source = self._load_editable_locator_source()
        self.window._latest_code = "def broken(:\n"
        self.window._refresh_annotation_panel()
        self.assertFalse(self.window.annotation_panel.tree.isEnabled())

        self.window._latest_code = source
        self.window._refresh_annotation_panel()
        first_group = self.window.annotation_panel.plan.marker_groups[0]
        first_action_item = self.window.annotation_panel.tree.topLevelItem(0).child(0)
        self.window.annotation_panel.tree.setCurrentItem(first_action_item)

        self.assertFalse(self.window.annotation_panel.expression_editor.isReadOnly())
        self.assertTrue(self.window.annotation_panel.apply_button.isEnabled())

    def test_duplicate_marker_headers_preserve_distinct_ids_by_occurrence(self):
        source = '''page.locator("#one").click()
# [MARKER: 报告截图]
page.locator("#two").click()
# [MARKER: 报告截图]
'''
        anchors = [
            {
                "marker_id": "marker-1",
                "codegen_idx": 0,
                "fingerprint": 'page.locator("#one").click()',
                "items": [{
                    "type": "marker",
                    "text": "# [MARKER: 报告截图]",
                    "marker_id": "marker-1",
                }],
            },
            {
                "marker_id": "marker-2",
                "codegen_idx": 1,
                "fingerprint": 'page.locator("#two").click()',
                "items": [{
                    "type": "marker",
                    "text": "# [MARKER: 报告截图]",
                    "marker_id": "marker-2",
                }],
            },
        ]

        annotations = build_annotations_from_source(source, anchors)

        self.assertEqual(
            [marker["marker_id"] for marker in annotations["markers"]],
            ["marker-1", "marker-2"],
        )


if __name__ == "__main__":
    unittest.main()
