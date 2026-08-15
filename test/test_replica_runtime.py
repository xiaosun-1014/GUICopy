import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from build_replica import build_replica
from replay_helpers import ReplicaServer
from replay_helpers import sha256_file
from replay_helpers import series_key_slug
from replica_models import (
    ActionTarget, BootstrapPlan, CaptureTimingProfile, DomNodeSnapshot, LocatorRecipe,
    InteractionRegion, RegionMember, Rect, ReplicaDocument, ReplicaFlow, ReplicaPage, ReplicaState, ReplicaTransition, SeriesBranch, StateEvidence,
)


def document(document_id, text, target=None):
    return ReplicaDocument(
        document_id, "p_main", "page", "main", None, None, None, None,
        {"width": 300, "height": 200}, 1, "css", 0, 0, f"assets/{document_id}.png", document_id, 3,
        targets=[] if target is None else [target],
        regions=[],
    )


def _series_list_region(document_id, members):
    root = DomNodeSnapshot(
        "div", "", {"id": "series", "role": "listbox"},
        Rect(0, 0, 300, 200, "page_viewport_css"),
        f'<div id="series" role="listbox"></div>', {},
    )
    return InteractionRegion(
        f"{document_id}_series", "series", document_id, root,
        members, None,
    )


def _member(member_id, label, selected=False, failed=False, y=0):
    dom = DomNodeSnapshot(
        "div", label, {"id": member_id, "role": "option", "aria-selected": "true" if selected else "false"},
        Rect(0, y, 300, 20, "region_content_css"),
        f'<div id="{member_id}" role="option" aria-selected="{"true" if selected else "false"}">{label}{" (failed)" if failed else ""}</div>',
        {},
    )
    return RegionMember(member_id, "div", dom)


def _series_members(selected_key=None):
    return [
        _member("ma", "Series A", selected=(selected_key == "A"), y=0),
        _member("mb", "Series B", selected=(selected_key == "B"), y=12),
        _member("mc", "Series C", selected=(selected_key == "C"), y=24),
        _member("md", "Series D", selected=False, failed=True, y=36),
    ]


