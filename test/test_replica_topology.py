import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageStat
from playwright.sync_api import sync_playwright

from capture_snapshot import capture_page_topology


class ReplicaTopologyTests(unittest.TestCase):
    def test_captures_popup_and_nested_frame_tree(self):
        fixture = Path(__file__).parent / "fixtures" / "replica_flow" / "host.html"
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 800, "height": 600})
            page = context.new_page()
            page.goto(fixture.as_uri())
            with page.expect_popup() as popup_info:
                page.locator("#open-popup").click()
            popup = popup_info.value
            popup.wait_for_load_state()
            pages, documents = capture_page_topology(
                [("page", page), ("page1", popup)],
                Path(tmp),
            )
            self.assertEqual({entry.page_var for entry in pages}, {"page", "page1"})
            main_documents = [document for document in documents if document.page_var == "page"]
            self.assertEqual(len(main_documents), 3)
            inner = next(document for document in main_documents if document.frame_name == "imageFrame")
            self.assertEqual(inner.frame_selector, "#image-frame")
            self.assertEqual(inner.frame_name, "imageFrame")
            self.assertEqual(inner.parent_document_id, next(document.document_id for document in main_documents if document.frame_name == "viewerHost"))
            self.assertEqual(Path(inner.screenshot_asset_relpath).suffix, ".jpeg")
            self.assertTrue((Path(tmp) / inner.screenshot_asset_relpath).exists())
            self.assertTrue((Path(tmp) / inner.screenshot_asset_relpath).with_suffix(".jpeg").exists())
            with Image.open(Path(tmp) / inner.screenshot_asset_relpath) as image:
                self.assertGreater(ImageStat.Stat(image.convert("L")).stddev[0], 1.0)
            browser.close()


if __name__ == "__main__":
    unittest.main()
