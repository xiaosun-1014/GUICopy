import tempfile
import unittest
from pathlib import Path
import ast
import io
import time

import batch_capture_replicate as replica_batch
from batch_capture_replicate import LiveCaptureSession, await_interactive_auth, build_flow_from_snapshots, build_from_manifest, capture_and_build, classify_capture_error, instrument_marked_actions, run_live_capture, validate_annotations
from playwright.sync_api import sync_playwright
from replay_helpers import write_manifest
from replica_models import BootstrapPlan, CaptureTimingProfile, ReplicaFlow, ReplicaState, StateEvidence
from replay_helpers import read_manifest


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


if __name__ == "__main__":
    unittest.main()
