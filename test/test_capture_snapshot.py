import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from capture_snapshot import capture_interaction_region, capture_locator_snapshot, capture_marker_interaction_region, capture_marker_panel_region, dom_snapshot_from_payload, sanitize_html


class CaptureSnapshotTests(unittest.TestCase):
    def test_sanitized_html_removes_executable_and_remote_content(self):
        html = '''<section onclick="steal()"><script src="https://evil.test/x.js"></script><img src="https://evil.test/a.png"><a href="https://evil.test">report</a><button id="open">Open</button></section>'''

        clean = sanitize_html(html)

        self.assertNotIn("script", clean)
        self.assertNotIn("onclick", clean)
        self.assertNotIn("https://evil.test", clean)
        self.assertIn('id="open"', clean)

    def test_sanitized_html_removes_credentials_and_form_submission(self):
        source = '<form action="/login"><input type="password" value="secret"><input type="hidden" name="csrf_token" value="token"><input id="safe" value="ok"></form>'

        clean = sanitize_html(source)

        self.assertNotIn("password", clean)
        self.assertNotIn("csrf", clean)
        self.assertNotIn("action=", clean)
        self.assertIn('id="safe"', clean)

    def test_dom_payload_preserves_locator_attributes_and_select_options(self):
        payload = {
            "tag_name": "select",
            "text": "Thin\nThick",
            "attributes": {"id": "series", "aria-label": "Series", "data-testid": "series-picker"},
            "rect": {"x": 1, "y": 2, "width": 100, "height": 30},
            "outer_html": '<select id="series"><option value="thin" selected>Thin</option><option value="thick" disabled>Thick</option></select>',
            "computed_style": {"display": "block"},
        }

        snapshot = dom_snapshot_from_payload(payload, "page_viewport_css")

        self.assertEqual(snapshot.attributes["data-testid"], "series-picker")
        self.assertIn('value="thin"', snapshot.outer_html)
        self.assertIn("disabled", snapshot.outer_html)
        self.assertEqual(snapshot.rect.coordinate_space, "page_viewport_css")

    def test_capture_locator_snapshot_reads_a_real_playwright_element(self):
        fixture = Path(__file__).parent / "fixtures" / "replica_flow" / "report.html"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.goto(fixture.as_uri())
            snapshot = capture_locator_snapshot(page.locator("#open-viewer"))
            browser.close()

        self.assertEqual(snapshot.tag_name, "button")
        self.assertEqual(snapshot.attributes["data-testid"], "open-viewer")
        self.assertEqual(snapshot.rect.coordinate_space, "page_viewport_css")
        self.assertIn("查看影像", snapshot.text)

    def test_capture_interaction_region_keeps_controls_for_adapter_access(self):
        fixture = Path(__file__).parent / "fixtures" / "replica_flow" / "report.html"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(fixture.as_uri())
            region = capture_interaction_region(page.locator("#report"), "report", "d_main")
            browser.close()

        self.assertEqual(region.region_type, "report")
        self.assertEqual(region.document_id, "d_main")
        self.assertEqual({member.dom.tag_name for member in region.members}, {"button", "input", "select", "canvas"})
        self.assertIn("data-testid", next(member.dom.attributes for member in region.members if member.dom.tag_name == "button"))

    def test_marker_regions_prefer_their_semantic_root_over_the_full_page(self):
        markup = '''<main id="report">Report</main>
        <section class="layout-toolbar" id="layout">Layout</section>
        <section class="series-list" id="series">Series</section>
        <section role="dialog" aria-label="DICOM metadata" id="metadata">Meta</section>
        <section class="window-level-dialog" id="wlww">WL/WW</section>
        <canvas id="image-canvas"></canvas>'''
        expected = {
            "报告截图": ("report", "report"),
            "序列布局切换": ("layout", "layout"),
            "序列选择": ("series", "series"),
            "Meta 信息工具": ("metadata", "metadata"),
            "窗宽窗位 WL/WW": ("wlww", "wlww"),
            "影像画布交互": ("canvas", "image-canvas"),
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            actual = {
                label: capture_marker_interaction_region(page, label, "d_main").root.attributes.get("id")
                for label in expected
            }
            region_types = {label: capture_marker_interaction_region(page, label, "d_main").region_type for label in expected}
            browser.close()

        self.assertEqual(actual, {label: root_id for label, (_, root_id) in expected.items()})
        self.assertEqual(region_types, {label: region_type for label, (region_type, _) in expected.items()})

    def test_metadata_panel_captures_full_container_from_candidates(self):
        # A click-opened meta panel whose container is unrelated to dicom/metadata/
        # dialog keywords (e.g. ftimage's #tagsBox with class "box-tags"). The
        # generic panel capture must resolve it via the candidate selectors and
        # keep its complete HTML (trigger button lives outside the panel).
        markup = """<div id="tagsBox" class="box-tags" style="display:block">
          <div class="content">
            <div class="panel"><div class="hd">Patient Information</div><div class="bd">
              <div class="item">Patient Name(x00100010): <span>Tang Yuan Hua</span></div>
              <div class="item">Patient ID(x00100020): <span>0003699549</span></div>
            </div></div>
            <div class="panel"><div class="hd">Study Information</div><div class="bd">
              <div class="item">Study Date(x00080020): <span>20260723</span></div>
            </div></div>
          </div>
          <a href="javascript:;" class="close">close</a>
        </div>
        <button id="btn-tags">Tags</button>"""
        from capture_snapshot import _MARKER_REGION_CANDIDATES
        candidates = _MARKER_REGION_CANDIDATES["Meta 信息工具"][1]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.set_content(markup)
            region = capture_marker_panel_region(page, candidates, "d_main")
            browser.close()

        self.assertIsNotNone(region)
        self.assertEqual(region.region_type, "metadata")
        # The full panel HTML is preserved on the region root (full_html was a
        # redundant alias of root.outer_html and has been removed).
        self.assertIn("tagsBox", region.root.outer_html)
        self.assertIn("Patient Name(x00100010)", region.root.outer_html)
        self.assertIn("20260723", region.root.outer_html)
        self.assertIn("Study Information", region.root.outer_html)
        # The panel root resolved to tagsBox (id matches [id*='tags']).
        self.assertIn('id="tagsBox"', region.root.outer_html)


if __name__ == "__main__":
    unittest.main()
