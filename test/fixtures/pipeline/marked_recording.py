"""Anonymous local marked recording used by the offline adapter+replica E2E.

This is a processed-script-style fixture (the source a user would capture). It
navigates ONLY to local ``file://`` fixture pages under
``test/fixtures/replica_flow/`` and carries every marker type the pipeline
supports. No real URL, patient value, token, or credential appears anywhere —
the fixture is fully anonymous and offline with respect to the network.

The pipeline relocates this source twice (a run-source copy under
``out/{hospital}/runs/{run_id}/source/`` and then an instrumented copy under the
capture directory), so the local ``file://`` target is located by walking up
from this file to the repository root (the pipeline always executes inside the
repo), rather than by an absolute path that would break after relocation.

The ``def run(playwright):`` / ``with sync_playwright(): run(playwright)`` shape
is required by the pipeline's offline rewrite, which buckets the top-level
statements of ``run()`` into per-marker blocks.

Nested-iframe coverage: the host page chains ``#iframe`` (frame_outer) ->
``#image-frame`` (frame_inner), and the ``Meta 信息工具`` marker traverses both
``content_frame`` hops. The viewer popup (``page1``) carries the remaining
markers on its single ``#popup-frame``.

Marker inventory:
  1. 报告截图           -> screenshot/wait marker (LLM-backed completion)
  2. 序列选择           -> series-selection marker (LLM-backed completion)
  3. Meta 信息工具       -> metadata extraction marker (nested iframe)
  4. 窗宽窗位 WL/WW     -> fixed window-level operation (no skill, kept as-is)
  5. 影像画布交互        -> canvas capture marker
"""

from pathlib import Path
from playwright.sync_api import sync_playwright


def _replica_flow_dir() -> Path:
    """Locate ``test/fixtures/replica_flow`` by walking from this file to the
    repository root. Generically recovers the fixture pages no matter where the
    pipeline relocates the recorded source into the run tree."""
    here = Path(__file__).resolve().parent
    current = here
    while True:
        candidate = current / "test" / "fixtures" / "replica_flow"
        if candidate.is_dir():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return here / "replica_flow"


_FIXTURE = _replica_flow_dir()


def run(playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.goto((_FIXTURE / "host.html").as_uri())
    # [MARKER: 报告截图]
    page.wait_for_timeout(200)
    # open the viewer popup (popup transition)
    with page.expect_popup() as popup_info:
        page.locator("#open-popup").click()
    page1 = popup_info.value
    # [MARKER: 序列选择]
    page1.locator("#popup-frame").content_frame.locator("#series-thick").click()
    # [MARKER: Meta 信息工具]
    page1.locator("#popup-frame").content_frame.locator("#metadata").click()
    # [MARKER: 窗宽窗位 WL/WW]
    page1.locator("#popup-frame").content_frame.locator("#ww").fill("2000")
    page1.locator("#popup-frame").content_frame.locator("#confirm").click()
    # [MARKER: 影像画布交互]
    page1.locator("#popup-frame").content_frame.locator("#overlaycanvas-0_0").click()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
