import tempfile
import unittest
from pathlib import Path

from replica_models import (
    BootstrapPlan,
    CaptureTimingProfile,
    ReplicaFlow,
    ReplicaState,
    StateEvidence,
)
from replay_helpers import read_manifest, sha256_file, write_manifest


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


if __name__ == "__main__":
    unittest.main()
