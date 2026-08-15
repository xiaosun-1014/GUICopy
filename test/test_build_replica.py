import json
import re
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from build_replica import _redact_known_series_identities, _series_member_html, build_replica
from replay_helpers import ReplicaServer
from replay_helpers import sha256_file
from replay_helpers import series_key_slug
from replica_models import (
    ActionTarget,
    BootstrapPlan,
    CaptureTimingProfile,
    DomNodeSnapshot,
    InteractionRegion,
    LocatorRecipe,
    Rect,
    RegionMember,
    ReplicaDocument,
    ReplicaFlow,
    ReplicaPage,
    ReplicaState,
    ReplicaTransition,
    StateEvidence,
)


class SeriesMemberPrivacyTests(unittest.TestCase):
    def test_series_member_removes_identity_from_root_and_descendants(self):
        raw_key = "1.2.826.0.1.3680043.201.9001"
        snapshot = DomNodeSnapshot(
            "li",
            "Anonymous series",
            {"id": raw_key, "title": "private label"},
            Rect(0, 0, 100, 30, "page_viewport_css"),
            (
                f'<li id="{raw_key}" title="private label" data-series-uid="{raw_key}">'
                f'<span id="thumb-{raw_key}">{raw_key}</span></li>'
            ),
            {"display": "block"},
        )
        rendered = _series_member_html(snapshot, "public-slug", selected=False, disabled=False)
        self.assertNotIn(raw_key, rendered)
        self.assertNotIn("private label", rendered)
        self.assertIn('data-replica-series-key="public-slug"', rendered)

    def test_known_identity_is_redacted_from_non_series_markup(self):
        raw_key = "1.2.826.0.1.3680043.201.9002"
        rendered = _redact_known_series_identities(
            f'<span id="thumb-{raw_key}">{raw_key}</span>',
            {raw_key: {"slug": "public-slug"}},
        )
        self.assertNotIn(raw_key, rendered)
        self.assertEqual(rendered.count("public-slug"), 2)


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

    def test_fill_followed_by_enter_stays_editable_until_enter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            input_node = DomNodeSnapshot(
                "input", "", {"id": "wl", "value": "40"}, Rect(10, 20, 80, 30, "page_viewport_css"),
                '<input id="wl" value="40">', {"display": "block"},
            )
            locator = LocatorRecipe('page.locator("#wl")', "page", [], "css", {"args": ["#wl"]}, None, None)
            fill = ActionTarget("a_fill", "m_wl", "fill", "locator", {"args": ["0"]}, locator, input_node, None, None, None, "execute", None, "d_main", "t_fill")
            press = ActionTarget("a_enter", "m_wl", "press", "locator", {"args": ["Enter"]}, locator, input_node, None, None, None, "execute", None, "d_main", "t_enter")
            page_model = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
            evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")
            states = [
                ReplicaState("s_000", 0, "", "page", [page_model], [ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 200, "height": 100}, 1, "css", 0, 0, "assets/main.png", "main", 3, targets=[fill])], [ReplicaTransition("t_fill", "a_fill", "s_000", "s_001", "page", "page", "same_page")], evidence),
                ReplicaState("s_001", 1, "", "page", [page_model], [ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 200, "height": 100}, 1, "css", 0, 0, "assets/main.png", "main", 3, targets=[press])], [ReplicaTransition("t_enter", "a_enter", "s_001", "s_002", "page", "page", "same_page")], evidence),
                ReplicaState("s_002", 2, "", "page", [page_model], [ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 200, "height": 100}, 1, "css", 0, 0, "assets/main.png", "main", 3, targets=[press])], [], evidence),
            ]
            flow = ReplicaFlow(1, "editable", "recorded.py", "hash", "now", {"width": 200, "height": 100}, BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", states, [])
            output = root / "replica"
            build_replica(flow, root, output)

            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                field = page.locator("#wl")
                field.click()
                self.assertEqual(field.input_value(), "40")
                self.assertEqual(field.get_attribute("autocomplete"), "off")
                self.assertEqual(field.evaluate("element => getComputedStyle(element).opacity"), "1")
                field.fill("125")
                self.assertEqual(page.url, server.url)
                self.assertEqual(field.input_value(), "125")
                field.press("Enter")
                page.wait_for_url("**/states/s_002/index.html")
                self.assertIn("/states/s_002/index.html", page.url)
                self.assertEqual(page.locator("#wl").input_value(), "125")
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
            self.assertIn('class="replica-metadata"', rendered)
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

    def test_metadata_members_are_not_rendered_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            member_dom = DomNodeSnapshot(
                "div", "Patient Name", {"id": "patient-name"},
                Rect(50, 80, 120, 20, "page_viewport_css"),
                '<div id="patient-name">Patient Name</div>', {"display": "block"},
            )
            panel = DomNodeSnapshot(
                "div", "Patient Name", {"id": "tagsBox"},
                Rect(40, 50, 300, 120, "page_viewport_css"),
                '<div id="tagsBox"><div id="patient-name">Patient Name</div></div>',
                {"display": "block"},
            )
            region = InteractionRegion(
                "r_meta", "metadata", "d_main", panel,
                [RegionMember("member", "div", member_dom)], None,
            )
            document = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 3, regions=[region],
            )
            flow = ReplicaFlow(
                1, "meta", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [ReplicaState(
                    "s_000", 0, "", "page",
                    [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                    [document], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )], [],
            )

            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")

            self.assertEqual(rendered.count('id="patient-name"'), 1)

    def test_metadata_panel_keeps_side_controls_reachable_and_panel_once(self):
        # Regression: capture_marker_panel_region 命中面板后，面板外的兄弟交互控件
        # （WL/WW 输入、确认按钮、canvas 等）仍应作为普通成员渲染并可点击，
        # 离线回放才能继续；面板 root 已逐字包含的内容不得重复渲染。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            panel = DomNodeSnapshot(
                "div", "Patient Name", {"id": "tagsBox"},
                Rect(40, 50, 300, 120, "page_viewport_css"),
                '<div id="tagsBox"><div id="patient-name">Patient Name</div></div>',
                {"display": "block"},
            )
            confirm = DomNodeSnapshot(
                "button", "确定", {"id": "confirm"},
                Rect(50, 200, 60, 24, "page_viewport_css"),
                '<button id="confirm">确定</button>', {"display": "block"},
            )
            canvas = DomNodeSnapshot(
                "canvas", "", {"id": "overlaycanvas-0_0"},
                Rect(20, 60, 100, 80, "page_viewport_css"),
                '<canvas id="overlaycanvas-0_0" width="100" height="80"></canvas>', {"display": "block"},
            )
            region = InteractionRegion(
                "r_meta", "metadata", "d_main", panel,
                [
                    RegionMember("in_panel", "div", panel),
                    RegionMember("side_confirm", "button", confirm),
                    RegionMember("side_canvas", "canvas", canvas),
                ],
                None,
            )
            document = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 3, regions=[region],
            )
            flow = ReplicaFlow(
                1, "meta", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [ReplicaState(
                    "s_000", 0, "", "page",
                    [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                    [document], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )], [],
            )

            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")

            # 面板 root 只渲染一次（不因 in_panel member 重复）。
            self.assertEqual(rendered.count('id="tagsBox"'), 1)
            self.assertEqual(rendered.count('id="patient-name"'), 1)
            # 面板外兄弟控件照常渲染为 overlay。
            self.assertIn('id="confirm"', rendered)
            self.assertIn('id="overlaycanvas-0_0"', rendered)

    def test_metadata_close_action_is_embedded_in_original_dom_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            close = DomNodeSnapshot(
                "a", "", {"class": "close"}, Rect(770, 0, 30, 30, "page_viewport_css"),
                '<a class="close"><i class="icon icon-close"></i></a>', {"display": "block"},
            )
            panel = DomNodeSnapshot(
                "div", "Patient Metadata", {"id": "tagsBox", "class": "box-tags"},
                Rect(0, 0, 800, 600, "page_viewport_css"),
                '<div id="tagsBox" class="box-tags"><div>Patient Metadata</div>'
                '<a class="close"><i class="icon icon-close"></i></a></div>',
                {"display": "block"},
            )
            target = ActionTarget(
                "a_close", "m_meta", "click", "locator", {},
                LocatorRecipe('page.locator("#tagsBox a.close")', "page", [], "css", {"args": ["#tagsBox a.close"]}, None, None),
                close, None, None, None, "execute", None, "d_main", None,
            )
            document = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 3, targets=[target],
                regions=[InteractionRegion("r_meta", "metadata", "d_main", panel, [], None)],
            )
            flow = ReplicaFlow(
                1, "close", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [ReplicaState(
                    "s_000", 0, "", "page",
                    [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                    [document], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )], [],
            )

            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")

            self.assertEqual(rendered.count('data-replica-action="a_close"'), 1)
            self.assertEqual(rendered.count('id="tagsBox"'), 1)
            self.assertNotIn("data-replica-back", rendered)
            self.assertIn("data-replica-panel-close", rendered)

    def test_tags_icon_is_not_rendered_as_metadata_panel_or_close_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            icon = DomNodeSnapshot(
                "i", "", {"class": "icon icon-tags"},
                Rect(700, 50, 20, 21, "page_viewport_css"),
                '<i class="icon icon-tags"></i>', {"display": "inline"},
            )
            tags = DomNodeSnapshot(
                "a", "Tags", {"class": "tool tool-tags", "title": "Tags"},
                Rect(690, 40, 40, 40, "page_viewport_css"),
                '<a class="tool tool-tags" title="Tags">Tags</a>', {"display": "block"},
            )
            target = ActionTarget(
                "a_tags", "m_meta", "click", "locator", {},
                LocatorRecipe('page.get_by_title("Tags")', "page", [], "title", {"args": ["Tags"]}, None, None),
                tags, None, None, None, "execute", None, "d_main", None,
            )
            document = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 3, targets=[target],
                regions=[InteractionRegion("r_icon", "metadata", "d_main", icon, [], None)],
            )
            flow = ReplicaFlow(
                1, "icon", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [ReplicaState(
                    "s_000", 0, "", "page",
                    [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                    [document], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )], [],
            )

            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")

            self.assertNotIn("data-replica-panel-region", rendered)
            self.assertNotIn("data-replica-back", rendered)
            self.assertEqual(rendered.count('data-replica-action="a_tags"'), 1)
            self.assertIn('role="link"', rendered)
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                self.assertEqual(page.get_by_role("link").filter(has_text="Tags").count(), 1)
                browser.close()

    def test_replica_scales_to_fit_smaller_viewport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            document = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 3,
            )
            flow = ReplicaFlow(
                1, "responsive", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [ReplicaState(
                    "s_000", 0, "", "page",
                    [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                    [document], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )], [],
            )
            output = root / "replica"
            build_replica(flow, root, output)

            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 400, "height": 300})
                page.goto(server.url)
                box = page.locator(".replica").bounding_box()
                self.assertAlmostEqual(box["width"], 400, delta=1)
                self.assertAlmostEqual(box["height"], 300, delta=1)
                self.assertGreaterEqual(box["x"], -1)
                self.assertGreaterEqual(box["y"], -1)
                self.assertLessEqual(box["x"] + box["width"], 401)
                self.assertLessEqual(box["y"] + box["height"], 301)
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth"), 400)
                browser.close()

    def test_replica_scales_up_to_fill_larger_viewport_and_versions_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            document = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0,
                "assets/main.png", "main", 3,
            )
            flow = ReplicaFlow(
                1, "responsive-large", "recorded.py", "hash", "now", {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000",
                [ReplicaState(
                    "s_000", 0, "", "page",
                    [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                    [document], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )], [],
            )
            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")

            self.assertRegex(rendered, r'replica_runtime\.js\?v=[0-9a-f]{12}')
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1200, "height": 900})
                page.goto(server.url)
                box = page.locator(".replica").bounding_box()
                self.assertAlmostEqual(box["width"], 1200, delta=1)
                self.assertAlmostEqual(box["height"], 900, delta=1)
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth"), 1200)
                self.assertEqual(page.evaluate("document.documentElement.scrollHeight"), 900)
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


    def test_series_members_receive_route_key_and_route_map_is_injected(self):
        from test.test_replica_runtime import _build_series_flow, _write_assets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_assets(root)
            flow = _build_series_flow()
            output = root / "replica"
            build_replica(flow, root, output)

            # Every series-region member is route-keyed in every document that
            # carries the series list (hub and all viewer states).
            hub = (output / "index.html").read_text(encoding="utf-8")
            for key in ("A", "B", "C", "D"):
                self.assertIn(f'data-replica-series-key="{series_key_slug(key)}"', hub)
            self.assertIn('role="option"', hub)
            self.assertIn('aria-selected="true"', hub)  # hub marks A selected
            # Failed branch is flagged disabled in the markup.
            self.assertIn(f'data-replica-series-key="{series_key_slug("D")}"', hub)
            self.assertIn('aria-disabled="true"', hub)  # D carries disabled state
            self.assertIn("window.__REPLICA_SERIES_ROUTE__", hub)
            # The raw series_key (which may be a real SeriesInstanceUID) must
            # never be written verbatim into the served HTML.
            self.assertNotIn("data-replica-series-key=\"B\"", hub)
            self.assertNotIn('"A": {', hub)

            # A viewer state's series region also carries route keys so a user can
            # jump directly between series without returning to the hub.
            vb = (output / "states" / "s_vb" / "index.html").read_text(encoding="utf-8")
            for key in ("A", "B", "C"):
                self.assertIn(f'data-replica-series-key="{series_key_slug(key)}"', vb)
            # B is the active series in its own viewer state.
            self.assertIn(f'data-replica-series-key="{series_key_slug("B")}"', vb)
            self.assertIn('aria-disabled="true"', vb)

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
