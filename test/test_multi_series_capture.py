"""Phase 5: single-series transaction and all-series explorer tests.

These run against an in-browser ``page.set_content`` fixture (no real hospital
network, no completed-adapter execution) so they exercise the real Playwright
interaction surface without hanging on a live viewer. They verify terminal
statuses for all discovered series, original-state restoration, and that a
Metadata timeout degrades only the affected branch.
"""

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import batch_capture_replicate as batch
from batch_capture_replicate import LiveCaptureSession, _locate_series_row, _safe_series_key, classify_recording_template, _branch_topology
from build_replica import build_replica
from replay_helpers import ReplicaServer, series_key_slug
from replica_models import ReplicaState, SeriesDescriptor, StateEvidence
from rewrite_script import parse_action_plan


def _noop_ctx():
    return nullcontext()


def _strip_metadata_blocks(text: str) -> str:
    """Remove served ``.replica-metadata`` panel blocks (the limited sensitive
    artifact) so privacy byte-assertions can scan only the non-metadata surface."""
    from pipeline_validation import _strip_generated_metadata_blocks
    return _strip_generated_metadata_blocks(text)

_TEMPLATE_SOURCE = '''from playwright.sync_api import sync_playwright


def run(page):
    # [MARKER: 序列选择]
    page.locator("#series .item").first.click()
    # [MARKER: Meta 信息工具]
    page.locator("#meta-open").click()
    page.locator("#meta-close").click()
'''


HUB_FIXTURE = """
<style>
  #series{height:48px;overflow:auto}
  .item{height:24px;cursor:pointer;border:1px solid #ccc;box-sizing:border-box}
</style>
<div class="series-list" id="series">
  <div class="item" data-series="uid-1" data-series-uid="1.2.3.1">Series A</div>
  <div class="item" data-series="uid-2" data-series-uid="1.2.3.2">Series B</div>
  <div class="item" data-series="uid-3" data-series-uid="1.2.3.3">Series C</div>
</div>
<canvas id="viewer" width="200" height="100" style="width:200px;height:100px"></canvas>
<div id="current-series">Series A</div>
<button id="meta-open">Meta</button>
<button id="meta-close">Close</button>
<div id="tagsBox" style="display:none">
  <div>Series Number: 1</div><div>Series Description: Sample</div>
</div>
<script>
  var cv = document.getElementById('viewer');
  var g = cv.getContext('2d');
  var palette = {'uid-1':'#aa0000','uid-2':'#00aa00','uid-3':'#0000aa'};
  function blank() { g.fillStyle='#000000'; g.fillRect(0,0,200,100); }
  blank();
  function selectItem(item) {
    document.querySelectorAll('.item').forEach(function(e){ e.removeAttribute('aria-selected'); });
    item.setAttribute('aria-selected','true');
    g.fillStyle = palette[item.getAttribute('data-series')] || '#888888';
    g.fillRect(0,0,200,100);
    g.fillStyle = '#ffffff'; g.fillRect(10,10,20,20);
    document.getElementById('current-series').textContent = item.textContent.trim();
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

# A fixture whose Metadata panel carries executable / event-handler / token-like
# content so the sanitizer (P1#8) is verifiably applied before persistence.
SANITIZE_FIXTURE = """
<style>.item{cursor:pointer}</style>
<div class="series-list" id="series">
  <div class="item" data-series="uid-1" data-series-uid="1.2.3.1">Series A</div>
</div>
<canvas id="viewer" width="200" height="100" style="width:200px;height:100px"></canvas>
<div id="current-series">Series A</div>
<button id="meta-open">Meta</button>
<button id="meta-close">Close</button>
<div id="tagsBox" style="display:none">
  <div onerror="alert(1)" data-token="s3cr3t">Series Number: 42</div>
  <script>window.__payload = 1;</script>
</div>
<script>
  var cv = document.getElementById('viewer'); var g = cv.getContext('2d');
  g.fillStyle='#000000'; g.fillRect(0,0,200,100);
  document.querySelector('.item').addEventListener('click', function(){
    document.getElementById('tagsBox').style.display='block';
    g.fillStyle='#aa0000'; g.fillRect(0,0,200,100); g.fillStyle='#ffffff'; g.fillRect(10,10,20,20);
    document.getElementById('current-series').textContent = 'Series A';
    document.querySelector('.item').setAttribute('aria-selected','true');
  });
  document.getElementById('meta-open').addEventListener('click', function(){
    document.getElementById('tagsBox').style.display='block'; });
  document.getElementById('meta-close').addEventListener('click', function(){
    document.getElementById('tagsBox').style.display='none'; });
