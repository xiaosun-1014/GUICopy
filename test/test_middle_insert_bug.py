"""复现 codegen 中段插入导致内容损坏的测试 + 验证修复。

根因：playwright codegen 在文件末尾有固定的"收尾段落"
（# ---- / context.close / browser.close / with sync_playwright...），
新增的录制动作被插入到收尾段落**之前**，而非文件末尾。
compute_codegen_appendix 只取末尾新增行，导致：
- 实际新增的动作行被丢弃
- 收尾段落被重复追加，造成代码损坏

修复：_on_code_ready 改为全量 codegen 替换 + marker 锚定重插。
"""
import sys
import unittest

from PyQt6.QtWidgets import QApplication

from main_gui import (
    MainWindow,
    compute_codegen_appendix,
)
from markers import DEFAULT_MARKERS

# Qt app singleton
_app = QApplication.instance() or QApplication(sys.argv)


class TestCodegenMiddleInsertBug(unittest.TestCase):
    """复现：compute_codegen_appendix 在 codegen 中段插入场景下的错误。"""

    V1 = (
        "import re\n"
        "from playwright.sync_api import Playwright, sync_playwright, expect\n"
        "\n"
        "\n"
        "def run(playwright: Playwright) -> None:\n"
        "    browser = playwright.chromium.launch(headless=False)\n"
        "    context = browser.new_context()\n"
        "    page = context.new_page()\n"
        "\n"
        "    # ---------------------\n"
        "    context.close()\n"
        "    browser.close()\n"
        "\n"
        "\n"
        "with sync_playwright() as playwright:\n"
        "    run(playwright)\n"
    )

    V2 = (
        "import re\n"
        "from playwright.sync_api import Playwright, sync_playwright, expect\n"
        "\n"
        "\n"
        "def run(playwright: Playwright) -> None:\n"
        "    browser = playwright.chromium.launch(headless=False)\n"
        "    context = browser.new_context()\n"
        "    page = context.new_page()\n"
        "    page.goto(\"https://example.com\")\n"
        "    page.get_by_text(\"查看\").click()\n"
        "\n"
        "    # ---------------------\n"
        "    context.close()\n"
        "    browser.close()\n"
        "\n"
        "\n"
        "with sync_playwright() as playwright:\n"
        "    run(playwright)\n"
    )

    def test_compute_codegen_appendix_misses_middle_insert(self):
        """确认 compute_codegen_appendix 在中段插入场景下的错误。"""
        v1_lines = self.V1.splitlines()
        delta, appendix = compute_codegen_appendix(len(v1_lines), self.V2)
        appendix_lines = appendix.split("\n")

        self.assertNotIn(
            '    page.goto("https://example.com")',
            appendix_lines,
            "BUG: 实际新增动作不在 appendix 中"
        )
        self.assertIn(
            'with sync_playwright() as playwright:',
            appendix_lines,
            "BUG: 收尾段被当作新增行"
        )


class TestNewModelFix(unittest.TestCase):
    """验证新模型（全量 codegen 替换 + marker 锚定）修复了问题。"""

    V1 = TestCodegenMiddleInsertBug.V1
    V2 = TestCodegenMiddleInsertBug.V2

    def setUp(self):
        self.win = MainWindow()

    def tearDown(self):
        self.win.close()

    def _cursor_at_line(self, line_idx):
        doc = self.win.code_view.document()
        block = doc.findBlockByNumber(line_idx)
        if block.isValid():
            cursor = self.win.code_view.textCursor()
            cursor.setPosition(block.position())
            self.win.code_view.setTextCursor(cursor)

    def test_push_v2_no_duplicate_closing(self):
        """V1→V2：不应有收尾段重复。"""
        self.win._on_code_ready(self.V1)
        self.win._on_code_ready(self.V2)

        panel = self.win.code_view.toPlainText()
        lines = panel.splitlines()

        # 新动作存在
        self.assertIn('page.goto("https://example.com")', panel)
        self.assertIn('page.get_by_text("查看").click()', panel)

        # 收尾段不重复
        sync_count = sum(1 for l in lines if 'with sync_playwright()' in l)
        self.assertEqual(sync_count, 1,
                         f"with sync_playwright 出现 {sync_count} 次（应为 1）")

    def test_marker_survives_push_at_anchor(self):
        """marker 在 codegen 推送后保持在锚点行之后。"""
        self.win._on_code_ready(self.V1)

        # 找到 page = context.new_page() 行并插入 marker
        v1_lines = self.V1.splitlines()
        anchor_idx = next(i for i, l in enumerate(v1_lines) if 'page = context.new_page()' in l)
        self._cursor_at_line(anchor_idx)
        self.win._insert_marker(DEFAULT_MARKERS[0])

        # 推送 V2
        self.win._on_code_ready(self.V2)

        panel = self.win.code_view.toPlainText()
        final_lines = panel.splitlines()

        # marker 存在
        self.assertIn("[MARKER: 报告截图", panel)
        marker_idx = next(i for i, l in enumerate(final_lines) if 'MARKER: 报告截图' in l)
        anchor_final_idx = next(i for i, l in enumerate(final_lines) if 'page = context.new_page()' in l)

        # marker 紧跟在锚点行之后
        self.assertEqual(marker_idx, anchor_final_idx + 1,
                         f"marker 应在锚点行之后: anchor={anchor_final_idx}, marker={marker_idx}")

        # 新动作在 marker 之后
        goto_idx = next(i for i, l in enumerate(final_lines) if 'page.goto' in l)
        self.assertLess(marker_idx, goto_idx)

        # 收尾段不重复
        sync_count = sum(1 for l in final_lines if 'with sync_playwright()' in l)
        self.assertEqual(sync_count, 1)


if __name__ == "__main__":
    unittest.main()
