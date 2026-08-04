"""Qt 级别复现测试：模拟完整 GUI 工作流。

直接创建 QApplication + MainWindow，通过调用内部方法模拟：
1. codegen 推送
2. 用户插入 marker
3. 再次 codegen 推送
4. 检查面板内容

这比纯文本测试更能发现 Qt 特有的问题。
"""
import sys
import unittest

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import QApplication

from main_gui import (
    MARKER_LINE_COLOR,
    MainWindow,
    compute_codegen_appendix,
    insert_marker_after_line,
)
from markers import DEFAULT_MARKERS, render

# 确保 QApplication 单例
_app = QApplication.instance()
if _app is None:
    _app = QApplication(sys.argv)


# 模拟 codegen 输出
CODGEN_V1 = """from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://example.com")
    page.get_by_text("查看影像").click()
"""

CODGEN_V2 = CODGEN_V1 + """    page.wait_for_timeout(1000)
    page.get_by_role("button", name="确定").click()
"""


class TestQtLevelWorkflow(unittest.TestCase):
    """Qt 级别的完整工作流测试。"""

    def setUp(self):
        self.win = MainWindow()

    def tearDown(self):
        self.win.close()

    def _set_panel_text(self, text: str) -> None:
        """直接设置面板文本。"""
        self.win.code_view.setPlainText(text)

    def _get_panel_text(self) -> str:
        """获取面板文本。"""
        return self.win.code_view.toPlainText()

    def _set_cursor_at_line(self, line_idx: int) -> None:
        """将光标移到指定行。"""
        cursor = self.win.code_view.textCursor()
        doc = self.win.code_view.document()
        block = doc.findBlockByNumber(line_idx)
        if block.isValid():
            cursor.setPosition(block.position())
            self.win.code_view.setTextCursor(cursor)

    def test_workflow_roundtrip_integrity(self):
        """验证 setPlainText → toPlainText 往返无损。"""
        self._set_panel_text(CODGEN_V1)
        result = self._get_panel_text()
        # 注意：Qt 可能会对末尾换行做规范化
        self.assertEqual(
            result.rstrip("\n"),
            CODGEN_V1.rstrip("\n"),
            "setPlainText → toPlainText 往返不应丢失内容"
        )

    def test_marker_insert_roundtrip(self):
        """验证插入 marker 后代码不被损坏。"""
        self._set_panel_text(CODGEN_V1)

        # 检查 V1 行数
        lines_before = self._get_panel_text().splitlines()
        v1_line_count = len(lines_before)

        # 在第 8 行 (page.goto) 后面插入 marker
        self._set_cursor_at_line(8)
        marker = DEFAULT_MARKERS[0]  # 报告截图
        self.win._insert_marker(marker)

        result = self._get_panel_text()

        # 验证原始代码行都在
        for line in lines_before:
            if line.strip():  # 跳过空行
                self.assertIn(
                    line, result,
                    f"原始行在 marker 插入后丢失: {line!r}"
                )

        # 验证 marker 存在
        self.assertIn("[MARKER: 报告截图", result)

        # 验证 marker 在 page.goto 之后
        lines_after = result.splitlines()
        goto_idx = next(i for i, l in enumerate(lines_after) if 'page.goto' in l)
        marker_idx = next(i for i, l in enumerate(lines_after) if 'MARKER: 报告截图' in l)
        self.assertLess(goto_idx, marker_idx, "marker 应该在 page.goto 之后")

    def test_multiline_marker_uses_one_stable_marker_id_and_deletes_as_a_group(self):
        self.win._on_code_ready(CODGEN_V1)
        self._set_cursor_at_line(8)
        self.win._insert_marker(DEFAULT_MARKERS[0])

        anchor = self.win._marker_anchors[0]
        marker_ids = {item["marker_id"] for item in anchor["items"]}
        self.assertEqual(len(marker_ids), 1)
        self.assertEqual(anchor["marker_id"], marker_ids.pop())

        self._set_cursor_at_line(9)
        self.win._delete_current_line()
        self.assertEqual(self.win._marker_anchors, [])
        self.assertNotIn("[MARKER: 报告截图", self._get_panel_text())

    def test_full_workflow_with_push_insert_push(self):
        """模拟完整工作流：推送 → 插入 marker → 再推送。"""
        # 步骤 1: 首次 codegen 推送
        self.win._on_code_ready(CODGEN_V1)
        panel_after_v1 = self._get_panel_text()

        # 验证 V1 内容完整
        self.assertIn('page.goto("https://example.com")', panel_after_v1)
        self.assertTrue(self.win._panel_initialized)

        # 确认 page.goto 在 V1 中的行号 (0-based)
        v1_lines = CODGEN_V1.splitlines()
        goto_line_idx = next(i for i, l in enumerate(v1_lines) if 'page.goto' in l)

        # 步骤 2: 在 page.goto 行后面插入 marker
        self._set_cursor_at_line(goto_line_idx)
        marker = DEFAULT_MARKERS[0]
        self.win._insert_marker(marker)
        panel_after_marker = self._get_panel_text()

        self.assertIn("[MARKER: 报告截图", panel_after_marker)
        # V1 的所有行应该还在
        for line in v1_lines:
            if line:
                self.assertIn(line, panel_after_marker,
                              f"V1 行在 marker 插入后丢失: {line!r}")

        # 步骤 3: codegen 推送 V2（新增 2 行）
        self.win._on_code_ready(CODGEN_V2)
        panel_final = self._get_panel_text()

        # 验证 V2 的新行也存在
        self.assertIn('page.wait_for_timeout(1000)', panel_final)
        self.assertIn('page.get_by_role("button", name="确定").click()', panel_final)

        # 验证没有行重复
        lines = panel_final.splitlines()
        goto_count = sum(1 for l in lines if 'page.goto("https://example.com")' in l)
        self.assertEqual(goto_count, 1, f"page.goto 出现了 {goto_count} 次")

        # 关键验证：检查 page.goto 行后面紧跟着的是否是 marker
        goto_idx = next(i for i, l in enumerate(lines) if 'page.goto("https://example.com")' in l)
        next_line = lines[goto_idx + 1] if goto_idx + 1 < len(lines) else ""
        self.assertIn("[MARKER: 报告截图", next_line,
                      "marker 应该紧跟在 page.goto 后面")

    def test_multiple_markers_no_corruption(self):
        """连续插入多个 marker 不会导致代码损坏。"""
        self.win._on_code_ready(CODGEN_V1)

        # 插入 3 个 marker 在相同位置
        markers = DEFAULT_MARKERS[:3]
        for i, marker in enumerate(markers):
            self._set_cursor_at_line(8)
            self.win._insert_marker(marker)

        panel = self._get_panel_text()

        # 所有 V1 行存在
        for line in CODGEN_V1.splitlines():
            if line:
                self.assertIn(line, panel, f"V1 行丢失: {line!r}")

        # 所有 marker 存在（动态读注册表，避免与 DEFAULT_MARKERS 脱节）
        for marker in DEFAULT_MARKERS[:3]:
            # marker.code 首行形如 "# [MARKER: 报告截图 @ {ts}]"，取 "[MARKER: " 起到 " ]" 前
            first_line = marker.code.split("\n")[0]
            tag = first_line[first_line.index("[MARKER: "):first_line.index("]", first_line.index("[MARKER: ")) + 1]
            # 去掉时间戳占位符段（如 " @ {ts}"），只比对 "[MARKER: 名称]"
            label = tag.split(" @ ")[0]
            self.assertIn(label, panel, f"marker {label!r} 未在面板中找到")

    def test_insert_at_last_line_then_push(self):
        """在最后一行插入 marker 后 codegen 再推送的场景。"""
        self.win._on_code_ready(CODGEN_V1)
        lines = CODGEN_V1.splitlines()
        last_idx = len(lines) - 1  # 最后一行的 index

        # 在最后一行插入 marker
        self._set_cursor_at_line(last_idx)
        marker = DEFAULT_MARKERS[0]
        self.win._insert_marker(marker)

        # 推送 V2
        self.win._on_code_ready(CODGEN_V2)
        panel = self._get_panel_text()

        # 所有行都存在
        self.assertIn("[MARKER: 报告截图", panel)
        self.assertIn('page.wait_for_timeout(1000)', panel)

    def test_text_changed_sync(self):
        """验证 _latest_code 始终与面板同步。"""
        self.win._on_code_ready(CODGEN_V1)
        self.assertEqual(self.win._latest_code, self._get_panel_text())

        self._set_cursor_at_line(8)
        self.win._insert_marker(DEFAULT_MARKERS[0])
        self.assertEqual(self.win._latest_code, self._get_panel_text())

        self.win._on_code_ready(CODGEN_V2)
        self.assertEqual(self.win._latest_code, self._get_panel_text())

    def test_marker_line_has_highlight_background(self):
        """marker 行整行应有淡黄背景（MarkerHighlighter 生效）。

        验证两点：
        1. 插入 marker 后，marker 行的 charFormat 背景为 MARKER_LINE_COLOR。
        2. codegen 推送（setPlainText 全量替换）后，背景自动恢复。
        """
        self.win._on_code_ready(CODGEN_V1)
        self._set_cursor_at_line(8)
        self.win._insert_marker(DEFAULT_MARKERS[0])

        expected = QColor(MARKER_LINE_COLOR)
        self._assert_marker_line_background(expected)

        # codegen 推送后背景应自动恢复（setPlainText 触发重高亮）
        self.win._on_code_ready(CODGEN_V2)
        self._assert_marker_line_background(expected)

    def _assert_marker_line_background(self, expected: QColor) -> None:
        panel = self._get_panel_text()
        lines = panel.splitlines()
        marker_line_idx = next(
            i for i, l in enumerate(lines) if 'MARKER: 报告截图' in l
        )
        # 高亮格式存在 block.layout().formats()，不在 charFormat()
        block = self.win.code_view.document().findBlockByNumber(marker_line_idx)
        formats = block.layout().formats()
        self.assertTrue(
            formats, "marker 行应至少有一个高亮格式范围（淡黄背景）"
        )
        for fmt_range in formats:
            bg = fmt_range.format.background().color()
            self.assertEqual(
                bg.rgb(), expected.rgb(),
                f"marker 行背景应为 {expected.name()}，实际 {bg.name()}"
            )


