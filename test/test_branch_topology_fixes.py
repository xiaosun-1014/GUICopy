"""Regression tests for the multi-series closure-review suggestions.

Covers two concrete production fixes that are pure/deterministic (no browser):

1. (closure suggestion #1) ``_branch_topology`` atomically remaps
   ``ReplicaPage.opener_page_id`` together with ``page_id`` /
   ``entry_document_id`` so a multi-page / popup branch keeps a consistent graph.
2. (closure suggestion #3) the synthetic Meta-open trigger is mounted on exactly
   ONE viewer document — the one its ``LocatorRecipe`` frame chain resolves to —
   never guessed from ``documents[-1]`` and never broadcast to every document.

These tests build branch snapshot state on disk and drive the real
``_build_branches_into_flow``/``_load_series_branch_snapshots`` chain, so they
exercise the production merge path without a live viewer.
"""

import json
import tempfile
import unittest
from pathlib import Path

from batch_capture_replicate import (
    _branch_topology,
    _build_branches_into_flow,
    _document_id_for_recipe,
    _load_series_branch_snapshots,
)
from replica_models import (
    FrameHop,
    LocatorRecipe,
    ReplicaDocument,
    ReplicaPage,
    ReplicaState,
    StateEvidence,
)
from rewrite_script import parse_action_plan


def _page(page_id, page_var, entry_document_id, is_active, opener=None):
    return ReplicaPage(
        page_id=page_id,
        page_var=page_var,
        page_kind="main" if page_var == "page" else "popup",
        opener_page_id=opener,
        window_name=None,
        entry_document_id=entry_document_id,
        is_active=is_active,
        is_closed=False,
    )


def _doc(document_id, page_id, page_var, parent, frame_selector=None, frame_id=None, frame_name=None):
    return ReplicaDocument(
        document_id=document_id,
        page_id=page_id,
        page_var=page_var,
        page_kind="main" if parent is None else "popup",
        parent_document_id=parent,
        frame_selector=frame_selector,
        frame_id=frame_id,
        frame_name=frame_name,
        viewport={"width": 800, "height": 600},
        device_scale_factor=1.0,
        screenshot_scale="css",
        scroll_x=0.0,
        scroll_y=0.0,
        screenshot_asset_relpath="assets/x.png",
        screenshot_sha256="h",
        screenshot_size_bytes=1,
    )


def _recipe(page_var, frame_hops):
    return LocatorRecipe(
        source_expression="x",
        page_var=page_var,
        frame_chain=[FrameHop(selector=s, frame_id=fi, frame_name=fn) for (s, fi, fn) in frame_hops],
        locator_kind="css",
        locator_args={"args": ["x"]},
        ordinal_op=None,
        ordinal_value=None,
    )


class BranchTopologyRemapTests(unittest.TestCase):
    """Suggestion #1: opener_page_id must remap with the page graph."""

    def test_opener_page_id_remapped_together_with_page_and_entry_doc(self):
        pages = [
            _page("p_000", "page", "d_p_000_root", True, opener=None),
            _page("p_001", "page1", "d_p_001_root", False, opener="p_000"),
        ]
        docs = [_doc("d_p_000_root", "p_000", "page", None)]
        new_pages, _new_docs = _branch_topology(pages, docs, "b007")
        by_var = {p.page_var: p for p in new_pages}
        # page_id and entry_document_id got the branch prefix...
        self.assertEqual(by_var["page"].page_id, "b007__p_000")
        self.assertEqual(by_var["page"].entry_document_id, "b007__d_p_000_root")
        # ...and the popup's opener_page_id points at the REMAPPED main page.
        self.assertEqual(by_var["page1"].opener_page_id, "b007__p_000")
        self.assertEqual(by_var["page1"].page_id, "b007__p_001")


class DocumentIdForRecipeTests(unittest.TestCase):
    """Suggestion #3 resolution: the owning-document resolver is deterministic."""

    def test_resolves_inner_frame_owner_from_frame_chain(self):
        pages = [_page("p_000", "page", "d_p_000_root", True)]
        docs = [
            _doc("d_p_000_root", "p_000", "page", None),
            _doc("d_p_000_f_001", "p_000", "page", "d_p_000_root",
                 frame_selector="#viewer-frame", frame_id="viewer-frame", frame_name=None),
        ]
        recipe = _recipe("page", [("#viewer-frame", "viewer-frame", None)])
        self.assertEqual(_document_id_for_recipe(recipe, pages, docs), "d_p_000_f_001")

    def test_empty_frame_chain_resolves_to_page_entry_document(self):
        pages = [_page("p_000", "page", "d_p_000_root", True)]
        docs = [_doc("d_p_000_root", "p_000", "page", None)]
        self.assertEqual(_document_id_for_recipe(_recipe("page", []), pages, docs), "d_p_000_root")

    def test_ambiguous_hop_returns_none_not_a_guess(self):
        pages = [_page("p_000", "page", "d_p_000_root", True)]
        docs = [
            _doc("d_p_000_root", "p_000", "page", None),
            _doc("d_p_000_f_001", "p_000", "page", "d_p_000_root",
                 frame_selector="#same-frame", frame_id="same-frame", frame_name=None),
            _doc("d_p_000_f_002", "p_000", "page", "d_p_000_root",
                 frame_selector="#same-frame", frame_id="same-frame", frame_name=None),
        ]
        recipe = _recipe("page", [("#same-frame", "same-frame", None)])
        self.assertIsNone(_document_id_for_recipe(recipe, pages, docs))

    def test_unresolvable_page_returns_none(self):
        pages = [_page("p_000", "page", "d_p_000_root", True)]
        docs = [_doc("d_p_000_root", "p_000", "page", None)]
        # A recipe whose page_var is not present cannot resolve.
        recipe = _recipe("page1", [])
        self.assertIsNone(_document_id_for_recipe(recipe, pages, docs))


