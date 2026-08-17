import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from build_replica import (
    _promote_series_regions_to_earliest_documents,
    _propagate_layout_variants_across_documents,
    _redact_known_series_identities,
    _reroute_branch_series_regions_to_viewer_documents,
    _series_member_html,
    build_replica,
)
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
    SeriesBranch,
    StateEvidence,
)


class SeriesMemberPrivacyTests(unittest.TestCase):
    def test_series_member_removes_identity_from_root_and_descendants(self):
        raw_key = "1.2.826.0.1.3680043.201.9001"
        snapshot = DomNodeSnapshot(
            "li",
            "Anonymous series",
            {"id": raw_key, "title": "private label"},
            Rect(11, 22, 100, 30, "page_viewport_css"),
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
        self.assertIn(
            'style="position:absolute;left:11px;top:22px;width:100px;height:30px;"',
            rendered,
        )

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

    def test_series_list_scroll_overflow_marks_below_fold_rows(self):
        # A series list whose rows extend below the captured fold must get a
        # scrollable overlay (content height = list extent) and the below-fold
        # rows must render their own content (opacity:1) instead of staying
        # transparent hit-targets with no screenshot underneath.
        from test.test_replica_runtime import _build_series_flow

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "hub.png").write_bytes(b"png")
            members = []
            for index in range(10):
                y = index * 24  # 0..216; index 9 lands at 216 >= viewport 200
                dom = DomNodeSnapshot(
                    "div", f"Series {index}", {"id": f"m{index}", "role": "option", "aria-selected": "false"},
                    Rect(0, y, 300, 20, "region_content_css"),
                    f'<div id="m{index}" role="option">Series {index}</div>', {},
                )
                members.append(RegionMember(f"m{index}", "div", dom))
            hub_root = DomNodeSnapshot(
                "div", "", {"id": "series", "role": "listbox"},
                Rect(0, 0, 300, 200, "page_viewport_css"),
                '<div id="series" role="listbox"></div>', {},
            )
            hub = ReplicaDocument(
                "d_hub", "p_main", "page", "main", None, None, None, None,
                {"width": 300, "height": 200}, 1, "css", 0, 0, "assets/hub.png", "hub", 3,
                regions=[InteractionRegion("d_hub_series", "series", "d_hub", hub_root, members, None)],
            )
            page = ReplicaPage("p_main", "page", "main", None, None, "d_hub", True, False)
            evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry")
            branch = SeriesBranch(
                "branch_9", "series9", "Series 9", 9, "d_hub", "m9", None, "click",
                None, None, None, "failed", "no_viewer_snapshot",
            )
            flow = ReplicaFlow(
                1, "tall", "recorded.py", "hash", "now", {"width": 300, "height": 200},
                BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(),
                "s_hub", [ReplicaState("s_hub", 0, "", "page", [page], [hub], [], evidence)],
                [],
                series_branches=[branch],
            )
            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")

            # Overlay becomes an independent scroll container sized to the list extent.
            self.assertIn(
                '<section class="overlay" style="overflow-y:auto;max-height:200px;'
                'height:236px;overscroll-behavior:contain;scrollbar-gutter:stable">',
                rendered,
            )
            # The entirely-below-fold row carries its own visible content flag.
            self.assertIn(f'data-replica-series-key="{series_key_slug("series9")}"', rendered)
            self.assertIn('data-replica-below-fold=""', rendered)
            self.assertIn(
                ".overlay>[data-replica-series-key][data-replica-below-fold]{opacity:1}",
                rendered,
            )
            # Boundary: a list that fits inside the fold keeps the plain overlay.
            guard_root = Path(tmp) / "guard"
            guard_root.mkdir()
            (guard_root / "assets").mkdir()
            from test.test_replica_runtime import _write_assets as _write_guard_assets
            _write_guard_assets(guard_root)
            flow_guard = _build_series_flow()
            guard_out = guard_root / "replica"
            build_replica(flow_guard, guard_root, guard_out)
            guard_html = (guard_out / "index.html").read_text(encoding="utf-8")
            self.assertIn('<section class="overlay">', guard_html)
            self.assertNotIn('overflow-y:auto;max-height:200px', guard_html)

    def test_meta_two_step_augmentation_synthesizes_tags_menu_state(self):
        from test.test_replica_runtime import _augment_meta_flow, _build_series_flow, _write_assets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_assets(root)
            flow = _augment_meta_flow()
            output = root / "replica"
            build_replica(flow, root, output)

            # Branch viewer: 更多 no longer jumps straight to branch metadata; it
            # goes through the synthesized Tags-menu middle state.
            vb = (output / "states" / "s_vx" / "index.html").read_text(encoding="utf-8")
            self.assertIn("btags_bx/index.html", vb)
            # The middle state shows the same series viewer plus a visible Tags row.
            btags = (output / "states" / "btags_bx" / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-replica-action="series:bx:tags"', btags)
            self.assertIn("data-replica-visible", btags)
            self.assertIn("s_mx/index.html", btags)
            self.assertIn(".overlay>[data-replica-visible]", btags)

            # Boundary: a plain multi-series flow (no synthetic series:*:meta_open
            # collapse, no unrendered recorded Tags step) must NOT gain btags states.
            guard = Path(tmp) / "guard"
            guard.mkdir()
            (guard / "assets").mkdir()
            _write_assets(guard)
            flow2 = _build_series_flow()
            out2 = guard / "replica"
            build_replica(flow2, guard, out2)
            self.assertFalse((out2 / "states" / "btags_branch_b").is_dir())
            vb2 = (out2 / "states" / "s_vb" / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("btags_", vb2)

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


def _series_member(member_id: str, text: str, y: float) -> RegionMember:
    return RegionMember(
        member_id,
        "series",
        DomNodeSnapshot(
            "li",
            text,
            {"id": f"uid-{member_id}"},
            Rect(30, y, 400, 24, "page_viewport_css"),
            f'<li id="uid-{member_id}" data-series-uid="{member_id}">{text}</li>',
            {"display": "block"},
        ),
    )


class SeriesPromotionTests(unittest.TestCase):
    """zs-style late series click: entry viewer state must still expose the
    captured series list after build (mirrors FT, where the entry viewer already
    carries it). See build_replica._promote_series_regions_to_earliest_documents.
    """

    def _build_flow_late_series(self):
        root_series = DomNodeSnapshot(
            "div",
            "",
            {"id": "HLeftThumnail"},
            Rect(20, 30, 420, 200, "page_viewport_css"),
            '<div id="HLeftThumnail"></div>',
            {"display": "block"},
        )
        series = InteractionRegion(
            "r_series",
            "series",
            "d_main",
            root_series,
            [
                _series_member("d_p_000_root_series_000", "Series 1", 40),
                _series_member("d_p_000_root_series_001", "Series 2", 70),
            ],
            None,
        )
        page_model = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
        evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")
        document_late = ReplicaDocument(
            "d_main", "p_main", "page", "main", None, None, None, None,
            {"width": 640, "height": 400}, 1, "css", 0, 0,
            "assets/main.png", "main", 3,
            regions=[series],
        )
        document_early = ReplicaDocument(
            "d_main", "p_main", "page", "main", None, None, None, None,
            {"width": 640, "height": 400}, 1, "css", 0, 0,
            "assets/main.png", "main", 3,
        )
        branch_state = ReplicaDocument(
            "d_main", "p_main", "page", "main", None, None, None, None,
            {"width": 640, "height": 400}, 1, "css", 0, 0,
            "assets/main.png", "main", 3,
        )
        states = [
            ReplicaState("s_000", 0, "", "page", [page_model], [document_early], [], evidence),
            ReplicaState("s_001", 1, "", "page", [page_model], [document_late], [], evidence),
            ReplicaState("bviewer_b000", 2, "", "page", [page_model], [branch_state], [], evidence),
            ReplicaState("bviewer_b001", 3, "", "page", [page_model], [branch_state], [], evidence),
        ]
        branches = [
            SeriesBranch(
                "b000", "series-key-000", "Series 1", 0, "d_main",
                "d_p_000_root_series_000", None, "dblclick",
                "bviewer_b000", None, None, "captured", None,
            ),
            SeriesBranch(
                "b001", "series-key-001", "Series 2", 1, "d_main",
                "d_p_000_root_series_001", None, "dblclick",
                "bviewer_b001", None, None, "captured", None,
            ),
        ]
        flow = ReplicaFlow(
            1, "zs-late", "recorded.py", "hash", "now", {"width": 640, "height": 400},
            BootstrapPlan(1, 1, True, {}),
            [],  # popup_expectations
            CaptureTimingProfile(),
            "s_000",
            states,
            [],  # warnings
            branches,  # series_branches
        )
        return flow

    def test_late_series_region_is_promoted_to_entry_viewer(self):
        flow = self._build_flow_late_series()
        promoted = _promote_series_regions_to_earliest_documents(flow)
        self.assertEqual(promoted, 1)
        entry_doc = next(
            document for document in flow.states[0].documents
            if document.document_id == "d_main"
        )
        self.assertEqual(
            len([r for r in entry_doc.regions if r.region_type == "series"]), 1
        )
        # The later state keeps its own (un-mutated) region.
        late_doc = next(
            document for document in flow.states[1].documents
            if document.document_id == "d_main"
        )
        self.assertEqual(len(late_doc.regions), 1)

    def test_promotion_idempotent(self):
        flow = self._build_flow_late_series()
        self.assertEqual(_promote_series_regions_to_earliest_documents(flow), 1)
        self.assertEqual(_promote_series_regions_to_earliest_documents(flow), 0)

    def test_entry_already_with_series_is_never_re_promoted(self):
        flow = self._build_flow_late_series()
        # Give the entry state its own series region: nothing to promote.
        entry_doc = flow.states[0].documents[0]
        entry_doc.regions.append(copy.deepcopy(flow.states[1].documents[0].regions[0]))
        self.assertEqual(_promote_series_regions_to_earliest_documents(flow), 0)

    def test_build_renders_series_keys_in_entry_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            flow = self._build_flow_late_series()
            output = root / "replica"
            build_replica(flow, root, output)
            entry_html = (output / "index.html").read_text(encoding="utf-8")
            keys = re.findall(r'data-replica-series-key="([^"]+)"', entry_html)
            # Served HTML only ever carries the public slug (never the raw
            # series_key, which may be a real UID).
            self.assertEqual(
                sorted(keys),
                sorted([series_key_slug("series-key-000"), series_key_slug("series-key-001")]),
            )
            self.assertIn("__REPLICA_SERIES_ROUTE__", entry_html)


class SeriesRerouteTests(unittest.TestCase):
    """Popup-style viewer (zscloud Dapeng): branch series regions captured onto
    the *outer* main-page document ``__d_p_000_root`` must be re-homed onto the
    branch's own viewer document ``__d_p_001_f_001`` so the branch viewer the
    user actually reaches is clickable. See
    build_replica._reroute_branch_series_regions_to_viewer_documents.
    """

    def _build_flow_popup_viewer(self):
        def snap(elm_id, tag, x, y, w, h, text):
            return DomNodeSnapshot(
                tag, text, {"id": elm_id},
                Rect(x, y, w, h, "page_viewport_css"),
                f'<{tag} id="{elm_id}">{text}</{tag}>',
                {"display": "block"},
            )

        def doc(document_id, page_id, page_var, parent=None):
            return ReplicaDocument(
                document_id, page_id, page_var, "popup" if page_var == "page1" else "main",
                parent, "iframe" if parent else None, None, None,
                {"width": 640, "height": 400}, 1, "css", 0, 0,
                "assets/main.png", "main", 3,
            )

        page_model = ReplicaPage("p_main", "page", "main", None, None, "d_p_000_root", True, False)
        popup_model = ReplicaPage("p_001", "page1", "popup", "main", "replica-popup", "d_p_001_root", True, False)
        evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")

        # 主路径：viewer document d_p_001_f_001 正确带 series region（基准）。
        main_series = InteractionRegion(
            "d_p_001_f_001_series", "series", "d_p_001_f_001",
            snap("mainbody", "body", 0, 0, 640, 400, "Liang,Jie"),
            [_series_member("b000_series_li", "Series A", 40)],
            None,
        )
        main_viewer_doc = doc("d_p_001_f_001", "p_001", "page1", parent="d_p_001_root")
        main_viewer_doc.regions.append(main_series)
        main_state = ReplicaState(
            "s_001", 0, "", "page", [page_model, popup_model],
            [doc("d_p_000_root", "p_main", "page"), doc("d_p_001_root", "p_001", "page1"), main_viewer_doc],
            [], evidence,
        )

        branch_states = []
        branches = []
        for index, (viewer_tail, branch_id, key, label, member_id) in enumerate([
            ("b000_a098f660dfb9", "b000", "series-key-000", "Series A", "b000_series_li"),
            ("b001_b7a5ac6223f5", "b001", "series-key-001", "Series B", "b001_series_li"),
        ]):
            outer_doc = doc(f"{viewer_tail}__d_p_000_root", "p_main", "page")
            # 错位：series region 挂在外层主 document（模拟 _capture_viewer_topology 挂 docs_out[0]）。
            outer_doc.regions.append(InteractionRegion(
                f"{viewer_tail}__series", "series", f"{viewer_tail}__d_p_000_root",
                snap("HLeftThumnail", "div", 20, 30, 420, 200, "Scout\nMPR\n101\n1幅"),
                [copy.deepcopy(_series_member(f"{branch_id}_series_li", label, 40))],
                None,
            ))
            viewer_doc = doc(f"{viewer_tail}__d_p_001_f_001", "p_001", "page1", parent=f"{viewer_tail}__d_p_001_root")
            branch_page = ReplicaPage("p_main", "page", "main", None, None, f"{viewer_tail}__d_p_000_root", True, False)
            branch_popup = ReplicaPage("p_001", "page1", "popup", "main", "replica-popup", f"{viewer_tail}__d_p_001_root", True, False)
            branch_state = ReplicaState(
                f"bviewer_{branch_id}", index + 1, "", "page", [branch_page, branch_popup],
                [
                    outer_doc,
                    doc(f"{viewer_tail}__d_p_001_root", "p_001", "page1"),
                    viewer_doc,
                ],
                [], evidence,
            )
            branch_states.append(branch_state)
            branches.append(SeriesBranch(
                branch_id, key, label, index, "d_p_001_f_001", member_id,
                None, "dblclick", f"bviewer_{branch_id}", None, None, "captured", None,
            ))

        flow = ReplicaFlow(
            1, "zs-popup", "recorded.py", "hash", "now", {"width": 640, "height": 400},
            BootstrapPlan(1, 1, True, {}), [],
            CaptureTimingProfile(), "s_000", [main_state] + branch_states,
            [], branches,
        )
        return flow

    def _branch_docs(self, flow, branch_id):
        state = next(st for st in flow.states if st.state_id == f"bviewer_{branch_id}")
        outer = next(d for d in state.documents if d.document_id.endswith("__d_p_000_root"))
        viewer = next(d for d in state.documents if d.document_id.endswith("__d_p_001_f_001"))
        return state, outer, viewer

    def test_branch_series_region_is_rerouted_to_viewer_document(self):
        flow = self._build_flow_popup_viewer()
        self.assertEqual(_reroute_branch_series_regions_to_viewer_documents(flow), 2)
        for branch_id in ("b000", "b001"):
            _, outer, viewer = self._branch_docs(flow, branch_id)
            self.assertEqual(len([r for r in viewer.regions if r.region_type == "series"]), 1)
            self.assertEqual(len([r for r in outer.regions if r.region_type == "series"]), 0)
            # 归属 document_id 一并更新，保持 region 与宿主 document 一致。
            self.assertEqual(viewer.regions[0].document_id, viewer.document_id)

    def test_reroute_idempotent(self):
        flow = self._build_flow_popup_viewer()
        self.assertEqual(_reroute_branch_series_regions_to_viewer_documents(flow), 2)
        self.assertEqual(_reroute_branch_series_regions_to_viewer_documents(flow), 0)

    def test_reroute_skips_when_region_already_on_viewer_document(self):
        flow = self._build_flow_popup_viewer()
        # 先给分支 viewer document 一个正确归属的 series region：不应再被搬走或重复。
        # （直接把错位的 region 手工放对位置，模拟已在正确 document）
        for branch_id in ("b000", "b001"):
            _, outer, viewer = self._branch_docs(flow, branch_id)
            region = outer.regions[0]
            outer.regions.remove(region)
            region.document_id = viewer.document_id
            viewer.regions.append(region)
        self.assertEqual(_reroute_branch_series_regions_to_viewer_documents(flow), 0)

    def test_build_renders_branch_series_keys_in_viewer_document_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "main.png").write_bytes(b"png")
            flow = self._build_flow_popup_viewer()
            output = root / "replica"
            build_replica(flow, root, output)
            # 分支 viewer iframe document：可点序列热区（只暴露 public slug）。
            viewer_html = (
                output / "states" / "bviewer_b000" / "documents" / "b000_a098f660dfb9__d_p_001_f_001" / "index.html"
            ).read_text(encoding="utf-8")
            keys = re.findall(r'data-replica-series-key="([^"]+)"', viewer_html)
            self.assertEqual(keys, [series_key_slug("series-key-000")])
            # 外层主 document（分享页背景，无人在此交互）不应再渲染序列热区。
            outer_html = (output / "states" / "bviewer_b000" / "index.html").read_text(encoding="utf-8")
            self.assertEqual(re.findall(r'data-replica-series-key="[^"]+"', outer_html), [])


def _layout_member(member_id: str, text: str, tag: str = "li", y: float = 40.0, elm_id: str = "", x: float = 360.0) -> RegionMember:
    """Build a layout region member; elm_id defaults to the member_id."""
    resolved_id = elm_id or "layout-{}".format(member_id)
    body = text if text else '<i class="icon"></i>'
    outer = "<{tag} id=\"{rid}\">{body}</{tag}>".format(tag=tag, rid=resolved_id, body=body)
    dom = DomNodeSnapshot(
        tag, text, {"id": resolved_id},
        Rect(x, y, 100, 28, "page_viewport_css"),
        outer,
        {"display": "block"},
    )
    return RegionMember(member_id, "layout", dom)


def _layout_region(document_id: str, members: list[RegionMember]) -> InteractionRegion:
    root = DomNodeSnapshot(
        "div", "", {"id": "layoutMenu"},
        Rect(340, 20, 140, 160, "page_viewport_css"),
        '<div id="layoutMenu"></div>', {"display": "block"},
    )
    return InteractionRegion(
        f"{document_id}_layout", "layout", document_id, root, members, None,
    )


class LayoutVariantTests(unittest.TestCase):
    """步骤4/5：布局 region 全部成员三态化 + __REPLICA_LAYOUTS__ 注入 + series
    热区裁剪 + z-index 语义化（方案 A「背景层替换」，布局与序列解耦）。
    """

    def _flow(self, layout_variants="default", layout_members=None, series_members=None, series_branch=False):
        """``layout_variants``: "default" fills the fixture pair, else passes as-is
        (None / {} prune layout support entirely — legacy-run shape).

        ``series_branch``: attach a real SeriesBranch so the series row routes to
        a public ``data-replica-series-key`` (mirrors the hub->viewer wiring).
        """
        page_model = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
        evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")
        layout_region = _layout_region(
            "d_main",
            layout_members if layout_members is not None else [
                _layout_member("m11", "1*1", elm_id="layout_1_1", y=40.0),
                _layout_member("m22", "2*2", elm_id="layout_2_2", y=70.0),
                _layout_member("micon", "", elm_id="ic-layout", y=100.0),
            ],
        )
        regions = []
        if series_members is not None:
            regions.append(
                InteractionRegion(
                    "d_main_series", "series", "d_main",
                    DomNodeSnapshot(
                        "div", "", {"id": "HLeftThumnail", "role": "listbox"},
                        Rect(20, 30, 420, 200, "page_viewport_css"),
                        '<div id="HLeftThumnail" role="listbox"></div>', {"display": "block"},
                    ),
                    series_members, None,
                )
            )
        regions = regions + [layout_region]
        variants = (
            {"1*1": "assets/lay11.png", "2*2": "assets/lay22.png"}
            if layout_variants == "default" else layout_variants
        )
        doc = ReplicaDocument(
            "d_main", "p_main", "page", "main", None, None, None, None,
            {"width": 640, "height": 400}, 1, "css", 0, 0,
            "assets/main.png", "main", 3,
            regions=regions,
            layout_variants=variants,
            default_layout="2*2" if variants else "",
        )
        states = [
            ReplicaState(
                "s_000", 0, "", "page", [page_model], [doc], [],
                StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
            )
        ]
        branches = []
        if series_branch and series_members:
            first_member = series_members[0]
            branches.append(SeriesBranch(
                "branch_000", "series-key-000", "Series 1", 0, "d_main",
                first_member.member_id, None, "click",
                None, None, None, "captured", None,
            ))
        return ReplicaFlow(
            1, "layout", "recorded.py", "hash", "now", {"width": 640, "height": 400},
            BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", states, [],
            branches,
        )

    def test_layout_variants_injected_and_layout_members_clickable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "main.png").write_bytes(b"main-bytes")
            (assets / "lay11.png").write_bytes(b"layout-1x1-bytes")
            (assets / "lay22.png").write_bytes(b"layout-2x2-bytes")
            series_members = [
                _series_member("d_p_000_root_series_000", "Series 1", 40),
            ]
            flow = self._flow(series_members=series_members, series_branch=True)
            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")

            # __REPLICA_LAYOUTS__ 注入（normalized 键 → by-hash 相对 URL）
            self.assertIn("window.__REPLICA_LAYOUTS__", rendered)
            self.assertIn('"1*1"', rendered)
            self.assertIn('"2*2"', rendered)
            # 布局变体资产按 by-hash 复制并引用（内容哈希出现在 URL 中）
            lay11_path = output / "assets" / "by-hash" / f"{sha256_file(assets / 'lay11.png')}.png"
            lay22_path = output / "assets" / "by-hash" / f"{sha256_file(assets / 'lay22.png')}.png"
            self.assertTrue(lay11_path.exists())
            self.assertTrue(lay22_path.exists())
            self.assertIn(f"assets/by-hash/{lay11_path.name}", rendered)
            self.assertIn(f"assets/by-hash/{lay22_path.name}", rendered)

            # 三态：
            # 1) 可点：layout_1_1 → data-replica-layout="1*1"；2*2 → "2*2"
            self.assertIn('data-replica-layout="1*1"', rendered)
            self.assertIn('data-replica-layout="2*2"', rendered)
            # 2) 纯装饰：ic-layout 无 variant，无 data-replica-layout
            icon_li = re.search(r'<li[^>]*id="ic-layout"[^>]*>.*?</li>', rendered)
            self.assertIsNotNone(icon_li)
            self.assertNotIn("data-replica-layout", icon_li.group(0))
            self.assertIn('data-replica-overlay=""', icon_li.group(0))
            # 3) series 热区与布局选项共存
            self.assertIn(f'data-replica-series-key="{series_key_slug("series-key-000")}"', rendered)
            self.assertIn('data-replica-layout', rendered)

    def test_layout_variants_absent_legacy_run_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "main.png").write_bytes(b"main-bytes")
            series_members = [_series_member("d_p_000_root_series_000", "Series 1", 40)]
            layout_members = [_layout_member("m11", "1*1", elm_id="layout_1_1", y=40.0)]
            flow = self._flow(
                layout_variants=None,
                layout_members=layout_members,
                series_members=series_members,
                series_branch=True,
            )
            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("__REPLICA_LAYOUTS__", rendered)
            # 无布局背景：layout 成员降级为纯装饰（无 data-replica-layout= 属性；
            # CSS 选择器里出现的 [data-replica-layout] 是规则名，不算成员属性）
            self.assertNotIn('data-replica-layout="', rendered)
            # series 热区保持可用（老 run 行为不变）
            self.assertIn(f'data-replica-series-key="{series_key_slug("series-key-000")}"', rendered)

    def test_layout_id_inference_handles_zscloud_spellings(self):
        from build_replica import _infer_layout_id
        # 常规 a*b / axb
        self.assertEqual(
            _infer_layout_id(DomNodeSnapshot("div", "2*2", {"id": "lg2"}, Rect(0, 0, 1, 1, "c"), "<div></div>", {})),
            "2*2",
        )
        self.assertEqual(
            _infer_layout_id(DomNodeSnapshot("div", "2x2", {"id": "lg2"}, Rect(0, 0, 1, 1, "c"), "<div></div>", {})),
            "2*2",
        )
        # zscloud：``*1 Shift+1`` 文本 → 1*1
        self.assertEqual(
            _infer_layout_id(DomNodeSnapshot("div", "*1 Shift+1", {"id": "lg_1"}, Rect(0, 0, 1, 1, "c"), "<div></div>", {})),
            "1*1",
        )
        # ``layout_1_1`` id → 1*1
        self.assertEqual(
            _infer_layout_id(DomNodeSnapshot("div", "", {"id": "layout_2_2"}, Rect(0, 0, 1, 1, "c"), "<div></div>", {})),
            "2*2",
        )
        # 纯图标 → None（纯装饰）
        self.assertIsNone(
            _infer_layout_id(DomNodeSnapshot("i", "", {"id": "ic-layout"}, Rect(0, 0, 1, 1, "c"), "<i></i>", {})),
        )

    def test_layout_option_zindex_above_series_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "main.png").write_bytes(b"main-bytes")
            (assets / "lay11.png").write_bytes(b"lay")
            flow = self._flow()
            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")
            layout_z = re.search(r'\.overlay>\[data-replica-layout\]\{z-index:3', rendered)
            series_z = re.search(r'\.overlay>\[data-replica-series-key\]\{z-index:2', rendered)
            self.assertIsNotNone(layout_z)
            self.assertIsNotNone(series_z)

    def test_layout_folder_defaults_disabled_layout_when_variant_missing(self):
        # disabled 态：variant 可推但无对应布局背景 → aria-disabled（不假装可点）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "main.png").write_bytes(b"main-bytes")
            (assets / "lay11.png").write_bytes(b"lay")
            layout_members = [
                _layout_member("m11", "1*1", elm_id="layout_1_1", y=40.0),
                _layout_member("m33", "3*3", elm_id="layout_3_3", y=70.0),
            ]
            # layout_variants 只有 1*1；3*3 无背景 → disabled
            flow = self._flow(
                layout_variants={"1*1": "assets/lay11.png"},
                layout_members=layout_members,
                series_members=[],
            )
            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-replica-layout="1*1"', rendered)
            self.assertIn('data-replica-layout="3*3"', rendered)
            self.assertIn('data-replica-layout="3*3"', rendered)
            three = re.search(r'data-replica-layout="3\*3"[^>]*', rendered)
            self.assertIsNotNone(three)
            self.assertIn('aria-disabled="true"', three.group(0))


