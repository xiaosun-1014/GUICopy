import tempfile
import unittest
from pathlib import Path

from replica_models import (
    BootstrapPlan,
    CaptureTimingProfile,
    LocatorRecipe,
    ReplicaFlow,
    ReplicaState,
    SeriesBranch,
    SeriesExpansionEvidence,
    StateEvidence,
)
from replay_helpers import read_manifest, sha256_file, write_manifest


def _locator() -> LocatorRecipe:
    return LocatorRecipe(
        source_expression='s.find_by_role("row").locator("xpath=..")',
        page_var="page",
        frame_chain=[],
        locator_kind="xpath",
        locator_args={"args": ["xpath=.."]},
        ordinal_op="nth",
        ordinal_value=2,
    )


def _branch(branch_id, series_key, ordinal, **kwargs) -> SeriesBranch:
    return SeriesBranch(
        branch_id=branch_id,
        series_key=series_key,
        label="Series %d" % ordinal,
        ordinal=ordinal,
        document_id="d_main",
        source_member_id="m_%s" % series_key,
        selector=_locator(),
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
        total_duration_ms=kwargs.get("total_duration_ms", 1200),
        warning=kwargs.get("warning"),
    )


def _v2_flow() -> ReplicaFlow:
    return ReplicaFlow(
        schema_version=2,
        flow_id="multi-series-flow",
        source_script_relpath="recorded.py",
        source_script_sha256="abc123",
        created_at="2026-08-14T00:00:00Z",
        viewport={"width": 1280, "height": 720},
        bootstrap=BootstrapPlan(1, 3, True, {"page": "main"}),
        popup_expectations=[],
        timing_profile=CaptureTimingProfile(),
        entry_state_id="s_branch_a",
        states=[
            ReplicaState(
                state_id="s_branch_a",
                ordinal=0,
                source_url="https://example.test/",
                active_page_var="page",
                pages=[],
                documents=[],
                transitions=[],
                evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
            ),
            ReplicaState(
                state_id="s_branch_a_meta",
                ordinal=1,
                source_url="https://example.test/",
                active_page_var="page",
                pages=[],
                documents=[],
                transitions=[],
                evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
            ),
            ReplicaState(
                state_id="s_branch_b",
                ordinal=2,
                source_url="https://example.test/",
                active_page_var="page",
                pages=[],
                documents=[],
                transitions=[],
                evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
            ),
            ReplicaState(
                state_id="s_branch_b_meta",
                ordinal=3,
                source_url="https://example.test/",
                active_page_var="page",
                pages=[],
                documents=[],
                transitions=[],
                evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
            ),
        ],
        warnings=[],
        series_branches=[
            _branch(
                "b_a", "series-1", 0,
                viewer_state_id="s_branch_a",
                metadata_state_id="s_branch_a_meta",
                return_state_id="s_branch_a",
            ),
            _branch(
                "b_b", "series-2", 1,
                viewer_state_id="s_branch_b",
                metadata_state_id="s_branch_b_meta",
                return_state_id="s_branch_b",
            ),
        ],
        series_expansion=_expansion(),
    )


class ReplicaManifestTests(unittest.TestCase):
    def _flow(self):
        return ReplicaFlow(
            schema_version=1,
            flow_id="fixture-flow",
            source_script_relpath="recorded.py",
            source_script_sha256="abc123",
            created_at="2026-07-29T00:00:00Z",
            viewport={"width": 1280, "height": 720},
            bootstrap=BootstrapPlan(1, 3, True, {"page": "main"}),
            popup_expectations=[],
            timing_profile=CaptureTimingProfile(),
            entry_state_id="s_000",
            states=[
                ReplicaState(
                    state_id="s_000",
                    ordinal=0,
                    source_url="https://example.test/?token=secret",
                    active_page_var="page",
                    pages=[],
                    documents=[],
                    transitions=[],
                    evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )
            ],
            warnings=[],
        )

    def test_manifest_round_trip_uses_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            write_manifest(manifest_path, self._flow())

            loaded = read_manifest(manifest_path, root)

            self.assertEqual(loaded.source_script_relpath, "recorded.py")
            self.assertEqual(loaded.states[0].state_id, "s_000")

    def test_script_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text("print('original')", encoding="utf-8")
            flow = self._flow()
            flow.source_script_sha256 = sha256_file(script)
            manifest_path = root / "manifest.json"
            write_manifest(manifest_path, flow)
            script.write_text("print('changed')", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "hash"):
                read_manifest(manifest_path, root, verify_source_hash=True)


