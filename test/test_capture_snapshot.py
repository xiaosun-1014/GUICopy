import unittest
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

from capture_snapshot import capture_interaction_region, capture_locator_snapshot, capture_marker_interaction_region, capture_marker_panel_region, capture_selector_closure, dom_snapshot_from_payload, sanitize_html


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

    def test_capture_locator_snapshot_preserves_live_input_value(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<input id="wl" value="40">')
            page.locator("#wl").fill("125")
            snapshot = capture_locator_snapshot(page.locator("#wl"))
            browser.close()

        self.assertEqual(snapshot.attributes["value"], "125")

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

    def test_capture_interaction_region_does_not_duplicate_styled_containers(self):
        markup = "<main id='root'>" + "".join(
            f"<div class='layer'>layer-{index}</div>" for index in range(250)
        ) + "<a class='tool' href='#'>Open</a><button>Apply</button></main>"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            region = capture_interaction_region(page.locator("#root"), "generic", "d_main")
            browser.close()

        self.assertEqual([member.dom.tag_name for member in region.members], ["a", "button"])

    def test_interaction_region_degrades_members_to_empty_on_batch_failure(self):
        """M1: a single evaluate_all failure must not drop the whole region."""
        import io as _io
        from unittest.mock import patch

        from capture_snapshot import _capture_locator_snapshots
        from replica_models import DomNodeSnapshot, Rect

        root_locator = SimpleNamespace(locator=lambda selector: SimpleNamespace())
        root_dom = DomNodeSnapshot(
            tag_name="section",
            text="Report",
            attributes={"id": "report"},
            rect=Rect(0, 0, 100, 50, "region_content_css"),
            outer_html='<section id="report">Report</section>',
            computed_style={"display": "block"},
        )
        stderr = _io.StringIO()
        with patch("sys.stderr", stderr), patch(
            "capture_snapshot.capture_locator_snapshot", return_value=root_dom
        ), patch(
            "capture_snapshot._capture_locator_snapshots",
            side_effect=RuntimeError("boom"),
        ):
            region = capture_interaction_region(root_locator, "report", "d_main")

        self.assertIsNotNone(region, "region root must survive a batch member failure")
        self.assertEqual(region.region_type, "report")
        self.assertEqual(region.document_id, "d_main")
        self.assertIs(region.root, root_dom, "region root must be preserved intact")
        self.assertIn("degraded", stderr.getvalue())
        # Batch members degrade to zero (plain section root is not a member).
        self.assertEqual(region.members, [])

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
        self.assertEqual([member.dom.attributes.get("id") for member in region.members], ["btn-tags"])

    def test_metadata_panel_rejects_tags_icon_and_patient_summary(self):
        markup = """
        <i class="icon icon-tags"></i>
        <aside id="patientInfo" class="patientInfo">Patient summary</aside>
        """
        from capture_snapshot import _MARKER_REGION_CANDIDATES
        candidates = _MARKER_REGION_CANDIDATES["Meta 信息工具"][1]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.set_content(markup)
            region = capture_marker_panel_region(page, candidates, "d_main")
            browser.close()

        self.assertIsNone(region)


def _fake_payload(tag: str = "div") -> dict:
    """A minimal evaluate payload the fake locators return (mirrors the DOM
    contract of ``capture_locator_snapshot``'s browser-side expression)."""
    return {
        "tag_name": tag,
        "text": "x",
        "attributes": {"id": "fake"},
        "rect": {"x": 1, "y": 2, "width": 10, "height": 10},
        "outer_html": f"<{tag} id='fake'>x</{tag}>",
        "computed_style": {"display": "block"},
    }


class CaptureSnapshotFakeLocatorTests(unittest.TestCase):
    """步骤 1 多匹配/无匹配防护：fake locator 单测，不依赖真实浏览器。

    真实浏览器仅用于验证「单匹配路径不受影响」（test_capture_locator_snapshot_reads_a_real_playwright_element）。
    """

    def test_capture_locator_snapshot_multimatch_returns_first(self):
        # count()>1 的 fake locator：capture_locator_snapshot 必须归一 .first，
        # 返回非空快照（不抛 strict-mode 异常）。
        payload = _fake_payload("li")
        first = SimpleNamespace()

        def first_evaluate(fn, *args):
            return payload
        first.evaluate = first_evaluate
        root = SimpleNamespace()
        root.count = lambda: 2
        root.first = first

        snapshot = capture_locator_snapshot(root, "page_viewport_css")

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.tag_name, "li")
        self.assertEqual(snapshot.rect.coordinate_space, "page_viewport_css")

    def test_capture_locator_snapshot_no_match_returns_none(self):
        root = SimpleNamespace()
        root.count = lambda: 0
        root.first = None

        self.assertIsNone(capture_locator_snapshot(root, "page_viewport_css"))
        # 不抛异常是契约的一部分：count()==0 → None，调用方显式处理。

    def test_capture_locator_snapshot_single_match_still_reads_payload(self):
        # 单匹配路径不受影响：evaluate 原样返回 payload。
        payload = _fake_payload("button")
        locator = SimpleNamespace()
        locator.count = lambda: 1
        locator.first = None

        def evaluate(fn, *args):
            return payload
        locator.evaluate = evaluate

        snapshot = capture_locator_snapshot(locator)
        self.assertEqual(snapshot.tag_name, "button")

    def test_capture_selector_closure_multimatch_returns_first(self):
        # capture_selector_closure 同源 risk：多匹配归一 .first，count()==0 返回 None。
        first = SimpleNamespace()

        def first_evaluate(fn, *args):
            return {"outer": "<div>outer</div>", "ancestors": 2, "siblings": 3, "sources": ["aria-label"]}
        first.evaluate = first_evaluate

        root = SimpleNamespace()
        root.count = lambda: 5
        root.first = first

        closure = capture_selector_closure(root, "a_001")
        self.assertIsNotNone(closure)
        self.assertEqual(closure.action_id, "a_001")
        self.assertEqual(closure.required_ancestor_count, 2)
        self.assertEqual(closure.required_sibling_count, 3)

    def test_capture_selector_closure_no_match_returns_none(self):
        root = SimpleNamespace()
        root.count = lambda: 0

        self.assertIsNone(capture_selector_closure(root, "a_001"))


if __name__ == "__main__":
    unittest.main()
