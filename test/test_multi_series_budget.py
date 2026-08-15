"""Phase 9: asset budget, patient-data protection, and breakpoint-recovery tests.

These run the explorer with a real in-browser fixture (no hospital network), but
mock the per-series transaction and the discovery source so the budget /
max_series / skipped_budget logic is exercised deterministically (a count-based
fake clock makes the total-time budget fair) rather than by wall-clock timing.
Patient-data checks assert no raw UID text leaks into generated artifacts, and
``git check-ignore`` confirms the sensitive output directories are covered.
"""

import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import batch_capture_replicate as batch
from batch_capture_replicate import (
    CaptureBranchOutcome,
    LiveCaptureSession,
    classify_recording_template,
    _safe_series_key,
)
from replica_models import SeriesCollectionEvidence, SeriesDescriptor
from replay_helpers import scan_text_for_secrets
from rewrite_script import parse_action_plan


MINI_HUB = """
<div class="series-list" id="series">
  <div class="item" data-series="uid-1" data-series-uid="1.2.3.1">Series A</div>
</div>
<canvas id="viewer" width="200" height="100" style="width:200px;height:100px"></canvas>
<button id="meta-open">Meta</button>
<button id="meta-close">Close</button>
<div id="tagsBox" style="display:none"><div>Series Number: 1</div></div>
<script>
  document.getElementById('meta-open').addEventListener('click', function(){
    document.getElementById('tagsBox').style.display='block'; });
  document.getElementById('meta-close').addEventListener('click', function(){
    document.getElementById('tagsBox').style.display='none'; });
</script>
"""


def _template():
    source = '''from playwright.sync_api import sync_playwright


def run(page):
    # [MARKER: 序列选择]
    page.locator("#series .item").first.click()
    # [MARKER: Meta 信息工具]
    page.locator("#meta-open").click()
    page.locator("#meta-close").click()
'''
    return classify_recording_template(parse_action_plan(source))


def _descriptors(n: int) -> list[SeriesDescriptor]:
    """Return ``n`` safe synthetic descriptors whose series_key is a raw UID."""
    descriptors = []
    for index in range(n):
        descriptors.append(SeriesDescriptor(
            series_key=f"1.2.3.{index + 100}", label=f"Series {index}", ordinal=index,
            document_id="d_series_hub", member_id=f"d_series_hub_series_{index:03d}",
            stable_attributes={"data-series": f"uid-{index}"},
            selected=False, explicit_frame_count=None, inferred_frame_count=None,
            activation="click",
        ))
    return descriptors


def _captured_outcome(_page, descriptor: SeriesDescriptor, _template, _pages, _config) -> CaptureBranchOutcome:
    return CaptureBranchOutcome(
        branch_id=_safe_series_key(descriptor), series_key=descriptor.series_key,
        label=descriptor.label, ordinal=descriptor.ordinal,
        document_id=descriptor.document_id, source_member_id=descriptor.member_id,
        activation=descriptor.activation or "click", capture_status="captured",
        fail_stage=None, error_type=None, warning=None,
    )


def _fake_clock():
    """Return ``(monotonic, counter)`` where monotonic returns successive ints.

    Every call advances the counter by one, so ``finalize_series_branches``'s
    total-budget check fires at a deterministically known iteration (elapsed =
    call index), independent of real wall-clock timing.
    """
    counter = itertools.count()
    return (lambda: next(counter)), counter


