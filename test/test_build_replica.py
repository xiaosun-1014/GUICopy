import json
import re
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


def _metadata_region(document_id: str) -> InteractionRegion:
    panel = DomNodeSnapshot(
        "div",
        "Patient Metadata",
        {"id": "tagsBox"},
        Rect(20, 20, 240, 120, "page_viewport_css"),
        '<div id="tagsBox">Patient Metadata</div>',
        {"display": "block"},
    )
    return InteractionRegion(f"{document_id}_metadata", "metadata", document_id, panel, [], None)


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

    def test_metadata_region_renders_complete_scrollable_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            panel = DomNodeSnapshot(
                "div",
                "Patient Information Final Metadata Row",
                {"id": "tagsBox"},
                Rect(40, 50, 300, 120, "page_viewport_css"),
                '<div id="tagsBox"><div style="height:300px">Final Metadata Row</div></div>',
                {"display": "block"},
            )
            document = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 3,
                regions=[InteractionRegion("r_meta", "metadata", "d_main", panel, [], None)],
            )
            flow = ReplicaFlow(
                1, "meta", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [ReplicaState(
                    "s_000", 0, "", "page",
                    [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                    [document], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )],
                [],
            )

            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")

            self.assertIn('data-replica-panel-region="r_meta"', rendered)
            self.assertIn("Final Metadata Row", rendered)
            self.assertIn("overflow-y:auto", rendered)
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                scroll_state = page.locator('[data-replica-panel-region="r_meta"]').evaluate(
                    "element => ({clientHeight: element.clientHeight, scrollHeight: element.scrollHeight})"
                )
                self.assertGreater(scroll_state["scrollHeight"], scroll_state["clientHeight"])
                browser.close()

    def test_non_entry_metadata_page_renders_panel_and_close_back_button(self):
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
                regions=[_metadata_region("d_meta")],
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
            # back must point exactly at the previous state's entry page (s_000 ->
            # output root index.html), and that target file must actually exist.
            back_target = re.search(r'data-replica-back="([^"]+)"', s1)
            self.assertIsNotNone(back_target, "back button must carry a data-replica-back target")
            self.assertEqual(back_target.group(1), "../../index.html")
            self.assertTrue((output / "states" / "s_001" / "../../index.html").resolve().is_file())
            self.assertIn("data-replica-panel-region", s1)
            self.assertIn("overflow-y:auto", s1)
            self.assertIn("replica-bg", s1)

    def test_back_button_click_navigates_to_previous_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            doc0 = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0, "assets/main.png", "main", 3,
            )
            doc1 = ReplicaDocument(
                "d_meta", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0, "assets/main.png", "meta", 3,
                regions=[_metadata_region("d_meta")],
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
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                state_url = server.url.replace("/index.html", "/states/s_001/index.html")
                page.goto(state_url)
                self.assertEqual(page.locator('[data-replica-back]').count(), 1)
                with page.expect_navigation() as nav:
                    page.click('[data-replica-back]')
                self.assertTrue(nav.value.url == server.url, f"expected back to entry page, got {nav.value.url}")
                browser.close()

    def test_non_metadata_state_has_no_close_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            page_model = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
            evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")
            states = []
            for ordinal in range(2):
                document = ReplicaDocument(
                    "d_main", "p_main", "page", "main", None, None, None, None,
                    {"width": 800, "height": 600}, 1, "css", 0, 0,
                    "assets/main.png", f"state-{ordinal}", 3,
                )
                states.append(ReplicaState(f"s_{ordinal:03d}", ordinal, "", "page", [page_model], [document], [], evidence))
            flow = ReplicaFlow(
                1, "plain", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", states, [],
            )

            output = root / "replica"
            build_replica(flow, root, output)

            rendered = (output / "states" / "s_001" / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("data-replica-back", rendered)

    def test_metadata_popup_places_close_on_active_popup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"main")
            (root / "assets" / "popup.png").write_bytes(b"popup")
            state0_document = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 4,
            )
            state1_main = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 4,
            )
            state1_popup = ReplicaDocument(
                "d_popup", "p_popup", "page1", "popup", None, None, None, None,
                {"width": 640, "height": 480}, 1, "css", 0, 0,
                "assets/popup.png", "popup", 5,
                regions=[_metadata_region("d_popup")],
            )
            main_page = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
            popup_page = ReplicaPage("p_popup", "page1", "popup", "p_main", "viewer", "d_popup", True, False)
            evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")
            flow = ReplicaFlow(
                1, "popup-meta", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [
                    ReplicaState("s_000", 0, "", "page", [main_page], [state0_document], [], evidence),
                    ReplicaState("s_001", 1, "", "page1", [main_page, popup_page], [state1_main, state1_popup], [], evidence),
                ],
                [],
            )

            output = root / "replica"
            build_replica(flow, root, output)
            state_root = output / "states" / "s_001"
            main_html = (state_root / "index.html").read_text(encoding="utf-8")
            popup_html = (state_root / "pages" / "p_popup" / "index.html").read_text(encoding="utf-8")

            self.assertNotIn("data-replica-back", main_html)
            self.assertIn("data-replica-back", popup_html)
            self.assertIn("index.html", popup_html)

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