class MetaOpenMountTests(unittest.TestCase):
    """Suggestion #3: the merged flow mounts exactly one Meta-open trigger, on the owning document."""

    _TEMPLATE = '''from playwright.sync_api import sync_playwright


def run(page):
    # [MARKER: 序列选择]
    page.locator("#series .item").first.click()
    # [MARKER: Meta 信息工具]
    page.locator("#viewer-frame").content_frame.locator("#meta-open").click()
    page.locator("#meta-close").click()
'''

    def _write_branch(self, branch_dir, branch_id):
        branch_dir.mkdir(parents=True, exist_ok=True)
        (branch_dir / "status.json").write_text(json.dumps({
            "branch_id": branch_id,
            "capture_status": "captured",
            "source_member_id": "m1",
            "warning": None,
            "fail_stage": None,
        }), encoding="utf-8")
        (branch_dir / "descriptor.json").write_text(json.dumps({
            "series_key": "1.2.3.9", "label": "Series A", "ordinal": 0,
            "document_id": "d_series_hub", "member_id": "m1",
            "stable_attributes": {"data-series-uid": "1.2.3.9"},
            "selected": False, "explicit_frame_count": None, "inferred_frame_count": None,
            "activation": "click",
        }), encoding="utf-8")

        # Viewer topology: root document + an inner frame document owning the Meta
        # trigger. Only the inner frame doc carries frame identity.
        root = _doc("d_p_000_root", "p_000", "page", None)
        inner = _doc("d_p_000_f_001", "p_000", "page", "d_p_000_root",
                     frame_selector="#viewer-frame", frame_id="viewer-frame", frame_name=None)
        pages = [_page("p_000", "page", "d_p_000_root", True)]
        self._write_topo(branch_dir / "viewer", pages, [root, inner])

        meta_doc = _doc("d_p_000_root", "p_000", "page", None)
        self._write_topo(branch_dir / "metadata", [_page("p_000", "page", "d_p_000_root", True)], [meta_doc])
        (branch_dir / "metadata" / "metadata_rows.json").write_text(json.dumps({
            "rows": [{"row_text_not_found": "series number"}], "outer_html": "",
        }), encoding="utf-8")

    @staticmethod
    def _write_topo(subdir, pages, docs):
        subdir.mkdir(parents=True, exist_ok=True)
        import dataclasses
        from replica_models import ActionTarget

        def doc_dict(d):
            payload = dataclasses.asdict(d)
            payload["targets"] = [dataclasses.asdict(t) for t in d.targets]
            payload["regions"] = []
            for region in d.regions:
                region_payload = dataclasses.asdict(region)
                region_payload["root"] = dataclasses.asdict(region.root)
                region_payload["members"] = []
                region_payload["series_collection"] = dataclasses.asdict(region.series_collection) if region.series_collection else None
                payload["regions"].append(region_payload)
            return payload

        (subdir / "topology.json").write_text(json.dumps({
            "pages": [dataclasses.asdict(p) for p in pages],
            "documents": [doc_dict(d) for d in docs],
        }), encoding="utf-8")

    def test_build_branches_mounts_meta_open_once_on_owning_document(self):
        plan = parse_action_plan(self._TEMPLATE)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture_root = root / "capture"
            branch_dir = capture_root / "series_branches" / "b009_tail"
            self._write_branch(branch_dir, "b009_tail")

            snapshots, _warnings, _exp = _load_series_branch_snapshots(capture_root)
            self.assertEqual(len(snapshots), 1)

            states = [ReplicaState(
                "s_000", 0, "", "page", [], [],
                [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"))
            ]
            branches, _evidence = _build_branches_into_flow(states, capture_root, plan, [])
            self.assertEqual(len(branches), 1)

            viewer_state = next(s for s in states if s.state_id == branches[0].viewer_state_id)
            triggers = [
                (doc.document_id, target)
                for doc in viewer_state.documents
                for target in doc.targets
                if target.action_id == f"series:b009_tail:meta_open"
            ]
            # Exactly one trigger, mounted on the OWNING (inner frame) document —
            # never duplicated onto the root or any sibling.
            self.assertEqual(len(triggers), 1, "meta-open trigger must be mounted exactly once")
            docs_by_id = {d.document_id: d for d in viewer_state.documents}
            owner_id, target = triggers[0]
            self.assertTrue(docs_by_id[owner_id].frame_id == "viewer-frame",
                            "meta-open trigger must live on the inner frame document that owns it")
            self.assertEqual(target.document_id, owner_id)


if __name__ == "__main__":
    unittest.main()
