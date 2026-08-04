import unittest

from playwright.sync_api import sync_playwright

from capture_snapshot import capture_marker_interaction_region


class ReplicaRegionTests(unittest.TestCase):
    def test_series_region_harvests_scrollable_items_and_restores_position(self):
        markup = """<style>#series{height:40px;overflow:auto}.item{height:20px}</style>
        <div id='series' class='series-list'>
          <div class='item' data-series='one'>Thin 1.0 400幅</div>
          <div class='item' data-series='two'>Thick 5.0 80幅</div>
          <div class='item' data-series='three'>MPR 120幅</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            page.locator("#series").evaluate("element => element.scrollTop = 20")
            region = capture_marker_interaction_region(page, "序列选择", "d_series", max_scroll_steps=10)
            restored = page.locator("#series").evaluate("element => element.scrollTop")
            browser.close()

        self.assertEqual(region.region_type, "series")
        self.assertEqual({member.dom.attributes.get("data-series") for member in region.members}, {"one", "two", "three"})
        self.assertEqual(restored, 20)
        self.assertTrue(region.series_collection.reached_end)
        self.assertEqual(region.series_collection.warning, None)

    def test_series_harvest_records_partial_warning_when_budget_is_exhausted(self):
        markup = """<style>#series{height:20px;overflow:auto}.item{height:20px}</style>
        <div id='series' class='series-list'>
          <div class='item' data-series='one'>One</div><div class='item' data-series='two'>Two</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            region = capture_marker_interaction_region(page, "序列选择", "d_series", max_scroll_steps=0)
            browser.close()

        self.assertTrue(region.series_collection.virtualized)
        self.assertFalse(region.series_collection.reached_end)
        self.assertEqual(region.series_collection.warning, "series_virtualized_partial")


if __name__ == "__main__":
    unittest.main()
