import ast
import tempfile
import unittest
import importlib.util
from pathlib import Path
from urllib.request import urlopen

from batch_capture_replicate import classify_recording_template, instrument_marked_actions
from replay_helpers import ReplicaServer
from rewrite_script import generate_replay_script, generate_serve_script, parse_action_plan


EXPANSION_SOURCE = '''from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        # [MARKER: 序列选择]
        page.locator("#series").click()
        # [MARKER: Meta 信息工具]
        page.locator("#meta-open").click()
        # [MARKER: Meta 信息工具]
        page.locator("#meta-close").click()
        browser.close()

run()
'''

MULTI_STEP_METADATA_SOURCE = '''from playwright.sync_api import sync_playwright

def run(page):
    # [MARKER: 序列选择]
    page.locator("#series").click()
    # [MARKER: Meta 信息工具]
    page.locator("#more").click()
    page.get_by_role("link").filter(has_text="Tags").click()
    page.locator("#meta-close").click()
'''

EXPANSION_CONFIG = {
    "expand_all_series": True,
    "max_series": 40,
    "per_series_timeout_s": 20,
    "total_series_timeout_s": 600,
    "viewer_capture_mode": "first_stable_frame",
}


class ExpansionHookInjectionTests(unittest.TestCase):
    def test_template_classification_detects_series_open_close(self):
        plan = parse_action_plan(EXPANSION_SOURCE)
        template = classify_recording_template(plan)
        self.assertTrue(template.complete)
        self.assertEqual(template.series_action.action_id, "a_000_001")
        self.assertEqual(template.metadata_open.action_id, "a_001_001")
        self.assertEqual(template.metadata_close.action_id, "a_002_001")

    def test_template_classification_preserves_multi_step_metadata_open(self):
        plan = parse_action_plan(MULTI_STEP_METADATA_SOURCE)
        template = classify_recording_template(plan)

        self.assertTrue(template.complete)
        self.assertEqual(template.metadata_open.action_id, "a_001_001")
        self.assertEqual(
            [action.action_id for action in template.metadata_open_actions],
            ["a_001_001", "a_001_002"],
        )
        final_open = template.metadata_open_actions[-1]
        self.assertEqual(final_open.action_id, "a_001_002")
        self.assertEqual(final_open.locator.locator_args["args"], ["link"])
        self.assertEqual(final_open.locator.locator_args["_filter"], {"has_text": "Tags"})
        self.assertEqual(template.metadata_close.action_id, "a_001_003")

    def test_expansion_hook_imported_only_when_enabled(self):
        instrumented = instrument_marked_actions(EXPANSION_SOURCE, expansion_config=EXPANSION_CONFIG)
        self.assertIn("capture_hook_expand_series", instrumented)
        self.assertTrue(
            instrumented.startswith(
                "from batch_capture_replicate import capture_hook_after, capture_hook_before, capture_hook_expand_series, capture_hook_failed"
            )
        )

    def test_no_expansion_hook_by_default(self):
        instrumented = instrument_marked_actions(EXPANSION_SOURCE)
        self.assertNotIn("capture_hook_expand_series", instrumented)
        self.assertIn("capture_hook_after", instrumented)
        self.assertNotIn("capture_hook_expand_series", instrumented)

    def test_expansion_hook_follows_close_after_and_precedes_browser_close(self):
        instrumented = instrument_marked_actions(EXPANSION_SOURCE, expansion_config=EXPANSION_CONFIG)
        ast.parse(instrumented)
        after_pos = instrumented.find('capture_hook_after("a_002_001"')
        expand_pos = instrumented.find("capture_hook_expand_series(page, lambda:")
        close_pos = instrumented.find("browser.close()")
        self.assertGreater(after_pos, -1)
        self.assertGreater(expand_pos, -1)
        self.assertGreater(close_pos, -1)
        self.assertLess(after_pos, expand_pos)
        self.assertLess(expand_pos, close_pos)
        # The expansion trigger carries the stable series + close action ids.
        self.assertIn("a_000_001", instrumented[expand_pos:])
        self.assertIn("a_002_001", instrumented[expand_pos:])
        # Expansion never fires for the open or series actions.
        self.assertEqual(instrumented.count("capture_hook_expand_series("), 1)

    def test_expansion_hook_sits_in_same_else_branch_as_close_after(self):
        # Both the close action's after-hook and the expansion hook must be the
        # immediate children of the same success ``else:`` (same indentation).
        # When the close action throws, the except branch runs and NEITHER the
        # after-hook NOR the expansion runs.
        instrumented = instrument_marked_actions(EXPANSION_SOURCE, expansion_config=EXPANSION_CONFIG)
        lines = instrumented.splitlines()
        try_idx = next(i for i, line in enumerate(lines) if 'page.locator("#meta-close").click()' in line)
        self.assertEqual(lines[try_idx].lstrip(), 'page.locator("#meta-close").click()')
        self.assertTrue(lines[try_idx + 1].lstrip().startswith("except Exception as error:"))
        else_idx = next(i for i in range(try_idx + 1, len(lines)) if lines[i].lstrip() == "else:")
        after_idx = next(i for i in range(else_idx + 1, len(lines)) if 'capture_hook_after("a_002_001"' in lines[i])
        expand_idx = next(i for i in range(else_idx + 1, len(lines)) if "capture_hook_expand_series(" in lines[i])
        self.assertEqual(
            len(lines[after_idx]) - len(lines[after_idx].lstrip()),
            len(lines[expand_idx]) - len(lines[expand_idx].lstrip()),
        )
        self.assertLess(after_idx, expand_idx)

    def test_marked_actions_run_exactly_once_without_expansion(self):
        instrumented = instrument_marked_actions(EXPANSION_SOURCE)
        # Each marked action's before/after must appear exactly once; the
        # original actions are not duplicated by instrumentation.
        for action_id in ("a_000_001", "a_001_001", "a_002_001"):
            self.assertEqual(instrumented.count(f'capture_hook_before("{action_id}"'), 1)
            self.assertEqual(instrumented.count(f'capture_hook_after("{action_id}"'), 1)
            self.assertEqual(instrumented.count(f'capture_hook_failed("{action_id}"'), 1)

    def test_incomplete_template_raises_when_expansion_requested(self):
        incomplete = '''from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        # [MARKER: 序列选择]
        page.locator("#series").click()
        # [MARKER: Meta 信息工具]
        page.locator("#meta-open").click()
        browser.close()

run()
'''
        with self.assertRaisesRegex(ValueError, "Metadata open and a Metadata close"):
            instrument_marked_actions(incomplete, expansion_config=EXPANSION_CONFIG)
        self.assertFalse(classify_recording_template(parse_action_plan(incomplete)).complete)

    def test_dblclick_series_activation_is_inherited(self):
        source = EXPANSION_SOURCE.replace('page.locator("#series").click()', 'page.locator("#series").dblclick()')
        instrumented = instrument_marked_actions(source, expansion_config=EXPANSION_CONFIG)
        # The inherited activation stays whatever the human recorded (dblclick) and
        # the expansion trigger still carries the series locator factory.
        self.assertIn('page.locator("#series").dblclick()', instrumented)
        self.assertIn("capture_hook_expand_series(page, lambda:", instrumented)
        self.assertIn("a_000_001", instrumented)
        self.assertIn("a_002_001", instrumented)


