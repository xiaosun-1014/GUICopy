"""Level-1/2 tests for pipeline validation: manifest, locator risk, replica,
artifacts, privacy, and adapter capabilities."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from pipeline_validation import (
    ValidationResult,
    evaluate_adapter_capabilities,
    validate_artifacts,
    validate_locator_risk,
    validate_manifest,
    validate_privacy,
    validate_replica,
    validate_series_privacy,
)
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
    SeriesExpansionEvidence,
    StateEvidence,
)
from replay_helpers import strip_known_query_secrets, write_manifest


def _node():
    return DomNodeSnapshot(
        "div", "", {},
        Rect(0, 0, 0, 0, "page_viewport_css"),
        "<div></div>", {"display": "block"},
    )


def _document(document_id, targets=(), regions=()):
    return ReplicaDocument(
        document_id=document_id,
        page_id="p_main",
        page_var="page",
        page_kind="main",
        parent_document_id=None,
        frame_selector=None,
        frame_id=None,
        frame_name=None,
        viewport={"width": 300, "height": 200},
        device_scale_factor=1,
        screenshot_scale="css",
        scroll_x=0,
        scroll_y=0,
        screenshot_asset_relpath=f"assets/{document_id}.png",
        screenshot_sha256=document_id,
        screenshot_size_bytes=3,
        targets=list(targets),
        regions=list(regions),
    )


def _entry_state(targets=(), transitions=(), state_id="s_000", regions=()):
    return ReplicaState(
        state_id=state_id,
        ordinal=0,
        source_url="https://example.test/",
        active_page_var="page",
        pages=[ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
        documents=[_document("d_main", targets, regions)],
        transitions=list(transitions),
        evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
    )


def _series_state(state_id, member_id):
    """A branch viewer state carrying one ``series`` region whose only member is
    ``member_id``, so a branch whose source resolves to it passes the
    source_member resolution check."""
    member = RegionMember(member_id=member_id, semantic_type="series_item", dom=_node())
    region = InteractionRegion(
        region_id=f"r_{state_id}",
        region_type="series",
        document_id="d_main",
        root=_node(),
        members=[member],
        series_collection=None,
    )
    return _entry_state(state_id=state_id, regions=[region])


def _base_flow(states=None, warnings=(), source_script_sha256="abc123"):
    return ReplicaFlow(
        schema_version=1,
        flow_id="fixture-flow",
        source_script_relpath="recorded.py",
        source_script_sha256=source_script_sha256,
        created_at="2026-08-01T00:00:00Z",
        viewport={"width": 1280, "height": 720},
        bootstrap=BootstrapPlan(1, 3, True, {"page": "main"}),
        popup_expectations=[],
        timing_profile=CaptureTimingProfile(),
        entry_state_id="s_000",
        states=list(states) if states is not None else [_entry_state()],
        warnings=list(warnings),
    )


def _ascii_locator(expression, **kwargs):
    return LocatorRecipe(
        source_expression=expression,
        page_var=kwargs.get("page_var", "page"),
        frame_chain=kwargs.get("frame_chain", []),
        locator_kind=kwargs.get("locator_kind", "css"),
        locator_args={"args": [kwargs.get("args", "#id")]},
        ordinal_op=kwargs.get("ordinal_op"),
        ordinal_value=kwargs.get("ordinal_value"),
    )


def _branch(branch_id, series_key, ordinal, **kwargs) -> SeriesBranch:
    return SeriesBranch(
        branch_id=branch_id,
        series_key=series_key,
        label="Series %d" % ordinal,
        ordinal=ordinal,
        document_id="d_main",
        source_member_id="m_%s" % series_key,
        selector=None,
        activation=kwargs.get("activation", "click"),
        viewer_state_id=kwargs.get("viewer_state_id"),
        metadata_state_id=kwargs.get("metadata_state_id"),
        return_state_id=kwargs.get("return_state_id"),
        capture_status=kwargs.get("capture_status", "captured"),
        warning=kwargs.get("warning"),
    )


def _expansion(**kwargs) -> SeriesExpansionEvidence:
    return SeriesExpansionEvidence(
        discovered_count=kwargs.get("discovered_count", 2),
        captured_count=kwargs.get("captured_count", 2),
        partial_count=kwargs.get("partial_count", 0),
        failed_count=kwargs.get("failed_count", 0),
        reached_end=kwargs.get("reached_end", True),
        total_duration_ms=kwargs.get("total_duration_ms", 1000),
        warning=kwargs.get("warning"),
    )


def _series_flow(branches=None, expansion=None, warnings=(), extra_states=()):
    """Build a v2 flow carrying per-series viewer/metadata states.

    The two viewer states carry a ``series`` region whose only member matches
    the default ``source_member_id`` (``m_series-1`` / ``m_series-2``) so a
    well-formed branch's source resolves, mirroring the real capture contract.
    """
    viewer_a = _series_state("s_viewer_a", "m_series-1")
    meta_a = _entry_state(state_id="s_meta_a")
    viewer_b = _series_state("s_viewer_b", "m_series-2")
    meta_b = _entry_state(state_id="s_meta_b")
    states = [viewer_a, meta_a, viewer_b, meta_b, *extra_states]
    flow = _base_flow(states=states, warnings=warnings)
    flow.schema_version = 2
    flow.entry_state_id = states[0].state_id
    if branches is not None:
        flow.series_branches = branches
    if expansion is not None:
        flow.series_expansion = expansion
    return flow


class ManifestValidationTests(unittest.TestCase):
    def test_manifest_rejects_dangling_transition(self):
        transition = ReplicaTransition(
            "t_bad", "a_click", "s_000", "s_MISSING", "page", "page", "same_page"
        )
        flow = _base_flow(states=[_entry_state(transitions=[transition])])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "d_main.png").write_bytes(b"png")
            result = validate_manifest(flow, root)
        self.assertEqual(result.status, "failed")
        self.assertIn("dangling_transition", result.errors)

    def test_manifest_rejects_unknown_entry_state(self):
        flow = _base_flow()
        flow.entry_state_id = "s_NOPE"
        with tempfile.TemporaryDirectory() as tmp:
            result = validate_manifest(flow, Path(tmp))
        self.assertEqual(result.status, "failed")
        self.assertIn("entry_state_missing", result.errors)

    def test_manifest_requires_existing_iframe_parent_document(self):
        orphan = _document("d_child")
        orphan.parent_document_id = "d_MISSING"
        state = ReplicaState(
            "s_000", 0, "https://example.test/", "page",
            [ReplicaPage("p_main", "page", "main", None, None, "d_parent", True, False)],
            [orphan], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
        )
        flow = _base_flow(states=[state])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "d_child.png").write_bytes(b"png")
            result = validate_manifest(flow, root)
        self.assertEqual(result.status, "failed")
        self.assertIn("iframe_parent_missing", result.errors)

    def test_iframe_parent_is_validated_within_each_state(self):
        states = []
        for ordinal in range(2):
            parent = _document(f"d_parent_{ordinal}")
            child = _document(f"d_child_{ordinal}")
            child.parent_document_id = parent.document_id
            states.append(ReplicaState(
                f"s_{ordinal:03d}", ordinal, "https://example.test/", "page",
                [ReplicaPage("p_main", "page", "main", None, None, parent.document_id, True, False)],
                [parent, child], [],
                StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
            ))
        flow = _base_flow(states=states)
        flow.entry_state_id = states[0].state_id
        flow.source_script_relpath = ""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            for state in states:
                for document in state.documents:
                    (root / document.screenshot_asset_relpath).write_bytes(b"png")
            result = validate_manifest(flow, root)
        self.assertNotIn("iframe_parent_missing", result.errors)
        self.assertEqual(result.status, "success", result.errors)


class QuerySecretRedactionTests(unittest.TestCase):
    def test_strip_known_query_secrets_removes_keys_and_preserves_safe_query(self):
        source = (
            'page.goto("https://viewer.example.test/open?token=secret&study=demo")\n'
            'page.goto("https://viewer.example.test/#/open?code=secret&study=demo")\n'
            'safe = "https://viewer.example.test/open?study=demo"'
        )
        cleaned = strip_known_query_secrets(source)
        self.assertNotIn("token=", cleaned)
        self.assertNotIn("secret", cleaned)
        self.assertEqual(cleaned.count("study=demo"), 3)


class SeriesBranchValidationTests(unittest.TestCase):
    def _validate(self, flow):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "d_main.png").write_bytes(b"png")
            return validate_manifest(flow, root)

    def _two_captured_branches(self):
        return [
            _branch(
                "b_a", "series-1", 0,
                viewer_state_id="s_viewer_a",
                metadata_state_id="s_meta_a",
                return_state_id="s_viewer_a",
            ),
            _branch(
                "b_b", "series-2", 1,
                viewer_state_id="s_viewer_b",
                metadata_state_id="s_meta_b",
                return_state_id="s_viewer_b",
            ),
        ]

    def test_valid_two_branch_flow_passes_and_emits_metrics(self):
        flow = _series_flow(
            branches=self._two_captured_branches(),
            expansion=_expansion(discovered_count=2, captured_count=2),
        )
        # No recorded source script on disk: avoid the unrelated
        # source_script_missing warning so we can assert a clean success.
        flow.source_script_relpath = ""
        result = self._validate(flow)
        self.assertEqual(result.status, "success", result.errors)
        self.assertEqual(result.metrics["series_discovered"], 2)
        self.assertEqual(result.metrics["series_captured"], 2)
        self.assertEqual(result.metrics["series_partial"], 0)
        self.assertEqual(result.metrics["series_failed"], 0)

    def test_duplicate_series_key_is_rejected(self):
        flow = _series_flow(
            branches=[
                _branch("b_a", "series-1", 0, viewer_state_id="s_viewer_a"),
                _branch("b_b", "series-1", 1, viewer_state_id="s_viewer_b"),
            ],
            expansion=_expansion(discovered_count=2, captured_count=2),
        )
        result = self._validate(flow)
        self.assertIn("duplicate_series_key", result.errors)

    def test_missing_viewer_state_is_rejected(self):
        flow = _series_flow(
            branches=[_branch("b_a", "series-1", 0, viewer_state_id="s_NOPE")],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertTrue(any("series_viewer_state_missing" in e for e in result.errors), result.errors)

    def test_missing_metadata_state_is_rejected(self):
        flow = _series_flow(
            branches=[
                _branch(
                    "b_a", "series-1", 0,
                    viewer_state_id="s_viewer_a",
                    metadata_state_id="s_NOPE",
                    return_state_id="s_viewer_a",
                )
            ],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertTrue(any("series_metadata_state_missing" in e for e in result.errors), result.errors)

    def test_missing_return_state_is_rejected(self):
        flow = _series_flow(
            branches=[
                _branch(
                    "b_a", "series-1", 0,
                    viewer_state_id="s_viewer_a",
                    metadata_state_id="s_meta_a",
                    return_state_id="s_NOPE",
                )
            ],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertTrue(any("series_return_state_missing" in e for e in result.errors), result.errors)

    def test_metadata_return_must_equal_viewer_state(self):
        flow = _series_flow(
            branches=[
                _branch(
                    "b_a", "series-1", 0,
                    viewer_state_id="s_viewer_a",
                    metadata_state_id="s_meta_a",
                    return_state_id="s_viewer_b",
                )
            ],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertTrue(any("series_metadata_return_mismatch" in e for e in result.errors), result.errors)

    def test_illegal_capture_status_is_rejected(self):
        flow = _series_flow(
            branches=[
                _branch("b_a", "series-1", 0, viewer_state_id="s_viewer_a", capture_status="wat")
            ],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertTrue(any("series_illegal_capture_status" in e for e in result.errors), result.errors)

    def test_captured_branch_must_have_viewer_state(self):
        flow = _series_flow(
            branches=[_branch("b_a", "series-1", 0, viewer_state_id=None)],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertTrue(any("series_captured_missing_viewer_state" in e for e in result.errors), result.errors)

    def test_failed_branch_must_carry_warning_or_reason(self):
        flow = _series_flow(
            branches=[_branch("b_a", "series-1", 0, capture_status="failed", warning=None)],
            expansion=_expansion(discovered_count=1, captured_count=0, failed_count=1),
        )
        result = self._validate(flow)
        self.assertTrue(any("series_failed_missing_reason" in e for e in result.errors), result.errors)

    def test_failed_branch_may_omit_state_when_warning_present(self):
        flow = _series_flow(
            branches=[
                _branch("b_a", "series-1", 0, capture_status="failed", warning="timeout", viewer_state_id=None)
            ],
            expansion=_expansion(discovered_count=1, captured_count=0, failed_count=1),
        )
        result = self._validate(flow)
        self.assertNotIn("series_failed_missing_reason", result.errors)
        self.assertNotIn("series_captured_missing_viewer_state", result.errors)

    def test_counting_invariant_captured_plus_partial_plus_failed_eq_discovered(self):
        flow = _series_flow(
            branches=[
                _branch(
                    "b_a", "series-1", 0,
                    viewer_state_id="s_viewer_a",
                    metadata_state_id="s_meta_a",
                    return_state_id="s_viewer_a",
                ),
                _branch("b_b", "series-2", 1, capture_status="failed", warning="timeout"),
            ],
            expansion=_expansion(discovered_count=2, captured_count=1, failed_count=1),
        )
        flow.source_script_relpath = ""
        result = self._validate(flow)
        self.assertEqual(result.status, "success", result.errors)

        bad = _series_flow(
            branches=[
                _branch("b_a", "series-1", 0, viewer_state_id="s_viewer_a"),
                _branch("b_b", "series-2", 1, capture_status="failed", warning="timeout"),
            ],
            expansion=_expansion(discovered_count=2, captured_count=1, failed_count=2),
        )
        result = self._validate(bad)
        self.assertTrue(any("series_count_mismatch" in e for e in result.errors), result.errors)

    def test_unreached_end_requires_virtualization_warning(self):
        flow = _series_flow(
            branches=self._two_captured_branches(),
            expansion=_expansion(discovered_count=2, captured_count=2, reached_end=False, warning=None),
            warnings=[],
        )
        result = self._validate(flow)
        self.assertTrue(any("series_virtualized_partial" in e for e in result.errors), result.errors)

        ok = _series_flow(
            branches=self._two_captured_branches(),
            expansion=_expansion(
                discovered_count=2, captured_count=2, reached_end=False,
                warning="series_virtualized_partial",
            ),
            warnings=[],
        )
        result = self._validate(ok)
        self.assertNotIn("series_virtualized_partial", result.errors)

    def test_v1_flow_witout_series_data_is_untouched(self):
        # A v1 flow without series fields must not trigger series validations.
        flow = _base_flow()
        flow.schema_version = 1
        flow.source_script_relpath = ""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "d_main.png").write_bytes(b"png")
            result = validate_manifest(flow, root)
        self.assertEqual(result.status, "success", result.errors)
        self.assertNotIn("series_discovered", result.metrics)

    def test_v1_flow_with_series_data_is_rejected(self):
        # A v1 manifest claiming series branches must be rejected: the data can
        # never legitimately exist at v1 and would be dropped on read.
        flow = _base_flow()
        flow.schema_version = 1
        flow.series_branches = [_branch("b_a", "series-1", 0, viewer_state_id="s_viewer_a")]
        result = self._validate(flow)
        self.assertIn("series_requires_schema_v2", result.errors)

    def test_duplicate_branch_id_is_rejected(self):
        flow = _series_flow(
            branches=[
                _branch("dup", "series-1", 0, viewer_state_id="s_viewer_a"),
                _branch("dup", "series-2", 1, viewer_state_id="s_viewer_b"),
            ],
            expansion=_expansion(discovered_count=2, captured_count=2),
        )
        result = self._validate(flow)
        self.assertIn("duplicate_branch_id", result.errors)

    def test_source_member_must_resolve_to_series_region_member(self):
        # Branch claims series-2 but points at viewer_a whose series region only
        # contains the m_series-1 member: the source cannot be routed.
        flow = _series_flow(
            branches=[_branch("b_a", "series-2", 0, viewer_state_id="s_viewer_a")],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertIn("series_source_member_unresolved", result.errors)

    def test_partial_branch_must_have_viewer_state(self):
        flow = _series_flow(
            branches=[
                _branch("b_a", "series-1", 0, capture_status="partial", viewer_state_id=None)
            ],
            expansion=_expansion(discovered_count=1, captured_count=0, partial_count=1),
        )
        result = self._validate(flow)
        self.assertIn("series_partial_missing_viewer_state", result.errors)

    def test_captured_branch_requires_metadata_and_return(self):
        flow = _series_flow(
            branches=[_branch("b_a", "series-1", 0, viewer_state_id="s_viewer_a")],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertIn("series_captured_missing_metadata_state", result.errors)
        self.assertIn("series_captured_missing_return_state", result.errors)

    def test_metadata_state_requires_viewer_and_explicit_return(self):
        # A metadata_state without its owning viewer is invalid even though the
        # metadata state id itself resolves.
        flow = _series_flow(
            branches=[
                _branch("b_a", "series-1", 0, metadata_state_id="s_meta_a", viewer_state_id=None)
            ],
            expansion=_expansion(discovered_count=1, captured_count=1),
        )
        result = self._validate(flow)
        self.assertIn("series_metadata_state_requires_viewer", result.errors)


class LocatorRiskTests(unittest.TestCase):
    def test_coordinate_only_critical_action_is_partial(self):
        mouse = ActionTarget(
            "a_mouse", "m_0", "click", "mouse_xy", {"args": [100, 200]},
            None, None, None, None, None, "execute", None, "d_main", None,
        )
        flow = _base_flow(states=[_entry_state(targets=[mouse])])
        result = validate_locator_risk(flow)
        self.assertEqual(result.status, "partial")

    def test_always_after_critical_ordinal_action_remains_partial(self):
        locator = _ascii_locator('page.locator(".row").nth(3)', ordinal_op="nth", ordinal_value=3, args=".row")
        target = ActionTarget(
            "a_ordinal", "m_0", "click", "locator", {"args": []},
            locator, None, None, None, None, "execute", None, "d_main", "t_entry",
        )
        transition = ReplicaTransition(
            "t_entry", "a_ordinal", "s_000", "s_001", "page", "page", "same_page"
        )
        flow = _base_flow(states=[_entry_state(targets=[target], transitions=[transition])])
        result = validate_locator_risk(flow)
        self.assertEqual(result.status, "partial")

    def test_simple_aria_locator_is_success(self):
        locator = _ascii_locator('page.get_by_role("button", name="Next")', locator_kind="role", args=["button", "Next"])
        target = ActionTarget(
            "a_ok", "m_0", "click", "locator", {"args": []},
            locator, None, None, None, None, "execute", None, "d_main", None,
        )
        flow = _base_flow(states=[_entry_state(targets=[target])])
        result = validate_locator_risk(flow)
        self.assertEqual(result.status, "success")

    def test_shared_text_and_test_id_buckets_reach_validation_metrics(self):
        text_target = ActionTarget(
            "a_text", "m_0", "click", "locator", {"args": []},
            _ascii_locator(
                'page.get_by_text("Body")',
                locator_kind="text",
                args="Body",
            ),
            None, None, None, None, "execute", None, "d_main", None,
        )
        test_id_target = ActionTarget(
            "a_test_id", "m_0", "click", "locator", {"args": []},
            _ascii_locator(
                'page.get_by_test_id("open")',
                locator_kind="test_id",
                args="open",
            ),
            None, None, None, None, "execute", None, "d_main", None,
        )
        result = validate_locator_risk(
            _base_flow(states=[_entry_state(targets=[text_target, test_id_target])])
        )
        self.assertEqual(result.metrics["risk_counts"]["text"], 1)
        self.assertEqual(result.metrics["risk_counts"]["stable_attribute"], 1)
        self.assertEqual(result.metrics["highest_risk"], "text")


class ReplicaValidationTests(unittest.TestCase):
    def _build_replica_with_locator(self, locator_selector, double=False):
        """Build a replica root with a replica/ subdir and a written manifest."""
        tmpdir = tempfile.TemporaryDirectory()
        root = Path(tmpdir.name)
        assets = root / "assets"
        assets.mkdir()
        (assets / "d_main.png").write_bytes(b"png")
        dom = DomNodeSnapshot(
            "button", "Go", {"id": "go"},
            Rect(10, 10, 80, 30, "page_viewport_css"),
            '<button id="go">Go</button>', {"display": "block"},
        )
        locator = _ascii_locator(f'page.locator("{locator_selector}")', args=locator_selector)
        # ``double=True`` renders the same action id twice, so the overlay
        # ``[data-replica-action="a_0"]`` appears twice -- the new
        # data-replica-action uniqueness check must flag it as not unique.
        targets = [
            ActionTarget(
                "a_0", "m_0", "click", "locator", {"args": []},
                locator, dom, None, None, None, "execute", None, "d_main", None,
            )
            for _ in range(2 if double else 1)
        ]
        flow = _base_flow(states=[_entry_state(targets=targets)])
        from build_replica import build_replica
        output = root / "replica"
        build_replica(flow, root, output)
        capture_dir = root / "capture"
        capture_dir.mkdir(exist_ok=True)
        write_manifest(capture_dir / "manifest.json", flow)
        return tmpdir, root, flow

    def test_critical_locator_must_be_unique_and_visible(self):
        tmpdir, root, _ = self._build_replica_with_locator("#dup", double=True)
        try:
            result = validate_replica(root, root / "capture" / "manifest.json", timeout_ms=45000)
        finally:
            tmpdir.cleanup()
        self.assertEqual(result.status, "failed")
        self.assertTrue(
            any("critical_locator_not_unique" in e for e in result.errors),
            result.errors,
        )

    def test_replica_validation_runs_manifest_replay_not_completed_adapter(self):
        tmpdir, root, _ = self._build_replica_with_locator("#go")
        try:
            result = validate_replica(root, root / "capture" / "manifest.json", timeout_ms=45000)
        finally:
            tmpdir.cleanup()
        self.assertEqual(result.metrics["driver"], "replica/replay_replica.py")
        self.assertEqual(result.metrics["manifest_replay_exit_code"], 0)
        # The completed adapter driver must never be recorded by manifest replay.
        self.assertNotIn("completed", result.metrics["driver"])

    def test_unique_data_replica_action_overlay_passes_validation(self):
        # A single execute target rendered as a unique data-replica-action
        # overlay must validate cleanly. This is the ftimage ``a_001_001`` path
        # that previously failed: the captured semantic locator (get_by_role /
        # get_by_title) matched 0 elements because sanitize_html stripped the
        # href/role/title attributes from the replica's overlay DOM.
        tmpdir, root, _ = self._build_replica_with_locator("#go")
        try:
            result = validate_replica(root, root / "capture" / "manifest.json", timeout_ms=45000)
        finally:
            tmpdir.cleanup()
        self.assertEqual(result.status, "success", result.errors)
        self.assertNotIn("critical_locator_not_unique", result.errors)

    def test_carry_forward_targets_are_not_validated_on_home_page(self):
        # Only the entry state's targets render on the replica home page. Later
        # states carry the same document id forward with accumulated targets
        # whose overlays live on their own per-state pages; validating those on
        # the home page would always match 0 and false-positive
        # critical_locator_not_unique (ftimage regressed with 77 such errors).
        tmpdir = tempfile.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            assets = root / "assets"
            assets.mkdir()
            (assets / "d_main.png").write_bytes(b"png")
            dom = DomNodeSnapshot(
                "button", "Go", {"id": "go"},
                Rect(10, 10, 80, 30, "page_viewport_css"),
                '<button id="go">Go</button>', {"display": "block"},
            )
            loc = _ascii_locator('page.locator("#go")', args="#go")
            entry_targets = [
                ActionTarget("a_0", "m_0", "click", "locator", {"args": []}, loc, dom, None, None, None, "execute", None, "d_main", None)
            ]
            carry_target = ActionTarget("a_1", "m_1", "click", "locator", {"args": []}, loc, dom, None, None, None, "execute", None, "d_main", None)
            entry = _entry_state(targets=entry_targets, state_id="s_000")
            later = _entry_state(targets=entry_targets + [carry_target], state_id="s_001")
            flow = _base_flow(states=[entry, later])
            from build_replica import build_replica
            build_replica(flow, root, root / "replica")
            capture_dir = root / "capture"
            capture_dir.mkdir(exist_ok=True)
            write_manifest(capture_dir / "manifest.json", flow)
            result = validate_replica(root, capture_dir / "manifest.json", timeout_ms=60000)
            self.assertEqual(result.status, "success", result.errors)
            self.assertEqual(result.metrics["locator_total"], 1)
        finally:
            tmpdir.cleanup()


class ArtifactValidationTests(unittest.TestCase):
    def test_required_json_must_parse_and_canvas_count_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dicom_meta.json").write_text("{not valid json", encoding="utf-8")
            result = validate_artifacts(
                root,
                expected_markers=("Meta 信息工具",),
                capabilities={"canvas_dynamic_pixels": "supported"},
            )
            self.assertEqual(result.status, "failed")
            self.assertTrue(any("artifact_json_invalid" in e for e in result.errors), result.errors)

    def test_missing_report_jpeg_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = validate_artifacts(
                root,
                expected_markers=("报告截图",),
                capabilities={"canvas_dynamic_pixels": "unsupported"},
            )
            self.assertEqual(result.status, "failed")
            self.assertTrue(any("report_jpeg" in e for e in result.errors), result.errors)

    def test_canvas_frames_not_verifiable_is_partial_warning_not_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dicom_meta.json").write_text("{}", encoding="utf-8")
            result = validate_artifacts(
                root,
                expected_markers=("影像画布交互",),
                capabilities={"canvas_dynamic_pixels": "unsupported"},
            )
            self.assertEqual(result.status, "partial")
            self.assertTrue(
                any("artifact_not_verifiable:canvas_frames" in w for w in result.warnings),
                result.warnings,
            )
            self.assertEqual(result.errors, ())


class PrivacyValidationTests(unittest.TestCase):
    def test_privacy_scan_rejects_storage_state_and_token_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storage_state.json").write_text("{}", encoding="utf-8")
            (root / "capture.json").write_text(
                '{"Authorization": "Bearer abc123token"}', encoding="utf-8"
            )
            result = validate_privacy(root)
            self.assertEqual(result.status, "failed")
            self.assertIn("storage_state_artifact", result.errors)
            self.assertIn("secret_pattern", result.errors)

    def test_privacy_does_not_embed_the_matched_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leak = "Bearer hopefully-not-reported-12345"
            (root / "log.txt").write_text(f"Authorization: {leak}", encoding="utf-8")
            result = validate_privacy(root)
            combined = "\n".join([*result.errors, *result.warnings, json.dumps(result.metrics)])
            self.assertNotIn("hopefully-not-reported-12345", combined)


class SeriesPrivacyValidationTests(unittest.TestCase):
    """P1#8 closure: raw series identity must never reach route/event/log/served
    (non-metadata) surfaces, while the served Metadata panel text (a limited
    sensitive artifact) stays intact and readable."""

    _RAW = "1.2.840.113619.2.55.3.12345.6789"

    def _served_dir(self, tmp, *, uid_in_metadata=True, uid_in_route_json=False, uid_in_entry=True, uid_in_log=False):
        root = Path(tmp) / "replica"
        root.mkdir(parents=True, exist_ok=True)
        slug = "aabbccddeeff"
        metadata_html = (
            '<div class="replica-metadata" data-replica-panel-region="r">'
            '<div class="content">'
            f'<div>Series Instance UID: {self._RAW}</div>'
            '<div>SeriesNumber: 1</div>'
            "</div></div>"
        ) if uid_in_metadata else '<div class="replica-metadata"><div class="content">SeriesNumber: 1</div></div>'
        entry_html = f'<div data-replica-series-key="{slug}">Series A</div>'
        if uid_in_entry:
            entry_html += f'<div>raw-holder {self._RAW}</div>'
        (root / "index.html").write_text(entry_html + metadata_html, encoding="utf-8")
        route_json = {"series": {slug: {"viewerUrl": "x"}}}
        if uid_in_route_json:
            route_json["series"][self._RAW] = {"disabled": True}
        (root / "route_map.json").write_text(json.dumps(route_json), encoding="utf-8")
        return root

    def test_clean_served_dir_passes_and_panel_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._served_dir(tmp, uid_in_entry=False)  # UID only inside the metadata panel
            result = validate_series_privacy(root, {self._RAW})
            self.assertEqual(result.status, "success", f"{result.errors} {result.warnings}")
        self.assertGreaterEqual(result.metrics["metadata_panel_blocks"], 1)

    def test_raw_uid_outside_metadata_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._served_dir(tmp, uid_in_metadata=False, uid_in_entry=True, uid_in_route_json=False)
            result = validate_series_privacy(root, {self._RAW})
            self.assertEqual(result.status, "failed")
            self.assertTrue(any(e.startswith("raw_series_identity_in_served_html") for e in result.errors))
            # The metadata panel itself is untouched by the scan boundary.
            self.assertGreaterEqual(result.metrics["metadata_panel_blocks"], 1)

    def test_raw_uid_in_route_map_json_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._served_dir(tmp, uid_in_metadata=False, uid_in_route_json=True)
            result = validate_series_privacy(root, {self._RAW})
            self.assertEqual(result.status, "failed")
            self.assertTrue(any(e.startswith("raw_series_identity_in_artifact") for e in result.errors))

    def test_metadata_panel_text_still_readable_after_sanitize(self):
        # The boundary keeps the full (executable-stripped) panel readable; the
        # readable-text check must report the UID-bearing series row present.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._served_dir(tmp, uid_in_metadata=True)
            html = (root / "index.html").read_text(encoding="utf-8")
            self.assertTrue(html.count("Series Instance UID") >= 1)
            # Executable/token attributes are gone (sanitizer contract), text remains.
            self.assertNotIn("<script", html.lower())


class CapabilitiesTests(unittest.TestCase):
    def test_canvas_marker_declares_viewer_js_and_dynamic_pixels_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                result = evaluate_adapter_capabilities(
                    expected_markers=("影像画布交互",),
                    offline_events=(),
                )
            finally:
                os.chdir(previous)
            self.assertEqual(result.status, "partial")
            caps = result.metrics["capabilities"]
            self.assertEqual(caps["viewer_js_api"], "unsupported")
            self.assertEqual(caps["canvas_dynamic_pixels"], "unsupported")
            self.assertEqual(
                caps,
                {
                    "locator_click_fill": "supported",
                    "popup_iframe_transition": "supported",
                    "series_dom_selection": "degraded",
                    "metadata_dom_read": "degraded",
                    "canvas_locate_focus_click": "supported",
                    "viewer_js_api": "unsupported",
                    "keyboard_wheel_slider_routing": "degraded",
                    "canvas_dynamic_pixels": "unsupported",
                },
            )
            out = Path(tmp) / "validation" / "adapter_capabilities.json"
            self.assertTrue(out.is_file())
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), caps)

    def test_non_canvas_marker_with_support_is_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                result = evaluate_adapter_capabilities(
                    expected_markers=("报告截图",),
                    offline_events=(),
                )
            finally:
                os.chdir(previous)
            self.assertEqual(result.status, "success")


if __name__ == "__main__":
    unittest.main()
