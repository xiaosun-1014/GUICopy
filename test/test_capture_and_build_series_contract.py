"""Subprocess full-pipeline contract test for the multi-series expansion chain.

Closes the P0#1 test gap the codex final review flagged at a layer no existing
test covers: neither a hand-built ideal model (test_replica_runtime /
test_build_replica) nor an in-process ``LiveCaptureSession`` call
(test_multi_series_capture) — but the REAL production subprocess pipeline.

``capture_and_build`` instruments the recorded script (injecting
``capture_hook_expand_series`` after the Metadata-close success branch), runs
it in a child ``codegen-marker`` Python process with
``REPLICA_EXPANSION_CONFIG`` set, so the close hook triggers the explorer and
real ``series_branches`` land on disk; the v2 manifest is written with
``series_branches``; ``build_from_manifest`` produces the offline site; and a
no-network Playwright session clicks every captured series, opens its Metadata
panel, and closes back to the same branch — with zero external HTTP requests.

The fixtures are anonymous and inline (``page.set_content``), so no hospital
network and no patient data are involved.
"""

import json
import re
import tempfile
import unittest
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

from batch_capture_replicate import capture_and_build
from pipeline_validation import validate_manifest
from replay_helpers import ReplicaServer, read_manifest, series_key_slug

# Three-series fixture mirroring test_multi_series_capture's in-process fixture:
# per-series canvas color + current-series text (ready evidence) and per-series
# Metadata tag/value (branch-unique) so a captured branch's served DOM and
# Metadata panel are distinguishable offline.
SERIES_FIXTURE = """
<style>
  #series{height:48px;overflow:auto}
  .item{height:24px;cursor:pointer;border:1px solid #ccc;box-sizing:border-box}
</style>
<div class="series-list" id="series">
  <div class="item" data-series="1.2.3.1" data-series-uid="1.2.3.1">Series A</div>
  <div class="item" data-series="1.2.3.2" data-series-uid="1.2.3.2">Series B</div>
  <div class="item" data-series="1.2.3.3" data-series-uid="1.2.3.3">Series C</div>
</div>
<canvas id="viewer" width="200" height="100" style="width:200px;height:100px"></canvas>
<div id="current-series">Series A</div>
<button id="meta-open">Meta</button>
<button id="meta-close">Close</button>
<div id="tagsBox" style="display:none"><div data-meta-series>SeriesNumber: 0</div></div>
<script>
  var cv = document.getElementById('viewer');
  var g = cv.getContext('2d');
  var palette = {'1.2.3.1':'#aa0000','1.2.3.2':'#00aa00','1.2.3.3':'#0000aa'};
  function blank() { g.fillStyle='#000000'; g.fillRect(0,0,200,100); }
  blank();
  function selectItem(item) {
    var uid = item.getAttribute('data-series');
    document.querySelectorAll('.item').forEach(function(e){ e.removeAttribute('aria-selected'); });
    item.setAttribute('aria-selected','true');
    g.fillStyle = palette[uid] || '#888888';
    g.fillRect(0,0,200,100);
    g.fillStyle = '#ffffff'; g.fillRect(10,10,20,20);
    document.getElementById('current-series').textContent = item.textContent.trim();
    document.querySelector('[data-meta-series]').textContent =
        'SeriesNumber: ' + uid.split('.').pop() + ' / uid=' + uid +
        ' / SeriesInstanceUID: ' + uid +
        ' / SeriesDescription: ' + item.textContent.trim();
  }
  document.querySelectorAll('.item').forEach(function(el){
    el.addEventListener('click', function(){ selectItem(el); });
    el.addEventListener('dblclick', function(){ selectItem(el); });
  });
  document.getElementById('meta-open').addEventListener('click', function(){
    document.getElementById('tagsBox').style.display = 'block';
  });
  document.getElementById('meta-close').addEventListener('click', function(){
    document.getElementById('tagsBox').style.display = 'none';
  });
</script>
"""