class SeriesHotspotClippingTests(unittest.TestCase):
    """步骤5 去重叠：series 热区裁剪掉与布局按钮重叠的命中带。"""

    def _flow_with_overlap(self):
        page_model = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
        evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")
        # 序列项 y∈[30,54) x∈[10,310)，布局按钮 y∈[40,68) x∈[40,140)
        # → 重叠带 y∈[40,54)
        layout_region = _layout_region(
            "d_main",
            [
                _layout_member("m22", "2*2", elm_id="layout_2_2", y=40.0, x=40.0),
            ],
        )
        series_member = RegionMember(
            "d_p_000_root_series_000", "series",
            DomNodeSnapshot(
                "li", "Series 1", {"id": "uids0"},
                Rect(10, 30, 300, 24, "page_viewport_css"),
                '<li id="uids0" data-series-uid="130">Series 1</li>',
                {"display": "block"},
            ),
        )
        series_region = InteractionRegion(
            "r_series_clip", "series", "d_main",
            DomNodeSnapshot(
                "div", "", {"id": "HLeftThumnail"},
                Rect(0, 0, 400, 200, "page_viewport_css"),
                '<div id="HLeftThumnail"></div>', {"display": "block"},
            ),
            [series_member], None,
        )
        doc = ReplicaDocument(
            "d_main", "p_main", "page", "main", None, None, None, None,
            {"width": 640, "height": 400}, 1, "css", 0, 0,
            "assets/main.png", "main", 3,
            regions=[series_region, layout_region],
            layout_variants={"2*2": "assets/lay22.png"},
            default_layout="2*2",
        )
        states = [
            ReplicaState(
                "s_000", 0, "", "page", [page_model], [doc], [], evidence,
            )
        ]
        branches = [SeriesBranch(
            "branch_000", "series-key-000", "Series 1", 0, "d_main",
            "d_p_000_root_series_000", None, "click",
            None, None, None, "captured", None,
        )]
        return ReplicaFlow(
            1, "overlap", "recorded.py", "hash", "now", {"width": 640, "height": 400},
            BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", states, [],
            branches,
        )

    def test_overlapping_series_row_clipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "main.png").write_bytes(b"main-bytes")
            (assets / "lay22.png").write_bytes(b"lay22-bytes")
            flow = self._flow_with_overlap()
            output = root / "replica"
            build_replica(flow, root, output)
            rendered = (output / "index.html").read_text(encoding="utf-8")
            slug = series_key_slug("series-key-000")
            series_style = re.search(
                r'data-replica-series-key="{}" style="([^"]+)"'.format(slug), rendered
            )
            self.assertIsNotNone(series_style)
            style = series_style.group(1)
            top = float(re.search(r"top:([\d.]+)px", style).group(1))
            height = float(re.search(r"height:([\d.]+)px", style).group(1))
            # 序列项 y∈[30,54)，布局按钮 y∈[40,68) → 重叠带 y∈[40,54) 在序列项底部，
            # 序列项热区裁剪后只保留顶部非重叠带 [30,40)，height=10。
            self.assertAlmostEqual(top, 30.0)
            self.assertAlmostEqual(height, 10.0)
            # 布局按钮仍可点
            self.assertIn('data-replica-layout="2*2"', rendered)


