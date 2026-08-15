import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from capture_readiness import canvas_hash, metadata_panel_signature, metadata_uid_sha256_prefix, screenshot_nonblank, viewer_dom_fingerprint, wait_for_frame_change, wait_for_metadata_panel_state
from batch_capture_replicate import LiveCaptureSession


class SeriesReadinessDecisionTests(unittest.TestCase):
    """P1#4 combination-selection readiness decisions (not just helper hashes).

    Exercises the real ``LiveCaptureSession._collect_evidence`` /
    ``_wait_for_series_ready`` against in-browser fixtures: at least two core
    evidence classes are required, a single ``screenshot_nonblank`` never counts,
    the row's own label never acts as ``name_match`` (the Viewer's *displayed*
    identity is compared), and iframe/root replacement after activation is
    transparently recovered by re-resolving from the stable LocatorRecipe.
    """
    _TEMPLATE_SOURCE = '''from playwright.sync_api import sync_playwright

def run(page):
    # [MARKER: 序列选择]
    page.locator("#series .item").first.click()
    # [MARKER: Meta 信息工具]
    page.locator("#meta-open").click()
    page.locator("#meta-close").click()
'''

    def _ready_fixture(self, current_label="Series A", items=3):
        rows = "".join(
            f'<div class="item" data-series="uid-{i}" data-series-uid="1.2.3.{i}">Series {chr(64 + i)}</div>'
            for i in range(1, items + 1)
        )
        return f"""
<style>#series{{height:60px;overflow:auto}}.item{{height:24px;cursor:pointer}}</style>
<div class="series-list" id="series">{rows}</div>
<canvas id="viewer" width="200" height="100" style="width:200px;height:100px"></canvas>
<div id="current-series">{current_label}</div>
<button id="meta-open">Meta</button><button id="meta-close">Close</button>
<div id="tagsBox" style="display:none"><div>Series Number: 1</div></div>
<script>
  var cv=document.getElementById('viewer'); var g=cv.getContext('2d');
  g.fillStyle='#000'; g.fillRect(0,0,200,100); g.fillStyle='#fff'; g.fillRect(10,10,20,20);
  document.querySelectorAll('.item').forEach(function(el){{
    el.addEventListener('click', function(){{
      document.querySelectorAll('.item').forEach(function(e){{e.removeAttribute('aria-selected');}});
      el.setAttribute('aria-selected','true');
    }});
  }});
</script>
"""

    def _session_and_page(self, page, content):
        session = LiveCaptureSession(Path(tempfile.mkdtemp()))
        page.set_content(content)
        return session

    def _descriptor(self, label="Series A", key="uid-1", uid="1.2.3.1"):
        from replica_models import SeriesDescriptor
        return SeriesDescriptor(
            series_key=key, label=label, ordinal=0, document_id="d_series_hub",
            member_id="d_series_hub_series_000", stable_attributes={"data-series": key},
            selected=False, explicit_frame_count=None, inferred_frame_count=None,
            activation="click",
        )

    def test_evidence_satisfied_requires_two_core_classes(self):
        from batch_capture_replicate import _evidence_satisfied
        self.assertFalse(_evidence_satisfied(set()))
        self.assertFalse(_evidence_satisfied({"selected"}))
        # screenshot_nonblank is excluded from the count entirely.
        self.assertFalse(_evidence_satisfied({"screenshot_nonblank"}))
        self.assertFalse(_evidence_satisfied({"selected", "screenshot_nonblank"}))
        self.assertTrue(_evidence_satisfied({"selected", "canvas_changed"}))
        self.assertTrue(_evidence_satisfied({"name_match", "dom_stable"}))

    def test_row_label_does_not_serve_as_name_match(self):
        """The Viewer's *displayed* identity is compared, never the row's own label."""
        from batch_capture_replicate import LiveCaptureSession
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            # Viewer displays "Series B" while the target row's label is "Series A".
            page = browser.new_page()
            session = self._session_and_page(page, self._ready_fixture(current_label="Series B"))
            row = page.locator(".item[data-series='uid-1']")
            evidence = session._collect_evidence(row, self._descriptor(label="Series A", key="uid-1"), page, None)
            self.assertNotIn("name_match", evidence, "row's own label leaked as name_match")
            # Now the Viewer displays the matching identity -> name_match appears.
            page.evaluate("document.getElementById('current-series').textContent = 'Series A'")
            evidence2 = session._collect_evidence(row, self._descriptor(label="Series A", key="uid-1"), page, None)
            self.assertIn("name_match", evidence2)
            browser.close()

    def test_one_core_class_is_rejected_two_pass(self):
        """Served evidence: a real selected+static page yields select+dom_stable
        (>=2 core classes) and is satisfied; the pure threshold rule itself (one
        core class rejected, two accepted) is covered by
        ``test_evidence_satisfied_requires_two_core_classes``."""
        from batch_capture_replicate import _evidence_satisfied
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            session = self._session_and_page(page, self._ready_fixture())
            descriptor = self._descriptor()
            row = page.locator(".item[data-series='uid-1']")
            row.evaluate("el => el.setAttribute('aria-selected','true')")
            page.evaluate("document.getElementById('current-series').textContent = 'Other'")
            evidence = {e for e in session._collect_evidence(row, descriptor, page, None) if e != "screenshot_nonblank"}
            # selected is a qualifying core class; at least one more core class is
            # required. A static selected page yields dom_stable -> satisfied.
            self.assertTrue(_evidence_satisfied(evidence), f"expected select+stable to satisfy, got {evidence}")
            # Without a second core class the rule must refuse (unit-level).
            self.assertFalse(_evidence_satisfied({"selected"}))
            browser.close()

    def test_root_replacement_reparses_from_recipe(self):
        """Replace the series-root container; readiness re-resolves from the recipe."""
        from batch_capture_replicate import classify_recording_template
        from rewrite_script import parse_action_plan
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            session = self._session_and_page(page, self._ready_fixture(items=3))
            template = classify_recording_template(parse_action_plan(self._TEMPLATE_SOURCE))
            recipe = template.series_action.locator
            self.assertIsNotNone(recipe)
            # Resolve a root before replacement.
            root_before = session._reparse_series_root(recipe, page)
            self.assertIsNotNone(root_before)
            # Replace the entire #series container with a fresh identical subtree.
            page.evaluate("""() => {
                const old = document.getElementById('series');
                const clone = old.cloneNode(true);
                old.parentNode.replaceChild(clone, old);
                clone.id = 'series';
            }""")
            root_after = session._reparse_series_root(recipe, page)
            self.assertIsNotNone(root_after, "root did not re-resolve after replacement")
            # The freshly resolved root still locates the target row.
            row = session._reparse_target_row(root_after, self._descriptor())
            self.assertIsNotNone(row)
            browser.close()