def _processed_script(source_fixture: str) -> str:
    """A self-contained recorded script with a complete expansion template:
    series select (marker) -> Meta open + close (marker). The child process
    executes this verbatim via ``page.set_content``."""
    return f'''from playwright.sync_api import sync_playwright


def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content({source_fixture!r})
        # [MARKER: 序列选择]
        page.locator("#series .item").first.click()
        # [MARKER: Meta 信息工具]
        page.locator("#meta-open").click()
        page.locator("#meta-close").click()
        browser.close()


run()
'''


def _marker_lines(source: str) -> list[tuple[int, str]]:
    """1-based line numbers + labels of every ``# [MARKER: ...]`` comment."""
    hits: list[tuple[int, str]] = []
    for line_no, line in enumerate(source.splitlines(), start=1):
        match = re.match(r"\s*#\s*\[MARKER:\s*([^\]]+)\]", line)
        if match:
            hits.append((line_no, match.group(1).strip()))
    return hits


def _write_annotations(script: Path) -> Path:
    """Replica annotations matching the script byte-for-byte (sha256 + marker
    lines), as the GUI writes them on save."""
    import hashlib

    payload = {
        "schema_version": 1,
        "source_script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "markers": [
            {"marker_id": str(uuid.uuid4()), "line": line_no, "label": label}
            for line_no, label in _marker_lines(script.read_text(encoding="utf-8"))
        ],
    }
    path = script.with_name(script.name + ".annotations.json")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _click_series_and_wait(page, series_key: str, viewer_state_id: str | None = None) -> bool:
    """Click a served series member bound by ``data-replica-series-key`` and wait
    for navigation into the target branch's viewer state.

    ``viewer_state_id`` pins the exact destination URL (``states/{viewer_state_id}``)
    so later clicks from inside a branch viewer cannot false-positive on a URL
    that already contains ``states/bviewer_``.
    """
    slug = series_key_slug(series_key)
    selector = f'[data-replica-series-key="{slug}"]'
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=8000)
    except Exception:
        return False
    page.locator(selector).first.click()
    try:
        if viewer_state_id is not None:
            page.wait_for_url(lambda url: f"states/{viewer_state_id}" in url, timeout=10000)
        else:
            page.wait_for_url(lambda url: "states/bviewer_" in url, timeout=10000)
        return True
    except Exception:
        return False