class MultiSeriesBudgetTests(unittest.TestCase):
    def test_max_series_caps_the_number_of_captured_branches(self):
        """50 discovered descriptors with max_series=3 must capture only 3.

        Every discovered descriptor still receives an honest terminal status: the
        returned page set now includes the max-limit tail as ``skipped_budget``
        (P1#6 closure) instead of silently dropping them, so the caller/flow sees
        each descriptor's terminal state with the counts conserved.
        """
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(MINI_HUB)
            fake_monotonic, _counter = _fake_clock()
            config = {"expand_all_series": True, "per_series_timeout_s": 1,
                      "total_series_timeout_s": 1_000_000, "max_series": 3}
            with patch.object(batch, "discover_series_candidates", return_value=(_descriptors(50), [], SeriesCollectionEvidence("scroll_harvest", False, 3, 50, 1, True, None, 50))), \
                 patch.object(session, "capture_one_series", side_effect=_captured_outcome), \
                 patch.object(batch.time, "monotonic", side_effect=fake_monotonic):
                outcomes = session.finalize_series_branches(page, _template(), config=config)
            branches_root = Path(tmp) / "series_branches"
            manifest = json.loads((branches_root / "series_capture_manifest.json").read_text(encoding="utf-8"))
            # The max-limit tail persists safe branch artefacts that the loader can
            # surface as first-class skipped branches in the flow.
            tail_dir_count = sum(
                1 for d in branches_root.iterdir()
                if d.is_dir() and (d / "status.json").is_file()
                and json.loads((d / "status.json").read_text(encoding="utf-8")).get("capture_status") == "skipped_budget"
            )
            browser.close()

        # The max_series hard cap stops capture at 3; the remaining 47 are
        # honest ``skipped_budget`` terminals (not silently dropped).
        self.assertEqual(len(outcomes), 50)
        by_status: dict[str, int] = {}
        for o in outcomes:
            by_status[o.capture_status] = by_status.get(o.capture_status, 0) + 1
        self.assertEqual(by_status.get("captured"), 3)
        self.assertEqual(by_status.get("skipped_budget"), 47)
        self.assertEqual(sorted(o.ordinal for o in outcomes if o.capture_status == "captured"), [0, 1, 2])
        self.assertTrue(manifest["count_conserved"])
        self.assertGreaterEqual(tail_dir_count, 47)

    def test_total_budget_exhaustion_marks_remaining_as_skipped_budget(self):
        """Once the total-time budget elapses, remaining descriptors become
        skipped_budget (not skipped_duplicate and not silently dropped)."""
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(MINI_HUB)
            fake_monotonic, _counter = _fake_clock()
            config = {"expand_all_series": True, "per_series_timeout_s": 1,
                      "total_series_timeout_s": 2, "max_series": 50}
            with patch.object(batch, "discover_series_candidates", return_value=(_descriptors(50), [], SeriesCollectionEvidence("scroll_harvest", False, 50, 50, 1, True, None, 50))), \
                 patch.object(session, "capture_one_series", side_effect=_captured_outcome), \
                 patch.object(batch.time, "monotonic", side_effect=fake_monotonic):
                outcomes = session.finalize_series_branches(page, _template(), config=config)
            branches_root = Path(tmp) / "series_branches"
            manifest = json.loads((branches_root / "series_capture_manifest.json").read_text(encoding="utf-8"))
            browser.close()

        # Consensus: exactly one branch captured before the fake budget elapsed
        # (elapsed==1 < 2), the remaining 49 skipped_budget.
        by_status: dict[str, int] = {}
        for o in outcomes:
            by_status[o.capture_status] = by_status.get(o.capture_status, 0) + 1
        self.assertEqual(by_status.get("captured"), 1)
        self.assertEqual(by_status.get("skipped_budget"), 49)
        self.assertNotIn("skipped_duplicate", [o.capture_status for o in outcomes])
        self.assertEqual(manifest["warning"], "series_budget_exhausted")
        self.assertTrue(manifest["count_conserved"])
        self.assertTrue(manifest["skipped_count"] >= 49)
        # The skipped branches remain auditable entries in the manifest.
        self.assertTrue(any(b["capture_status"] == "skipped_budget" for b in manifest["branches"]))

    def test_generated_artifacts_contain_no_raw_uid_or_patient_text(self):
        """Series keys are raw UIDs on purpose; generated artifacts must only
        carry hash/slug forms so no patient text leaks into the repo."""
        safe = _safe_series_key(_descriptors(1)[0])  # descriptor.series_key == "1.2.3.100"
        self.assertNotIn("1.2.3", safe)
        # A synthetic log line that would be produced by replay must not trigger
        # credential scanners when it stays free of raw credentials/UIDs.
        clean_log = '{"event":"series_capture_completed","branch_id":"b000_abc123456789","ordinal":0,"error":null}'
        self.assertEqual(scan_text_for_secrets(clean_log), [])
        # A URL whose query carries a known secret KEY is still flagged even when
        # the value is REDACTED -- the scanner keys on the presence of the
        # credential key, which is the conservative behavior we rely on.
        self.assertIn("known_source_query", scan_text_for_secrets("https://app.test/path?access_token=REDACTED"))

    def test_controlled_reload_is_audited_in_manifest(self):
        """Three hub-unrecoverable failures allow exactly one controlled reload;
        the manifest must record it (audit trail)."""
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(MINI_HUB)

            def raise_hub(_page, _descriptor, _template, _pages, _config):
                raise batch.HubUnrecoverableError("hub lost")

            config = {"expand_all_series": True, "per_series_timeout_s": 1,
                      "total_series_timeout_s": 1000, "max_series": 3}
            with patch.object(batch, "discover_series_candidates", return_value=(_descriptors(3), [], SeriesCollectionEvidence("scroll_harvest", False, 3, 3, 1, True, None, 3))), \
                 patch.object(session, "capture_one_series", side_effect=raise_hub):
                outcomes = session.finalize_series_branches(page, _template(), config=config)
            branches_root = Path(tmp) / "series_branches"
            manifest = json.loads((branches_root / "series_capture_manifest.json").read_text(encoding="utf-8"))
            browser.close()

        self.assertTrue(manifest["reloaded"])
        self.assertEqual(manifest["warning"], "series_reload_recovered_once")
        # Failures are preserved as auditable entries, never silently dropped.
        self.assertTrue(all(o.capture_status == "failed" for o in outcomes))
        self.assertEqual(len(outcomes), 3)


class GitIgnoreCoverageTests(unittest.TestCase):
    """Defensive checks that sensitive capture/replica outputs are never tracked."""

    REPO_ROOT = Path(__file__).resolve().parents[1]

    def test_sensitive_output_paths_are_git_ignored(self):
        probe_paths = [
            "out/hospital/runs/run-001/capture/series_branches/b000_abc123/viewer/topology.json",
            "out/hospital/runs/run-001/pipeline_report.html",
            "snapshots/",
            "replicas/",
            "annotations/",
            "capture/",
            "spy/",
        ]
        for probe in probe_paths:
            result = subprocess.run(
                ["git", "check-ignore", "-q", "--", probe],
                cwd=str(self.REPO_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode, 0, f"{probe} is not ignored: {result.stderr}"
            )


if __name__ == "__main__":
    unittest.main()
