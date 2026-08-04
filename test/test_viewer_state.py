import unittest

from playwright.sync_api import sync_playwright

from skills._shared.viewer_state import (
    select_structural_series,
    wait_for_post_action_state,
    wait_for_pre_action_state,
)


class ViewerStateTests(unittest.TestCase):
    def test_structural_series_ignores_report_summary_and_selects_best_item(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<div id="reportContainer" style="position:absolute;left:200px">'
                '<h4 class="title">影像 序列: 8 影像: 1180</h4></div>'
                '<a class="tool tool-more disabled">More</a>'
                '<div id="seriesList"><ul>'
                '<li data-seriesid="1"><a>Scout 共 2张</a></li>'
                '<li data-seriesid="2"><a>10.0_lung 共 41张</a></li>'
                '<li data-seriesid="3"><a ondblclick="'
                "document.querySelector('#reportContainer').style.display='none';"
                "document.querySelector('.tool-more').classList.remove('disabled');"
                '">1.5_lung 共 278张</a></li>'
                '<li data-seriesid="4"><a>3.0 MPR-Cor_bone 共 131张</a></li>'
                '</ul></div>'
            )

            selected = select_structural_series(page)

            self.assertEqual(selected, ("1.5_lung 共 278张", 278))
            self.assertFalse(page.locator("#reportContainer").is_visible())
            browser.close()

    def test_pre_action_waits_for_async_report_footer(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content("<main>Loading</main>")
            page.evaluate(
                """() => setTimeout(() => {
                    document.body.insertAdjacentHTML(
                        "beforeend",
                        '<div id="reportContainer">'
                        + '<div class="report-footer">Ready</div></div>'
                    );
                }, 100)"""
            )

            wait_for_pre_action_state(page, "序列选择")

            self.assertTrue(
                page.locator("#reportContainer .report-footer").is_visible()
            )
            browser.close()

    def test_post_action_waits_until_overlay_hidden_and_toolbar_enabled(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(
                '<div id="reportContainer">Report</div>'
                '<a class="tool tool-more disabled">More</a>'
            )
            page.evaluate(
                """() => setTimeout(() => {
                    document.querySelector("#reportContainer").style.display = "none";
                    document.querySelector(".tool-more").classList.remove("disabled");
                }, 100)"""
            )

            ready = wait_for_post_action_state(
                page,
                "序列选择",
                timeout_s=2.0,
                stable_s=0.1,
            )

            self.assertTrue(ready)
            self.assertFalse(page.locator("#reportContainer").is_visible())
            browser.close()


if __name__ == "__main__":
    unittest.main()