class MultiStateSeriesPromotionTests(unittest.TestCase):
    """步骤3：同一 document 的所有无 series region 的主路径状态都被提升。"""

    def test_promotion_covers_all_post_layout_states(self):
        root_series = DomNodeSnapshot(
            "div", "", {"id": "HLeftThumnail"},
            Rect(20, 30, 420, 200, "page_viewport_css"),
            '<div id="HLeftThumnail"></div>', {"display": "block"},
        )
        series = InteractionRegion(
            "r_series", "series", "d_main", root_series,
            [
                _series_member("d_p_000_root_series_000", "Series 1", 40),
                _series_member("d_p_000_root_series_001", "Series 2", 70),
            ],
            None,
        )
        page_model = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
        evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")

        def doc():
            return ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 640, "height": 400}, 1, "css", 0, 0,
                "assets/main.png", "main", 3,
            )

        s001_doc = doc()
        s002_doc = doc()
        s003_doc = doc()
        series_doc = doc()
        series_doc.regions.append(copy.deepcopy(series))
        states = [
            ReplicaState("s_000", 0, "", "page", [page_model], [doc()], [], evidence),
            ReplicaState("s_001", 1, "", "page", [page_model], [s001_doc], [], evidence),
            ReplicaState("s_002", 2, "", "page", [page_model], [s002_doc], [], evidence),
            ReplicaState("s_003", 3, "", "page", [page_model], [s003_doc], [], evidence),
            ReplicaState("s_004", 4, "", "page", [page_model], [series_doc], [], evidence),
        ]
        flow = ReplicaFlow(
            1, "zs-late-multi", "recorded.py", "hash", "now", {"width": 640, "height": 400},
            BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", states, [],
        )
        promoted = _promote_series_regions_to_earliest_documents(flow)
        # s_000..s_003 四个无 series 的状态（含入口 s_000）都被提升
        self.assertEqual(promoted, 4)
        for idx, expected in ((0, "s_000"), (1, "s_001"), (2, "s_002"), (3, "s_003")):
            cand = flow.states[idx].documents[0]
            self.assertEqual(
                len([r for r in cand.regions if r.region_type == "series"]), 1,
                f"{expected} 应被提升出 series region",
            )
        # s_004 本身有 region，不被重复提升
        self.assertEqual(len(series_doc.regions), 1)


