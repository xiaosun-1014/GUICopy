import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills._shared import canvas_capture


class CanvasCaptureTests(unittest.TestCase):
    def test_equal_sized_frames_are_all_retained(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "canvas_frames"

            def capture_frame(_viewer_frame, path):
                path.write_bytes(b"x" * 2048)
                return canvas_capture.CaptureResult(path=path, method="test")

            with patch.object(canvas_capture, "_find_viewer_frame", return_value="viewer"), \
                 patch.object(canvas_capture, "_parse_total_frames", return_value=3, create=True), \
                 patch.object(
                     canvas_capture,
                     "_navigate_to_frame",
                     return_value=canvas_capture.NavigationResult(
                         method="test", changed=True
                     ),
                     create=True,
                 ), \
                 patch.object(canvas_capture, "_capture_frame", side_effect=capture_frame, create=True):
                results = canvas_capture.capture_canvas_interaction(
                    viewer_page=object(),
                    click_x=10,
                    click_y=20,
                    total_frames=3,
                    output_root=output_root,
                    series_name="test-series",
                )

            self.assertEqual(3, len(results))
            self.assertEqual(
                [
                    "canvas_frame_0001.jpeg",
                    "canvas_frame_0002.jpeg",
                    "canvas_frame_0003.jpeg",
                ],
                [result.path.name for result in results],
            )
            self.assertEqual({2048}, {result.path.stat().st_size for result in results})

            run_dir = results[0].path.parent
            self.assertEqual(3, len(list(run_dir.glob("canvas_frame_*.jpeg"))))
            manifest = json.loads((run_dir / "capture_manifest.json").read_text())
            self.assertEqual("test-series", manifest["series_name"])
            self.assertEqual(3, manifest["requested_frame_count"])
            self.assertEqual(3, manifest["saved_frame_count"])
            self.assertEqual(3, len(manifest["frames"]))
            self.assertEqual(
                {
                    "frame_index",
                    "filename",
                    "capture_method",
                    "navigation_method",
                    "change_confirmed",
                    "file_size",
                    "warning",
                },
                set(manifest["frames"][0]),
            )
            self.assertEqual([1, 2, 3], [frame["frame_index"] for frame in manifest["frames"]])
            self.assertEqual([2048] * 3, [frame["file_size"] for frame in manifest["frames"]])
            self.assertEqual(
                ["canvas_frame_0001.jpeg", "canvas_frame_0002.jpeg", "canvas_frame_0003.jpeg"],
                [frame["filename"] for frame in manifest["frames"]],
            )

    def test_each_capture_run_uses_an_isolated_timestamp_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "canvas_frames"

            def capture_frame(_viewer_frame, path):
                path.write_bytes(b"x" * 2048)
                return canvas_capture.CaptureResult(path=path, method="test")

            with patch.object(canvas_capture, "_find_viewer_frame", return_value="viewer"), \
                 patch.object(canvas_capture, "_parse_total_frames", return_value=1, create=True), \
                 patch.object(canvas_capture, "_capture_frame", side_effect=capture_frame, create=True):
                first = canvas_capture.capture_canvas_interaction(
                    object(), 0, 0, total_frames=1, output_root=output_root
                )
                second = canvas_capture.capture_canvas_interaction(
                    object(), 0, 0, total_frames=1, output_root=output_root
                )

            self.assertNotEqual(first[0].path.parent, second[0].path.parent)
            self.assertRegex(first[0].path.parent.name, r"^\d{8}_\d{6}_\d{6}$")
            self.assertRegex(second[0].path.parent.name, r"^\d{8}_\d{6}_\d{6}$")
            self.assertTrue((first[0].path.parent / "capture_manifest.json").is_file())
            self.assertTrue((second[0].path.parent / "capture_manifest.json").is_file())

    def test_invalid_capture_file_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "canvas_frames"

            def capture_frame(_viewer_frame, path):
                path.write_bytes(b"too-small")
                return canvas_capture.CaptureResult(path=path, method="test")

            with patch.object(canvas_capture, "_find_viewer_frame", return_value="viewer"), \
                 patch.object(canvas_capture, "_capture_frame", side_effect=capture_frame, create=True):
                with self.assertRaises(ValueError):
                    canvas_capture.capture_canvas_interaction(
                        object(), 0, 0, total_frames=1, output_root=output_root
                    )

    def test_numbered_file_count_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "canvas_frames"

            def capture_frame(_viewer_frame, path):
                path.write_bytes(b"x" * 2048)
                if path.name.endswith("0002.jpeg"):
                    path.with_name("canvas_frame_9999.jpeg").write_bytes(b"x" * 2048)
                return canvas_capture.CaptureResult(path=path, method="test")

            with patch.object(canvas_capture, "_find_viewer_frame", return_value="viewer"), \
                 patch.object(canvas_capture, "_capture_frame", side_effect=capture_frame, create=True):
                with self.assertRaisesRegex(RuntimeError, "count mismatch"):
                    canvas_capture.capture_canvas_interaction(
                        object(), 0, 0, total_frames=2, output_root=output_root
                    )

    def test_unconfirmed_navigation_still_saves_every_index(self):
        class FakeViewer:
            def locator(self, _selector):
                raise RuntimeError("no canvas locator")

        viewer = FakeViewer()
        with patch.object(canvas_capture, "_find_viewer_frame", return_value=viewer), \
             patch.object(canvas_capture, "_canvas_hash", return_value=123, create=True), \
             patch.object(canvas_capture, "_goto_frame_api", return_value=True, create=True), \
             patch.object(canvas_capture, "_goto_frame_keyboard", return_value=True, create=True), \
             patch.object(canvas_capture, "_goto_frame_wheel", return_value=True, create=True), \
             patch.object(canvas_capture, "_goto_frame_slider", return_value=True, create=True), \
             patch.object(canvas_capture, "_wait_for_frame_change", return_value=False, create=True):
            navigation = canvas_capture._navigate_to_frame(object(), 2, total_frames=3)

        self.assertEqual("unconfirmed", navigation.method)
        self.assertFalse(navigation.changed)
        self.assertTrue(navigation.warning)

        with tempfile.TemporaryDirectory() as temp_dir:
            def capture_frame(_viewer_frame, path):
                path.write_bytes(b"x" * 2048)
                return canvas_capture.CaptureResult(path=path, method="test")

            with patch.object(canvas_capture, "_find_viewer_frame", return_value=viewer), \
                 patch.object(canvas_capture, "_navigate_to_frame", return_value=navigation), \
                 patch.object(canvas_capture, "_capture_frame", side_effect=capture_frame):
                results = canvas_capture.capture_canvas_interaction(
                    object(), 0, 0, total_frames=3, output_root=Path(temp_dir)
                )

            self.assertEqual(3, len(results))
            manifest = json.loads(
                (results[0].path.parent / "capture_manifest.json").read_text()
            )
            self.assertEqual([True, False, False], [
                frame["change_confirmed"] for frame in manifest["frames"]
            ])
            self.assertEqual(["", navigation.warning, navigation.warning], [
                frame["warning"] for frame in manifest["frames"]
            ])

    def test_capture_uses_page_fallback_when_canvas_methods_fail(self):
        class FakePage:
            def __init__(self):
                self.calls = []

            def screenshot(self, **kwargs):
                self.calls.append(kwargs)
                Path(kwargs["path"]).write_bytes(b"p" * 2048)

        class FakeViewerFrame:
            def __init__(self):
                self.page = FakePage()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "canvas_frame_0001.jpeg"
            viewer = FakeViewerFrame()
            with patch.object(
                canvas_capture, "_capture_canvas_js", side_effect=RuntimeError("js failed"), create=True
            ) as capture_js, patch.object(
                canvas_capture, "_capture_canvas_locator", side_effect=RuntimeError("locator failed"), create=True
            ) as capture_locator:
                result = canvas_capture._capture_frame(viewer, target)

            self.assertEqual("page", result.method)
            capture_js.assert_called_once_with(viewer, target)
            capture_locator.assert_called_once_with(viewer, target)
            self.assertEqual(
                [{"path": str(target), "type": "jpeg", "quality": 95, "full_page": True}],
                viewer.page.calls,
            )

    def test_page_screenshot_is_last_capture_fallback(self):
        class FakePage:
            def screenshot(self, **kwargs):
                Path(kwargs["path"]).write_bytes(b"p" * 2048)

        class FakeViewerFrame:
            page = FakePage()

            def locator(self, _selector):
                raise RuntimeError("locator failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "canvas_frame_0001.jpeg"
            with patch.object(
                canvas_capture,
                "_canvas_js",
                return_value=None,
            ):
                result = canvas_capture._capture_frame(FakeViewerFrame(), target)

            self.assertEqual("page", result.method)
            self.assertGreaterEqual(target.stat().st_size, 1024)


if __name__ == "__main__":
    unittest.main()
