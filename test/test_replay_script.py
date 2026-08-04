import tempfile
import unittest
import importlib.util
from pathlib import Path
from urllib.request import urlopen

from replay_helpers import ReplicaServer
from rewrite_script import generate_replay_script, generate_serve_script


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
