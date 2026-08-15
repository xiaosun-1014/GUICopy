import unittest

from playwright.sync_api import sync_playwright

from capture_snapshot import capture_marker_interaction_region, discover_series_candidates


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

    # ---- Phase 2: discover_series_candidates API ----

    def test_discovery_plain_list_keeps_dom_order_and_selected_state(self):
        markup = """<div id='s'>
          <div class='item' data-series='a' aria-selected='true'>A series</div>
          <div class='item' data-series='b'>B series</div>
          <div class='item' data-series='c'>C series</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            descriptors, members, evidence = discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=5)
            browser.close()

        self.assertEqual([d.series_key for d in descriptors], ["a", "b", "c"])
        self.assertEqual([d.ordinal for d in descriptors], [0, 1, 2])
        self.assertEqual([d.label for d in descriptors], ["A series", "B series", "C series"])
        # Descriptor count is auditable against region members with unique, aligned ids.
        self.assertEqual(len(descriptors), len(members))
        self.assertEqual([m.member_id for m in members], [d.member_id for d in descriptors])
        self.assertEqual(len({m.member_id for m in members}), len(members))
        self.assertEqual(evidence.discovered_count, 3)
        # Selected state is correct and the short list reached its end.
        self.assertTrue(descriptors[0].selected)
        self.assertFalse(descriptors[1].selected)
        self.assertTrue(evidence.reached_end)

    def test_discovery_virtualized_list_deduplicates_reused_nodes(self):
        # A single DOM row is reused; as the container scrolls, the row's
        # content cycles one -> two -> three -> one -> ... Discovery must
        # collect each logical series exactly once.
        markup = """<style>#s{height:30px;overflow:auto}#row{height:30px}</style>
        <div id='s'><div id='row' class='item' data-series='one'>Series one</div></div>
        <script>
          const el = document.getElementById('s');
          const row = document.getElementById('row');
          const names = [['one', 'Series one'], ['two', 'Series two'], ['three', 'Series three']];
          el.addEventListener('scroll', () => {
            const idx = Math.floor(el.scrollTop / 30) % 3;
            row.setAttribute('data-series', names[idx][0]);
            row.textContent = names[idx][1];
          });
        </script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 500, "height": 300})
            page.set_content(markup)
            # Pad container height so scrolling has room while row stays the only item.
            page.locator("#s").evaluate("el => { el.style.height = '30px'; const hold = document.createElement('div'); hold.style.height='500px'; el.appendChild(hold); }")
            descriptors, members, evidence = discover_series_candidates(
                page.locator("#s"), "d_virt", max_scroll_steps=40, max_duration_s=3.0
            )
            browser.close()

        keys = {d.series_key for d in descriptors}
        self.assertEqual(len(descriptors), 3)
        self.assertEqual(keys, {"one", "two", "three"})
        self.assertEqual(evidence.collected_count, 3)
        self.assertEqual(evidence.discovered_count, 3)

    def test_discovery_same_name_series_produce_distinct_keys_via_stable_attrs(self):
        markup = """<div id='s'>
          <div class='item' data-series='Thin' data-series-uid='uid-1'>Thin series</div>
          <div class='item' data-series='Thin' data-series-uid='uid-2'>Thin series</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            descriptors, _, _ = discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=5)
            browser.close()

        keys = [d.series_key for d in descriptors]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(set(keys), {"uid-1", "uid-2"})

    def test_discovery_no_stable_attribute_fallback_key_is_deterministic(self):
        markup = """<div id='s'><ul>
          <li>Alpha</li><li>Beta</li>
        </ul></div>"""
        def run():
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.set_content(markup)
                descriptors, _, _ = discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=5)
                browser.close()
                return [(d.series_key, d.ordinal) for d in descriptors]

        first = run()
        second = run()
        self.assertEqual(len(first), 2)
        self.assertEqual(len(set(key for key, _ in first)), 2)
        # Fully deterministic within (and across) identical captures.
        self.assertEqual(first, second)
        self.assertIn("alpha", first[0][0])
        self.assertIn("d1", first[0][0])

    def test_discovery_budget_exhausted_reports_partial_not_complete(self):
        markup = """<style>#s{height:20px;overflow:auto}.item{height:20px}</style>
        <div id='s'><div class='item' data-series='one'>One</div><div class='item' data-series='two'>Two</div></div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            descriptors, _, evidence = discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=0)
            browser.close()

        self.assertFalse(evidence.reached_end)
        self.assertEqual(evidence.warning, "series_virtualized_partial")
        self.assertEqual(evidence.discovered_count, 2)

    def test_discovery_restores_original_scroll_position(self):
        markup = """<style>#s{height:40px;overflow:auto}.item{height:20px}</style>
        <div id='s'>
          <div class='item' data-series='a'>A</div><div class='item' data-series='b'>B</div>
          <div class='item' data-series='c'>C</div><div class='item' data-series='d'>D</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            page.locator("#s").evaluate("el => el.scrollTop = 20")
            discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=10)
            restored = page.locator("#s").evaluate("el => el.scrollTop")
            browser.close()

        self.assertEqual(restored, 20)


if __name__ == "__main__":
    unittest.main()
