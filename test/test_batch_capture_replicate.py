import tempfile
import unittest
from pathlib import Path
import ast
import hashlib
import io
import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import batch_capture_replicate as replica_batch
from batch_capture_replicate import LiveCaptureSession, await_interactive_auth, build_flow_from_snapshots, build_from_manifest, capture_and_build, capture_to_manifest, classify_capture_error, instrument_marked_actions, merge_annotation_uuids, run_live_capture, validate_annotations
from playwright.sync_api import sync_playwright
from replay_helpers import sha256_file, write_manifest
from replica_models import BootstrapPlan, CaptureTimingProfile, ReplicaFlow, ReplicaState, ReplicaTransition, StateEvidence
from replay_helpers import read_manifest
from rewrite_script import parse_action_plan


class BatchCaptureReplicateTests(unittest.TestCase):
    def test_popup_entry_skips_a_small_shell_for_the_next_complete_state(self):
        transition = SimpleNamespace(mode="popup", to_state_id="s_001")
        frame = lambda size: SimpleNamespace(parent_document_id="root", screenshot_size_bytes=size)
        states = [
            SimpleNamespace(state_id="s_000", transitions=[transition], documents=[]),
            SimpleNamespace(state_id="s_001", transitions=[], documents=[frame(10_000)]),
            SimpleNamespace(state_id="s_002", transitions=[], documents=[frame(200_000)]),
        ]

        replica_batch._skip_popup_shell_state(states)

        self.assertEqual(transition.to_state_id, "s_002")

    def test_popup_entry_does_not_skip_loaded_four_view_layout(self):
        shell_target = SimpleNamespace(action_id="open_layout", marker_id="layout")
        option_target = SimpleNamespace(action_id="choose_layout", marker_id="layout")
        entry = SimpleNamespace(
            state_id="s_000",
            transitions=[ReplicaTransition(
                "t_open_viewer", "open_viewer", "s_000", "s_001", "page", "page1", "popup",
            )],
            documents=[],
        )
        shell = SimpleNamespace(
            state_id="s_001",
            transitions=[ReplicaTransition(
                "t_open_layout", "open_layout", "s_001", "s_002", "page1", "page1", "same_page",
            )],
            documents=[SimpleNamespace(parent_document_id="root", screenshot_size_bytes=410_000, targets=[shell_target])],
        )
        menu = SimpleNamespace(
            state_id="s_002",
            transitions=[ReplicaTransition(
                "t_choose_layout", "choose_layout", "s_002", "s_003", "page1", "page1", "same_page",
            )],
            documents=[SimpleNamespace(parent_document_id="root", screenshot_size_bytes=700_000, targets=[shell_target, option_target])],
        )
        closed = SimpleNamespace(
            state_id="s_003", transitions=[],
            documents=[SimpleNamespace(parent_document_id="root", screenshot_size_bytes=570_000, targets=[shell_target, option_target])],
        )

        replica_batch._skip_popup_shell_state([entry, shell, menu, closed])

        self.assertEqual(entry.transitions[0].to_state_id, "s_001")
        self.assertEqual(shell.transitions[0].to_state_id, "s_002")

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

    def test_metadata_unstable_panel_does_not_abort_after_capture(self):
        # A metadata panel whose content never stabilizes must not make
        # ensure_post_action_state raise: raising would skip the after capture
        # in LiveCaptureSession.after and silently drop the marked action from
        # the flow (missing after topology.json -> _has_snapshot_pair fails).
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<button id="btn-tags">Tags</button>'
                '<div id="tagsBox"><div><span id="live">0</span></div></div>'
                '<script>'
                'let n = 0;'
                'setInterval(() => { document.querySelector("#live").textContent = ++n; }, 40);'
                '</script>'
            )
            target = lambda: page.locator("#btn-tags")

            # The live counter keeps changing, so the signature never stabilizes
            # within the short timeout. This must return normally (no raise) and
            # the after snapshot must still be written.
            replica_batch.ensure_post_action_state(
                page,
                "Meta 信息工具",
                target,
                timeout_s=0.6,
                stable_s=0.2,
            )
            session._capture("a_meta", "after", page, target, "Meta 信息工具")
            topology = json.loads(
                (Path(tmp) / "snapshots" / "a_meta" / "after" / "topology.json").read_text(encoding="utf-8")
            )
            browser.close()

        self.assertTrue(topology["documents"])

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

    def test_pre_action_waits_until_target_is_actionable(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<button id="open-viewer">Open viewer</button>'
                '<div id="loading" style="position:fixed;inset:0;z-index:10">Loading</div>'
            )
            page.evaluate(
                """() => setTimeout(() => {
                    document.querySelector("#loading").remove();
                }, 400)"""
            )

            started = time.monotonic()
            replica_batch.wait_for_pre_action_state(
                page,
                "报告截图",
                lambda: page.locator("#open-viewer"),
            )
            elapsed = time.monotonic() - started

            self.assertGreaterEqual(elapsed, 0.3)
            self.assertEqual(page.locator("#loading").count(), 0)
            browser.close()

    def test_report_post_action_waits_for_popup_frame_content(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<button id="open">Open</button>')
            with page.expect_popup() as popup_info:
                page.evaluate("window.open('about:blank')")
            popup = popup_info.value
            popup.set_content(
                '<div id="loading">正在运行...</div>'
                '<iframe id="viewer"></iframe>'
            )
            popup.evaluate(
                """() => setTimeout(() => {
                    const doc = document.querySelector('#viewer').contentDocument;
                    doc.body.innerHTML = '<canvas width="200" height="100"></canvas>';
                    const canvas = doc.querySelector('canvas');
                    canvas.getContext('2d').fillRect(0, 0, 200, 100);
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = 'white';
                    ctx.fillRect(50, 20, 100, 60);
                }, 200)"""
            )

            started = time.monotonic()
            replica_batch.ensure_post_action_state(
                page,
                "报告截图",
                timeout_s=2.0,
                stable_s=0.2,
            )
            elapsed = time.monotonic() - started

            self.assertGreaterEqual(elapsed, 0.8)
            self.assertEqual(popup.frames[1].locator("canvas").count(), 1)
            browser.close()

    def test_report_post_action_does_not_accept_a_black_canvas(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            with page.expect_popup() as popup_info:
                page.evaluate("window.open('about:blank')")
            popup = popup_info.value
            popup.set_content(
                '<div>正在运行...</div><iframe id="viewer"></iframe>'
            )
            popup.evaluate(
                """() => {
                    const doc = document.querySelector('#viewer').contentDocument;
                    doc.body.innerHTML = '<canvas width="200" height="100"></canvas>';
                    doc.querySelector('canvas').getContext('2d').fillRect(0, 0, 200, 100);
                }"""
            )

            self.assertFalse(replica_batch._wait_for_report_popup_state(
                page, timeout_s=0.4, stable_s=0.1,
            ))
            browser.close()

    def test_report_post_action_rejects_a_blank_popup(self):
        with patch.object(
            replica_batch,
            "_wait_for_report_popup_state",
            return_value=False,
        ) as wait:
            with self.assertRaisesRegex(TimeoutError, "non-blank"):
                replica_batch.ensure_post_action_state(
                    object(),
                    "报告截图",
                )
        self.assertEqual(wait.call_args.kwargs["timeout_s"], 60.0)

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

    def test_nested_frame_series_uses_scroll_harvest(self):
        # A 序列选择 series list living in the INNERMOST of two nested iframes must
        # be routed through the marker-aware series harvest (scroll collection),
        # not the generic parent-region capture. Regression for the routing bug
        # where nested non-metadata markers fell into the generic else branch.
        #
        # The nested iframes are built via set_content (same-origin) so Playwright's
        # ``window.frameElement`` resolution used by ``_capture()`` frame routing
        # works (file:// iframes are cross-origin and yield null there).
        inner_html = """<section id="series" class="series-list" style="height:64px;overflow:auto">
          <div class="item" data-series="SERIES-1" data-series-uid="uid-1" style="height:24px">Coronal 1.0 400amp</div>
          <div class="item" data-series="SERIES-2" data-series-uid="uid-2" style="height:24px">Axial 5.0 80amp</div>
          <div class="item" data-series="SERIES-3" data-series-uid="uid-3" style="height:24px">Sagittal 2.0 120amp</div>
        </section>"""
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.set_content('<iframe id="outer" name="outerFrame" style="width:400px;height:300px"></iframe>')
            page.frames[-1].set_content('<iframe id="series-frame" name="seriesFrame" style="width:300px;height:200px"></iframe>')
            inner = page.frames[-1]
            inner.set_content(inner_html)
            series_root = inner.locator("#series")
            # Scroll the series container to a valid midway position so restore is
            # observable (max scrollTop == clientHeight-scrollHeight == 8 here).
            series_root.evaluate("element => element.scrollTop = 4")
            target = inner.locator("[data-series='SERIES-2']")

            session.before("a_series_001", page, lambda: target, "序列选择")

            # scrollTop must be restored to its original value after the capture.
            restored = series_root.evaluate("element => element.scrollTop")
            topology = json.loads(
                (Path(tmp) / "snapshots" / "a_series_001" / "before" / "topology.json").read_text(encoding="utf-8")
            )
            browser.close()

        self.assertEqual(restored, 4)
        # The innermost frame document (frame_id "series-frame" / name "seriesFrame")
        # is the one holding the series list.
        innermost = next(
            document for document in topology["documents"]
            if document.get("frame_id") == "series-frame" or document.get("frame_name") == "seriesFrame"
        )
        series_regions = [r for r in innermost["regions"] if r["region_type"] == "series"]
        self.assertEqual(len(series_regions), 1)
        region = series_regions[0]
        # The nested 序列选择 marker must carry SeriesCollectionEvidence, i.e. it
        # went through the scroll harvest (the generic parent-region path yields
        # series_collection=None and fails this assertion).
        self.assertIsNotNone(region["series_collection"])
        self.assertEqual(region["series_collection"]["collected_count"], 3)
        collected = {member["dom"]["attributes"].get("data-series") for member in region["members"]}
        self.assertEqual(collected, {"SERIES-1", "SERIES-2", "SERIES-3"})

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

    # --- Phase 4: independent expansion hook runtime ---

    def test_expand_series_hook_is_a_safe_noop_when_disabled(self):
        # With a wired session but expansion disabled, the hook must not call
        # the session entry at all.
        session = Mock()
        with patch.object(replica_batch, "_EXPANSION_CONFIG", {"expand_all_series": False}), \
             patch.object(replica_batch, "_LIVE_SESSION", session):
            replica_batch.capture_hook_expand_series(object(), lambda: None, "a_000_001", "a_002_001")
        session.expand_series.assert_not_called()

    def test_expand_series_hook_is_a_safe_noop_without_session(self):
        with patch.object(replica_batch, "_EXPANSION_CONFIG", {"expand_all_series": True}), \
             patch.object(replica_batch, "_LIVE_SESSION", None):
            replica_batch.capture_hook_expand_series(object(), lambda: None, "a_000_001", "a_002_001")
        # no exception raised

    def test_expand_series_hook_delegates_to_session_entry_when_enabled(self):
        session = Mock()
        with patch.object(replica_batch, "_EXPANSION_CONFIG", {"expand_all_series": True, "max_series": 40}), \
             patch.object(replica_batch, "_LIVE_SESSION", session):
            replica_batch.capture_hook_expand_series(object(), lambda: None, "a_000_001", "a_002_001")
        session.expand_series.assert_called_once()
        self.assertEqual(session.expand_series.call_args.args[2], "a_000_001")
        self.assertEqual(session.expand_series.call_args.args[3], "a_002_001")

    def test_session_expand_series_is_a_callable_noop_placeholder(self):
        # The Phase 4 session-side entry is intentionally a side-effect-free stub
        # (Phase 5 installs the explorer body). It must be safely callable.
        session = LiveCaptureSession("out")
        session.expand_series(object(), lambda: None, "a_000_001", "a_002_001")
        # no exception, no output written

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

    # --- Phase 6: merge branch snapshots into a schema-v2 flow ---

    @staticmethod
    def _template_source():
        return '''from playwright.sync_api import sync_playwright


def run(page):
    # [MARKER: 序列选择]
    page.locator("#series a").first.click()
    # [MARKER: Meta 信息工具]
    page.locator("#meta-open").click()
    page.locator("#meta-close").click()
'''

    @staticmethod
    def _branch_doc(document_id="d_p_000_root"):
        return {
            "document_id": document_id, "page_id": "p_000", "page_var": "page",
            "page_kind": "main", "parent_document_id": None, "frame_selector": None,
            "frame_id": None, "frame_name": None, "viewport": {"width": 800, "height": 600},
            "device_scale_factor": 1.0, "screenshot_scale": "css",
            "scroll_x": 0.0, "scroll_y": 0.0, "screenshot_asset_relpath": "",
            "screenshot_sha256": "h", "screenshot_size_bytes": 100,
        }

    def _write_branch(self, root, branch_id, series_key, ordinal, status, with_metadata=False):
        branch_dir = root / "series_branches" / branch_id
        viewer_dir = branch_dir / "viewer"
        viewer_dir.mkdir(parents=True, exist_ok=True)
        descriptor = {
            "series_key": series_key, "label": f"Series {ordinal}", "ordinal": ordinal,
            "document_id": "d_series_hub", "member_id": f"d_series_hub_series_{ordinal:03d}",
            "stable_attributes": {"data-series": series_key}, "selected": False,
            "explicit_frame_count": None, "inferred_frame_count": None, "activation": "click",
        }
        (branch_dir / "descriptor.json").write_text(json.dumps(descriptor), encoding="utf-8")
        (viewer_dir / "topology.json").write_text(
            json.dumps({"pages": [], "documents": [self._branch_doc(f"d_p_000_root_{branch_id}")]}),
            encoding="utf-8",
        )
        status_payload = {
            "branch_id": branch_id, "series_key_sha256": "x", "ordinal": ordinal,
            "capture_status": status, "fail_stage": ("metadata" if status == "partial" else None),
            "error_type": None, "warning": ("series_capture_partial" if status == "partial" else None),
            "activation": "click", "metadata_captured": with_metadata,
        }
        if with_metadata:
            meta_dir = branch_dir / "metadata"
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / "topology.json").write_text(
                json.dumps({"pages": [], "documents": [self._branch_doc(f"d_p_000_root_meta_{branch_id}")]}),
                encoding="utf-8",
            )
            # The writer/capture path is ``metadata/metadata_rows.json`` (see
            # _capture_metadata_transaction); the loader reads it from there.
            (meta_dir / "metadata_rows.json").write_text(
                json.dumps({"rows": [{"row": "Series Number: 1"}], "outer_html": "", "uid_sha256_prefix": "abc"}),
                encoding="utf-8",
            )
        (branch_dir / "status.json").write_text(json.dumps(status_payload), encoding="utf-8")

    def test_branch_merge_preserves_entry_and_creates_unique_states(self):
        from rewrite_script import parse_action_plan
        from replica_models import ReplicaState, StateEvidence
        plan = parse_action_plan(self._template_source())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_branch(root, "b000_abcd", "uid-a", 0, "captured", with_metadata=True)
            self._write_branch(root, "b001_efgh", "uid-b", 1, "captured", with_metadata=False)
            self._write_branch(root, "b002_ijkl", "uid-c", 2, "failed", with_metadata=False)
            entry = ReplicaState("s_000", 0, "", "page", [], [], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))
            states = [entry]
            warnings = []
            branches, expansion = replica_batch._build_branches_into_flow(states, root, plan, warnings)

        # Original entry state is preserved and untouched.
        self.assertEqual(states[0].state_id, "s_000")
        # Unique viewer states exist for each captured branch.
        viewer_ids = [b.viewer_state_id for b in branches if b.capture_status == "captured"]
        self.assertEqual(len(viewer_ids), 2)
        self.assertEqual(len(set(viewer_ids)), 2)
        captured = {b.series_key: b for b in branches if b.capture_status == "captured"}
        # Metadata-open trigger lives on the viewer state with a transition to metadata.
        viewer_state_a = next(s for s in states if s.state_id == captured["uid-a"].viewer_state_id)
        self.assertTrue(any(t.to_state_id == captured["uid-a"].metadata_state_id for t in viewer_state_a.transitions))
        self.assertTrue(any("meta_open" in t.transition_id for t in viewer_state_a.transitions))
        self.assertTrue(any("meta_open" in t.action_id for doc in viewer_state_a.documents for t in doc.targets if "meta_open" in t.action_id))
        # Metadata-close transition returns explicitly to the same branch viewer.
        meta_state_a = next(s for s in states if s.state_id == captured["uid-a"].metadata_state_id)
        self.assertEqual(meta_state_a.transitions[0].to_state_id, captured["uid-a"].viewer_state_id)
        self.assertEqual(captured["uid-a"].return_state_id, captured["uid-a"].viewer_state_id)
        # Failed branch stays in series_branches but references no state.
        failed = next(b for b in branches if b.capture_status == "failed")
        self.assertEqual(failed.series_key, "uid-c")
        self.assertIsNone(failed.viewer_state_id)
        self.assertIsNone(failed.metadata_state_id)
        self.assertIsNotNone(failed.warning)
        # Expansion evidence conserves counts.
        self.assertEqual(expansion.discovered_count, 3)
        self.assertEqual(expansion.captured_count, 2)
        self.assertEqual(expansion.failed_count, 1)

    def test_branch_merge_metadata_success_has_unique_metadata_state(self):
        from rewrite_script import parse_action_plan
        from replica_models import ReplicaState, StateEvidence
        plan = parse_action_plan(self._template_source())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_branch(root, "b000_abcd", "uid-a", 0, "captured", with_metadata=True)
            self._write_branch(root, "b001_efgh", "uid-b", 1, "captured", with_metadata=False)
            states = [ReplicaState("s_000", 0, "", "page", [], [], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))]
            branches, _expansion = replica_batch._build_branches_into_flow(states, root, plan, [])

        captured = {b.series_key: b for b in branches if b.capture_status == "captured"}
        # Metadata-successful branch has its own metadata state; other has none.
        self.assertIsNotNone(captured["uid-a"].metadata_state_id)
        self.assertIsNone(captured["uid-b"].metadata_state_id)
        meta_ids = [b.metadata_state_id for b in branches if b.metadata_state_id]
        self.assertEqual(len(meta_ids), 1)
        self.assertEqual(len(set(meta_ids)), 1)

    def test_branch_merge_partial_still_produces_viewer_state(self):
        from rewrite_script import parse_action_plan
        from replica_models import ReplicaState, StateEvidence
        plan = parse_action_plan(self._template_source())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Partial: metadata failed but the viewer itself succeeded.
            self._write_branch(root, "b000_abcd", "uid-a", 0, "partial", with_metadata=False)
            states = [ReplicaState("s_000", 0, "", "page", [], [], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))]
            branches, expansion = replica_batch._build_branches_into_flow(states, root, plan, [])

        branch = branches[0]
        self.assertEqual(branch.capture_status, "partial")
        self.assertIsNotNone(branch.viewer_state_id)
        self.assertIsNone(branch.metadata_state_id)
        self.assertEqual(expansion.partial_count, 1)
        # The partial branch's viewer state is a real state in the flow.
        self.assertTrue(any(s.state_id == branch.viewer_state_id for s in states))

    def test_load_series_branch_snapshots_reads_dirs_and_dedupes_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_branch(root, "b000_abcd", "uid-a", 0, "captured", with_metadata=True)
            snapshots, warnings, expansion = replica_batch._load_series_branch_snapshots(root)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].capture_status, "captured")
        self.assertEqual(snapshots[0].source_member_id, "d_series_hub_series_000")
        self.assertTrue(snapshots[0].viewer_documents)
        self.assertTrue(snapshots[0].metadata_documents)
        self.assertEqual(snapshots[0].metadata_rows, [{"row": "Series Number: 1"}])
        self.assertIsNone(expansion)

    def test_load_snapshot_state_rebases_series_list_full_asset(self):
        # The scroll-stitched series-list background is stored phase-relative and
        # must be rebased against the capture root exactly like screenshots, or
        # the builder cannot resolve it and silently falls back to the static
        # overlay-scroll replica.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            phase = root / "snapshots" / "a_000_001" / "after"
            assets = phase / "assets"
            assets.mkdir(parents=True)
            (assets / "series_list_full_d_test.jpeg").write_bytes(b"jpeg")
            doc = self._branch_doc("d_p_000_root")
            doc["screenshot_asset_relpath"] = "assets/d_p_000_root.png"
            doc["series_list_full_asset_relpath"] = "assets/series_list_full_d_test.jpeg"
            (phase / "topology.json").write_text(
                json.dumps({"pages": [], "documents": [doc]}), encoding="utf-8"
            )

            _, documents = replica_batch._load_snapshot_state(root, "a_000_001", "after")

            self.assertEqual(
                documents[0].series_list_full_asset_relpath,
                "snapshots/a_000_001/after/assets/series_list_full_d_test.jpeg",
            )
            self.assertEqual(
                documents[0].screenshot_asset_relpath,
                "snapshots/a_000_001/after/assets/d_p_000_root.png",
            )

    def test_expand_series_reconstructs_template_and_delegates_when_source_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            replay = output / "instrumented_replay.py"
            replay.write_text(self._template_source(), encoding="utf-8")
            session = LiveCaptureSession(output)
            with patch.object(session, "finalize_series_branches") as finalize:
                session.expand_series(object(), lambda: None, "a_000_001", "a_002_001")
            self.assertEqual(finalize.call_count, 1)
            template = finalize.call_args.args[1]
            self.assertTrue(template.complete)
            self.assertIsNotNone(template.series_action)
            self.assertIsNotNone(template.metadata_open)
            self.assertIsNotNone(template.metadata_close)

    def test_expand_series_is_noop_without_replay_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = LiveCaptureSession(Path(tmp))
            with patch.object(session, "finalize_series_branches") as finalize:
                session.expand_series(object(), lambda: None)
            finalize.assert_not_called()


