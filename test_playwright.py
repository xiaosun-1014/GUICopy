from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://uicloud.com/film/#/91310104MABXFDB98F-06D8R3A")
    page.goto("https://uicloud.com/film/#/?shortCode=91310104MABXFDB98F-06D8R3A")
    page.goto("https://uicloud.com/film/#/report/rissys/patient/u121bwIbOzaIBuJGbGol%2BNGZNVTSwEtvNIyzmQ14PGqXPPBYeKScWt3lQXRoV%2B%2F5lKW0MNuyoRPdabREH0lpB1NHVLFiaAju2WVzD5FmzRLbw%2FpxDh%2FUooinjdyEfTu%2FQS2Z7J4uxUBhRenuIypoGyYJR0woviIhm6ip5Y3Dd9w%3D/100")
    page.goto("https://uicloud.com/film/#/risreport/patient/100")
    page.get_by_test_id("pi-action-pdfReport").get_by_role("img").click()
    page.get_by_test_id("filmMobile-pdf-reader-canvas-1").click(position={"x":478,"y":348})
    page.get_by_role("button", name="Close this dialog").click()
    with page.expect_popup() as page1_info:
        page.get_by_test_id("pi-action-images").locator("div").first.click()
    page1 = page1_info.value
    page1.frame_locator("[id=\"\\32 d-iframe\"]").get_by_role("button", name="序列布局").click()
    page1.frame_locator("[id=\"\\32 d-iframe\"]").get_by_role("button", name="1*1 Shift+1").click()
    page1.frame_locator("[id=\"\\32 d-iframe\"]").get_by_text("1.0 x 1.0_MedMPR204362幅").dblclick()
    page1.frame_locator("[id=\"\\32 d-iframe\"]").locator("#overlaycanvas-0_0").click(position={"x":846,"y":650})
    page1.frame_locator("[id=\"\\32 d-iframe\"]").locator("#popTagText_WL").fill("0")
    page1.frame_locator("[id=\"\\32 d-iframe\"]").locator("#popTagText_WL").press("Enter")
    page1.frame_locator("[id=\"\\32 d-iframe\"]").locator("#overlaycanvas-0_0").click(position={"x":844,"y":665})
    page1.frame_locator("[id=\"\\32 d-iframe\"]").locator("#popTagText_WW").fill("2000")
    page1.frame_locator("[id=\"\\32 d-iframe\"]").locator("#popTagText_WW").press("Enter")

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