class TestInsertAtSpecificPositions(unittest.TestCase):
    """测试在不同位置插入 marker 的行为。"""

    def setUp(self):
        self.win = MainWindow()
        self.win._on_code_ready(CODGEN_V1)

    def tearDown(self):
        self.win.close()

    def _get_panel_text(self) -> str:
        return self.win.code_view.toPlainText()

    def _cursor_at(self, line_idx: int) -> None:
        cursor = self.win.code_view.textCursor()
        doc = self.win.code_view.document()
        block = doc.findBlockByNumber(line_idx)
        if block.isValid():
            cursor.setPosition(block.position())
            self.win.code_view.setTextCursor(cursor)

    def test_insert_after_indented_line(self):
        """在缩进行后面插入 marker：marker 应该沿用缩进。"""
        # line 4: "    browser = playwright.chromium.launch(headless=False)" (4 spaces)
        self._cursor_at(4)
        self.win._insert_marker(DEFAULT_MARKERS[0])
        panel = self._get_panel_text()
        lines = panel.splitlines()

        # 找到 marker 行
        marker_idx = next(i for i, l in enumerate(lines) if 'MARKER: 报告截图' in l)
        marker_line = lines[marker_idx]
        self.assertTrue(
            marker_line.startswith("    "),
            f"marker 应该有 4 空格缩进，实际: {marker_line!r}"
        )
        # 验证 screenshot 行也有缩进
        screenshot_idx = next(i for i, l in enumerate(lines) if 'page.screenshot' in l)
        screenshot_line = lines[screenshot_idx]
        self.assertTrue(
            screenshot_line.startswith("    "),
            f"screenshot 行应该有缩进，实际: {screenshot_line!r}"
        )

    def test_insert_after_deeply_indented_line(self):
        """在更深缩进行后面插入 marker（模拟 with 块内）。"""
        # 先通过 _on_code_ready 初始化包含 with 块的文本
        extra = """    with page.expect_popup() as page1_info:
        page.get_by_text("test").click()
    page1 = page1_info.value
"""
        full = CODGEN_V1 + extra
        # 需要用 _on_code_ready 重置
        self.win._panel_initialized = False
        self.win._on_code_ready(full)

        # 找到 with 块内的 click 行（8 空格缩进）
        full_lines = full.splitlines()
        click_idx = next(i for i, l in enumerate(full_lines) if 'page.get_by_text("test")' in l)

        self._cursor_at(click_idx)
        self.win._insert_marker(DEFAULT_MARKERS[0])
        panel = self._get_panel_text()
        lines = panel.splitlines()

        marker_idx = next(i for i, l in enumerate(lines) if 'MARKER: 报告截图' in l)
        marker_line = lines[marker_idx]
        self.assertTrue(
            marker_line.startswith("        "),
            f"深层缩进的 marker 应该有 8 空格缩进，实际: {marker_line!r}"
        )


