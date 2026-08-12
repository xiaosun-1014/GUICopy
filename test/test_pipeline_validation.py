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
)
from replica_models import (
    ActionTarget,
    BootstrapPlan,
    CaptureTimingProfile,
    DomNodeSnapshot,
    LocatorRecipe,
    Rect,
    ReplicaDocument,
    ReplicaFlow,
    ReplicaPage,
    ReplicaState,
    ReplicaTransition,
    StateEvidence,
)
from replay_helpers import write_manifest


def _document(document_id, targets=()):
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
        regions=[],
    )


def _entry_state(targets=(), transitions=(), state_id="s_000"):
    return ReplicaState(
        state_id=state_id,
        ordinal=0,
        source_url="https://example.test/",
        active_page_var="page",
        pages=[ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
        documents=[_document("d_main", targets)],
        transitions=list(transitions),
        evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
    )


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