class DeadEndBackTests(unittest.TestCase):
    """步骤3 死胡同兜底：无 out transition 的非入口状态渲染 data-replica-back。"""

    def _flow_dead_end(self):
        page_model = ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)
        evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")

        def doc():
            return ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 640, "height": 400}, 1, "css", 0, 0,
                "assets/main.png", "main", 3,
            )

        entry_doc = doc()
        entry_doc.regions.append(
            InteractionRegion(
                "r_series_entry", "series", "d_main",
                DomNodeSnapshot("div", "", {"id": "HLeftThumnail"}, Rect(0, 0, 300, 100, "page_viewport_css"),
                                '<div id="HLeftThumnail"></div>', {"display": "block"}),
                [_series_member("d_p_000_root_series_000", "Series 1", 30)], None,
            )
        )
        # 前一可交互状态 s_002 带 series region（真实 promotion 后一致）
        mid_doc = doc()
        mid_doc.regions.append(
            InteractionRegion(
                "r_series_mid", "series", "d_main",
                DomNodeSnapshot("div", "", {"id": "HLeftThumnail"}, Rect(0, 0, 300, 100, "page_viewport_css"),
                                '<div id="HLeftThumnail"></div>', {"display": "block"}),
                [_series_member("d_p_000_root_series_000", "Series 1", 30)], None,
            )
        )
        # s_003：非入口、无 out transition、有 series region（死胡同）
        dead_doc = doc()
        dead_doc.regions.append(
            InteractionRegion(
                "r_series_dead", "series", "d_main",
                DomNodeSnapshot("div", "", {"id": "HLeftThumnail"}, Rect(0, 0, 300, 100, "page_viewport_css"),
                                '<div id="HLeftThumnail"></div>', {"display": "block"}),
                [_series_member("d_p_000_root_series_000", "Series 1", 30)], None,
            )
        )
        states = [
            ReplicaState("s_000", 0, "", "page", [page_model], [entry_doc], [], evidence),
            ReplicaState("s_001", 1, "", "page", [page_model], [doc()], [], evidence),
            ReplicaState("s_002", 2, "", "page", [page_model], [mid_doc], [], evidence),
            ReplicaState("s_003", 3, "", "page", [page_model], [dead_doc], [], evidence),
        ]
        return ReplicaFlow(
            1, "dead-end", "recorded.py", "hash", "now", {"width": 640, "height": 400},
            BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", states, [],
        )

    def test_dead_end_state_gets_back_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "main.png").write_bytes(b"main-bytes")
            flow = self._flow_dead_end()
            output = root / "replica"
            build_replica(flow, root, output)
            dead_html = (output / "states" / "s_003" / "index.html").read_text(encoding="utf-8")
            # 死胡同状态获得返回入口（data-replica-back → s_002 相对路径）
            match = re.search(r'data-replica-back="([^"]+)"', dead_html)
            self.assertIsNotNone(match, "s_003 死胡同应有 data-replica-back")
            self.assertIn("s_002/index.html", match.group(1))
            # 入口状态不应有返回按钮
            entry_html = (output / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("data-replica-back", entry_html)


class LayoutVariantPropagationTests(unittest.TestCase):
    """布局变体向「拥有同一 viewer document 的更早状态」传播——入口/popup
    viewer 先于录制布局 marker 状态（zscloud s_001 vs s_002），必须也有可点
    布局按钮，否则「布局调整后无法继续下一步」。"""

    def _layout_region_with_members(self, document_id):
        """Layout region with clickable 1*1 / 2*2 members (drives data-replica-layout)."""
        members = [
            RegionMember(
                f"{document_id}_layout_000", "layout",
                DomNodeSnapshot("button", "", {"id": "layout_1_1"},
                                Rect(360, 40, 40, 40, "page_viewport_css"),
                                '<button id="layout_1_1">1*1</button>', {}),
            ),
            RegionMember(
                f"{document_id}_layout_001", "layout",
                DomNodeSnapshot("button", "", {"id": "layout_2_2"},
                                Rect(400, 40, 40, 40, "page_viewport_css"),
                                '<button id="layout_2_2">2*2</button>', {}),
            ),
        ]
        root = DomNodeSnapshot("div", "", {"id": "cellStyle"},
                               Rect(360, 0, 300, 300, "page_viewport_css"),
                               '<div id="cellStyle"></div>', {})
        return InteractionRegion(f"{document_id}_layout", "layout", document_id, root, members, None)

    def _build_flow_popup_viewer(self):
        def doc(document_id, page_id, page_var, variants=None, with_layout_region=False):
            regions = [self._layout_region_with_members(document_id)] if with_layout_region else []
            return ReplicaDocument(
                document_id, page_id, page_var, "popup" if page_var == "page1" else "main",
                None, "iframe" if page_var == "page1" else None, None, None,
                {"width": 640, "height": 400}, 1, "css", 0, 0,
                "assets/main.png", "main", 3,
                layout_variants=variants,
                regions=regions,
            )
        evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "state")
        page_model = ReplicaPage("p_main", "page", "main", None, None, "d_p_000_root", True, False)
        popup_model = ReplicaPage("p_001", "page1", "popup", "main", "replica-popup", "d_p_001_root", True, False)
        # 布局 region 只挂在带 variants 的状态（s_002）上——传播后 s_001 的 popup 页
        # 也应渲染它（与真机 zscloud 一致：layout region 在 s_002，s_001 无 region）。
        # 注意：传播复制的是 variants，不复制 region；为测 `data-replica-layout` 渲染，
        # 让两个状态都带 layout region（region 本身由 region 传播/捕获决定，独立于 variants）。
        viewer = doc("d_p_001_f_001", "p_001", "page1", with_layout_region=True)
        viewer_with_variants = doc(
            "d_p_001_f_001", "p_001", "page1",
            {"1*1": "assets/l11.png", "2*2": "assets/l22.png"},
            with_layout_region=True,
        )
        s1 = ReplicaState("s_001", 0, "", "page", [page_model, popup_model],
                          [doc("d_p_000_root", "p_main", "page"), doc("d_p_001_root", "p_001", "page1"), viewer], [], evidence)
        s2 = ReplicaState("s_002", 1, "", "page", [page_model, popup_model],
                          [doc("d_p_000_root", "p_main", "page"), doc("d_p_001_root", "p_001", "page1"), viewer_with_variants], [], evidence)
        return ReplicaFlow(1, "popup-layout", "recorded.py", "hash", "now", {"width": 640, "height": 400},
                           BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_001", [s1, s2], [])

    def test_variants_propagate_to_earlier_state_owning_same_document(self):
        flow = self._build_flow_popup_viewer()
        propagated = _propagate_layout_variants_across_documents(flow)
        self.assertEqual(propagated, 1)
        s1_viewer = next(
            d for d in flow.states[0].documents if d.document_id == "d_p_001_f_001"
        )
        self.assertEqual(s1_viewer.layout_variants, {"1*1": "assets/l11.png", "2*2": "assets/l22.png"})

    def test_propagation_idempotent_and_skips_already_populated(self):
        flow = self._build_flow_popup_viewer()
        self.assertEqual(_propagate_layout_variants_across_documents(flow), 1)
        self.assertEqual(_propagate_layout_variants_across_documents(flow), 0)

    def test_propagation_noop_when_no_variants_anywhere(self):
        flow = self._build_flow_popup_viewer()
        for state in flow.states:
            for document in state.documents:
                document.layout_variants = {}
        self.assertEqual(_propagate_layout_variants_across_documents(flow), 0)

    def test_build_renders_layout_in_earlier_viewer_state(self):
        flow = self._build_flow_popup_viewer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir(parents=True, exist_ok=True)
            for f in ("main.png", "l11.png", "l22.png"):
                (root / "assets" / f).write_bytes(b"png")
            output = root / "replica"
            build_replica(flow, root, output)
            # zscloud Dapeng：popup viewer 是 s_001 的 page1 壳页（pages/p_001），
            # 先于带 layout_variants 的 s_002；传播后 s_001 的 popup 页必须注入布局。
            s1_html = (output / "pages" / "p_001" / "index.html").read_text(encoding="utf-8")
            self.assertIn("window.__REPLICA_LAYOUTS__", s1_html)
            self.assertIn('data-replica-layout="1*1"', s1_html)
            self.assertIn('data-replica-layout="2*2"', s1_html)
            s2_html = (output / "states" / "s_002" / "pages" / "p_001" / "index.html").read_text(encoding="utf-8")
            self.assertIn("window.__REPLICA_LAYOUTS__", s2_html)

    def test_early_state_zero_rect_layout_region_is_replaced_by_later_full_region(self):
        """早期状态（布局 marker 前）的 layout region 浮层未展开：布局选项成员
        rect 全 0 → 渲染成 (0,0) 挤在角落不可点。传播必须用后续状态的完整 region
        （选项 rect 非 0）深拷贝替换，让布局按钮落在正确坐标（zscloud s_001 vs s_002）。"""
        flow = self._build_flow_popup_viewer()
        # 复刻真机：s_001 的 layout region 选项 rect 全 0（浮层未展开），仅按钮本体 40x40
        s1_viewer = next(
            d for d in flow.states[0].documents if d.document_id == "d_p_001_f_001"
        )
        lr1 = next(r for r in s1_viewer.regions if r.region_type == "layout")
        for member in lr1.members:
            if member.dom.attributes.get("id", "").startswith("layout_"):
                member.dom.rect = Rect(0, 0, 0, 0, "region_content_css")
        # buttons 本体保留 40x40 → 旧判定会误判 region 有效；新判定看可推断选项 rect
        propagated = _propagate_layout_variants_across_documents(flow)
        self.assertGreaterEqual(propagated, 2)  # variants + region 替换各计 1
        replaced = next(
            d for d in flow.states[0].documents if d.document_id == "d_p_001_f_001"
        )
        lr1_after = next(r for r in replaced.regions if r.region_type == "layout")
        layout_1_1 = next(
            m for m in lr1_after.members
            if m.dom.attributes.get("id") == "layout_1_1"
        )
        self.assertTrue(layout_1_1.dom.rect.width > 0, "layout_1_1 rect 应被补全为非 0")
        # fixture 中 layout_1_1 坐标来自 s_002 的完整 region（x=360），非 0 占位
        self.assertEqual(layout_1_1.dom.rect.x, 360.0)


if __name__ == "__main__":
    unittest.main()
