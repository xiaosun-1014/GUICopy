import tempfile
import unittest
from pathlib import Path
import ast
import hashlib
import io
import json
import subprocess
import time
from unittest.mock import patch

import batch_capture_replicate as replica_batch
from batch_capture_replicate import LiveCaptureSession, await_interactive_auth, build_flow_from_snapshots, build_from_manifest, capture_and_build, capture_to_manifest, classify_capture_error, instrument_marked_actions, merge_annotation_uuids, run_live_capture, validate_annotations
from playwright.sync_api import sync_playwright
from replay_helpers import sha256_file, write_manifest
from replica_models import BootstrapPlan, CaptureTimingProfile, ReplicaFlow, ReplicaState, StateEvidence
from replay_helpers import read_manifest
from rewrite_script import parse_action_plan


class BatchCaptureReplicateTests(unittest.TestCase):
    def test_offline_build_emits_json_progress_and_entrypoint(self):
        flow = ReplicaFlow(1, "empty", "recorded.py", "hash", "now", {"width": 1, "height": 1}, BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", [ReplicaState("s_000", 0, "", "page", [], [], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))], [])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            write_manifest(manifest, flow)
            events = []

            entrypoint = build_from_manifest(manifest, root, root / "replica", events.append)

            self.assertTrue(entrypoint.exists())
            self.assertEqual(events[-1]["event"], "completed")

    def test_instrumentation_wraps_marked_actions_without_breaking_popup_syntax(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: 报告截图]\n    with page.expect_popup() as popup_info:\n        page.get_by_role("button", name="Open").click()\n    page1 = popup_info.value\n'''

        instrumented = instrument_marked_actions(source)

        ast.parse(instrumented)
        self.assertIn('capture_hook_before("a_000_001", page, lambda:', instrumented)
        self.assertIn('capture_hook_after("a_000_001", page, lambda:', instrumented)

    def test_interactive_instrumentation_uses_headed_browser_and_hook_gate(self):
        source = '''from playwright.sync_api import sync_playwright

def run(page):
    browser = sync_playwright().start().chromium.launch()
    # [MARKER: 报告截图]
    page.get_by_role("button", name="Open").click()
'''

        instrumented = instrument_marked_actions(source, interactive_auth=True)

        self.assertIn("chromium.launch(headless=False)", instrumented)
        self.assertIn("capture_hook_before", instrumented)

    def test_marked_action_failure_does_not_stop_later_marked_actions(self):
        source = '''from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(100)
        page.set_content('<button id="ok">OK</button>')
        # [MARKER: Meta 信息工具]
        page.locator("#missing").click()
        page.locator("#ok").click()
        browser.close()

run()
'''
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "recorded.py"
            script.write_text(source, encoding="utf-8")

            result = run_live_capture(script, Path(tmp) / "capture")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"event": "action_failed"', result.stdout)

    def test_build_skips_failed_marked_actions_with_no_snapshot_pair(self):
        source = '''from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(100)
        page.set_content('<button id="ok">OK</button>')
        # [MARKER: Meta 信息工具]
        page.locator("#missing").click()
        page.locator("#ok").click()
        browser.close()

run()
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")

            entrypoint = capture_and_build(script, root / "export")
            manifest = read_manifest(root / "export" / "capture" / "manifest.json", root / "export" / "capture")

        self.assertTrue(entrypoint.name == "index.html")
        self.assertIn("action_capture_failed:a_000_001", manifest.warnings)

    def test_live_capture_session_writes_before_and_after_marked_action_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<button id="go">Go</button>')
            session.before("a_000_001", page, marker_label="报告截图")
            page.locator("#go").click()
            session.after("a_000_001", page, marker_label="报告截图")
            browser.close()

            self.assertTrue((Path(tmp) / "snapshots" / "a_000_001" / "before" / "assets").exists())
            self.assertTrue((Path(tmp) / "snapshots" / "a_000_001" / "after" / "assets").exists())
            before = (Path(tmp) / "snapshots" / "a_000_001" / "before" / "topology.json").read_text(encoding="utf-8")
            self.assertIn('\"region_type\": \"report\"', before)

    def test_after_capture_survives_target_removed_by_action(self):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<button id="close" onclick="this.remove()">Close</button>')
            target = lambda: page.locator("#close")
            session.before("a_000_001", page, target, "Meta 信息工具")
            target().click()

            session.after("a_000_001", page, target, "Meta 信息工具")

            self.assertTrue((Path(tmp) / "snapshots" / "a_000_001" / "after" / "topology.json").exists())
            browser.close()

    def test_metadata_panel_is_captured_from_target_frame(self):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.set_content('<iframe id="viewer" style="width:500px;height:400px"></iframe>')
            frame = page.frames[-1]
            frame.set_content(
                '<button id="btn-tags">Tags</button>'
                '<div id="tagsBox"><div>Patient Name: Example</div></div>'
            )
            target = lambda: page.frame_locator("#viewer").locator("#btn-tags")

            session._capture("a_meta", "after", page, target, "Meta 信息工具")
            topology = json.loads(
                (Path(tmp) / "snapshots" / "a_meta" / "after" / "topology.json").read_text(encoding="utf-8")
            )
            browser.close()

        child = next(document for document in topology["documents"] if document["parent_document_id"] is not None)
        metadata = next(region for region in child["regions"] if region["region_type"] == "metadata")
        self.assertEqual(metadata["root"]["attributes"].get("id"), "tagsBox")
        self.assertIn('id="tagsBox"', metadata["root"]["outer_html"])

    def test_metadata_post_action_waits_for_stable_panel_content(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<button id="btn-tags" onclick="openMetadata()">Tags</button>'
                '<div id="tagsBox" style="display:none"><div>Patient Name: Example</div></div>'
                '<script>'
                'function openMetadata() {'
                '  const panel = document.querySelector("#tagsBox");'
                '  panel.style.display = "block";'
                '  setTimeout(() => panel.insertAdjacentHTML("beforeend", "<div id=late-row>Study Date: 20260812</div>"), 150);'
                '}'
                '</script>'
            )
            target = lambda: page.locator("#btn-tags")
            target().click()

            replica_batch.ensure_post_action_state(
                page,
                "Meta 信息工具",
                target,
                timeout_s=2.0,
                stable_s=0.2,
            )

            self.assertEqual(page.locator("#late-row").count(), 1)
            browser.close()

    def test_flow_uses_after_target_when_before_target_is_missing(self):
        source = '''from playwright.sync_api import sync_playwright

def run(page):
    # [MARKER: 序列选择]
    page.locator("#series").click()
'''
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")
            session = LiveCaptureSession(root / "capture")
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<button id="series">Series</button>')
            target = lambda: page.locator("#series")
            session.before("a_000_001", page, target, "序列选择")
            target().click()
            session.after("a_000_001", page, target, "序列选择")
            (root / "capture" / "snapshots" / "a_000_001" / "before" / "target.json").unlink()

            flow = build_flow_from_snapshots(script, root / "capture")

            self.assertEqual(flow.states[0].documents[0].targets[0].action_id, "a_000_001")
            browser.close()

    def test_sequence_post_action_waits_until_report_overlay_is_hidden(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<div id="reportContainer">Report</div>'
                '<a class="tool tool-more disabled">More</a>'
            )
            page.evaluate(
                """() => setTimeout(() => {
                    document.querySelector("#reportContainer").style.display = "none";
                }, 100)"""
            )
            page.evaluate(
                """() => setTimeout(() => {
                    document.querySelector(".tool-more").classList.remove("disabled");
                }, 150)"""
            )
            page.evaluate(
                """() => setTimeout(() => {
                    document.querySelector("#reportContainer").style.display = "block";
                }, 250)"""
            )
            page.evaluate(
                """() => setTimeout(() => {
                    document.querySelector("#reportContainer").style.display = "none";
                }, 650)"""
            )

            started = time.monotonic()
            replica_batch.wait_for_post_action_state(page, "序列选择")
            elapsed = time.monotonic() - started

            self.assertFalse(page.locator("#reportContainer").is_visible())
            self.assertNotIn("disabled", page.locator(".tool-more").get_attribute("class"))
            self.assertGreaterEqual(elapsed, 0.9)
            browser.close()

    def test_sequence_state_retries_dblclick_when_first_attempt_does_not_exit_report(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<div id="reportContainer">Report</div>'
                '<a class="tool tool-more disabled">More</a>'
                '<button id="series" ondblclick="'
                "document.body.dataset.attempts = String(Number(document.body.dataset.attempts || 0) + 1);"
                "if (Number(document.body.dataset.attempts) >= 2) {"
                "document.querySelector('#reportContainer').style.display = 'none';"
                "document.querySelector('.tool-more').classList.remove('disabled');"
                '}">Series</button>'
            )
            target = lambda: page.locator("#series")
            target().dblclick()

            replica_batch.ensure_post_action_state(
                page,
                "序列选择",
                target,
                timeout_s=0.3,
                stable_s=0.1,
            )

            self.assertEqual(page.locator("body").get_attribute("data-attempts"), "2")
            self.assertFalse(page.locator("#reportContainer").is_visible())
            browser.close()

    def test_sequence_pre_action_waits_for_async_report_content(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<div id="reportContainer" style="display:none">'
                '<div class="report-footer">Report ready</div>'
                '</div>'
            )
            page.evaluate(
                """() => setTimeout(() => {
                    document.querySelector("#reportContainer").style.display = "block";
                }, 400)"""
            )

            replica_batch.wait_for_pre_action_state(page, "序列选择")

            self.assertTrue(page.locator("#reportContainer .report-footer").is_visible())
            browser.close()

    def test_sequence_pre_action_waits_for_report_inserted_after_initial_load(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content("<main>Loading</main>")
            page.evaluate(
                """() => setTimeout(() => {
                    document.body.insertAdjacentHTML(
                        "beforeend",
                        '<div id="reportContainer"><div class="report-footer">Report ready</div></div>'
                    );
                }, 400)"""
            )

            replica_batch.wait_for_pre_action_state(page, "序列选择")

            self.assertTrue(page.locator("#reportContainer .report-footer").is_visible())
            browser.close()

    def test_live_capture_session_records_popup_pages_created_by_action(self):
        fixture = Path(__file__).parent / "fixtures" / "replica_flow" / "host.html"
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(fixture.as_uri())
            session.before("a_000_001", page, marker_label="报告截图")
            with page.expect_popup() as popup_info:
                page.locator("#open-popup").click()
            popup_info.value.wait_for_load_state()
            session.after("a_000_001", page, marker_label="报告截图")
            browser.close()

            topology = (Path(tmp) / "snapshots" / "a_000_001" / "after" / "topology.json").read_text(encoding="utf-8")
        self.assertIn('\"page_var\": \"page1\"', topology)

    def test_frame_target_region_is_attached_to_popup_frame_document(self):
        fixture = Path(__file__).parent / "fixtures" / "replica_flow" / "host.html"
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(fixture.as_uri())
            with page.expect_popup() as popup_info:
                page.locator("#open-popup").click()
            popup = popup_info.value
            popup.wait_for_load_state()
            series = lambda: popup.locator("#popup-frame").content_frame.locator("#series-thick")
            session.before("a_001_001", popup, series, "序列选择")
            series().click()
            session.after("a_001_001", popup, series, "序列选择")
            browser.close()
            topology = (Path(tmp) / "snapshots" / "a_001_001" / "after" / "topology.json").read_text(encoding="utf-8")

        self.assertIn('\"id\": \"series-thick\"', topology)

    def test_subprocess_runner_executes_instrumented_local_script_once(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run():\n    with sync_playwright() as playwright:\n        browser = playwright.chromium.launch()\n        page = browser.new_page()\n        page.set_content('<button id="go">Go</button>')\n        # [MARKER: Meta 信息工具]\n        page.locator("#go").click()\n        browser.close()\n\nrun()\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")

            result = run_live_capture(script, root / "capture")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "capture" / "snapshots" / "a_000_001" / "after" / "assets").exists())
            flow = build_flow_from_snapshots(script, root / "capture")
            self.assertEqual([state.state_id for state in flow.states], ["s_000", "s_001"])
            self.assertEqual(flow.states[0].transitions[0].action_id, "a_000_001")
            self.assertEqual(flow.states[0].documents[0].targets[0].dom.attributes["id"], "go")
            self.assertGreaterEqual(flow.states[0].documents[0].targets[0].selector_closure.required_ancestor_count, 1)

    def test_live_capture_removes_stale_snapshot_directories(self):
        source = '''from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content('<button id="go">Go</button>')
        # [MARKER: Meta 信息工具]
        page.locator("#go").click()
        browser.close()

run()
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            output = root / "capture"
            stale = output / "snapshots" / "stale_action"
            stale.mkdir(parents=True)
            (stale / "old.txt").write_text("stale", encoding="utf-8")
            script.write_text(source, encoding="utf-8")

            result = run_live_capture(script, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(stale.exists())

    def test_capture_and_build_creates_manifest_and_offline_entrypoint(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run():\n    with sync_playwright() as playwright:\n        browser = playwright.chromium.launch()\n        page = browser.new_page()\n        page.set_content('<button id="go">Go</button>')\n        # [MARKER: 影像画布交互]\n        page.locator("#go").click()\n        browser.close()\n\nrun()\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")

            events = []
            entrypoint = capture_and_build(script, root / "export", events.append)

            self.assertTrue(entrypoint.exists())
            manifest = root / "export" / "capture" / "manifest.json"
            self.assertTrue(manifest.exists())
            flow = read_manifest(manifest, root / "export" / "capture")
            self.assertEqual(len(flow.states), 1)
            self.assertIsNone(flow.states[0].transitions[0].to_state_id)
            self.assertEqual([event["event"] for event in events], ["capture_started", "capture_finished", "build_started", "build_finished"])

    def test_region_dom_change_creates_state_for_non_always_after_marker(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run():\n    with sync_playwright() as playwright:\n        browser = playwright.chromium.launch()\n        page = browser.new_page()\n        page.set_content('<section id="report"><button id="go" onclick="this.parentElement.dataset.status=\\'ready\\'">Go</button></section>')\n        # [MARKER: 报告截图]\n        page.locator("#go").click()\n        browser.close()\n\nrun()\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")

            run_live_capture(script, root / "capture")
            flow = build_flow_from_snapshots(script, root / "capture")

        self.assertEqual([state.state_id for state in flow.states], ["s_000", "s_001"])
        self.assertEqual(flow.states[1].evidence.decision_reason, "region_dom_changed")

    def test_annotation_validation_rejects_stale_source_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            annotations = root / "replica_annotations.json"
            script.write_text("print('recorded')\n", encoding="utf-8")
            annotations.write_text('{"schema_version": 1, "source_script_sha256": "stale", "markers": []}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "do not match"):
                validate_annotations(script, annotations)

    def test_interactive_auth_requires_jsonl_continue_command(self):
        events = []

        await_interactive_auth(io.StringIO('{"command":"continue_after_auth"}\n'), events.append, timeout_s=1)

        self.assertEqual([event["event"] for event in events], ["auth_required", "auth_completed"])

    def test_storage_state_is_injected_without_becoming_a_capture_artifact(self):
        source = 'browser.new_context()\n# [MARKER: 报告截图]\npage.locator("#go").click()\n'

        instrumented = instrument_marked_actions(source, use_storage_state=True)

        self.assertIn("REPLICA_STORAGE_STATE", instrumented)
        ast.parse(instrumented)

    def test_capture_errors_have_stable_gui_categories(self):
        self.assertEqual(classify_capture_error(RuntimeError("authentication_cancelled")), "authentication")
        self.assertEqual(classify_capture_error(RuntimeError("locator strict mode violation")), "selector_failure")
        self.assertEqual(classify_capture_error(RuntimeError("net::ERR_CONNECTION_REFUSED")), "network")

    # --- Task 5: batch_capture annotations UUID writeback ---

    @staticmethod
    def _annotated_plan(source):
        marker_line = next(i for i, line in enumerate(source.splitlines(), 1) if "MARKER:" in line)
        return parse_action_plan(source), marker_line

    def test_merge_uuids_writes_gui_uuid_into_groups_and_action_targets(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: 序列选择]\n    page.locator("#series").click()\n'''
        plan, marker_line = self._annotated_plan(source)
        payload = {"schema_version": 1, "source_script_sha256": "x", "markers": [{"marker_id": "uuid-sel", "line": marker_line, "label": "序列选择"}]}

        mapping = merge_annotation_uuids(plan, payload)

        self.assertEqual(mapping, {"m_000": "uuid-sel"})
        self.assertEqual(plan.marker_groups[0].marker_id, "uuid-sel")
        self.assertEqual(plan.marker_groups[0].actions[0].marker_id, "uuid-sel")

    def test_merge_uuids_normalizes_whitespace_and_case_of_labels(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: Meta 信息工具]\n    page.locator("#go").click()\n'''
        plan, marker_line = self._annotated_plan(source)
        payload = {"markers": [{"marker_id": "uuid-meta", "line": marker_line, "label": "  meta 信息工具  "}]}

        mapping = merge_annotation_uuids(plan, payload)

        self.assertEqual(mapping, {"m_000": "uuid-meta"})

    def test_merge_uuids_rejects_duplicate_same_line_and_label(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: 序列选择]\n    page.locator("#series").click()\n'''
        plan, marker_line = self._annotated_plan(source)
        payload = {"markers": [
            {"marker_id": "uuid-a", "line": marker_line, "label": "序列选择"},
            {"marker_id": "uuid-b", "line": marker_line, "label": " 序列选择 "},
        ]}

        with self.assertRaisesRegex(ValueError, "duplicate"):
            merge_annotation_uuids(plan, payload)

    def test_merge_uuids_rejects_annotation_with_no_matching_group(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: 序列选择]\n    page.locator("#series").click()\n'''
        plan, marker_line = self._annotated_plan(source)
        payload = {"markers": [
            {"marker_id": "uuid-sel", "line": marker_line, "label": "序列选择"},
            {"marker_id": "uuid-orphan", "line": 999, "label": "报告截图"},
        ]}

        with self.assertRaisesRegex(ValueError, "missing marker group"):
            merge_annotation_uuids(plan, payload)

    def test_merge_uuids_rejects_group_with_no_matching_annotation(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: 序列选择]\n    page.locator("#series").click()\n'''
        plan, _marker_line = self._annotated_plan(source)
        payload = {"markers": []}

        with self.assertRaisesRegex(ValueError, "missing annotation"):
            merge_annotation_uuids(plan, payload)

    def test_merge_uuids_rejects_label_mismatch_on_aligned_line(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: 序列选择]\n    page.locator("#series").click()\n'''
        plan, marker_line = self._annotated_plan(source)
        payload = {"markers": [{"marker_id": "uuid-wrong", "line": marker_line, "label": "报告截图"}]}

        with self.assertRaisesRegex(ValueError, "label mismatch"):
            merge_annotation_uuids(plan, payload)

    def test_validate_annotations_returns_payload_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            annotations = root / "replica_annotations.json"
            script.write_text("print('recorded')\n", encoding="utf-8")
            payload = {
                "schema_version": 1,
                "source_script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "markers": [{"marker_id": "uuid-ok", "line": 1, "label": "序列选择"}],
            }
            annotations.write_text(json.dumps(payload), encoding="utf-8")

            result = validate_annotations(script, annotations)

            self.assertEqual(result, payload)

    def test_annotation_uuid_is_written_into_built_flow_action_target(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: 序列选择]\n    page.locator("#series").click()\n'''
        marker_line = next(i for i, line in enumerate(source.splitlines(), 1) if "MARKER:" in line)
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")
            session = LiveCaptureSession(root / "capture")
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<button id="series">Series</button>')
            target = lambda: page.locator("#series")
            session.before("a_000_001", page, target, "序列选择")
            target().click()
            session.after("a_000_001", page, target, "序列选择")
            payload = {"schema_version": 1, "source_script_sha256": "x", "markers": [{"marker_id": "uuid-built", "line": marker_line, "label": "序列选择"}]}

            flow = build_flow_from_snapshots(script, root / "capture", payload)

            self.assertEqual(flow.states[0].documents[0].targets[0].marker_id, "uuid-built")
            browser.close()

    def test_build_flow_without_annotations_keeps_regenerated_marker_id(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run(page):\n    # [MARKER: 序列选择]\n    page.locator("#series").click()\n'''
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")
            session = LiveCaptureSession(root / "capture")
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<button id="series">Series</button>')
            target = lambda: page.locator("#series")
            session.before("a_000_001", page, target, "序列选择")
            target().click()
            session.after("a_000_001", page, target, "序列选择")

            flow = build_flow_from_snapshots(script, root / "capture")

            self.assertEqual(flow.states[0].documents[0].targets[0].marker_id, "m_000")
            self.assertEqual(flow.states[0].documents[0].targets[0].action_id, "a_000_001")
            browser.close()


    # --- Task 5: capture quality/timeout outcomes explicit ---

    @staticmethod
    def _annotated_source():
        source = '''from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content('<button id="go">Go</button>')
        # [MARKER: 序列选择]
        page.locator("#go").click()
        browser.close()

run()
'''
        marker_line = next(i for i, line in enumerate(source.splitlines(), 1) if "MARKER:" in line)
        return source, marker_line

    def test_capture_to_manifest_does_not_build_replica(self):
        source, marker_line = self._annotated_source()
        flow = ReplicaFlow(
            1, "empty", "recorded.py", "hash", "now", {"width": 1, "height": 1},
            BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", [], [],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")
            annotations = root / "replica_annotations.json"
            payload = {
                "schema_version": 1,
                "source_script_sha256": sha256_file(script),
                "markers": [{"marker_id": "gui-uuid-sel", "line": marker_line, "label": "序列选择"}],
            }
            annotations.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "capture"
            with patch(
                "batch_capture_replicate.run_live_capture",
                return_value=subprocess.CompletedProcess(["replay"], 0, "out", ""),
            ) as runner:
                with patch("batch_capture_replicate.build_flow_from_snapshots", return_value=flow):
                    manifest = capture_to_manifest(script, annotations, output, capture_timeout_s=7)

            self.assertTrue(manifest.exists())
            self.assertFalse((output / "replica" / "index.html").exists())
            self.assertEqual(runner.call_args.kwargs["timeout_s"], 7)

    def test_capture_manifest_preserves_gui_marker_uuid(self):
        source, marker_line = self._annotated_source()
        gui_marker_uuid = "gui-uuid-sel"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")
            annotations = root / "replica_annotations.json"
            payload = {
                "schema_version": 1,
                "source_script_sha256": sha256_file(script),
                "markers": [{"marker_id": gui_marker_uuid, "line": marker_line, "label": "序列选择"}],
            }
            annotations.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "capture"

            manifest = capture_to_manifest(script, annotations, output)
            flow = read_manifest(manifest, output)

            self.assertEqual(flow.states[0].documents[0].targets[0].marker_id, gui_marker_uuid)

    def test_capture_and_build_forwards_timeout_to_live_capture(self):
        with patch(
            "batch_capture_replicate.run_live_capture",
            side_effect=subprocess.TimeoutExpired(["fixture"], 7),
        ) as runner:
            with self.assertRaises(subprocess.TimeoutExpired):
                capture_and_build("recorded.py", "out", capture_timeout_s=7)
        self.assertEqual(runner.call_args.kwargs["timeout_s"], 7)

    def test_build_from_manifest_verifies_explicit_source_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flow_root = root / "capture"
            flow_root.mkdir()
            source = flow_root / "recorded.py"
            source.write_text("print('original')\n", encoding="utf-8")
            flow = ReplicaFlow(
                1, "fixture", "recorded.py", sha256_file(source), "now", {"width": 1, "height": 1},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [ReplicaState("s_000", 0, "", "page", [], [], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))], [],
            )
            manifest = flow_root / "manifest.json"
            write_manifest(manifest, flow)
            # Mutate the source the manifest points to so its hash diverges.
            source.write_text("print('changed!')\n", encoding="utf-8")
            output = root / "replica"

            with self.assertRaisesRegex(ValueError, "hash"):
                build_from_manifest(manifest, flow_root, output, source_path=source)


if __name__ == "__main__":
    unittest.main()