class StepOneTargetSnapshotTests(unittest.TestCase):
    """步骤 1：multi-match / 无匹配 target 捕获不静默丢失，target.json 落盘受控。

    全部用 fake locator，不依赖真实浏览器。
    """

    def test_capture_locator_snapshot_multimatch_returns_first(self):
        # count()>1 的 fake locator：capture_locator_snapshot 归一 .first 返回非空，
        # 不再抛 strict-mode（防 Z1 静默吞）。
        from capture_snapshot import capture_locator_snapshot

        payload = {
            "tag_name": "li", "text": "x",
            "attributes": {"id": "row"},
            "rect": {"x": 0, "y": 0, "width": 10, "height": 10},
            "outer_html": "<li id='row'>x</li>",
            "computed_style": {"display": "block"},
        }
        first = SimpleNamespace()
        first.evaluate = lambda fn, *a: payload
        root = SimpleNamespace()
        root.count = lambda: 2
        root.first = first

        snapshot = capture_locator_snapshot(root)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.tag_name, "li")

    def test_capture_locator_snapshot_no_match_returns_none(self):
        from capture_snapshot import capture_locator_snapshot

        root = SimpleNamespace()
        root.count = lambda: 0

        self.assertIsNone(capture_locator_snapshot(root))

    def test_missing_target_evidence_is_reported_in_flow_warnings(self):
        # 有完整快照对（before+after topology）但 target.json 缺失 → build 侧必须把
        # missing_target_evidence 标进 flow.warnings（pipeline_report 可见），
        # 让「target 本可捕获却缺失/被静默丢」显式可审计。
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
            page.set_content('<div id="series">Series</div>')
            target = lambda: page.locator("#series")
            session.before("a_000_001", page, target, "序列选择")
            target().click()
            session.after("a_000_001", page, target, "序列选择")
            # 模拟 multi-match/无匹配被静默丢 target：删掉 before+after 两个 target.json
            # （_load_target_snapshot 会 before 优先、无则 fallback after），保留快照对。
            for phase in ("before", "after"):
                (root / "capture" / "snapshots" / "a_000_001" / phase / "target.json").unlink()

            flow = build_flow_from_snapshots(script, root / "capture")

            self.assertIn("missing_target_evidence:a_000_001", flow.warnings)
            browser.close()

    def test_multimatch_action_target_json_is_written_not_silently_dropped(self):
        # 走 LiveCaptureSession._capture 的 target 落盘路径，用**真实**
        # capture_locator_snapshot：multi-match locator 现在被 .first 归一，
        # 不再抛 strict-mode，因此 target.json 被写入而非静默丢弃（Z1 修复）。
        dom_payload = {
            "tag_name": "div", "text": "target",
            "attributes": {"id": "series"},
            "rect": {"x": 0, "y": 0, "width": 100, "height": 20},
            "outer_html": "<div id='series'>target</div>",
            "computed_style": {"display": "block"},
        }
        first = SimpleNamespace()

        def first_evaluate(fn, *args):
            source = str(fn)
            # frame-owner probe 的 JS 含 frameElement；selector closure 的 JS 含 ancestors；
            # 其余（DOM snapshot）返回完整 payload。
            if "frameElement" in source:
                return {"id": None, "name": None}
            if "ancestors" in source:
                return {"outer": "<div id='series'>target</div>", "ancestors": 1,
                        "siblings": 0, "sources": []}
            return dom_payload
        first.evaluate = first_evaluate
        target_locator = SimpleNamespace()
        target_locator.count = lambda: 3
        target_locator.first = first
        target_locator.locator = lambda sel: SimpleNamespace(count=lambda: 0)
        page = Mock()
        page.context = SimpleNamespace(pages=[page])
        page.viewport_size = {"width": 800, "height": 600}
        page.is_closed = lambda: False
        page.wait_for_timeout = lambda ms: None
        page.url = "https://example.com/film/#/shared"
        page.evaluate = Mock(return_value=0)
        with tempfile.TemporaryDirectory() as tmp:
            session = LiveCaptureSession(Path(tmp))
            capture_dir = Path(tmp) / "snapshots" / "a_000_001" / "after"
            capture_dir.mkdir(parents=True, exist_ok=True)  # topology 写入前目录需存在
            with patch("batch_capture_replicate.capture_page_topology", return_value=([], [])) as topo:
                session._capture("a_000_001", "after", page, lambda: target_locator, "报告截图")
            # target_locator.count()==3 是 multi-match；capture_locator_snapshot 内部归一
            # .first、不抛 strict-mode —— target.json 被写入而非静默丢弃。
            self.assertTrue((capture_dir / "target.json").exists())
            self.assertTrue((capture_dir / "selector_closure.json").exists())
            topo.assert_called_once()


