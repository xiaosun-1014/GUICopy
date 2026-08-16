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


def _tall_scroll_flow():
    """Hand-rolled flow whose hub series list extends below the 300x200 fold.

    The ninth member (``m9`` at y=216) sits entirely below the captured fold and
    is wired to a real viewer + metadata branch, so a runtime test can scroll the
    overlay and click it without a hub round-trip.
    """
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
        Rect(0, 0, 300, 200, "page_viewport_css"), '<div id="series" role="listbox"></div>', {},
    )
    hub = ReplicaDocument(
        "d_hub", "p_main", "page", "main", None, None, None, None,
        {"width": 300, "height": 200}, 1, "css", 0, 0, "assets/hub.png", "hub", 3,
        regions=[InteractionRegion("d_hub_series", "series", "d_hub", hub_root, members, None)],
    )
    mark = DomNodeSnapshot(
        "div", "TALL viewer unique", {"id": "viewer-tall"},
        Rect(0, 60, 300, 90, "page_viewport_css"),
        '<div id="viewer-tall">TALL viewer unique</div>', {},
    )
    viewer = ReplicaDocument(
        "d_vt", "p_main", "page", "main", None, None, None, None,
        {"width": 300, "height": 200}, 1, "css", 0, 0, "assets/vt.png", "vt", 2,
        targets=[
            ActionTarget(
                "meta_open_t", "m_meta", "click", "locator", {},
                LocatorRecipe('page.locator("#meta-t")', "page", [], "css", {"args": ["#meta-t"]}, None, None),
                DomNodeSnapshot("div", "Metadata", {"id": "meta-t", "data-testid": "meta-open"},
                                Rect(0, 160, 100, 30, "page_viewport_css"),
                                '<div id="meta-t" data-testid="meta-open">Metadata</div>', {}),
                None, None, None, "execute", None, "d_vt", None,
            ),
            ActionTarget("viewer_tall_mark", "m_viewer", "hover", "locator", {}, None,
                         mark, None, None, None, "explicit_skip", "display", "d_vt", None),
        ],
    )
    metadata = ReplicaDocument(
        "d_mt", "p_main", "page", "main", None, None, None, None,
        {"width": 300, "height": 200}, 1, "css", 0, 0, "assets/mt.png", "mt", 2,
        regions=[InteractionRegion(
            "d_mt_meta", "metadata", "d_mt",
            DomNodeSnapshot("div", "Metadata TALL unique", {"id": "mpanel-t", "class": "tagsBox"},
                            Rect(0, 0, 300, 200, "page_viewport_css"),
                            '<div id="mpanel-t" class="tagsBox"><div id="m-tag-t">tag-TALL: value-TALL</div></div>', {}),
            [], None,
        )],
    )
    states = [
        ReplicaState("s_hub", 0, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_hub", True, False)], [hub], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry")),
        ReplicaState("s_vt", 1, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_vt", True, False)], [viewer], [ReplicaTransition("t_meta_t", "meta_open_t", "s_vt", "s_mt", "page", "page", "same_page")], StateEvidence(False, False, False, False, 0, 0, 0, 0, "viewer")),
        ReplicaState("s_mt", 2, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_mt", True, False)], [metadata], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "metadata")),
    ]
    branches = [SeriesBranch(
        "branch_t", "tallseries", "Series 9", 9, "d_hub", "m9", None, "click",
        "s_vt", "s_mt", "s_vt", "captured", None,
    )]
    return ReplicaFlow(
        1, "tall-scroll", "recorded.py", "hash", "now", {"width": 300, "height": 200},
        BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_hub", states, [],
        series_branches=branches,
    )


def _augment_meta_flow():
    """Compact flow with the exact collapsed shapes the build-time two-step
    Metadata augmentation targets: a synthetic ``series:bx:meta_open`` branch
    jump that lands directly on the branch metadata state, and a recorded main
    Tags step (``a_tags``) that has a transition but no rendered element.
    """
    def doc(document_id, asset, targets=None, regions=None):
        return ReplicaDocument(
            document_id, "p_main", "page", "main", None, None, None, None,
            {"width": 300, "height": 200}, 1, "css", 0, 0, asset, document_id, 3,
            targets=targets or [], regions=regions or [],
        )

    meta_panel = lambda document_id, tag: InteractionRegion(
        f"r_{document_id}", "metadata", document_id,
        DomNodeSnapshot("div", "Metadata panel", {"id": "mpanel", "class": "tagsBox"},
                        Rect(0, 0, 300, 200, "page_viewport_css"),
                        f'<div id="mpanel" class="tagsBox"><div>{tag}</div></div>', {}),
        [], None,
    )
    more = ActionTarget(
        "a_more", "m_0", "click", "locator", {}, None,
        DomNodeSnapshot("a", "更多", {"data-testid": "more"}, Rect(250, 0, 40, 40, "page_viewport_css"),
                        '<a data-testid="more">更多</a>', {}),
        None, None, None, "execute", None, "d_main_viewer", None,
    )
    x_more = ActionTarget(
        "series:bx:meta_open", "m_x", "click", "locator", {}, None,
        DomNodeSnapshot("a", "更多", {"data-testid": "more-x"}, Rect(250, 0, 40, 40, "page_viewport_css"),
                        '<a data-testid="more-x">更多</a>', {}),
        None, None, None, "execute", None, "d_vx", None,
    )
    main_viewer = doc("d_main_viewer", "assets/va.png", [more])
    menu = doc("d_menu", "assets/va.png")
    meta_main = doc("d_meta_main", "assets/ma.png", regions=[meta_panel("d_meta_main", "main-meta")])
    viewer_x = doc("d_vx", "assets/vb.png", [x_more])
    meta_x = doc("d_mx", "assets/mb.png", regions=[meta_panel("d_mx", "x-meta")])
    states = [
        ReplicaState("s_main", 0, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_main_viewer", True, False)],
                     [main_viewer], [ReplicaTransition("t_more", "a_more", "s_main", "s_menu", "page", "page", "same_page")],
                     StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry")),
        ReplicaState("s_menu", 1, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_menu", True, False)],
                     [menu], [ReplicaTransition("t_tags", "a_tags", "s_menu", "s_meta_main", "page", "page", "same_page")],
                     StateEvidence(False, False, False, False, 0, 0, 0, 0, "viewer")),
        ReplicaState("s_meta_main", 2, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_meta_main", True, False)],
                     [meta_main], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "metadata")),
        ReplicaState("s_vx", 3, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_vx", True, False)],
                     [viewer_x], [ReplicaTransition("t_bx", "series:bx:meta_open", "s_vx", "s_mx", "page", "page", "same_page")],
                     StateEvidence(False, False, False, False, 0, 0, 0, 0, "viewer")),
        ReplicaState("s_mx", 4, "", "page", [ReplicaPage("p_main", "page", "main", None, None, "d_mx", True, False)],
                     [meta_x], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "metadata")),
    ]
    branches = [SeriesBranch("bx", "X", "Series X", 0, "d_hub", "mx", None, "click", "s_vx", "s_mx", "s_vx", "captured", None)]
    return ReplicaFlow(1, "meta-two-step", "recorded.py", "hash", "now", {"width": 300, "height": 200},
                       BootstrapPlan(1, 1, True, {}), [], CaptureTimingProfile(), "s_main", states, [],
                       series_branches=branches)