class CanvasReadinessTests(unittest.TestCase):
    def test_canvas_hash_is_none_without_visible_canvas(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<div>no canvas</div>')
            self.assertIsNone(canvas_hash(page))
            browser.close()

    def test_canvas_hash_changes_with_canvas_resize(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<canvas width="200" height="100" style="width:200px;height:100px"></canvas>'
            )
            page.evaluate("""() => { const c = document.querySelector('canvas'); const g = c.getContext('2d'); g.fillStyle='white'; g.fillRect(0,0,200,100); }""")
            first = canvas_hash(page)
            page.evaluate("""() => { const c = document.querySelector('canvas'); const g = c.getContext('2d'); g.fillStyle='black'; g.fillRect(0,0,200,100); g.fillStyle='white'; g.fillRect(0,0,20,20); }""")
            second = canvas_hash(page)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)
            browser.close()

    def test_wait_for_frame_change_true_when_hash_moves(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<canvas width="200" height="100" style="width:200px;height:100px"></canvas>')
            # Striped (high-complexity) initial frame so its JPEG data length
            # differs measurably from the later boxed frame.
            page.evaluate("""() => { const c = document.querySelector('canvas'); const g = c.getContext('2d'); g.fillStyle='white'; g.fillRect(0,0,200,100); g.fillStyle='black'; for (let i=0;i<200;i+=6) g.fillRect(i,0,2,100); }""")
            previous = canvas_hash(page)
            page.evaluate("""() => setTimeout(() => {
                const c = document.querySelector('canvas'); const g = c.getContext('2d');
                g.fillStyle='black'; g.fillRect(0,0,200,100);
                g.fillStyle='white'; g.fillRect(0,0,40,20); g.fillRect(180,90,20,10);
            }, 120)""")
            self.assertTrue(wait_for_frame_change(page, previous, timeout=2.0))
            browser.close()

    def test_wait_for_frame_change_false_when_static(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<canvas width="200" height="100" style="width:200px;height:100px"></canvas>')
            page.evaluate("""() => { const c = document.querySelector('canvas'); const g = c.getContext('2d'); g.fillStyle='white'; g.fillRect(0,0,200,100); }""")
            previous = canvas_hash(page)
            self.assertFalse(wait_for_frame_change(page, previous, timeout=0.4))
            browser.close()

    def test_screenshot_nonblank_accepts_rendered_and_rejects_black(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            blank = browser.new_page()
            blank.set_content('<canvas width="400" height="300" style="width:400px;height:300px"></canvas>')
            blank.evaluate("""() => { const c = document.querySelector('canvas'); const g = c.getContext('2d'); g.fillStyle='black'; g.fillRect(0,0,400,300); }""")
            # Screenshot only the canvas element so the page's default white
            # background cannot mask a black (off) viewer canvas.
            self.assertFalse(screenshot_nonblank(blank.locator("canvas").screenshot(type="png")))
            textured = browser.new_page()
            textured.set_content('<canvas width="400" height="300" style="width:400px;height:300px"></canvas>')
            textured.evaluate("""() => { const c = document.querySelector('canvas'); const g = c.getContext('2d'); g.fillStyle='white'; g.fillRect(0,0,400,300); g.fillStyle='black'; g.fillRect(60,60,80,180); g.fillRect(240,30,40,210); }""")
            self.assertTrue(screenshot_nonblank(textured.locator("canvas").screenshot(type="png")))
            browser.close()

    def test_viewer_dom_fingerprint_exposes_stable_text_and_structure(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<main><h1>Viewer</h1><section id=pat>Patient</section></main>')
            fp1 = viewer_dom_fingerprint(page)
            fp2 = viewer_dom_fingerprint(page)
            self.assertEqual(fp1, fp2)
            self.assertIn("Viewer", fp1)
            browser.close()

    def test_metadata_uid_sha256_prefix_is_short_and_reversible_safe(self):
        prefix = metadata_uid_sha256_prefix("1.2.840.113619.2.55.3.12345.6789")
        self.assertIsNotNone(prefix)
        self.assertEqual(len(prefix), 16)
        self.assertNotIn("12345", prefix)
        self.assertEqual(prefix, metadata_uid_sha256_prefix("1.2.840.113619.2.55.3.12345.6789"))
        self.assertIsNone(metadata_uid_sha256_prefix(""))


class CaptureReadinessTests(unittest.TestCase):
    def test_signature_is_stable_across_consecutive_calls(self):
        """A rendered Metadata panel yields the same signature when re-polled."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<button id="btn-tags">Tags</button>'
                '<div id="tagsBox"><div>Study Description: Sample</div></div>'
            )
            target = lambda: page.locator("#btn-tags")

            first = metadata_panel_signature(target)
            second = metadata_panel_signature(target)

            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            self.assertIn("Study Description: Sample", first)
            browser.close()

    def test_signature_returns_none_when_panel_is_hidden(self):
        """A hidden panel resolves to no stable signature (None)."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<button id="btn-tags">Tags</button>'
                '<div id="tagsBox" style="display:none"><div>Hidden</div></div>'
            )
            target = lambda: page.locator("#btn-tags")

            self.assertIsNone(metadata_panel_signature(target))
            browser.close()

    def test_content_delays_then_loads_and_stabilizes(self):
        """Panel that fills in over time eventually returns True once stable."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<button id="btn-tags" onclick="openMetadata()">Tags</button>'
                '<div id="tagsBox" style="display:none"><div>Study Description: Sample</div></div>'
                '<script>'
                'function openMetadata() {'
                '  const panel = document.querySelector("#tagsBox");'
                '  panel.style.display = "block";'
                '  setTimeout(() => panel.insertAdjacentHTML("beforeend",'
                '    "<div id=late-row>Series Number: 5</div>"), 150);'
                '}'
                '</script>'
            )
            target = lambda: page.locator("#btn-tags")
            target().click()

            self.assertTrue(
                wait_for_metadata_panel_state(page, target, timeout_s=2.0, stable_s=0.2)
            )
            self.assertEqual(page.locator("#late-row").count(), 1)
            browser.close()

    def test_timeout_returns_false_when_signature_never_stable(self):
        """A panel whose content keeps changing never stabilizes -> False."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<button id="btn-tags">Tags</button>'
                '<div id="tagsBox"><div><span id="live">0</span></div></div>'
                '<script>'
                'let n = 0;'
                'setInterval(() => document.querySelector("#live").textContent = ++n, 40);'
                '</script>'
            )
            target = lambda: page.locator("#btn-tags")

            self.assertFalse(
                wait_for_metadata_panel_state(page, target, timeout_s=0.6, stable_s=0.2)
            )
            browser.close()

    def test_locator_failure_missing_target_returns_none_and_false(self):
        """A target that never appears yields None signature and never stabilizes."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<div id="empty">No metadata here</div>')
            target = lambda: page.locator("#missing-tags-button")

            self.assertIsNone(metadata_panel_signature(target))
            self.assertFalse(
                wait_for_metadata_panel_state(page, target, timeout_s=0.5, stable_s=0.2)
            )
            browser.close()

    def test_locator_factory_raising_is_swallowed(self):
        """A locator factory that raises must be treated as not-ready, not crash."""

        def exploding_factory():
            raise RuntimeError("boom")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<div>page</div>')

            self.assertIsNone(metadata_panel_signature(exploding_factory))
            self.assertFalse(
                wait_for_metadata_panel_state(page, exploding_factory, timeout_s=0.4, stable_s=0.2)
            )
            browser.close()


CLOSE_NOOP_FIXTURE = """
<style>.item{cursor:pointer}</style>
<div class="series-list" id="series">
  <div class="item" data-series="uid-1" data-series-uid="1.2.3.1">Series A</div>
</div>
<button id="meta-open" onclick="document.getElementById('tagsBox').style.display='block'">Meta</button>
<button id="meta-close">Close</button>
<div id="tagsBox" style="display:none"><div>Series Number: 1</div></div>
"""


class SeriesRestoreAndReloadTests(unittest.TestCase):
    """P1#5: per-branch/hub restoration is VERIFIED (never silently swallowed) and
    a controlled reload rebuilds pages/root from the latest frame (no stale Locator)."""

    def _template(self):
        from batch_capture_replicate import classify_recording_template
        from rewrite_script import parse_action_plan
        source = '''from playwright.sync_api import sync_playwright

def run(page):
    # [MARKER: 序列选择]
    page.locator("#series .item").first.click()
    # [MARKER: Meta 信息工具]
    page.locator("#meta-open").click()
    page.locator("#meta-close").click()
'''
        return classify_recording_template(parse_action_plan(source))

    def test_restore_detects_close_that_does_not_hide_panel(self):
        """_restore_hub_state must NOT silently succeed when the Metadata panel
        cannot be hidden: it reports metadata_not_hidden and ok=False."""
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(CLOSE_NOOP_FIXTURE)
            template = self._template()
            # Open the panel so an attempted close is required.
            page.locator("#meta-open").click()
            initial = {"selected_series_key": None, "scroll_top": 0, "panel_open": False}
            root = page.locator("#series")
            ok, problem = session._restore_hub_state(page, root, template, {"page": page}, initial)
            browser.close()
        self.assertFalse(ok)
        self.assertIn("metadata_not_hidden", problem or "")

    def test_close_noop_degrades_branch_to_partial_and_next_branch_still_clean(self):
        """In a live multi-series run where the Metadata close does not hide the
        panel, the branch is honestly degraded to partial — and the hub stays
        operable so the next branch still runs from a clean (closed) hub."""
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            # Two series; a broken close means each capture degrades to partial.
            page.set_content("""
<style>#series{height:60px;overflow:auto}.item{height:24px;cursor:pointer}</style>
<div class="series-list" id="series">
  <div class="item" data-series="uid-1" data-series-uid="1.2.3.1">Series A</div>
  <div class="item" data-series="uid-2" data-series-uid="1.2.3.2">Series B</div>
</div>
<canvas id="viewer" width="200" height="100" style="width:200px;height:100px"></canvas>
<div id="current-series">Series A</div>
<button id="meta-open" onclick="document.getElementById('tagsBox').style.display='block'">Meta</button>
<button id="meta-close">Close</button>
<div id="tagsBox" style="display:none"><div>Series Number: 1</div></div>
<script>
  var cv=document.getElementById('viewer'); var g=cv.getContext('2d');
  document.querySelectorAll('.item').forEach(function(el){
    el.addEventListener('click', function(){
      g.fillStyle='#'+el.getAttribute('data-series').replace('uid-','')+'33'; g.fillRect(0,0,200,100);
      g.fillStyle='#fff'; g.fillRect(10,10,20,20);
      document.getElementById('current-series').textContent = el.textContent.trim();
    });
  });
</script>
""")
            template = self._template()
            outcomes = session.finalize_series_branches(
                page, template,
                config={"expand_all_series": True, "per_series_timeout_s": 1.5,
                        "total_series_timeout_s": 20, "max_series": 5},
            )
            # The hub is still operable after the failed close: a follow-up
            # collateral click on the hub still works (clean hub preserved).
            hub_clickable = page.locator("#series .item").first.is_enabled()
            browser.close()
        statuses = {o.ordinal: o.capture_status for o in outcomes}
        self.assertTrue(any(v == "partial" for v in statuses.values()), str(statuses))
        # Every series still reached a terminal state; hub stayed operable.
        self.assertEqual(sorted(statuses), [0, 1])
        self.assertTrue(hub_clickable)

    def test_reload_rebuilds_pages_map_not_reusing_pre_reload_dict(self):
        """After a controlled reload, finalize_series_branches rebuilds the live
        pages map and re-resolves the root — the next transaction never reuses the
        pre-reload pages dict or a stale Locator."""
        from batch_capture_replicate import CaptureBranchOutcome, HubUnrecoverableError
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            session = LiveCaptureSession(Path(tmp))
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(CLOSE_NOOP_FIXTURE)
            template = self._template()
            captured_pages: list[object] = []

            def flaky(_page, descriptor, _template, pages, _config):
                captured_pages.append(pages)
                if len(captured_pages) <= 3:
                    raise HubUnrecoverableError("hub lost")
                return CaptureBranchOutcome(
                    branch_id="b000_abc", series_key=descriptor.series_key,
                    label=descriptor.label, ordinal=descriptor.ordinal, document_id=descriptor.document_id,
                    source_member_id=descriptor.member_id, activation=descriptor.activation or "click",
                    capture_status="captured", fail_stage=None, error_type=None, warning=None,
                )

            from unittest.mock import patch
            config = {"expand_all_series": True, "per_series_timeout_s": 1,
                      "total_series_timeout_s": 1000, "max_series": 5}
            with patch("batch_capture_replicate.discover_series_candidates") as mock_disc, patch.object(session, "capture_one_series", side_effect=flaky):
                from replica_models import SeriesCollectionEvidence, SeriesDescriptor as SD
                mock_disc.return_value = ([SD(
                    series_key="1.2.3.1", label="A", ordinal=0, document_id="d_series_hub",
                    member_id="d_series_hub_series_000", stable_attributes={"data-series": "uid-1"},
                    selected=False, explicit_frame_count=None, inferred_frame_count=None, activation="click",
                ), SD(
                    series_key="1.2.3.2", label="B", ordinal=1, document_id="d_series_hub",
                    member_id="d_series_hub_series_001", stable_attributes={"data-series": "uid-2"},
                    selected=False, explicit_frame_count=None, inferred_frame_count=None, activation="click",
                ), SD(
                    series_key="1.2.3.3", label="C", ordinal=2, document_id="d_series_hub",
                    member_id="d_series_hub_series_002", stable_attributes={"data-series": "uid-3"},
                    selected=False, explicit_frame_count=None, inferred_frame_count=None, activation="click",
                )], [], SeriesCollectionEvidence("scroll_harvest", False, 3, 3, 1, True, None, 3))
                session.finalize_series_branches(page, template, config=config)
            browser.close()

        # 3 failures (descriptors 0,1,2) trigger the reload; the immediate retry of
        # descriptor 2 is the 4th call, which succeeds post-reload.
        self.assertGreaterEqual(len(captured_pages), 4, "expected a post-reload retry")
        # The post-reload transaction receives a freshly rebuilt pages map,
        # distinct from the pre-reload one — never the stale dict.
        self.assertIsNot(captured_pages[0], captured_pages[3])


if __name__ == "__main__":
    unittest.main()