class StepTwoLayoutCaptureTests(unittest.TestCase):
    """步骤 2：布局捕获的控制流单测（fake locator，不依赖真实站点）。

    覆盖变体推断、连点顺序、稳定采样、降级策略；真实连点只在浏览器可访问时执行。
    """

    def test_layout_variant_id_infers_from_text(self):
        self.assertEqual(replica_batch._layout_variant_id("*1 Shift+1"), "1*1")
        self.assertEqual(replica_batch._layout_variant_id("2*2"), "2*2")
        self.assertEqual(replica_batch._layout_variant_id("1×2"), "1*2")
        self.assertEqual(replica_batch._layout_variant_id("3x3"), "3*3")
        self.assertIsNone(replica_batch._layout_variant_id("品字"))
        self.assertIsNone(replica_batch._layout_variant_id(""))

    def test_layout_variant_id_infers_from_title_with_shift_suffix(self):
        """Dapeng 布局 option button 文本为空，布局规格在 ``title`` 属性
        （``title="2*2 Shift+4"``）。title 里的 ``Shift+`` 后缀不得干扰推断。"""
        for title, expected in [
            ("1*1 Shift+1", "1*1"),
            ("2*2 Shift+4", "2*2"),
            ("3*3 Shift+9", "3*3"),
            ("1*2 Shift+2", "1*2"),
            ("2*1", "2*1"),
            ("2*3 Shift+6", "2*3"),
            ("1*3 Shift+3", "1*3"),
        ]:
            self.assertEqual(replica_batch._layout_variant_id(title), expected, title)

    def test_sample_layout_background_waits_for_canvas_and_returns_by_hash(self):
        # 画布 width>0 立即满足 + 连续两次 PNG 不变 → 返回 by-hash relpath。
        import io as _io
        from PIL import Image as _PILImage
        buf = _io.BytesIO()
        _PILImage.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
        stable_png = buf.getvalue()
        page = Mock()
        page.evaluate = Mock(return_value=1024)

        def canvas_png():
            return stable_png
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = replica_batch._sample_layout_background(
                page, canvas_png, root, "d_main", "2*2",
            )
            self.assertIsNotNone(rel)
            self.assertTrue((root / rel).exists())
            self.assertIn("assets/by-hash/", rel)

    def test_sample_layout_background_timeout_returns_none(self):
        # 画布始终 width=0 → 超时返回 None（该变体失败，不阻断）。
        import io as _io
        from PIL import Image as _PILImage
        buf = _io.BytesIO()
        _PILImage.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
        page = Mock()
        page.evaluate = Mock(return_value=0)

        def canvas_png():
            return buf.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            rel = replica_batch._sample_layout_background(
                page, canvas_png, Path(tmp), "d_main", "1*1",
                poll_interval_s=0.02, stability_timeout_s=0.1,
            )
            self.assertIsNone(rel)

    def test_sample_all_layout_variants_degrades_when_all_fail(self):
        # 所有布局选项点击后画布无变化 → layout_variants 为空 + log 记 layout_capture_partial，
        # 绝不抛异常。
        from replica_models import ReplicaDocument
        warnings: list[str] = []
        doc = ReplicaDocument(
            document_id="d_main", page_id="p_000", page_var="page", page_kind="main",
            parent_document_id=None, frame_selector=None, frame_id=None, frame_name=None,
            viewport={"width": 800, "height": 600}, device_scale_factor=1.0, screenshot_scale="css",
            scroll_x=0.0, scroll_y=0.0, screenshot_asset_relpath="assets/d_main.png",
            screenshot_sha256="h", screenshot_size_bytes=1,
        )
        payload = {
            "tag_name": "button", "text": "2*2",
            "attributes": {"id": "layout_2_2"},
            "rect": {"x": 0, "y": 0, "width": 30, "height": 30},
            "outer_html": "<button id='layout_2_2'>2*2</button>",
            "computed_style": {"display": "block"},
        }
        first = SimpleNamespace()
        first.evaluate = lambda fn, *a: payload
        root = SimpleNamespace()
        root.count = lambda: 3
        root.first = first
        # 每个成员点击后画布指纹不变（canvas_hash 返回相同值）→ 全部跳过。
        variant = SimpleNamespace(is_visible=lambda: True, inner_text=lambda: "2*2",
                                   text_content=lambda: "2*2", click=lambda *a, **k: None,
                                   get_attribute=lambda name: None,
                                   locator=lambda sel: SimpleNamespace(count=lambda: 0))
        root.locator = lambda sel: SimpleNamespace(count=lambda: 3, nth=lambda i: variant)
        target_document = doc
        page = Mock()
        page.evaluate = Mock(return_value=0)

        with patch("batch_capture_replicate._canvas_hash_or_none", return_value=123):
            variants, default_layout = replica_batch._sample_all_layout_variants(
                page, root, target_document, Path("C:/tmp/nonexistent_capture_root"),
                log=warnings.append,
            )

        self.assertEqual(variants, {})
        self.assertEqual(default_layout, "")
        self.assertTrue(any("layout_capture_partial" in message for message in warnings))

    def test_sample_all_layout_variants_infers_from_title_when_text_empty(self):
        """Dapeng 布局 option：text 为空、规格在 title 属性（缺陷 F 回归锁）。
        点击后画布变化（canvas_hash 前后不同）→ 稳定采样 → 变体进 layout_variants。"""
        from replica_models import ReplicaDocument
        import io as _io
        from PIL import Image as _PILImage
        snapshot_png = _io.BytesIO()
        _PILImage.new("RGB", (8, 8), (10, 20, 30)).save(snapshot_png, "PNG")
        stable_png = snapshot_png.getvalue()
        doc = ReplicaDocument(
            document_id="d_main", page_id="p_000", page_var="page", page_kind="main",
            parent_document_id=None, frame_selector=None, frame_id=None, frame_name=None,
            viewport={"width": 800, "height": 600}, device_scale_factor=1.0, screenshot_scale="css",
            scroll_x=0.0, scroll_y=0.0, screenshot_asset_relpath="assets/d_main.png",
            screenshot_sha256="h", screenshot_size_bytes=1,
        )
        # 布局 option：text()==''、title="2*2 Shift+4"。
        variant = SimpleNamespace(
            is_visible=lambda: True,
            inner_text=lambda: "",
            text_content=lambda: "",
            get_attribute=lambda name: "2*2 Shift+4" if name == "title" else None,
            click=lambda *a, **k: None,
            locator=lambda sel: SimpleNamespace(count=lambda: 0),
        )
        root = SimpleNamespace(count=lambda: 1, first=SimpleNamespace(), locator=lambda sel: SimpleNamespace(count=lambda: 1, nth=lambda i: variant))
        page = Mock()
        page.evaluate = Mock(return_value=1024)
        # canvas 指纹：点击前 111，点击后 222（变化 → 尝试采样）；采样 PNG 稳定返回。
        with tempfile.TemporaryDirectory() as tmp:
            hashes = iter([111, 222])
            def fake_hash(_page):
                return next(hashes, 222)
            with patch("batch_capture_replicate._canvas_hash_or_none", side_effect=fake_hash), \
                 patch("batch_capture_replicate._canvas_png_or_none", return_value=stable_png):
                variants, default_layout = replica_batch._sample_all_layout_variants(
                    page, root, doc, Path(tmp),
                )
            self.assertIn("2*2", variants, "title 推断出的变体应采到")
            rel = variants.get("2*2")
            self.assertTrue(rel and (Path(tmp) / rel).exists(), f"by-hash 资产未落盘: {rel}")


if __name__ == "__main__":
    unittest.main()
