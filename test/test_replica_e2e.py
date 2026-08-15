import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from batch_capture_replicate import capture_and_build
from build_replica import build_replica
from replay_helpers import ReplicaServer
from replay_helpers import series_key_slug


class ReplicaEndToEndTests(unittest.TestCase):
    def test_popup_frame_sequence_transition_replays_offline(self):
        fixture = Path(__file__).parent / "fixtures" / "replica_flow" / "host.html"
        source = f'''from playwright.sync_api import sync_playwright
def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto({fixture.as_uri()!r})
        # [MARKER: 报告截图]
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
run()
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")
            entrypoint = capture_and_build(script, root / "export")
            replay_result = subprocess.run(
                [sys.executable, str(entrypoint.parent / "replay_replica.py")],
                cwd=entrypoint.parent,
                text=True,
                capture_output=True,
                timeout=45,
            )
            self.assertEqual(replay_result.returncode, 0, replay_result.stderr)
            with ReplicaServer(entrypoint.parent) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                external_requests = []
                page.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith("http://127.0.0.1") else None)
                page.goto(server.url)
                with page.expect_popup() as popup_info:
                    page.locator("#open-popup").click()
                popup = popup_info.value
                popup.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith("http://127.0.0.1") else None)
                frame = popup.frame_locator("#popup-frame")
                frame.locator("#series-thick").click()
                self.assertEqual(frame.locator("#series-thick").get_attribute("aria-selected"), "true")
                frame.locator("#metadata").click()
                self.assertIn("PatientName", frame.locator("#dicom").inner_text())
                frame.locator("#ww").fill("2000")
                self.assertEqual(frame.locator("#ww").input_value(), "2000")
                frame.locator("#confirm").click()
                self.assertEqual(frame.locator("#overlaycanvas-0_0").count(), 1)
                frame.locator("#overlaycanvas-0_0").click()
                self.assertEqual(external_requests, [])
                browser.close()


    def test_marked_local_recording_replays_offline_without_external_requests(self):
        source = '''from playwright.sync_api import sync_playwright\n\ndef run():\n    with sync_playwright() as playwright:\n        browser = playwright.chromium.launch()\n        page = browser.new_page()\n        page.set_content('<button id="go">Go</button>')\n        # [MARKER: Meta 信息工具]\n        page.locator("#go").click()\n        browser.close()\n\nrun()\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recorded.py"
            script.write_text(source, encoding="utf-8")
            entrypoint = capture_and_build(script, root / "export")
            with ReplicaServer(entrypoint.parent) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                external_requests = []
                page.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith("http://127.0.0.1") else None)
                page.goto(server.url)
                self.assertEqual(page.locator("#go").count(), 1)
                page.locator("#go").click()
                self.assertIn("states/s_001", page.url)
                self.assertEqual(external_requests, [])
                browser.close()

    def test_multi_series_replica_navigates_offline_without_external_requests(self):
        from test.test_replica_runtime import _build_series_flow, _write_assets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_assets(root)
            flow = _build_series_flow()
            output = root / "replica"
            build_replica(flow, root, output)
            with ReplicaServer(output) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 300, "height": 200})
                external_requests = []
                page.on(
                    "request",
                    lambda request: external_requests.append(request.url)
                    if not request.url.startswith("http://127.0.0.1")
                    else None,
                )
                page.goto(server.url)
                # A -> B -> Metadata(B) -> close(B) -> C, all offline.
                page.locator(f'[data-replica-series-key="{series_key_slug("B")}"]').click()
                page.wait_for_url("**/states/s_vb/index.html")
                self.assertEqual(page.locator("#viewer-vb").inner_text(), "vb viewer unique")
                page.locator('[data-testid="meta-open"]').click()
                page.wait_for_url("**/states/s_mb/index.html")
                self.assertEqual(page.locator("#m-tag-mb").inner_text(), "tag-B: value-B")
                page.locator("[data-replica-back]").click()
                page.wait_for_url("**/states/s_vb/index.html")
                self.assertEqual(page.locator("#viewer-vb").inner_text(), "vb viewer unique")
                page.locator(f'[data-replica-series-key="{series_key_slug("C")}"]').click()
                page.wait_for_url("**/states/s_vc/index.html")
                self.assertEqual(page.locator("#viewer-vc").inner_text(), "vc viewer unique")
                self.assertEqual(external_requests, [])
                browser.close()


if __name__ == "__main__":
    unittest.main()
