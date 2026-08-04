import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from build_replica import build_replica
from replay_helpers import ReplicaServer
from replica_models import (
    ActionTarget, BootstrapPlan, CaptureTimingProfile, DomNodeSnapshot, LocatorRecipe,
    InteractionRegion, RegionMember, Rect, ReplicaDocument, ReplicaFlow, ReplicaPage, ReplicaState, ReplicaTransition, StateEvidence,
)


def document(document_id, text, target=None):
    return ReplicaDocument(
        document_id, "p_main", "page", "main", None, None, None, None,
        {"width": 300, "height": 200}, 1, "css", 0, 0, f"assets/{document_id}.png", document_id, 3,
        targets=[] if target is None else [target],
        regions=[],
    )


class ReplicaRuntimeTests(unittest.TestCase):
    def test_series_option_click_updates_aria_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "d_main.png").write_bytes(b"png")
            root_dom = DomNodeSnapshot("div", "", {"id": "series", "role": "listbox"}, Rect(0, 0, 200, 80, "page_viewport_css"), '<div id="series" role="listbox"></div>', {})
            one = DomNodeSnapshot("div", "One", {"id": "one", "role": "option", "aria-selected": "true"}, Rect(0, 0, 100, 20, "region_content_css"), '<div id="one" role="option" aria-selected="true">One</div>', {})
            two = DomNodeSnapshot("div", "Two", {"id": "two", "role": "option", "aria-selected": "false"}, Rect(0, 20, 100, 20, "region_content_css"), '<div id="two" role="option" aria-selected="false">Two</div>', {})
            region = InteractionRegion("r_series", "series", "d_main", root_dom, [RegionMember("one", "div", one), RegionMember("two", "div", two)], None)
            doc = ReplicaDocument("d_main", "p_main", "page", "main", None, None, None, None, {"width": 300, "height": 200}, 1, "css", 0, 0, "assets/d_main.png", "d_main", 3, regions=[region])
            flow = ReplicaFlow(1, "series", "recorded.py", "hash", "now", {"width": 300, "height": 200}, BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", [ReplicaState("s_000", 0, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)], [doc], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))], [])
            output = root / "replica"
            build_replica(flow, root, output)
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                page.locator("#two").click()
                self.assertEqual(page.locator("#two").get_attribute("aria-selected"), "true")
                self.assertEqual(page.locator("#one").get_attribute("aria-selected"), "false")
                browser.close()

    def test_click_transitions_to_manifest_declared_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "d_0.png").write_bytes(b"png")
            (assets / "d_1.png").write_bytes(b"png")
            button = DomNodeSnapshot("button", "Next", {"id": "next"}, Rect(10, 10, 80, 30, "page_viewport_css"), '<button id="next">Next</button>', {"display": "block"})
            target = ActionTarget("a_next", "m_0", "click", "locator", {}, LocatorRecipe('page.locator("#next")', "page", [], "css", {"args": ["#next"]}, None, None), button, None, None, None, "execute", None, "d_0", "t_next")
            done = DomNodeSnapshot("p", "Done", {"id": "done"}, Rect(10, 10, 80, 30, "page_viewport_css"), '<p id="done">Done</p>', {"display": "block"})
            state0 = ReplicaState("s_000", 0, "https://example.test", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_0", True, False)], [document("d_0", "start", target)], [ReplicaTransition("t_next", "a_next", "s_000", "s_001", "page", "page", "same_page")], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))
            state1 = ReplicaState("s_001", 1, "https://example.test/next", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_1", True, False)], [ReplicaDocument("d_1", "p_main", "page", "main", None, None, None, None, {"width": 300, "height": 200}, 1, "css", 0, 0, "assets/d_1.png", "d_1", 3, regions=[])], [], StateEvidence(False, True, False, False, 0, 0, 0, 0, "transition"))
            state1.documents[0].regions = []
            state1.documents[0].targets = [ActionTarget("a_done", "m_1", "hover", "locator", {}, None, done, None, None, None, "explicit_skip", "display", "d_1", None)]
            flow = ReplicaFlow(1, "runtime", "recorded.py", "hash", "now", {"width": 300, "height": 200}, BootstrapPlan(1, 1, True, {"page": "main"}), [], CaptureTimingProfile(), "s_000", [state0, state1], [])
            output = root / "replica"
            build_replica(flow, root, output)
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                self.assertEqual(page.locator("#next").evaluate("element => getComputedStyle(element).opacity"), "0")
                page.locator("#next").click()
                self.assertEqual(page.locator("#done").inner_text(), "Done")
                browser.close()

    def test_non_action_overlay_child_cannot_block_action_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "d_0.png").write_bytes(b"png")
            (assets / "d_1.png").write_bytes(b"png")
            tags = DomNodeSnapshot(
                "a",
                "Tags",
                {"id": "tags"},
                Rect(10, 10, 40, 40, "page_viewport_css"),
                '<a id="tags"><i class="icon icon-tags"></i><span>Tags</span></a>',
                {"display": "block"},
            )
            target = ActionTarget(
                "a_tags",
                "m_0",
                "click",
                "locator",
                {},
                LocatorRecipe('page.locator("#tags")', "page", [], "css", {"args": ["#tags"]}, None, None),
                tags,
                None,
                None,
                None,
                "execute",
                None,
                "d_0",
                "t_tags",
            )
            obstruction = DomNodeSnapshot(
                "i",
                "",
                {"class": "icon icon-tags"},
                Rect(20, 20, 20, 20, "region_content_css"),
                '<i class="icon icon-tags"></i>',
                {"display": "block"},
            )
            region_root = DomNodeSnapshot(
                "div",
                "",
                {"id": "toolbar"},
                Rect(0, 0, 100, 100, "page_viewport_css"),
                '<div id="toolbar"></div>',
                {"display": "block"},
            )
            region = InteractionRegion(
                "r_toolbar",
                "meta",
                "d_0",
                region_root,
                [RegionMember("icon", "i", obstruction)],
                None,
            )
            first = document("d_0", "start", target)
            first.regions = [region]
            done = DomNodeSnapshot(
                "p",
                "Metadata",
                {"id": "metadata"},
                Rect(10, 10, 100, 30, "page_viewport_css"),
                '<p id="metadata">Metadata</p>',
                {"display": "block"},
            )
            second = document("d_1", "metadata")
            second.targets = [
                ActionTarget("a_done", "m_1", "hover", "locator", {}, None, done, None, None, None, "explicit_skip", "display", "d_1", None)
            ]
            page_model = ReplicaPage("p_main", "page", "main", None, None, "d_0", True, False)
            state0 = ReplicaState(
                "s_000",
                0,
                "",
                "page",
                [page_model],
                [first],
                [ReplicaTransition("t_tags", "a_tags", "s_000", "s_001", "page", "page", "same_page")],
                StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
            )
            state1 = ReplicaState(
                "s_001",
                1,
                "",
                "page",
                [ReplicaPage("p_main", "page", "main", None, None, "d_1", True, False)],
                [second],
                [],
                StateEvidence(False, False, False, False, 0, 0, 0, 0, "transition"),
            )
            flow = ReplicaFlow(
                1,
                "overlay-hit",
                "recorded.py",
                "hash",
                "now",
                {"width": 300, "height": 200},
                BootstrapPlan(1, 1, True, {"page": "main"}),
                [],
                CaptureTimingProfile(),
                "s_000",
                [state0, state1],
                [],
            )
            output = root / "replica"
            build_replica(flow, root, output)

            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                hit_action = page.evaluate(
                    "() => document.elementFromPoint(30, 30)?.closest('[data-replica-action]')?.dataset.replicaAction"
                )

                self.assertEqual(hit_action, "a_tags")
                page.mouse.click(30, 30)
                self.assertEqual(page.locator("#metadata").inner_text(), "Metadata")
                browser.close()

    def test_popup_transition_opens_target_state_in_new_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            for name in ("d_main", "d_popup"):
                (root / "assets" / f"{name}.png").write_bytes(b"png")
            opener = DomNodeSnapshot("button", "Open", {"id": "open-popup"}, Rect(5, 5, 80, 30, "page_viewport_css"), '<button id="open-popup">Open</button>', {"display": "block"})
            action = ActionTarget("a_open", "m_0", "click", "locator", {}, LocatorRecipe('page.locator("#open-popup")', "page", [], "css", {"args": ["#open-popup"]}, None, None), opener, None, None, None, "execute", None, "d_main", "t_open")
            finished = DomNodeSnapshot("p", "Popup ready", {"id": "popup-ready"}, Rect(5, 5, 100, 30, "page_viewport_css"), '<p id="popup-ready">Popup ready</p>', {"display": "block"})
            main = document("d_main", "main", action)
            popup = ReplicaDocument("d_popup", "p_popup", "page1", "popup", None, None, None, None, {"width": 300, "height": 200}, 1, "css", 0, 0, "assets/d_popup.png", "popup", 3, targets=[ActionTarget("a_ready", "m_1", "hover", "locator", {}, None, finished, None, None, None, "explicit_skip", "display", "d_popup", None)])
            state0 = ReplicaState("s_000", 0, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)], [main], [ReplicaTransition("t_open", "a_open", "s_000", "s_001", "page", "page1", "popup")], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))
            state1 = ReplicaState("s_001", 1, "", "page1", [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False), ReplicaPage("p_popup", "page1", "popup", "p_main", "viewer", "d_popup", True, False)], [popup], [], StateEvidence(False, False, True, False, 0, 0, 0, 0, "popup"))
            flow = ReplicaFlow(1, "popup", "recorded.py", "hash", "now", {"width": 300, "height": 200}, BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_000", [state0, state1], [])
            output = root / "replica"
            build_replica(flow, root, output)
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                with page.expect_popup() as popup_info:
                    page.locator("#open-popup").click()
                self.assertEqual(popup_info.value.locator("#popup-ready").inner_text(), "Popup ready")
                self.assertEqual(page.locator("#open-popup").count(), 1)
                browser.close()


if __name__ == "__main__":
    unittest.main()
