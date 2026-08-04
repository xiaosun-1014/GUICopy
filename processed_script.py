import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(viewport={"width":1696,"height":880})
    page = context.new_page()
    page.goto("https://yyx.ftimage.cn/dimage/index.html?stm=V01A94C5E51229D7469B429E6341A8CF03A09AB54FA188D63625B4DF4ACFA2FB2E5729127FE60C3937F225769D48FB1E7D00E8BF5C8A4D708B5FA058D89CB763AC1")
    # [MARKER: 报告截图 @ 20260729_194032]
    # page.screenshot(path='report_20260729_194032.png', full_page=True)
    # [MARKER: 序列选择]
    # TODO: 对当前序列帧做判定 / 切帧
    page.get_by_role("link", name="x 10.0_lung 共 41张").click()
    page.get_by_role("link", name="x 10.0_lung 共 41张").click()
    # [MARKER: Meta 信息工具 @ 20260729_194056]
    # TODO: 提取当前检查的 Meta 信息 (Patient / Study / Series)
    page.get_by_title("更多").click()
    page.get_by_role("link", description="Tags", exact=True).click()
    page.get_by_role("link").filter(has_text=re.compile(r"^$")).click()
    # [MARKER: 窗宽窗位 WL/WW]
    # TODO: 批量遍历预设窗 (肺窗/骨窗/软组织窗)
    page.get_by_role("link").filter(has_text="预设窗宽窗位").click()
    page.locator("input[name=\"customizeWl\"]").click()
    page.locator("input[name=\"customizeWl\"]").fill("0")
    page.locator("input[name=\"customizeWW\"]").click()
    page.locator("input[name=\"customizeWW\"]").fill("2000")
    page.get_by_role("button", name="确定").click()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)