import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from runtime_python import codegen_python_executable
from rewrite_script import generate_offline_adapter_script, generate_serve_script


COMPLETED = '''from playwright.sync_api import sync_playwright

def run(playwright):
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://real.example/login?token=secret")
    page.locator("#password").fill("secret")
    # [MARKER: 报告截图]
    page.locator("#open-viewer").click()
    # [MARKER: Meta 信息工具]
    page.locator("#metadata").click()
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
'''

COMPLETED_WITH_CANVAS = COMPLETED.replace(
    "# [MARKER: Meta 信息工具]",
    "# [MARKER: 影像画布交互]",
).replace(
    'page.locator("#metadata").click()',
    'page.locator("canvas").click()',
)


class OfflineAdapterTests(unittest.TestCase):
    def test_rewrite_removes_live_bootstrap_but_keeps_post_marker_business_logic(self):
        generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
        ast.parse(generated)
        self.assertNotIn("https://real.example", generated)
        self.assertNotIn('#password").fill', generated)
        self.assertIn('#open-viewer").click', generated)
        self.assertIn('#metadata").click', generated)
        self.assertIn("ReplicaServer", generated)

    def test_rewrite_blocks_non_loopback_requests(self):
        generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
        self.assertIn("context.route", generated)
        self.assertIn("external_requests", generated)
        self.assertIn("offline_external_request", generated)

    def test_rewrite_emits_marker_start_and_finish_events(self):
        generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
        self.assertIn("marker_started", generated)
        self.assertIn("marker_finished", generated)
        self.assertIn("报告截图", generated)
        self.assertIn("Meta 信息工具", generated)

    def test_rewrite_uses_bootstrap_plan_entry_bindings(self):
        generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
        self.assertIn('pages = {"page": page}', generated)
        self.assertIn("restored from local entry binding", generated)

    def test_static_only_canvas_policy_emits_degraded_instead_of_false_failure(self):
        generated = generate_offline_adapter_script(
            COMPLETED_WITH_CANVAS,
            ".",
            "validation",
            capability_policy={"影像画布交互": "static-only"},
        )
        self.assertIn("marker_degraded", generated)
        self.assertIn("canvas_dynamic_pixels", generated)


class OfflineAdapterExecutionTests(unittest.TestCase):
    """Real local execution: build a minimal replica and run the generated runner."""

    def test_generated_offline_runner_executes_against_minimal_replica(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(
                """<!doctype html><html><body>
<button id="open-viewer">Open</button>
<button id="metadata">Meta</button>
</body></html>""",
                encoding="utf-8",
            )
            (root / "serve_replica.py").write_text(
                generate_serve_script(), encoding="utf-8"
            )
            generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
            script = root / "completed_fixture_offline.py"
            script.write_text(generated, encoding="utf-8")
            try:
                result = subprocess.run(
                    [codegen_python_executable(), str(script)],
                    cwd=root,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                self.fail("generated offline runner timed out (30s)")
            self.assertEqual(result.returncode, 0, result.stderr)
            events_path = root / "validation" / "events.jsonl"
            self.assertTrue(events_path.exists())
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            names = [event["event"] for event in events]
            self.assertIn("marker_started", names)
            self.assertIn("marker_finished", names)
            external_requests = json.loads(
                (root / "validation" / "external_requests.json").read_text(encoding="utf-8")
            )
            self.assertEqual(external_requests, [])

    def test_python_egress_guard_records_and_rejects_non_loopback_call(self):
        """A marker block doing a raw Python outbound call must be recorded into
        the shared ``external_requests`` and fail the run as
        ``offline_external_request`` (F1) — it must NOT silently pass with an
        empty external_requests the way only the browser route would allow."""
        completed = COMPLETED.replace(
            '    page.locator("#metadata").click()',
            '    import urllib.request\n'
            '    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n'
            '    _opener.open("http://192.0.2.1/")',
        )
        # sanity: the injected call actually lands in a marker block
        self.assertIn("# [MARKER: Meta 信息工具]", completed)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(
                """<!doctype html><html><body>
<button id="open-viewer">Open</button>
<button id="metadata">Meta</button>
</body></html>""",
                encoding="utf-8",
            )
            (root / "serve_replica.py").write_text(
                generate_serve_script(), encoding="utf-8"
            )
            generated = generate_offline_adapter_script(completed, ".", "validation")
            script = root / "completed_egress_offline.py"
            script.write_text(generated, encoding="utf-8")
            try:
                result = subprocess.run(
                    [codegen_python_executable(), str(script)],
                    cwd=root,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                self.fail("generated offline runner timed out (30s)")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("offline_external_request", result.stderr)
            external_requests = json.loads(
                (root / "validation" / "external_requests.json").read_text(encoding="utf-8")
            )
            self.assertTrue(external_requests)
            self.assertTrue(any("192.0.2.1" in str(entry) for entry in external_requests))


if __name__ == "__main__":
    unittest.main()
