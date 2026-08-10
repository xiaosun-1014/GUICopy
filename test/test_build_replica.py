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

    def test_metadata_region_renders_scrollable_panel_with_full_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "main.png").write_bytes(b"png")
            panel_html = (
                '<div id="tagsBox" class="box-tags"><div class="panel">'
                '<div class="hd">Patient Information</div><div class="bd">'
                '<div class="item">Patient Name(x00100010): <span>Tang Yuan Hua</span></div>'
                '<div class="item">Patient ID(x00100020): <span>0003699549</span></div>'
                '</div></div><div class="panel"><div class="hd">Study Information</div>'
                '<div class="bd"><div class="item">Study Date(x00080020): <span>20260723</span></div>'
                '</div></div></div>'
            )
            root_dom = DomNodeSnapshot("div", "", {}, Rect(100, 50, 300, 400, "page_viewport_css"), panel_html, {})
            metadata_region = InteractionRegion(
                "d_main_metadata", "metadata", "d_main", root_dom, [], None, full_html=panel_html
            )
            main = ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 800, "height": 600}, 1, "css", 0, 0, "assets/main.png", "main", 3, regions=[metadata_region])
            flow = ReplicaFlow(1, "meta", "recorded.py", "hash", "now", {"width": 800, "height": 600}, BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", [ReplicaState("s_000", 0, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)], [main], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))], [])
            output = root / "replica"

            build_replica(flow, root, output)

            index = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-replica-panel=""', index)
            self.assertIn("overflow-y:auto", index)
            self.assertIn("Patient Name(x00100010)", index)
            self.assertIn("Study Date(x00080020)", index)
            self.assertIn('left:100px', index)
            self.assertIn('max-height:400px', index)

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
