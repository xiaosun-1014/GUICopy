import io
import unittest

from PIL import Image, ImageDraw

from capture_snapshot import compute_image_diff, decide_state, is_visual_change, wait_for_visual_stability
from replica_models import StateDiffProfile, StateEvidence


def png_with_box(box=None):
    image = Image.new("RGB", (100, 80), "black")
    if box:
        ImageDraw.Draw(image).rectangle(box, fill="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class StateDiffSpikeTests(unittest.TestCase):
    def test_masked_dynamic_badge_does_not_count_as_visual_change(self):
        before = png_with_box()
        after = png_with_box((90, 0, 99, 9))

        metrics = compute_image_diff(
            before,
            after,
            StateDiffProfile(),
            mask_rects=[{"x": 90, "y": 0, "width": 10, "height": 10}],
        )

        self.assertEqual(metrics.changed_pixel_ratio, 0.0)
        self.assertEqual(metrics.mean_abs_diff, 0.0)

    def test_series_change_reports_regional_difference(self):
        metrics = compute_image_diff(
            png_with_box(),
            png_with_box((10, 10, 59, 59)),
            StateDiffProfile(),
        )

        self.assertGreaterEqual(metrics.changed_pixel_ratio, 0.02)
        self.assertGreaterEqual(metrics.mean_abs_diff, 3.5)

    def test_stability_wait_returns_after_required_matching_samples(self):
        samples = iter([png_with_box((1, 1, 2, 2))] * 3)

        image, stable = wait_for_visual_stability(
            lambda: next(samples),
            StateDiffProfile(stability_interval_ms=0, stability_rounds=2),
            timeout_ms=50,
        )

        self.assertTrue(stable)
        self.assertEqual(image, png_with_box((1, 1, 2, 2)))

    def test_idle_badges_never_trigger_a_visual_state(self):
        profile = StateDiffProfile()
        baseline = png_with_box()
        for index in range(30):
            changed = png_with_box((90 + index % 2, 0, 99, 9))
            metrics = compute_image_diff(
                baseline,
                changed,
                profile,
                mask_rects=[{"x": 87, "y": 0, "width": 13, "height": 13}],
            )
            self.assertFalse(is_visual_change(metrics, profile))

    def test_series_metadata_and_wlww_changes_trigger_a_visual_state(self):
        profile = StateDiffProfile()
        changes = [
            png_with_box((5, 5, 60, 35)),
            png_with_box((20, 20, 75, 60)),
            png_with_box((0, 50, 99, 79)),
        ]
        for changed in changes:
            self.assertTrue(is_visual_change(compute_image_diff(png_with_box(), changed, profile), profile))

    def test_state_decision_uses_evidence_priority_and_always_after(self):
        profile = StateDiffProfile()
        empty = StateEvidence(False, False, False, False, 0, 0, 0, 0, "")
        self.assertEqual(decide_state(empty, profile), (False, "no_material_change"))
        self.assertEqual(decide_state(empty, profile, always_after=True), (True, "marker_always_after"))
        dom_changed = StateEvidence(False, False, False, True, 0, 0, 0, 0, "")
        self.assertEqual(decide_state(dom_changed, profile), (True, "region_dom_changed"))


if __name__ == "__main__":
    unittest.main()
