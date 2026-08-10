import json
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from build_replica import build_replica
from replay_helpers import ReplicaServer
from replay_helpers import sha256_file
from replica_models import (
    ActionTarget,
    BootstrapPlan,
    CaptureTimingProfile,
    DomNodeSnapshot,
    InteractionRegion,
    LocatorRecipe,
    Rect,
    ReplicaDocument,
    ReplicaFlow,
    ReplicaPage,
    ReplicaState,
    StateEvidence,
)


class BuildReplicaTests(unittest.TestCase):
    def test_builder_creates_iframe_document_and_overlay_locator_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "main.png").write_bytes(b"png")
            (assets / "child.png").write_bytes(b"png")
            button = DomNodeSnapshot("button", "Open", {"id": "open", "data-testid": "open-viewer"}, Rect(10, 20, 80, 30, "page_viewport_css"), '<button id="open" data-testid="open-viewer">Open</button>', {"display": "block"})
            child = ReplicaDocument("d_child", "p_main", "page", "main", "d_main", "#viewer", "viewer", "viewer", {"width": 300, "height": 200}, 1, "css", 0, 0, "assets/child.png", "child", 3)
            main = ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 800, "height": 600}, 1, "css", 0, 0, "assets/main.png", "main", 3, targets=[ActionTarget("a_000_001", "m_000", "click", "locator", {}, LocatorRecipe('page.locator("#open")', "page", [], "css", {"args": ["#open"]}, None, None), button, None, None, None, "execute", None, "d_main", "t_a_000_001")])
            flow = ReplicaFlow(1, "fixture", "recorded.py", "hash", "now", {"width": 800, "height": 600}, BootstrapPlan(1, 1, True, {"page": "main"}), [], CaptureTimingProfile(), "s_000", [ReplicaState("s_000", 0, "https://example.test", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)], [main, child], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))], [])
            output = root / "replica"

            build_replica(flow, root, output)

            index = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn('src="documents/d_child/index.html"', index)
            self.assertIn('data-testid="open-viewer"', index)
            child_document = output / "documents" / "d_child" / "index.html"
            self.assertTrue(child_document.exists())
            self.assertIn(f'src="../../assets/by-hash/{sha256_file(assets / "child.png")}.png"', child_document.read_text(encoding="utf-8"))
            self.assertEqual(len(list((output / "assets" / "by-hash").glob("*.png"))), 1)
            locator_mapping = json.loads((output / "locator_mapping.json").read_text(encoding="utf-8"))
            self.assertEqual(locator_mapping["a_000_001"]["locator_risk"], "stable_id")
            self.assertEqual(locator_mapping["a_000_001"]["marker_id"], "m_000")
            report = json.loads((output / "replica_build_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], 1)
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                self.assertEqual(page.locator("#open").count(), 1)
                self.assertTrue(page.locator("#open").is_visible())
                page.goto(server.url + "?debug=1")
                self.assertTrue(page.locator("html").evaluate("element => element.classList.contains('replica-debug')"))
                self.assertEqual(page.frame_locator("#viewer").locator("body").count(), 1)
                browser.close()

    def test_non_entry_page_renders_close_back_button_without_covering_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"; assets.mkdir()
            (assets / "main.png").write_bytes(b"png")
            doc0 = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0, "assets/main.png", "main", 3,
            )
            doc1 = ReplicaDocument(
                "d_meta", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0, "assets/main.png", "meta", 3,
            )
            flow = ReplicaFlow(
                1, "meta", "recorded.py", "hash", "now",
                {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [],
                CaptureTimingProfile(), "s_000",
                [
                    ReplicaState(
                        "s_000", 0, "", "page",
                        [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                        [doc0], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                    ),
                    ReplicaState(
                        "s_001", 1, "", "page",
                        [ReplicaPage("p_main", "page", "main", None, None, "d_meta", True, False)],
                        [doc1], [], StateEvidence(True, False, False, False, 0, 0, 0, 0, "nav"),
                    ),
                ],
                [],
            )
            output = root / "replica"

            build_replica(flow, root, output)

            entry_html = (output / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("data-replica-back", entry_html)
            s1 = (output / "states" / "s_001" / "index.html").read_text(encoding="utf-8")
            self.assertIn("data-replica-back=", s1)
            self.assertIn("关闭", s1)
            self.assertRegex(s1, r'data-replica-back="[^"]*index\.html"')
            self.assertNotIn("data-replica-panel", s1)
            self.assertIn("replica-bg", s1)

    def test_same_document_id_uses_state_specific_screenshot_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "first.png").write_bytes(b"first")
            (root / "assets" / "second.png").write_bytes(b"second")
            first = ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 1, "height": 1}, 1, "css", 0, 0, "assets/first.png", "first", 5)
            second = ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 1, "height": 1}, 1, "css", 0, 0, "assets/second.png", "second", 6)
            page = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
            evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry")
            flow = ReplicaFlow(1, "assets", "recorded.py", "hash", "now", {"width": 1, "height": 1}, BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", [ReplicaState("s_000", 0, "", "page", [page], [first], [], evidence), ReplicaState("s_001", 1, "", "page", [page], [second], [], evidence)], [])

            output = root / "replica"
            build_replica(flow, root, output)

            self.assertIn(f"assets/by-hash/{sha256_file(root / 'assets' / 'first.png')}.png", (output / "index.html").read_text(encoding="utf-8"))
            self.assertIn(f"assets/by-hash/{sha256_file(root / 'assets' / 'second.png')}.png", (output / "states" / "s_001" / "index.html").read_text(encoding="utf-8"))


    def test_builder_fails_when_required_screenshot_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            # NOTE: only child.png exists; main.png is deliberately absent.
            (assets / "child.png").write_bytes(b"png")
            main = ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 1, "height": 1}, 1, "css", 0, 0, "assets/main.png", "main", 3)
            child = ReplicaDocument("d_child", "p_main", "page", "main", "d_main", "#viewer", "viewer", "viewer", {"width": 1, "height": 1}, 1, "css", 0, 0, "assets/child.png", "child", 3)
            evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry")
            flow = ReplicaFlow(1, "missing", "recorded.py", "hash", "now", {"width": 1, "height": 1}, BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", [ReplicaState("s_000", 0, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)], [main, child], [], evidence)], [])
            output = root / "replica"

            with self.assertRaisesRegex(FileNotFoundError, "screenshot"):
                build_replica(flow, root, output)


if __name__ == "__main__":
    unittest.main()