class TestEdgeCases(unittest.TestCase):
    """边界条件测试。"""

    def setUp(self):
        self.win = MainWindow()

    def tearDown(self):
        self.win.close()

    def _set_cursor_at_line(self, line_idx: int) -> None:
        cursor = self.win.code_view.textCursor()
        doc = self.win.code_view.document()
        block = doc.findBlockByNumber(line_idx)
        if block.isValid():
            cursor.setPosition(block.position())
            self.win.code_view.setTextCursor(cursor)

    def test_insert_in_empty_panel(self):
        """空面板中插入 marker（不应该崩溃）。"""
        # 面板为空，cursor 在位置 0
        self.win._insert_marker(DEFAULT_MARKERS[0])
        panel = self.win.code_view.toPlainText()
        self.assertIn("[MARKER: 报告截图", panel)

    def test_repeated_codegen_push_with_markers(self):
        """多次交替 codegen 推送和 marker 插入。"""
        # 推送 V1
        self.win._on_code_ready(CODGEN_V1)

        # 插入 marker
        doc = self.win.code_view.document()
        block = doc.findBlockByNumber(8)
        cursor = self.win.code_view.textCursor()
        cursor.setPosition(block.position())
        self.win.code_view.setTextCursor(cursor)
        self.win._insert_marker(DEFAULT_MARKERS[0])

        # 推送 V2
        self.win._on_code_ready(CODGEN_V2)

        # 推送 V3（更多行）
        v3 = CODGEN_V2 + """    page.screenshot(path="end.png")
    context.close()
    browser.close()
"""
        self.win._on_code_ready(v3)

        panel = self.win.code_view.toPlainText()

        # 检查所有关键行存在且不重复
        lines = panel.splitlines()
        for unique_line in [
            'page.goto("https://example.com")',
            'page.wait_for_timeout(1000)',
            'browser.close()',
        ]:
            count = sum(1 for l in lines if unique_line in l)
            self.assertEqual(
                count, 1,
                f"'{unique_line}' 出现了 {count} 次（应为 1 次）"
            )

    def test_user_edits_panel_then_codegen_pushes(self):
        """通过 _display_items 插入的标记在 codegen 推送后保留。

        注意：在录制期间直接通过光标自由编辑文本（非右键菜单操作）
        会在下次 codegen 推送时被 _rebuild_display() 覆盖。
        用户应使用右键菜单「插入标记」/「删除当前行」做结构化编辑。
        """
        self.win._on_code_ready(CODGEN_V1)

        # 通过 _insert_marker 插入（结构化操作，会更新 _display_items）
        self._set_cursor_at_line(7)  # page.goto 行
        self.win._insert_marker(DEFAULT_MARKERS[0])
        marker_text = "[MARKER: 报告截图"

        panel_before_push = self.win.code_view.toPlainText()
        self.assertIn(marker_text, panel_before_push)

        # codegen 推送 V2
        self.win._on_code_ready(CODGEN_V2)

        panel_after_push = self.win.code_view.toPlainText()
        # 标记必须保留（在 _display_items 中）
        self.assertIn(marker_text, panel_after_push,
                      "通过右键菜单插入的标记在 codegen 推送后丢失了！")
        # V2 新行必须在，且位于标记之前（codegen 内容连续）
        self.assertIn('page.wait_for_timeout(1000)', panel_after_push)
        self.assertIn('page.get_by_role("button", name="确定").click()', panel_after_push)

        # 验证 codegen 新行在 panel 中存在，且在 page.get_by_text 之后
        # （marker 插在 page.goto 和 page.get_by_text 之间，所以 marker 在 get_by_text 之前；
        #  V2 新行追加在最后一个 codegen 条目之后，即在 page.get_by_text 之后）
        lines = panel_after_push.splitlines()
        marker_idx = next(i for i, l in enumerate(lines) if 'MARKER: 报告截图' in l)
        get_by_idx = next(i for i, l in enumerate(lines) if 'page.get_by_text' in l)
        wait_idx = next(i for i, l in enumerate(lines) if 'page.wait_for_timeout' in l)

        # marker 在 page.get_by_text 之前（因为 marker 是在 page.goto 后插入的）
        self.assertLess(marker_idx, get_by_idx,
                        "marker 应在 page.get_by_text 之前")
        # V2 新行在 page.get_by_text 之后（追加到最后一个 codegen 条目之后）
        self.assertLess(get_by_idx, wait_idx,
                         "V2 新行应在 page.get_by_text 之后")


if __name__ == "__main__":
    unittest.main()