class MultiSeriesManifestTests(unittest.TestCase):
    def test_v2_flow_serializes_series_branches_and_expansion(self):
        flow = _v2_flow()
        data = flow.to_dict()
        self.assertEqual(data["schema_version"], 2)
        branches = data["series_branches"]
        self.assertEqual(len(branches), 2)
        self.assertEqual(branches[0]["branch_id"], "b_a")
        self.assertEqual(branches[0]["viewer_state_id"], "s_branch_a")
        self.assertEqual(branches[0]["metadata_state_id"], "s_branch_a_meta")
        self.assertEqual(branches[0]["return_state_id"], "s_branch_a")
        # selector is itself a nested model: must round-trip as a dict, not be dropped.
        self.assertEqual(branches[0]["selector"]["locator_kind"], "xpath")
        self.assertEqual(branches[1]["series_key"], "series-2")
        expansion = data["series_expansion"]
        self.assertEqual(expansion["discovered_count"], 2)
        self.assertEqual(expansion["captured_count"], 2)
        self.assertTrue(expansion["reached_end"])

    def test_manifest_round_trip_preserves_branch_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            write_manifest(manifest_path, _v2_flow())
            loaded = read_manifest(manifest_path, root)
        self.assertEqual(len(loaded.series_branches), 2)
        for original, loaded_branch in zip(_v2_flow().series_branches, loaded.series_branches):
            self.assertEqual(loaded_branch.branch_id, original.branch_id)
            self.assertEqual(loaded_branch.series_key, original.series_key)
            self.assertEqual(loaded_branch.ordinal, original.ordinal)
            self.assertEqual(loaded_branch.viewer_state_id, original.viewer_state_id)
            self.assertEqual(loaded_branch.metadata_state_id, original.metadata_state_id)
            self.assertEqual(loaded_branch.return_state_id, original.return_state_id)
            self.assertEqual(loaded_branch.capture_status, original.capture_status)
        # State/branch IDs must remain stable and unique across the round trip.
        branch_ids = [b.branch_id for b in loaded.series_branches]
        self.assertEqual(len(branch_ids), len(set(branch_ids)))
        state_ids = [s.state_id for s in loaded.states]
        self.assertEqual(len(state_ids), len(set(state_ids)))
        self.assertEqual(
            loaded.series_expansion.discovered_count,
            _v2_flow().series_expansion.discovered_count,
        )

    def test_v1_fixture_reads_back_with_empty_branches(self):
        v1 = ReplicaFlow(
            schema_version=1,
            flow_id="old-flow",
            source_script_relpath="recorded.py",
            source_script_sha256="abc123",
            created_at="2026-07-29T00:00:00Z",
            viewport={"width": 1280, "height": 720},
            bootstrap=BootstrapPlan(1, 3, True, {"page": "main"}),
            popup_expectations=[],
            timing_profile=CaptureTimingProfile(),
            entry_state_id="s_000",
            states=[
                ReplicaState(
                    state_id="s_000",
                    ordinal=0,
                    source_url="https://example.test/",
                    active_page_var="page",
                    pages=[],
                    documents=[],
                    transitions=[],
                    evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                )
            ],
            warnings=[],
        )
        # Write a raw v1 dict WITHOUT the new keys, then read back: from_dict
        # must fill defaults rather than fabricate data for legacy manifests.
        import json
        raw = dict(v1.to_dict())
        raw.pop("series_branches", None)
        raw.pop("series_expansion", None)
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            loaded = read_manifest(manifest_path, Path(tmp))
        self.assertEqual(loaded.schema_version, 1)
        self.assertEqual(loaded.series_branches, [])
        self.assertIsNone(loaded.series_expansion)

    def test_from_dict_rejects_unknown_version_but_reads_v2(self):
        import json
        v2 = _v2_flow().to_dict()
        loaded = ReplicaFlow.from_dict(v2)
        self.assertEqual(loaded.schema_version, 2)
        self.assertEqual(len(loaded.series_branches), 2)
        with self.assertRaises(ValueError):
            future = dict(v2)
            future["schema_version"] = 3
            ReplicaFlow.from_dict(future)

    def test_v1_series_round_trip_cannot_silently_drop_branch_fields(self):
        # A v1 manifest must never be allowed to carry series data: writing it
        # and reading it back would silently discard the branches/expansion (the
        # v1 reader strips series fields). Avoid the trap by refusing to write.
        corrupted = _v2_flow()
        corrupted.schema_version = 1  # fabricated: v1 claiming series data
        with self.assertRaisesRegex(ValueError, "schema v1"):
            corrupted.to_dict()
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            with self.assertRaisesRegex(ValueError, "schema v1"):
                write_manifest(manifest_path, corrupted)

    def test_legal_v1_round_trip_still_works_with_new_guard(self):
        # A genuine v1 flow without series fields must round-trip unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            flow = ReplicaFlow(
                schema_version=1,
                flow_id="pure-v1",
                source_script_relpath="recorded.py",
                source_script_sha256="abc123",
                created_at="2026-07-29T00:00:00Z",
                viewport={"width": 1280, "height": 720},
                bootstrap=BootstrapPlan(1, 3, True, {"page": "main"}),
                popup_expectations=[],
                timing_profile=CaptureTimingProfile(),
                entry_state_id="s_000",
                states=[
                    ReplicaState(
                        state_id="s_000",
                        ordinal=0,
                        source_url="https://example.test/",
                        active_page_var="page",
                        pages=[],
                        documents=[],
                        transitions=[],
                        evidence=StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry"),
                    )
                ],
                warnings=[],
            )
            write_manifest(manifest_path, flow)
            loaded = read_manifest(manifest_path, root)
        self.assertEqual(loaded.schema_version, 1)
        self.assertEqual(loaded.series_branches, [])
        self.assertIsNone(loaded.series_expansion)


if __name__ == "__main__":
    unittest.main()