def _build_series_flow():
    """Construct a hand-rolled multi-series flow (no Phase 6 dependency).

    States: hub(A/B/C/D) -> viewer_A/B/C each with a Metadata trigger and a
    per-branch Metadata state whose close returns *explicitly* to the branch's
    Viewer state.
    """
    page_model = ReplicaPage("p_main", "page", "main", None, None, "d_hub", True, False)

    def viewer_doc(document_id, doc_base, selected_key, meta_action=None):
        members = _series_members(selected_key)
        body_html = f'<div id="viewer-{doc_base}">{doc_base} viewer unique</div>'
        viewer_dom = DomNodeSnapshot(
            "div", f"{doc_base} viewer unique", {"id": f"viewer-{doc_base}"},
            Rect(0, 60, 300, 90, "page_viewport_css"),
            body_html, {},
        )
        targets = []
        if meta_action is not None:
            targets.append(ActionTarget(
                meta_action, "m_meta", "click", "locator", {},
                LocatorRecipe(f'page.locator("#meta-{doc_base}")', "page", [], "css", {"args": [f"#meta-{doc_base}"]}, None, None),
                DomNodeSnapshot(
                    "div", "Metadata", {"id": f"meta-{doc_base}", "data-testid": "meta-open"},
                    Rect(0, 160, 100, 30, "page_viewport_css"),
                    f'<div id="meta-{doc_base}" data-testid="meta-open">Metadata</div>', {},
                ),
                None, None, None, "execute", None, document_id, None,
            ))
        # Render the unique viewer block as an overlay node (explicit_skip so it
        # appears in DOM for assertion without being a recording action).
        targets.append(ActionTarget(
            f"viewer_{doc_base}", "m_viewer", "hover", "locator", {}, None,
            viewer_dom, None, None, None, "explicit_skip", "display", document_id, None,
        ))
        return ReplicaDocument(
            document_id, "p_main", "page", "main", None, None, None, None,
            {"width": 300, "height": 200}, 1, "css", 0, 0,
            f"assets/{doc_base}.png", doc_base, len(doc_base),
            targets=targets,
            regions=[_series_list_region(document_id, members)],
        )

    def metadata_doc(document_id, doc_base, key):
        panel = DomNodeSnapshot(
            "div", f"Metadata {key} unique tag",
            {"id": f"mpanel-{doc_base}", "class": "tagsBox"},
            Rect(0, 0, 300, 200, "page_viewport_css"),
            f'<div id="mpanel-{doc_base}" class="tagsBox"><div id="m-tag-{doc_base}">tag-{key}: value-{key}</div></div>',
            {},
        )
        return ReplicaDocument(
            document_id, "p_main", "page", "main", None, None, None, None,
            {"width": 300, "height": 200}, 1, "css", 0, 0,
            f"assets/{doc_base}.png", doc_base, len(doc_base),
            regions=[InteractionRegion(f"{document_id}_meta", "metadata", document_id, panel, [], None)],
        )

    hub_members = _series_members("A")
    hub_doc = ReplicaDocument(
        "d_hub", "p_main", "page", "main", None, None, None, None,
        {"width": 300, "height": 200}, 1, "css", 0, 0,
        "assets/hub.png", "hub", 3,
        regions=[_series_list_region("d_hub", hub_members)],
    )

    # Ordinals are deliberately arranged so each branch Metadata state's *ordinal
    # predecessor* is a different branch's state -- only the explicit
    # return_state_id must win.
    states = [
        ReplicaState("s_hub", 0, "", "page", [page_model], [hub_doc], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry")),
        ReplicaState("s_va", 1, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_va", True, False)], [viewer_doc("d_va", "va", "A", "meta_open_a")], [ReplicaTransition("t_meta_a", "meta_open_a", "s_va", "s_ma", "page", "page", "same_page")], StateEvidence(False, False, False, False, 0, 0, 0, 0, "viewer")),
        ReplicaState("s_vb", 2, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_vb", True, False)], [viewer_doc("d_vb", "vb", "B", "meta_open_b")], [ReplicaTransition("t_meta_b", "meta_open_b", "s_vb", "s_mb", "page", "page", "same_page")], StateEvidence(False, False, False, False, 0, 0, 0, 0, "viewer")),
        ReplicaState("s_vc", 3, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_vc", True, False)], [viewer_doc("d_vc", "vc", "C", "meta_open_c")], [ReplicaTransition("t_meta_c", "meta_open_c", "s_vc", "s_mc", "page", "page", "same_page")], StateEvidence(False, False, False, False, 0, 0, 0, 0, "viewer")),
        # ordinal predecessor of s_mb is s_ma (not s_vb) -- return must go to s_vb.
        ReplicaState("s_ma", 4, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_ma", True, False)], [metadata_doc("d_ma", "ma", "A")], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "metadata")),
        ReplicaState("s_mb", 5, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_mb", True, False)], [metadata_doc("d_mb", "mb", "B")], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "metadata")),
        ReplicaState("s_mc", 6, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_mc", True, False)], [metadata_doc("d_mc", "mc", "C")], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "metadata")),
    ]

    branches = [
        SeriesBranch("branch_a", "A", "Series A", 0, "d_hub", "ma", None, "click", "s_va", "s_ma", "s_va", "captured", None),
        SeriesBranch("branch_b", "B", "Series B", 1, "d_hub", "mb", None, "click", "s_vb", "s_mb", "s_vb", "captured", None),
        SeriesBranch("branch_c", "C", "Series C", 2, "d_hub", "mc", None, "click", "s_vc", "s_mc", "s_vc", "captured", None),
        SeriesBranch("branch_d", "D", "Series D", 3, "d_hub", "md", None, "click", None, None, None, "failed", "no_viewer_snapshot"),
    ]
    flow = ReplicaFlow(
        1, "multi-series", "recorded.py", "hash", "now",
        {"width": 300, "height": 200},
        BootstrapPlan(1, 1, True, {"page": "main"}),
        [], CaptureTimingProfile(), "s_hub", states, [],
        series_branches=branches,
    )
    return flow


def _write_assets(root: Path) -> None:
    """Write distinct screenshot bytes for every document so the by-hash assets differ."""
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for base, payload in [
        ("hub", b"hub-bytes"),
        ("va", b"viewer-A-bytes"),
        ("vb", b"viewer-B-bytes"),
        ("vc", b"viewer-C-bytes"),
        ("ma", b"meta-A-bytes"),
        ("mb", b"meta-B-bytes"),
        ("mc", b"meta-C-bytes"),
    ]:
        (assets / f"{base}.png").write_bytes(payload)


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
                # Match the browser viewport to the flow viewport (300x200) so the
                # replica is not scaled and the raw coordinates (30, 30) at which the
                # decorative overlay ''i'' overlaps the action land on the action.
                page = browser.new_page(viewport={"width": 300, "height": 200})
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

    def test_multi_series_route_navigation_metadata_return_and_disabled_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_assets(root)
            flow = _build_series_flow()
            output = root / "replica"
            build_replica(flow, root, output)

            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                # Match viewport to the flow viewport so the replica is at 1:1 scale
                # and every element (including the near-bottom Metadata button) is
                # on-screen and clickable via locators.
                page = browser.new_page(viewport={"width": 300, "height": 200})
                page.goto(server.url)

                # Failed branch D is visible but aria-disabled and never navigates.
                self.assertEqual(page.locator(f'[data-replica-series-key="{series_key_slug("D")}"]').count(), 1)
                self.assertEqual(
                    page.locator(f'[data-replica-series-key="{series_key_slug("D")}"]').get_attribute("aria-disabled"), "true"
                )
                before = page.url
                page.locator(f'[data-replica-series-key="{series_key_slug("D")}"]').click(force=True)
                self.assertEqual(page.url, before, "failed branch must not navigate")
                self.assertEqual(
                    page.locator(f'[data-replica-series-key="{series_key_slug("D")}"]').get_attribute("aria-disabled"), "true"
                )

                # From A click B -> enter viewer_state_B.
                page.locator(f'[data-replica-series-key="{series_key_slug("B")}"]').click()
                page.wait_for_url("**/states/s_vb/index.html")
                self.assertIn("/states/s_vb/index.html", page.url)

                # B option is aria-selected=true, others false; final content actually
                # switched to B (unique viewer DOM + B screenshot asset).
                self.assertEqual(
                    page.locator(f'[data-replica-series-key="{series_key_slug("B")}"]').get_attribute("aria-selected"), "true"
                )
                for key in ("A", "C"):
                    self.assertEqual(
                        page.locator(f'[data-replica-series-key="{series_key_slug(key)}"]').get_attribute("aria-selected"), "false"
                    )
                self.assertEqual(page.locator("#viewer-vb").inner_text(), "vb viewer unique")
                bg_src = page.locator(".replica-bg").get_attribute("src")
                self.assertIn(sha256_file(root / "assets" / "vb.png"), bg_src)

                # From B click Metadata -> metadata_state_B with B's unique tag.
                page.locator('[data-testid="meta-open"]').click()
                page.wait_for_url("**/states/s_mb/index.html")
                self.assertIn("/states/s_mb/index.html", page.url)
                self.assertEqual(page.locator("#m-tag-mb").inner_text(), "tag-B: value-B")

                # Close metadata -> must return to B (s_vb), NOT the ordinal predecessor
                # (s_ma). Only the explicit return_state_id may win.
                page.locator("[data-replica-back]").click()
                page.wait_for_url("**/states/s_vb/index.html")
                self.assertIn("/states/s_vb/index.html", page.url)
                self.assertEqual(page.locator("#viewer-vb").inner_text(), "vb viewer unique")

                # From B click C directly (no hub round-trip needed).
                page.locator(f'[data-replica-series-key="{series_key_slug("C")}"]').click()
                page.wait_for_url("**/states/s_vc/index.html")
                self.assertIn("/states/s_vc/index.html", page.url)
                self.assertEqual(page.locator("#viewer-vc").inner_text(), "vc viewer unique")
                self.assertEqual(
                    page.locator(f'[data-replica-series-key="{series_key_slug("C")}"]').get_attribute("aria-selected"), "true"
                )
                browser.close()


if __name__ == "__main__":
    unittest.main()
