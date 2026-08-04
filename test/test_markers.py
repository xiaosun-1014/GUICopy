"""markers.py 单元测试。"""
import re
import unittest

from markers import DEFAULT_MARKERS, Marker, render


class TestMarkerRegistry(unittest.TestCase):
    def test_default_markers_have_unique_names(self):
        names = [m.name for m in DEFAULT_MARKERS]
        self.assertEqual(len(names), len(set(names)), f"重名：{names}")

    def test_all_markers_have_nonempty_label_and_code(self):
        for m in DEFAULT_MARKERS:
            self.assertTrue(m.label.strip(), f"label 为空：{m.name}")
            self.assertTrue(m.code.strip(), f"code 为空：{m.name}")

    def test_render_substitutes_timestamp(self):
        m = Marker(name="x", label="x", code="[MARKER @ {ts}]\n# TODO\n")
        out = render(m)
        # {ts} 必须被替换为数字时间戳（YYYYMMDD_HHMMSS 形式，8 位日期 + _ + 6 位时间）
        self.assertNotIn("{ts}", out)
        self.assertRegex(out, r"\[MARKER @ \d{8}_\d{6}\]")


if __name__ == "__main__":
    unittest.main()