class CaptureAndBuildSeriesContractTests(unittest.TestCase):
    """Real subprocess pipeline: annotations gate -> instrumented replay with
    REPLICA_EXPANSION_CONFIG -> real series_branches -> v2 manifest -> builder
    -> offline clicks."""

    def _run_pipeline(self, root: Path):
        script = root / "recorded.py"
        script.write_text(_processed_script(SERIES_FIXTURE), encoding="utf-8")
        annotations = _write_annotations(script)
        output_root = root / "export"
        entrypoint = capture_and_build(
            script,
            output_root,
            annotations_path=annotations,
            capture_timeout_s=600,
            expansion_config={
                "expand_all_series": True,
                "max_series": 6,
                "per_series_timeout_s": 2,
                "total_series_timeout_s": 120,
                "viewer_capture_mode": "first_stable_frame",
            },
        )
        return script, entrypoint, output_root / "capture", output_root / "replica"

    def test_real_capture_build_branches_manifest_and_offline_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, entrypoint, capture_root, replica_root = self._run_pipeline(root)

            # ---- 1. REAL branch artifacts landed with routable regions. ----
            branches_root = capture_root / "series_branches"
            self.assertTrue((branches_root / "series_capture_manifest.json").is_file())
            manifest = _read_json(branches_root / "series_capture_manifest.json")
            self.assertEqual(manifest["discovered_count"], 3, "fixture has exactly 3 series")
            self.assertTrue(manifest["count_conserved"], "branch conservation must hold")

            captured = []
            for branch_dir in branches_root.iterdir():
                status_path = branch_dir / "status.json"
                if not status_path.is_file():
                    continue
                status = _read_json(status_path)
                if status["capture_status"] != "captured":
                    continue
                # status.json keeps only the sha256; the raw series_key lives in
                # descriptor.json (the limited sensitive descriptor artifact).
                descriptor = _read_json(branch_dir / "descriptor.json")
                captured.append((status, descriptor, branch_dir))
                # Viewer topology carries a series region ...
                viewer = _read_json(branch_dir / "viewer" / "topology.json")
                self.assertTrue(
                    any(r["region_type"] == "series" for doc in viewer["documents"] for r in doc.get("regions", [])),
                    f"branch {status['branch_id']} viewer missing series region",
                )
                # ... a Metadata topology + rows fallback, and a real open trigger.
                self.assertTrue((branch_dir / "metadata" / "topology.json").is_file())
                meta_topo = _read_json(branch_dir / "metadata" / "topology.json")
                self.assertTrue(
                    any(r["region_type"] == "metadata" for doc in meta_topo["documents"] for r in doc.get("regions", [])),
                    f"branch {status['branch_id']} metadata missing metadata region",
                )
                self.assertTrue((branch_dir / "metadata" / "metadata_rows.json").is_file())
                meta_open = _read_json(branch_dir / "meta_open_target.json")
                self.assertTrue(
                    meta_open.get("outer_html"),
                    f"branch {status['branch_id']} meta_open outer_html empty",
                )
            self.assertGreaterEqual(
                len(captured), 2, "expected at least 2 captured branches from the real subprocess run"
            )
            by_key = {descriptor["series_key"]: (status, descriptor, branch_dir)
                      for status, descriptor, branch_dir in captured}
            self.assertIn("1.2.3.1", by_key)
            # No raw series identity in the branch directory name (safe slug only).
            for _, _, branch_dir in captured:
                self.assertNotIn("1.2.3", branch_dir.name, "raw UID must not enter the branch dir name")

            # ---- 1b. per-series Metadata 互异（验收判据：≥2 分支互不相同） ----
            # 每个分支的 metadata_rows.json 必须携带它自己的 uid 身份；两个分支
            # 的内容与 uid 指纹都不能相同，否则「点 B → Meta 是 B」不成立。
            metadata_rows_by_key = {
                descriptor["series_key"]: _read_json(branch_dir / "metadata" / "metadata_rows.json")
                for _status, descriptor, branch_dir in captured
            }
            self.assertGreaterEqual(
                len(metadata_rows_by_key), 2,
                "per-series metadata distinctness needs at least 2 captured branches",
            )
            keys = sorted(metadata_rows_by_key)
            self.assertNotEqual(
                metadata_rows_by_key[keys[0]], metadata_rows_by_key[keys[1]],
                "metadata_rows.json must differ across captured series",
            )
            self.assertNotEqual(
                metadata_rows_by_key[keys[0]].get("uid_sha256_prefix"),
                metadata_rows_by_key[keys[1]].get("uid_sha256_prefix"),
                "per-series uid identity must differ across branches",
            )

            # ---- 2. The REAL v2 manifest round-trips and validates. ----
            flow = read_manifest(capture_root / "manifest.json", capture_root, verify_source_hash=True)
            self.assertEqual(flow.schema_version, 2, "captured manifest must be schema v2")
            self.assertTrue(flow.series_branches, "v2 flow must carry series branches")
            branch_keys = {b.series_key for b in flow.series_branches}
            self.assertEqual(branch_keys, {"1.2.3.1", "1.2.3.2", "1.2.3.3"})
            result = validate_manifest(flow, capture_root)
            self.assertEqual(result.status, "success", f"manifest validation failed: {result.errors}")

            # ---- 3. The real builder output is servable and clickable offline. ----
            self.assertTrue((replica_root / "index.html").is_file())
            with ReplicaServer(replica_root) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                external_requests = []
                page.on(
                    "request",
                    lambda request: external_requests.append(request.url)
                    if not request.url.startswith("http://127.0.0.1")
                    else None,
                )
                page.goto(server.url)
                self.assertGreaterEqual(page.locator("[data-replica-series-key]").count(), 2,
                                        "entry viewer must expose route-keyed series members")
                viewer_state_by_key = {b.series_key: b.viewer_state_id for b in flow.series_branches}
                labels = {"1.2.3.1": "Series A", "1.2.3.2": "Series B", "1.2.3.3": "Series C"}

                # Click every captured branch and land on THAT branch's viewer.
                previous_bg: str | None = None
                for status, descriptor, _branch_dir in captured:
                    key = descriptor["series_key"]
                    viewer_state_id = viewer_state_by_key[key]
                    self.assertTrue(
                        _click_series_and_wait(page, key, viewer_state_id),
                        f"clicking {key} did not navigate to {viewer_state_id}",
                    )
                    self.assertIn(f"states/{viewer_state_id}", page.url)
                    option = page.locator(f'[data-replica-series-key="{series_key_slug(key)}"]').first
                    self.assertEqual(option.inner_text().strip(), labels[key],
                                     "viewer DOM did not switch to the clicked series")
                    self.assertEqual(option.get_attribute("aria-selected"), "true",
                                     "clicked series option not marked selected")
                    bg = page.locator(".replica-bg").first.get_attribute("src")
                    if previous_bg is not None:
                        self.assertNotEqual(bg, previous_bg,
                                            "viewer screenshot asset did not change across branches")
                    previous_bg = bg
                self.assertEqual(external_requests, [])
                browser.close()

    def test_branch_metadata_clicks_open_and_close_returns_same_viewer(self):
        """Per-branch Metadata through the real subprocess chain: the branch
        viewer renders a real meta-open trigger; clicking it shows the branch's
        unique tag/value; close returns to the SAME branch viewer (not the
        ordinal predecessor)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, entrypoint, capture_root, replica_root = self._run_pipeline(root)

            branches_root = capture_root / "series_branches"
            captured_by_key = {}
            for branch_dir in branches_root.iterdir():
                status_path = branch_dir / "status.json"
                if not status_path.is_file():
                    continue
                status = _read_json(status_path)
                if status["capture_status"] == "captured":
                    descriptor = _read_json(branch_dir / "descriptor.json")
                    captured_by_key[descriptor["series_key"]] = status
            self.assertIn("1.2.3.2", captured_by_key)

            flow = read_manifest(capture_root / "manifest.json", capture_root)
            branch = next(b for b in flow.series_branches if b.series_key == "1.2.3.2")

            with ReplicaServer(replica_root) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                self.assertTrue(_click_series_and_wait(page, "1.2.3.2"), "navigate to Series B failed")
                viewer_url = page.url
                self.assertIn("states/bviewer_", viewer_url)

                # Real captured Metadata trigger -> bmeta_ state.
                trigger = f'[data-replica-action="series:{branch.branch_id}:meta_open"]'
                self.assertGreaterEqual(page.locator(trigger).count(), 1, "metadata trigger overlay missing")
                page.locator(trigger).first.click()
                try:
                    page.wait_for_url(lambda url: "states/bmeta_" in url, timeout=10000)
                except Exception:
                    self.fail("clicking metadata trigger did not open the branch metadata state")

                panel = page.locator(".replica-metadata").first
                self.assertTrue(panel.is_visible(), "metadata panel not visible")
                body = panel.inner_text()
                self.assertIn("SeriesNumber", body)
                self.assertIn("uid=1.2.3.2", body, "metadata panel lacks branch-unique value")

                # Close returns to the SAME viewer branch, not an ordinal predecessor.
                close_btn = page.locator("[data-replica-back], [data-replica-panel-close]").first
                self.assertGreaterEqual(close_btn.count(), 1, "metadata close control missing")
                close_btn.click()
                try:
                    page.wait_for_url(lambda url: "states/bviewer_" in url, timeout=10000)
                except Exception:
                    self.fail("close did not return to a branch viewer")
                self.assertIn(
                    f"states/{branch.viewer_state_id}", page.url,
                    f"close must return to the SAME branch viewer {branch.viewer_state_id}; "
                    f"got {page.url}",
                )
                browser.close()

    def test_stale_annotations_rejected_before_capture(self):
        """The annotation hash gate fires before any replay: editing the script
        after saving annotations must raise instead of capturing with mismatch."""
        from batch_capture_replicate import capture_to_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(_processed_script(SERIES_FIXTURE), encoding="utf-8")
            annotations = _write_annotations(script)
            # Touch the script so its sha256 no longer matches the annotations.
            script.write_text(
                script.read_text(encoding="utf-8") + "\n# post-save note\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                capture_to_manifest(script, annotations, root / "capture")


if __name__ == "__main__":
    unittest.main()