def _tall_page_flow():
    """The tall multi-series flow, with the hub's full-content list capture wired.

    Mimics a re-captured run whose series list has a scroll-stitched background
    asset covering the whole container (300x236) and a recorded content height,
    so the builder should render the page taller and scrollable instead of the
    overlay-scroll fallback.
    """
    flow = _tall_scroll_flow()
    hub_state = next(state for state in flow.states if state.state_id == "s_hub")
    hub_doc = hub_state.documents[0]
    hub_doc.series_list_full_asset_relpath = "assets/list_full.jpeg"
    hub_doc.series_list_content_height = 236
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

    def test_series_list_scroll_reveals_below_fold_row_and_clicks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_assets(root)
            (root / "assets" / "vt.png").write_bytes(b"viewer-TALL")
            (root / "assets" / "mt.png").write_bytes(b"meta-TALL")
            flow = _tall_scroll_flow()
            output = root / "replica"
            build_replica(flow, root, output)

            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 300, "height": 200})
                page.goto(server.url)

                below_key = series_key_slug("tallseries")
                # Premise: before scrolling, the spot where the below-fold row
                # will appear holds a blank (non-route) overlay node, so the row
                # is genuinely unreachable without the list's own scroll.
                pre = page.evaluate(
                    "() => document.elementFromPoint(150, 185)?.closest('[data-replica-series-key]')?.getAttribute('data-replica-series-key') || null"
                )
                self.assertIsNone(pre)

                # Scroll the overlay to its bottom; the below-fold row now shows
                # its own rendered content (opacity 1) and is clickable there.
                page.locator(".overlay").evaluate("el => { el.scrollTop = el.scrollHeight - el.clientHeight; }")
                page.wait_for_timeout(200)
                hit = page.evaluate(
                    """() => {
                        const el = document.elementFromPoint(150, 190);
                        if (!el) return null;
                        const r = el.closest('[data-replica-series-key]');
                        if (!r) return null;
                        return { key: r.getAttribute('data-replica-series-key'),
                                 opacity: getComputedStyle(r).opacity };
                    }"""
                )
                self.assertIsNotNone(hit)
                self.assertEqual(hit["key"], below_key)
                self.assertEqual(hit["opacity"], "1")

                page.mouse.click(150, 190)
                page.wait_for_url("**/states/s_vt/index.html")
                self.assertIn("/states/s_vt/index.html", page.url)
                self.assertEqual(page.locator("#viewer-tall").inner_text(), "TALL viewer unique")

                # Its Metadata opens (and would return to this branch's viewer).
                page.locator('[data-testid="meta-open"]').click()
                page.wait_for_url("**/states/s_mt/index.html")
                self.assertEqual(page.locator("#m-tag-t").inner_text(), "tag-TALL: value-TALL")
                browser.close()

    def test_branch_meta_open_is_two_step_via_tags_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_assets(root)
            flow = _augment_meta_flow()
            output = root / "replica"
            build_replica(flow, root, output)

            # Build shape: the branch viewer's 更多 no longer jumps straight to
            # the metadata state, and the recorded Tags step got a real element.
            vb = (output / "states" / "s_vx" / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("states/s_mx/index.html", vb.split("__REPLICA_SERIES_ROUTE__")[0])
            self.assertIn("btags_bx/index.html", vb)
            menu = (output / "states" / "s_menu" / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-replica-action="a_tags"', menu)

            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 300, "height": 200})
                host = server.url.replace("/index.html", "")

                # Branch path: 更多 -> Tags menu (same series viewer) -> Tags -> metadata.
                page.goto(f"{host}/states/s_vx/index.html", wait_until="load", timeout=30000)
                page.locator('[data-replica-action="series:bx:meta_open"]').click()
                page.wait_for_url("**/states/btags_bx/index.html")
                tags = page.locator('[data-replica-action="series:bx:tags"]')
                self.assertEqual(tags.count(), 1)
                self.assertEqual(tags.get_attribute("data-replica-visible"), "")
                self.assertEqual(tags.evaluate("el => getComputedStyle(el).opacity"), "1")
                tags.click()
                page.wait_for_url("**/states/s_mx/index.html")
                self.assertEqual(page.locator(".replica-metadata").count(), 1)

                # Recorded main path: the previously dead-end Tags step is clickable.
                page.goto(f"{host}/states/s_menu/index.html", wait_until="load", timeout=30000)
                page.locator('[data-replica-action="a_tags"]').click()
                page.wait_for_url("**/states/s_meta_main/index.html")
                self.assertEqual(page.locator(".replica-metadata").count(), 1)
                browser.close()

    def test_series_list_full_panel_scrolls_with_page_and_stays_clickable(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_assets(root)
            (root / "assets" / "vt.png").write_bytes(b"viewer-TALL")
            (root / "assets" / "mt.png").write_bytes(b"meta-TALL")
            Image.new("RGB", (300, 236), (12, 24, 36)).save(root / "assets" / "list_full.jpeg", "JPEG")
            flow = _tall_page_flow()
            output = root / "replica"
            build_replica(flow, root, output)

            hub = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn('style="width:300px;height:200px;overflow-y:auto;overscroll-behavior:contain"', hub)
            self.assertIn('style="height:200px"', hub)  # page screenshot pinned to the fold
            self.assertIn('class="series-pane-bg"', hub)
            self.assertNotIn('overflow-y:auto;max-height:200px', hub)  # no overlay scroll in tall mode

            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 300, "height": 200})
                page.goto(server.url)
                self.assertGreaterEqual(
                    page.locator(".replica").evaluate("el => el.scrollHeight - el.clientHeight"),
                    30,
                )
                self.assertEqual(page.locator(".series-pane-bg").count(), 1)
                # Below-fold row (content y 216) is not reachable before scrolling.
                pre = page.evaluate(
                    "() => document.elementFromPoint(150, 190)?.closest('[data-replica-series-key]')?.getAttribute('data-replica-series-key') || null"
                )
                self.assertIsNone(pre)
                page.locator(".replica").evaluate("el => { el.scrollTop = el.scrollHeight - el.clientHeight; }")
                page.wait_for_timeout(200)
                hit = page.evaluate(
                    """() => {
                        const el = document.elementFromPoint(150, 190);
                        if (!el) return null;
                        const r = el.closest('[data-replica-series-key]');
                        return r ? r.getAttribute('data-replica-series-key') : null;
                    }"""
                )
                self.assertEqual(hit, series_key_slug("tallseries"))
                page.mouse.click(150, 190)
                page.wait_for_url("**/states/s_vt/index.html")
                self.assertEqual(page.locator("#viewer-tall").inner_text(), "TALL viewer unique")
                browser.close()


if __name__ == "__main__":
    unittest.main()