class ReplicaServerTests(unittest.TestCase):
    def test_server_exposes_entrypoint_only_on_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<h1>Offline replica</h1>", encoding="utf-8")
            with ReplicaServer(root) as server:
                self.assertTrue(server.url.startswith("http://127.0.0.1:"))
                with urlopen(server.url, timeout=3) as response:
                    self.assertIn(b"Offline replica", response.read())

    def test_generated_replay_script_is_syntax_valid_and_skips_bootstrap(self):
        script = generate_replay_script("replica", {"page": "main", "page1": "popup"})

        compile(script, "replay_fixture.py", "exec")
        self.assertIn("ReplicaServer", script)
        self.assertIn("from serve_replica import ReplicaServer", script)
        self.assertIn("page = context.new_page()", script)
        self.assertNotIn("page.goto(\"https://", script)

    def test_generated_replay_script_executes_popup_and_frame_actions(self):
        script = generate_replay_script(
            ".",
            {"page": "main", "page1": "popup"},
            [
                {
                    "action_type": "click",
                    "action_source_kind": "locator",
                    "action_args": {"args": []},
                    "page_var": "page",
                    "locator": {"page_var": "page", "frame_chain": [], "locator_kind": "role", "locator_args": {"args": ["button"], "name": "Open"}, "ordinal_op": None},
                    "transition": {"mode": "popup", "target_page_var": "page1"},
                },
                {
                    "action_type": "fill",
                    "action_source_kind": "locator",
                    "action_args": {"args": ["2000"]},
                    "page_var": "page1",
                    "locator": {"page_var": "page1", "frame_chain": [{"selector": "#iframe"}], "locator_kind": "css", "locator_args": {"args": ["#ww"]}, "ordinal_op": None},
                },
            ],
        )

        compile(script, "replay_actions.py", "exec")
        self.assertIn("expect_popup()", script)
        self.assertIn("pages['page1'] = popup_info.value", script)
        self.assertIn("frame_locator('#iframe').locator('#ww').fill('2000')", script)

    def test_generated_replay_script_restores_title_locator(self):
        script = generate_replay_script(
            ".",
            {"page": "main"},
            [
                {
                    "action_type": "click",
                    "action_source_kind": "locator",
                    "action_args": {"args": []},
                    "page_var": "page",
                    "locator": {"page_var": "page", "frame_chain": [], "locator_kind": "title", "locator_args": {"args": ["更多"]}, "ordinal_op": None},
                }
            ],
        )

        compile(script, "replay_title.py", "exec")
        self.assertIn("pages['page'].get_by_title('更多').click()", script)

    def test_generated_replay_script_preserves_chained_locator_expression(self):
        script = generate_replay_script(
            ".",
            {"page": "main"},
            [
                {
                    "action_type": "click",
                    "action_source_kind": "locator",
                    "action_args": {"args": []},
                    "page_var": "page",
                    "locator": {
                        "source_expression": "page.get_by_role('link').filter(has_text='预设窗宽窗位')",
                        "page_var": "page",
                        "frame_chain": [],
                        "locator_kind": "role",
                        "locator_args": {"args": ["link"]},
                        "ordinal_op": None,
                    },
                }
            ],
        )

        compile(script, "replay_filter.py", "exec")
        self.assertIn(
            "pages['page'].get_by_role('link').filter(has_text='预设窗宽窗位').click()",
            script,
        )

    def test_generated_server_script_is_syntax_valid_and_serves_local_root(self):
        script = generate_serve_script()

        compile(script, "serve_fixture.py", "exec")
        self.assertIn("ReplicaServer", script)
        self.assertIn("replica_root", script)

    def test_generated_server_script_has_no_project_module_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serve_replica.py"
            path.write_text(generate_serve_script(), encoding="utf-8")
            spec = importlib.util.spec_from_file_location("standalone_replica_server", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

        self.assertTrue(hasattr(module, "ReplicaServer"))


if __name__ == "__main__":
    unittest.main()