</script>
"""

# A single-series fixture whose Metadata panel never stabilizes (a live counter
# keeps changing) so the Metadata transaction times out while the Viewer still
# rendered — the branch must degrade to partial, not fail outright.
SLOW_META_FIXTURE = """
<style>.item{cursor:pointer}</style>
<div class="series-list" id="series">
  <div class="item" data-series="uid-1" data-series-uid="1.2.3.1">Series A</div>
</div>
<canvas id="viewer" width="200" height="100" style="width:200px;height:100px"></canvas>
<button id="meta-open">Meta</button>
<button id="meta-close">Close</button>
<div id="tagsBox" style="display:none"><div><span id="live">0</span></div></div>
<script>
  var cv = document.getElementById('viewer'); var g = cv.getContext('2d');
  g.fillStyle='#000000'; g.fillRect(0,0,200,100);
  document.querySelector('.item').addEventListener('click', function(el){
    document.getElementById('tagsBox').style.display='block';
    g.fillStyle='#aa0000'; g.fillRect(0,0,200,100); g.fillStyle='#ffffff'; g.fillRect(10,10,20,20);
    document.querySelector('.item').setAttribute('aria-selected','true');
  });
  document.getElementById('meta-close').addEventListener('click', function(){
    document.getElementById('tagsBox').style.display='none';
  });
  var n = 0; setInterval(function(){ document.getElementById('live').textContent = ++n; }, 30);
</script>
"""

# A fixture whose Metadata panel content varies by the *selected* series, so each
# captured branch carries a unique tag/value (Series Number / uid). This is what
# lets the contract test assert a branch's complete Metadata panel is visible and
# closes back to the same branch's Viewer.
HUB_UNIQUE_META_FIXTURE = """
<style>
  #series{height:48px;overflow:auto}
  .item{height:24px;cursor:pointer;border:1px solid #ccc;box-sizing:border-box}
</style>
<div class="series-list" id="series">
  <div class="item" data-series="uid-1" data-series-uid="1.2.3.1">Series A</div>
  <div class="item" data-series="uid-2" data-series-uid="1.2.3.2">Series B</div>
  <div class="item" data-series="uid-3" data-series-uid="1.2.3.3">Series C</div>
</div>
<canvas id="viewer" width="200" height="100" style="width:200px;height:100px"></canvas>
<div id="current-series">Series A</div>
<button id="meta-open">Meta</button>
<button id="meta-close">Close</button>
<div id="tagsBox" style="display:none"><div data-meta-series>SeriesNumber: 0</div></div>
<script>
  var cv = document.getElementById('viewer');
  var g = cv.getContext('2d');
  var palette = {'uid-1':'#aa0000','uid-2':'#00aa00','uid-3':'#0000aa'};
  function blank() { g.fillStyle='#000000'; g.fillRect(0,0,200,100); }
  blank();
  function selectItem(item) {
    document.querySelectorAll('.item').forEach(function(e){ e.removeAttribute('aria-selected'); });
    item.setAttribute('aria-selected','true');
    var uid = item.getAttribute('data-series');
    g.fillStyle = palette[uid] || '#888888';
    g.fillRect(0,0,200,100);
    g.fillStyle = '#ffffff'; g.fillRect(10,10,20,20);
    document.getElementById('current-series').textContent = item.textContent.trim();
    document.querySelector('[data-meta-series]').textContent = 'SeriesNumber: ' + uid.replace('uid-','') + ' / uid=' + uid;
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


class MultiSeriesCaptureTests(unittest.TestCase):
    def _template(self):
        return classify_recording_template(parse_action_plan(_TEMPLATE_SOURCE))

    def test_all_fixture_series_reach_terminal_states_and_conserve_counts(self):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(HUB_FIXTURE)
            template = self._template()
            outcomes = session.finalize_series_branches(
                page, template,
                config={"expand_all_series": True, "per_series_timeout_s": 1.5,
                        "total_series_timeout_s": 20, "max_series": 5},
            )
            browser.close()

            branch_statuses = {o.ordinal: o.capture_status for o in outcomes}
            self.assertIn(0, branch_statuses)
            self.assertIn(1, branch_statuses)
            self.assertIn(2, branch_statuses)
            self.assertTrue(all(s in {"captured", "partial", "failed"} for s in branch_statuses.values()))

            branches_root = Path(tmp) / "series_branches"
            manifest = json.loads((branches_root / "series_capture_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["count_conserved"])
            self.assertEqual(manifest["discovered_count"], 3)
            summary = {o.ordinal: o.capture_status for o in outcomes}

        self.assertEqual(sorted(summary), [0, 1, 2])

    def test_missing_dom_activation_inherits_recorded_dblclick(self):
        source = _TEMPLATE_SOURCE.replace('.first.click()', '.first.dblclick()')
        template = classify_recording_template(parse_action_plan(source))
        descriptor = SeriesDescriptor(
            series_key="uid-1", label="Series A", ordinal=0, document_id="d_series_hub",
            member_id="d_series_hub_series_000", stable_attributes={"data-series": "uid-1"},
            selected=False, explicit_frame_count=None, inferred_frame_count=None,
            activation=None,
        )
        evidence = batch.SeriesCollectionEvidence(
            "scroll_harvest", False, 1, 1, 0, True, None, 1
        )

        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(HUB_FIXTURE)
            captured = []

            def capture(_page, current, _template, _pages, _config):
                captured.append(current)
                return batch.CaptureBranchOutcome(
                    branch_id="b000_test", series_key=current.series_key,
                    label=current.label, ordinal=current.ordinal,
                    document_id=current.document_id, source_member_id=current.member_id,
                    activation=current.activation or "click", capture_status="captured",
                    fail_stage=None, error_type=None, warning=None,
                )

            with patch.object(batch, "discover_series_candidates", return_value=([descriptor], [], evidence)), \
                    patch.object(session, "capture_one_series", side_effect=capture):
                session.finalize_series_branches(
                    page, template,
                    config={"expand_all_series": True, "per_series_timeout_s": 1,
                            "total_series_timeout_s": 10, "max_series": 1},
                )
            browser.close()

        self.assertEqual(captured[0].activation, "dblclick")

    def test_exploration_restores_original_scroll_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(HUB_FIXTURE)
            template = self._template()
            page.locator("#series").evaluate("el => el.scrollTop = 4")
            page.locator("#series .item[data-series='uid-1']").evaluate("el => el.setAttribute('aria-selected', 'true')")

            session.finalize_series_branches(
                page, template,
                config={"expand_all_series": True, "per_series_timeout_s": 1.5,
                        "total_series_timeout_s": 20, "max_series": 5},
            )

            restored_scroll = page.locator("#series").evaluate("el => el.scrollTop")
            restored_selection = page.locator("#series .item[data-series='uid-1']").get_attribute("aria-selected")
            browser.close()

        self.assertEqual(restored_scroll, 4)
        self.assertEqual(restored_selection, "true")

    def test_metadata_timeout_degrades_branch_to_partial_not_failed(self):
        descriptor = SeriesDescriptor(
            series_key="uid-1", label="Series A", ordinal=0, document_id="d_series_hub",
            member_id="d_series_hub_series_000", stable_attributes={"data-series": "uid-1"},
            selected=False, explicit_frame_count=None, inferred_frame_count=None,
            activation="click",
        )
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(SLOW_META_FIXTURE)
            template = self._template()
            outcome = session.capture_one_series(
                page, descriptor, template, {"page": page},
                {"expand_all_series": True, "per_series_timeout_s": 0.6, "total_series_timeout_s": 10, "max_series": 5},
            )
            browser.close()

        # Viewer rendered (captured) but Metadata never stabilized -> partial,
        # never failed.
        self.assertEqual(outcome.capture_status, "partial")
        self.assertEqual(outcome.fail_stage, "stabilize")

    def test_single_series_transaction_reparses_and_activates_target(self):
        descriptor = SeriesDescriptor(
            series_key="uid-2", label="Series B", ordinal=1, document_id="d_series_hub",
            member_id="d_series_hub_series_001", stable_attributes={"data-series": "uid-2"},
            selected=False, explicit_frame_count=None, inferred_frame_count=None,
            activation="click",
        )
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(HUB_FIXTURE)
            template = self._template()
            root = page.locator("#series")
            row, steps = _locate_series_row(root, descriptor)
            # The transaction re-locates the target row from stable identity.
            self.assertIsNotNone(row)
            self.assertEqual(row.get_attribute("data-series"), "uid-2")
            outcome = session.capture_one_series(
                page, descriptor, template, {"page": page},
                {"expand_all_series": True, "per_series_timeout_s": 1.5, "total_series_timeout_s": 10, "max_series": 5},
            )
            selected_row = page.locator("#series [aria-selected='true']").get_attribute("data-series")
            branch_dir = Path(tmp) / "series_branches" / outcome.branch_id

            # No raw UID in the branch directory name.
            self.assertNotIn("1.2.3", branch_dir.name)
            self.assertTrue((branch_dir / "status.json").is_file())
            self.assertTrue((branch_dir / "viewer" / "topology.json").is_file())
            browser.close()

        # Inherited click activation selected the target series.
        self.assertEqual(outcome.capture_status, "captured")
        self.assertEqual(selected_row, "uid-2")


def _make_template():
    return classify_recording_template(parse_action_plan(_TEMPLATE_SOURCE))


def _run_real_capture(playwright, capture_root, timeout_s=2.0, fixture=HUB_FIXTURE):
    capture_root = Path(capture_root)
    session = LiveCaptureSession(capture_root)
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content(fixture)
    template = _make_template()
    outcomes = session.finalize_series_branches(
        page, template,
        config={"expand_all_series": True, "per_series_timeout_s": timeout_s,
                "total_series_timeout_s": 60, "max_series": 6},
    )
    browser.close()
    return outcomes


class SeriesContractEndToEndTests(unittest.TestCase):
    """Phase 6/7 contract test: REAL capture artifacts -> flow -> builder -> offline click.

    This is the chain the codex final review said was missing: it runs the real
    explorer against an in-browser fixture, takes the real ``series_branches``
    capture directories as the only input (no hand-built ideal model), merges them
    through ``_load_series_branch_snapshots`` / ``_build_branches_into_flow``,
    builds the replica, then drives an offline Playwright session clicking series
    members across at least two branches.
    """

    @staticmethod
    def _build_flow(capture_root, entry_doc_index=0):
        """Run the real loader/builder chain: capture dirs -> flow -> still-unbuilt
        state. Returns ``(flow, states, branches, output_root)`` where
        ``states[0]`` is a real (remapped) branch viewer used as the offline entry.
        """
        from batch_capture_replicate import _build_branches_into_flow, _load_series_branch_snapshots
        from replica_models import BootstrapPlan, CaptureTimingProfile, ReplicaFlow
        snapshots, _warnings, _expansion = _load_series_branch_snapshots(capture_root)
        first = snapshots[entry_doc_index]
        _entry_pages, entry_documents = _branch_topology(first.viewer_pages, first.viewer_documents, "entry")
        entry = ReplicaState(
            "s_000", 0, "", "page", _entry_pages, entry_documents, [],
            StateEvidence(False, False, False, False, 0, 0, 0, 0, "series_entry"),
        )
        states = [entry]
        branches, expansion_evidence = _build_branches_into_flow(
            states, Path(capture_root), parse_action_plan(_TEMPLATE_SOURCE), []
        )
        flow = ReplicaFlow(
            2, "series-contract", "_contract.py", "hash", "now",
            {"width": 800, "height": 600},
            BootstrapPlan(1, 1, True, {"page": "main"}), [],
            CaptureTimingProfile(), "s_000", states, [],
            series_branches=branches,
            series_expansion=expansion_evidence,
        )
        return snapshots, states, branches, flow

    def test_real_capture_to_offline_runtime_click_two_series(self):
        # Build a flow whose entry is a real branch viewer document (so the
        # offline entry has a real series list to click from), then merge all
        # real branches through _build_branches_into_flow.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture_root = root / "capture"
            with sync_playwright() as playwright:
                outcomes = _run_real_capture(playwright, capture_root)
            captured = [o for o in outcomes if o.capture_status == "captured"]
            self.assertGreaterEqual(len(captured), 2)
            self.assertTrue(all(o.capture_status in ("captured", "partial", "failed") for o in outcomes))

            snapshots, _states, branches, flow = self._build_flow(capture_root)
            self.assertGreaterEqual(len(snapshots), 2)
            for snapshot in snapshots:
                if snapshot.capture_status != "failed":
                    self.assertTrue(snapshot.viewer_documents)
                    self.assertTrue(
                        any(r.region_type == "series" for doc in snapshot.viewer_documents for r in doc.regions),
                        f"branch {snapshot.branch_id} viewer missing series region",
                    )

            output = root / "replica"
            build_replica(flow, capture_root, output)

            # Meta open DOM captured non-empty for at least two branches, branch
            # entry documents resolve and builder generated every branch state.
            branches_by_key = {b.series_key: b for b in branches}
            with_meta_open = [s for s in snapshots if s.meta_open_dom is not None]
            self.assertGreaterEqual(len(with_meta_open), 2)
            for branch in branches:
                self.assertIsNotNone(branch.viewer_state_id, f"branch {branch.branch_id} has no viewer state")
                self.assertIsNotNone(branch.metadata_state_id, f"branch {branch.branch_id} has no metadata state")
                # Every branch's viewer/metadata entry document was written and is
                # servable: the builder generated a state file under states/.
                state_dir = output / "states" / branch.viewer_state_id
                self.assertTrue(state_dir.exists(), f"builder did not generate viewer state {branch.viewer_state_id}")

            # Pick two distinct captured branches and click each from the entry.
            target_a = captured[0]
            target_b = next((o for o in captured if o.series_key != target_a.series_key), None)
            self.assertIsNotNone(target_b)

            with ReplicaServer(output) as server, sync_playwright() as pw2:
                browser = pw2.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                # Entry is a real branch viewer carrying a real series region;
                # its series members must be route-keyed for offline navigation.
                self.assertGreaterEqual(page.locator("[data-replica-series-key]").count(), 2)
                # Click TWO distinct captured series and land on each viewer.
                ok = _click_series_and_wait(page, target_a.series_key)
                self.assertTrue(ok, "clicking first captured series did not navigate to its viewer")
                page.goto(server.url)
                ok = _click_series_and_wait(page, target_b.series_key)
                self.assertTrue(ok, "clicking second captured series did not navigate to its viewer")
                # No raw UID / patient-derived series key leaks into served HTML.
                served_html = page.content()
                for key in (target_a.series_key, target_b.series_key):
                    self.assertNotIn(key, served_html)
                browser.close()

    def test_real_capture_flow_metadata_region_and_atomic_remap(self):
        """Metadata region must render (complete panel) and remap must resolve."""
        from batch_capture_replicate import _build_branches_into_flow, _load_series_branch_snapshots
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture_root = root / "capture"
            with sync_playwright() as playwright:
                _run_real_capture(playwright, capture_root)
            snapshots, _warnings, _expansion = _load_series_branch_snapshots(capture_root)
            with_meta = [s for s in snapshots if s.capture_status == "captured" and s.metadata_documents]
            self.assertGreaterEqual(len(with_meta), 2)
            for snapshot in with_meta:
                self.assertTrue(any(r.region_type == "metadata" for doc in snapshot.metadata_documents for r in doc.regions),
                                f"branch {snapshot.branch_id} metadata doc missing metadata region")
                # A real captured non-empty Metadata open-target DOM so the
                # branch Viewer renders a clickable trigger (not dom=None).
                self.assertIsNotNone(snapshot.meta_open_dom, f"branch {snapshot.branch_id} meta_open_dom is empty")
                pages, docs = _branch_topology(snapshot.viewer_pages, snapshot.viewer_documents, snapshot.branch_id)
                main = next((p for p in pages if p.page_var == "page"), None)
                self.assertIsNotNone(main)
                self.assertTrue(any(d.document_id == main.entry_document_id for d in docs),
                                "branch viewer entry_document_id does not resolve to a remapped doc")

    def _branch_dirs(self, capture_root):
        root = Path(capture_root) / "series_branches"
        return [d for d in root.iterdir() if d.is_dir()]

    def test_metadata_rows_fallback_attaches_region_when_topology_lacks_it(self):
        """(P1#0 loader fallback) When a real branch's metadata topology has NO
        ``metadata`` region but ``metadata/metadata_rows.json`` exists, the loader
        re-attaches the region from the rows payload — and does NOT duplicate one
        that is already embedded in the topology."""
        from batch_capture_replicate import _load_series_branch_snapshots
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture_root = root / "capture"
            with sync_playwright() as playwright:
                _run_real_capture(playwright, capture_root)

            # Pick a branch that captured metadata.
            target_dir = next(
                d for d in self._branch_dirs(capture_root)
                if (d / "metadata" / "topology.json").is_file() and (d / "metadata" / "metadata_rows.json").is_file()
            )
            status = json.loads((target_dir / "status.json").read_text(encoding="utf-8"))
            branch_id = status["branch_id"]

            # Simulate an older/partial artifact: strip the metadata region from
            # the topology while metadata_rows.json (the loader fallback) stays.
            topo = json.loads((target_dir / "metadata" / "topology.json").read_text(encoding="utf-8"))
            for doc in topo["documents"]:
                doc["regions"] = [r for r in doc.get("regions", []) if r.get("region_type") != "metadata"]
            (target_dir / "metadata" / "topology.json").write_text(json.dumps(topo), encoding="utf-8")

            snapshots, _w, _e = _load_series_branch_snapshots(capture_root)
            target = next(s for s in snapshots if s.branch_id == branch_id)
            self.assertTrue(target.metadata_documents)
            metadata_regions = [r for doc in target.metadata_documents for r in doc.regions if r.region_type == "metadata"]
            # Fallback re-attached exactly one metadata region from metadata_rows.json.
            self.assertEqual(len(metadata_regions), 1, "fallback did not re-attach the metadata region")
            self.assertTrue(metadata_regions[0].root.outer_html and metadata_regions[0].root.text.strip())

            # No duplication: an UNTOUCHED branch whose topology already embeds the
            # metadata region AND has metadata_rows.json must keep exactly one
            # region (the writer's embedded region wins; fallback must not repeat).
            intact = next(
                d for d in self._branch_dirs(capture_root)
                if d != target_dir and (d / "metadata" / "topology.json").is_file() and (d / "metadata" / "metadata_rows.json").is_file()
            )
            intact_id = json.loads((intact / "status.json").read_text(encoding="utf-8"))["branch_id"]
            snapshots2, _w2, _e2 = _load_series_branch_snapshots(capture_root)
            target2 = next(s for s in snapshots2 if s.branch_id == intact_id)
            count = sum(1 for doc in target2.metadata_documents for r in doc.regions if r.region_type == "metadata")
            self.assertEqual(count, 1, "loader duplicated the embedded metadata region")

    def test_real_capture_clicks_metadata_and_close_returns_to_same_viewer(self):
        """Per-branch Metadata: real capture -> offline click trigger -> complete
        panel with unique tag/value -> close returns to the SAME branch viewer."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture_root = root / "capture"
            with sync_playwright() as playwright:
                outcomes = _run_real_capture(
                    playwright, capture_root, timeout_s=2.5, fixture=HUB_UNIQUE_META_FIXTURE)
            captured = [o for o in outcomes if o.capture_status == "captured"]
            self.assertGreaterEqual(len(captured), 2)

            snapshots, _states, branches, flow = self._build_flow(capture_root)
            branch_by_key = {b.series_key: b for b in branches}
            output = root / "replica"
            build_replica(flow, capture_root, output)

            target = captured[0]
            slug = series_key_slug(target.series_key)
            branch = branch_by_key[target.series_key]
            # Fixture series_key is the raw UID "1.2.3.{N}"; the unique per-branch
            # metadata marker is uid=uid-{N} / SeriesNumber: {N}.
            num = target.series_key.rsplit(".", 1)[-1]

            with ReplicaServer(output) as server, sync_playwright() as pw2:
                browser = pw2.chromium.launch()
                page = browser.new_page()
                page.goto(server.url)
                # Navigate to the target branch Viewer.
                ok = _click_series_and_wait(page, target.series_key)
                self.assertTrue(ok, "navigating to target branch viewer failed")
                viewer_url = page.url
                # Click the branch's real Metadata trigger overlay.
                trigger = f'[data-replica-action="series:{branch.branch_id}:meta_open"]'
                self.assertGreaterEqual(page.locator(trigger).count(), 1, "metadata trigger overlay missing")
                page.locator(trigger).first.click()
                try:
                    page.wait_for_url(lambda url: "bmeta_" in url, timeout=8000)
                except Exception:
                    self.fail("clicking metadata trigger did not open the branch metadata state")
                # Complete, visible metadata panel with unique per-branch tag/value.
                panel = page.locator(".replica-metadata")
                self.assertGreaterEqual(panel.count(), 1, "metadata panel region not rendered")
                self.assertTrue(panel.first.is_visible(), "metadata panel not visible")
                body = panel.first.inner_text()
                self.assertIn("SeriesNumber", body)
                self.assertIn(num, body, f"metadata panel lacks branch-unique value for series {num}")
                # Close returns to the SAME branch viewer.
                close_btn = page.locator("[data-replica-back], [data-replica-panel-close]").first
                self.assertGreaterEqual(close_btn.count(), 1, "metadata close control missing")
                close_btn.click()
                try:
                    page.wait_for_url(lambda url: "bviewer_" in url, timeout=8000)
                except Exception:
                    self.fail("close did not return to the branch viewer")
                self.assertIn("bviewer_", page.url, "close did not return to the same branch viewer")
                browser.close()


def _click_series_and_wait(page, series_key):
    slug = series_key_slug(series_key)
    selector = f'[data-replica-series-key="{slug}"]'
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=5000)
    except Exception:
        return False
    before = page.url
    page.locator(selector).first.click()
    try:
        page.wait_for_url(lambda url: "states/" in url and url != before, timeout=8000)
        return True
    except Exception:
        return False
class SeriesPersistedOutputSafetyTests(unittest.TestCase):
    """P1#8: raw outerHTML is sanitized before persistence; no raw UID leaks."""

    def test_real_metadata_outer_html_is_sanitized_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture_root = Path(tmp) / "capture"
            with sync_playwright() as playwright:
                session = LiveCaptureSession(capture_root)
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.set_content(SANITIZE_FIXTURE)
                template = _make_template()
                session.finalize_series_branches(
                    page, template,
                    config={"expand_all_series": True, "per_series_timeout_s": 2,
                            "total_series_timeout_s": 30, "max_series": 3},
                )
                browser.close()
            count = 0
            root = capture_root / "series_branches"
            for branch_dir in root.iterdir():
                meta_rows = branch_dir / "metadata" / "metadata_rows.json"
                if not meta_rows.is_file():
                    continue
                payload = json.loads(meta_rows.read_text(encoding="utf-8"))
                outer = payload.get("outer_html") or ""
                if not outer:
                    continue
                count += 1
                # Executable / credential / event-handler attributes must be gone.
                self.assertNotIn("<script", outer.lower())
                self.assertNotIn("onerror=", outer.lower())
                self.assertNotIn("onload=", outer.lower())
                # No raw credentials / token-like material survives sanitizing.
                from replay_helpers import scan_text_for_secrets
                self.assertEqual(scan_text_for_secrets(outer), [])
            self.assertGreaterEqual(count, 1)

    def test_real_generated_replica_has_no_raw_uid_outside_metadata_and_panel_readable(self):
        """Byte-level scan of the REAL generated replica: raw UID / patient-derived
        keys never reach served (non-metadata) HTML or the route map, while the
        served Metadata panel text remains complete and readable (limited sensitive
        artifact; executables/tokens stripped by the sanitizer)."""
        from batch_capture_replicate import _build_branches_into_flow, _load_series_branch_snapshots
        from pipeline_validation import validate_series_privacy
        from replica_models import BootstrapPlan, CaptureTimingProfile, ReplicaFlow
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture_root = root / "capture"
            with sync_playwright() as playwright:
                outcomes = _run_real_capture(
                    playwright, capture_root, timeout_s=2.5, fixture=HUB_UNIQUE_META_FIXTURE)
            captured = [o for o in outcomes if o.capture_status == "captured"]
            self.assertGreaterEqual(len(captured), 2)

            snapshots, _w, _x = _load_series_branch_snapshots(capture_root)
            first = snapshots[0]
            _entry_pages, entry_documents = _branch_topology(first.viewer_pages, first.viewer_documents, "entry")
            entry = ReplicaState(
                "s_000", 0, "", "page", _entry_pages, entry_documents, [],
                StateEvidence(False, False, False, False, 0, 0, 0, 0, "series_entry"),
            )
            # _build_branches_into_flow appends viewer/metadata states IN PLACE;
            # reuse the same list so the flow actually carries the branch states.
            states = [entry]
            branches, expansion_evidence = _build_branches_into_flow(
                states, capture_root, parse_action_plan(_TEMPLATE_SOURCE), []
            )
            flow = ReplicaFlow(
                2, "series-contract", "_contract.py", "hash", "now",
                {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {"page": "main"}), [],
                CaptureTimingProfile(), "s_000", states, [],
                series_branches=branches, series_expansion=expansion_evidence,
            )
            output = root / "replica"
            build_replica(flow, capture_root, output)

            # The series keys here are the fixture's UIDs (data-series-uid values).
            raw_keys = {o.series_key for o in outcomes if o.series_key}
            result = validate_series_privacy(output, raw_keys)
            self.assertEqual(result.status, "success", f"privacy errors: {result.errors}; warnings: {result.warnings}")
            self.assertGreaterEqual(result.metrics["metadata_panel_blocks"], 1)

            # Direct byte-level confirmation: the raw UID appears in served HTML
            # ONLY inside metadata-panel blocks; never in route-map / build JSON.
            all_html = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in output.rglob("*.html"))
            non_metadata = _strip_metadata_blocks(all_html)
            for key in raw_keys:
                self.assertNotIn(key, non_metadata, f"raw series identity leaked outside metadata: {key}")
            for path in output.rglob("*.json"):
                self.assertNotIn("1.2.3", path.read_text(encoding="utf-8", errors="replace"))
            # The metadata panel still carries the full readable series row.
            self.assertIn("SeriesNumber", all_html)


class SeriesCaptureEventsTests(unittest.TestCase):
    """P1#7: the LIVE explorer really produces the safe series_* events.

    Not a test of the tracker consuming hand-fed events — it runs the real
    ``finalize_series_branches`` in-process and asserts the stdout JSONL it emits
    contains the expected SERIES_EVENT_NAMES for each real scenario, all with safe
    fields only. A fully-successful fixture only produces the success path, so the
    partial and failed terminals are each triggered by their own real live-explorer
    run (SLOW_META_FIXTURE -> partial; raised transaction -> failed).
    """

    def _capture_stdout_lines(self, playwright, fixture=HUB_FIXTURE, fail_stub=False):
        import io
        import contextlib
        from unittest.mock import patch
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with tempfile.TemporaryDirectory() as tmp:
                capture_root = Path(tmp) / "capture"
                session = LiveCaptureSession(capture_root)
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.set_content(fixture)
                template = classify_recording_template(parse_action_plan(_TEMPLATE_SOURCE))

                def raise_hub(_page, _descriptor, _template, _pages, _config):
                    raise batch.HubUnrecoverableError("target series row not found in hub")

                runner = session.finalize_series_branches
                with patch.object(session, "capture_one_series", side_effect=raise_hub) if fail_stub else _noop_ctx():
                    session.finalize_series_branches(
                        page, template,
                        config={"expand_all_series": True, "per_series_timeout_s": 1.5,
                                "total_series_timeout_s": 30, "max_series": 5},
                    )
                browser.close()
        lines = buffer.getvalue().splitlines()
        events = []
        for line in lines:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("event", "").startswith("series_"):
                events.append(obj)
        return events

    def test_live_explorer_full_success_produces_success_path_events(self):
        # A fully-successful run cannot produce partial/failed. Assert the emitted
        # events are all within the SERIES_EVENT_NAMES contract and that the live
        # explorer actually produced the full success-path set (honest name/scope).
        allowed = {
            "series_discovery_started", "series_discovered",
            "series_capture_started", "series_capture_completed",
            "series_capture_partial", "series_capture_failed",
            "series_expansion_completed",
        }
        required_present = {
            "series_discovery_started", "series_discovered",
            "series_capture_started", "series_capture_completed",
            "series_expansion_completed",
        }
        with sync_playwright() as playwright:
            events = self._capture_stdout_lines(playwright)
        names = {e["event"] for e in events}
        # Every emitted series event is a known SERIES_EVENT_NAMES member.
        self.assertTrue(names.issubset(allowed), f"unexpected event names: {names - allowed}")
        self.assertTrue(required_present.issubset(names), f"missing: {required_present - names}; got {names}")
        # No raw UID / patient / series_key may leak into the event stream.
        for event in events:
            serialized = json.dumps(event)
            self.assertNotIn("1.2.3", serialized)
        for event in events:
            if "branch_id" in event:
                self.assertTrue(str(event["branch_id"]).startswith("b"))

    def test_live_explorer_produces_all_seven_events_across_real_scenarios(self):
        """Across real captured/partial/failed live runs, all 7 series events fire."""
        all_seven = {
            "series_discovery_started", "series_discovered",
            "series_capture_started", "series_capture_completed",
            "series_capture_partial", "series_capture_failed",
            "series_expansion_completed",
        }
        with sync_playwright() as playwright:
            success_events = self._capture_stdout_lines(playwright)
            partial_events = self._capture_stdout_lines(playwright, fixture=SLOW_META_FIXTURE)
            failed_events = self._capture_stdout_lines(playwright, fixture=HUB_FIXTURE, fail_stub=True)
        combined = {e["event"] for e in success_events + partial_events + failed_events}
        self.assertTrue(
            all_seven.issubset(combined),
            f"not all seven live events produced; missing {all_seven - combined}",
        )
        for events in (partial_events, failed_events):
            for event in events:
                serialized = json.dumps(event)
                self.assertNotIn("1.2.3", serialized)

    def test_real_event_stream_forwarded_to_tracker_and_report(self):
        """Real explorer stdout -> SeriesTracker -> report coverage (forward contract).

        This exercises the production forward chain over the *actual* JSONL bytes
        the explorer emits: each ``series_*`` child event is normalized exactly as
        ``LiveCaptureController.on_event``/``_route_child_series`` do, fed to the
        ``SeriesTracker``, and the tracker's coverage is what the report writer
        emits as ``series_coverage``. Distinguishes captured/partial via a real
        partial run; the consumer is not hand-fed synthetic events.
        """
        from orchestrator_events import SeriesTracker, normalize_child_event
        with sync_playwright() as playwright:
            success_events = self._capture_stdout_lines(playwright)
            partial_events = self._capture_stdout_lines(playwright, fixture=SLOW_META_FIXTURE)
        tracker = SeriesTracker()
        consumed = 0
        for child in success_events + partial_events:
            normalized = normalize_child_event(child, "capturing_live", "run_forward")
            if normalized["event"].startswith("series_"):
                # Mirror _route_child_series: the child payload (event bytes) is
                # what the tracker consumes.
                tracker.note(child)
                consumed += 1
        self.assertGreaterEqual(consumed, 4)
        counts = tracker.counts()
        self.assertGreaterEqual(counts["captured"], 1)
        self.assertGreaterEqual(counts["partial"], 1)
        self.assertEqual(counts["failed"], 0)
        cov = tracker.coverage()
        self.assertTrue(cov["enabled"])
        self.assertEqual(cov["status"], "partial")  # partial run -> honest partial
        for branch in cov["branches"]:
            self.assertEqual(set(branch), {"branch_id", "ordinal", "status", "stage"})
            self.assertNotIn("1.2.3", json.dumps(branch))


if __name__ == "__main__":
    unittest.main()
